# Deploying PR Flagger

One Linux server runs everything: the service, its web interface, and the sandbox
containers each pull request is tested in. It keeps running on its own — watching
repositories, running pull requests, re-reading what each repository is for, and
learning from its reviews — and you open it in a browser.

Two ways to install it. Both end with the service on `127.0.0.1:8000`, HTTPS in front
of it, and a sign-in page.

| | **A. systemd (recommended)** | **B. Docker Compose** |
|---|---|---|
| The service runs | directly on the server, in a Python virtualenv | in a container |
| Sandboxes run | in containers on the server's Docker | the same |
| Upgrade | `git pull`, `pip install`, restart | `git pull`, `docker compose up -d --build` |
| Watch out for | nothing special | the data directory must be mounted at the same path inside and out (the compose file does this) |

## 1. The server

- **Size:** 2–4 vCPU, 8 GB RAM, 60 GB disk. Each pull request runs its suite twice in
  a sandbox capped at `[sandbox] default_memory_mb` (1 GB by default), two at a time on
  a 4-core machine. The service refuses to start a job below 5% free disk.
- **OS:** any recent Linux with Docker. The steps below use Ubuntu 24.04.
- **Use a dedicated machine.** The service needs the Docker socket, which is
  root-equivalent. Don't share the server with anything else.

**On AWS (EC2):** a `t3.large` (2 vCPU, 8 GiB) with a 60 GB gp3 volume costs roughly
$60–65 a month on demand in us-east-1 — check current pricing for your region. That is
the same credit Bedrock spends; `[budget]` in `config.toml` caps only model spend. In
the instance's security group allow **443 and 80** (80 lets Caddy obtain the
certificate) from anywhere, and **22 only from your own IP**. Never open 8000.

Point a DNS name (e.g. `flagger.example.com`) at the server's address before step 4.

## 2. Credentials

Every secret goes in one environment file — never in `config.toml`, never in a
repository. `deploy/.env.example` lists them all:

| Variable | What it is | Get it |
|---|---|---|
| `PRFLAGGER_ADMIN_TOKEN` | sign in as admin (add repos, run, acknowledge) | `openssl rand -hex 32` |
| `PRFLAGGER_VIEWER_TOKEN` | optional: sign in read-only | `openssl rand -hex 32` |
| `GITHUB_TOKEN` | polling, review history, cloning private repos | a fine-grained token with read access to the repos' contents and pull requests |
| `AWS_BEARER_TOKEN_BEDROCK` | the model (or `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY`) | Bedrock console → API keys |
| `AWS_REGION` | where Bedrock is called | e.g. `us-east-1` |
| `PRFLAGGER_NOTIFY_WEBHOOK` | optional: where major-update alerts are posted | a Slack incoming webhook URL |
| `PRFLAGGER_GITHUB_API` | GitHub Enterprise Server only | `https://<host>/api/v3` |

The service refuses to listen on anything but loopback without `PRFLAGGER_ADMIN_TOKEN`.
Changing the admin token signs everyone out.

## 3A. Install with systemd

```bash
# Docker, git, Python
sudo apt-get update
sudo apt-get install -y docker.io git python3-venv

# A system user that may use Docker, with its data in /var/lib/prflagger
sudo useradd --system --create-home --home-dir /var/lib/prflagger \
     --shell /usr/sbin/nologin prflagger
sudo usermod -aG docker prflagger

# The code, installed into its own virtualenv
sudo git clone https://github.com/Oruwe/PR-review /opt/prflagger/src
sudo python3 -m venv /opt/prflagger/venv
sudo /opt/prflagger/venv/bin/pip install /opt/prflagger/src

# Configuration and secrets
sudo install -d -m 750 -o root -g prflagger /etc/prflagger
sudo install -m 640 -o root -g prflagger /opt/prflagger/src/deploy/.env.example /etc/prflagger/env
sudo install -m 640 -o root -g prflagger /opt/prflagger/src/deploy/config.example.toml /etc/prflagger/config.toml
sudoedit /etc/prflagger/env           # fill in the tokens
sudoedit /etc/prflagger/config.toml   # the repositories to watch (see "Choosing repositories")

# Run it, and keep it running
sudo cp /opt/prflagger/src/deploy/prflagger.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now prflagger
curl -s localhost:8000/api/health     # "started": true, "auth": "tokens"
```

To run a command as the service, with its settings (used below as `pf`):

```bash
pf() { sudo -u prflagger bash -c 'set -a; . /etc/prflagger/env; set +a;
  PRFLAGGER_CACHE_DIR=/var/lib/prflagger PRFLAGGER_CONFIG=/etc/prflagger/config.toml \
  /opt/prflagger/venv/bin/prflagger "$@"' _ "$@"; }
```

## 3B. Install with Docker Compose

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 git
sudo git clone https://github.com/Oruwe/PR-review /opt/prflagger/src
cd /opt/prflagger/src/deploy

sudo cp .env.example .env && sudo chmod 600 .env
sudoedit .env    # the tokens, and DOCKER_GID=$(getent group docker | cut -d: -f3)

# The data directory, owned by the container's user, holding config.toml
sudo install -d -o 10001 -g 10001 /var/lib/prflagger
sudo install -o 10001 -g 10001 config.example.toml /var/lib/prflagger/config.toml
sudoedit /var/lib/prflagger/config.toml   # the repositories to watch

sudo docker compose up -d --build
curl -s localhost:8000/api/health
```

Commands run inside the container: `pf() { sudo docker compose exec prflagger prflagger "$@"; }`

**Why the same path twice.** The service starts each sandbox on the server's Docker and
mounts the pull request's checkout into it. Docker resolves that mount path on the
server, so a checkout the service sees at `/var/lib/prflagger/worktrees/…` must exist at
exactly that path on the server. `compose.yaml` mounts the data directory at the same
path on both sides for this reason; if you change `PRFLAGGER_DATA`, both sides change
together. It also mounts the server's cgroup tree read-only, which is how each
sandbox's memory is measured from inside the container.

**Building behind a restrictive proxy.** If plain-HTTP package downloads are blocked or
a proxy intercepts TLS, pass `--build-arg APT_MIRROR=https://deb.debian.org`, the proxy
as `--build-arg https_proxy=…`, and its CA as a build secret
(`--secret id=proxy_ca,src=proxy-ca.crt`). The CA is used during the build only and is
not kept in the image.

## 4. HTTPS

```bash
sudo apt-get install -y caddy
sudo cp /opt/prflagger/src/deploy/Caddyfile /etc/caddy/Caddyfile
sudoedit /etc/caddy/Caddyfile        # your domain instead of flagger.example.com
sudo systemctl reload caddy
```

Caddy obtains and renews the certificate itself and passes the live WebSocket through.
Open `https://your-domain/` and sign in with the admin token.

## 5. First run

```bash
pf llm check                  # one tiny real call: credentials, region, model id, pricing
```

It prints the reply, the tokens and what they cost, or exactly why it could not — for
example `Bedrock refused the credentials`. Until it succeeds, runs still happen and say
"adjudication skipped" with the reason.

Then add repositories in the interface (or in `config.toml`). For each one the service
clones it, reads what it is for, learns its reviewers' standards from merged pull
requests, and starts running every open pull request. The first reading of a large
repository takes a few minutes.

### Choosing repositories to watch

You do not need a busy repository of your own. Watching is read-only: the service reads
a repository's open pull requests and runs them in its own sandbox. It never comments,
pushes or opens anything on GitHub, so you can watch any public repository and nobody
there will see it.

It is most useful where three things hold:

- **Python.** Python gets the full depth: the public-API diff, the call graph, and a
  charter read from `pyproject.toml` and the README. JavaScript, TypeScript and Go get
  less.
- **A test suite that runs offline in minutes.** Sandboxes have no network, and every
  pull request runs the suite twice, at the commit it branched from and at its head.
  Test-only dependencies are installed from `test`/`tests`/`dev` extras, PEP 735
  dependency groups, or requirements files. Poetry's `dev-dependencies` are not yet.
- **Pull requests that maintainers review closely.** Mined standards come from review
  comments that were acted on before a merge.

`deploy/config.example.toml` starts with two that meet them. Each was run end to end on
a real open pull request before being recommended:

| | Suite in the sandbox (base / head) | Peak memory | Result |
|---|---|---|---|
| `pallets/click` #3859 | 2,084 tests, 8 s each | 131 MB | no findings: the pull request only adds type-checker configuration |
| `python-attrs/attrs` #1626 | 1,412 / 1,414 tests, 28 s each | 260 MB | two new mypy errors at head |

Four of attrs' packaging tests fail at every commit, because the sandbox imports attrs
from the checkout rather than from an installed distribution. They fail the same way on
both sides, so they produce no findings. A run's first image build adds about a minute.

**Add a repository where you know the answer.** Fork `pallets/click`, open small pull
requests inside your fork, and uncomment the fork's entry in the config. Good ones to try:

- an edge case changed without saying so;
- a new public function;
- a README-only change.

Then check that the service flags exactly what you did, and nothing else.

**Keep `max_prs` small at first.** The first poll queues every open pull request it
finds, up to `max_prs`, and each run takes a minute or two. Raise it once you know the
time and cost.

**Set `GITHUB_TOKEN`.** Without one, GitHub allows 60 API requests an hour for
everything the service does: polling, and learning from past reviews. A fine-grained
token with read-only access to public repositories is enough.

**After a week, look at:**

- which findings were useful and which were noise, for each kind: behaviour, API, lint,
  coverage;
- how long runs take, on each run's page;
- what each pull request cost, from `GET /api/budget`.

## 6. Backups

Everything learned — runs, findings, each repository's memory and its history, mined
standards, notifications, spend — is one SQLite database. Back it up while the service
runs:

```bash
pf backup --out /var/lib/prflagger/backups/$(date +%F).tar.gz
```

Nightly, keeping two weeks, as root's crontab (`sudo crontab -e`) — systemd install:

```cron
15 3 * * * sudo -u prflagger bash -c 'set -a; . /etc/prflagger/env; set +a; PRFLAGGER_CACHE_DIR=/var/lib/prflagger PRFLAGGER_CONFIG=/etc/prflagger/config.toml /opt/prflagger/venv/bin/prflagger backup --out /var/lib/prflagger/backups/$(date +\%F).tar.gz' && find /var/lib/prflagger/backups -name '*.tar.gz' -mtime +14 -delete
```

Copy the archives off the machine too (e.g. `aws s3 cp`). Worktrees, images and
transcripts are not included; they are rebuilt from the repositories.

**Restore** — the service must be stopped; `restore` refuses otherwise, and keeps the
database it replaces beside the restored one:

```bash
sudo systemctl stop prflagger && pf restore /path/to/backup.tar.gz && sudo systemctl start prflagger
# Compose:
sudo docker compose stop && sudo docker compose run --rm prflagger restore /var/lib/prflagger/backups/…tar.gz && sudo docker compose start
```

## 7. Upgrading

Take a backup, then:

```bash
cd /opt/prflagger/src && sudo git pull
sudo /opt/prflagger/venv/bin/pip install /opt/prflagger/src && sudo systemctl restart prflagger
# Compose:
cd /opt/prflagger/src/deploy && sudo docker compose up -d --build
```

The database schema migrates itself on start.

## 8. Operating it

- **Logs:** `journalctl -u prflagger -f`, or `sudo docker compose logs -f`.
- **Health:** `GET /api/health` (open, for monitors) reports the queue, sandbox
  capacity, free disk, whether GitHub and the model are available, and open major alerts.
- **Spend:** `GET /api/budget` — what the model has cost, against the caps in `[budget]`.
- **Disk:** the service reclaims old worktrees, images and transcripts hourly;
  `pf gc` does it now.
- **Signing out everyone:** change `PRFLAGGER_ADMIN_TOKEN` and restart.

| Symptom | Cause |
|---|---|
| `refusing to listen on 0.0.0.0 without a sign-in token` | set `PRFLAGGER_ADMIN_TOKEN` |
| runs show no peak memory | the cgroup tree is not mounted (Compose) or not readable |
| every run says adjudication was skipped | run `pf llm check`; it names the reason |
| a private repository will not clone | `GITHUB_TOKEN` lacks read access to it |
| `permission denied … docker.sock` | the service user is not in the `docker` group (`DOCKER_GID` for Compose) |
