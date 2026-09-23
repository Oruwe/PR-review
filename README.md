# PR Flagger

An always-on pull-request verification service. It does not review code or give opinions.
It runs every pull request in a sandbox, compares the result against the repository's own
code and its own mined knowledge, and reports facts — each citing evidence you can check.

`SPEC.md` is the authoritative design. `CLAUDE.md` holds the non-negotiable rules.

## Running it

```bash
pip install -e ".[dev]"

python -m prflagger.cli watch --repo owner/name   # start watching a repository
python -m prflagger.cli serve                     # the service + web interface
```

Then open <http://127.0.0.1:8000>. Repositories can also be declared in `config.toml`, and
added from the web interface.

## What you get

**What it is for** — before anything else, the repository's own account of itself: its
goal, who it is for, what it does, and the rules it sets itself. Every line of it is quoted
from the repository's files with the file and line it came from; nothing is inferred and no
model writes it. Pull requests are judged against this and nothing else.

**The map** — what a repository *is*, before any pull request is considered. Modules sized
by lines and coloured by churn, the dependency graph, hotspots with their factors broken
out, the language split, which modules carry no tests, and the exact commands the repository
will be run with.

**The queue** — open pull requests ranked by the weight of what was found, not the count.
One behaviour change outranks six lint nits.

**The run** — the sandbox as it executes. Stage timeline, live CPU, memory against its
limit, process count, per-test results as they land, and a searchable console. The verbatim
`docker run` invocation is shown, so the isolation is auditable rather than asserted.

**What it holds itself to** — the standards the repository declares in its own
configuration, and the ones its reviewers enforced: a review comment counts only if the
author changed the code after it and the pull request merged. Each standard links every
comment behind it.

**The report** — each observation with all four contract fields: what changed, how we know,
what in this repository makes it matter, and a confidence. Where a model is configured, each
is then read against the repository's charter, standards and code, with a suggestion when
one is warranted — and every reference in either is checked before it is shown. Plus a
coverage statement naming what the run could *not* check, and why.

## How a pull request is verified

```
worktree base + head  →  build image (keyed on lockfiles, not commits)
                      →  run the suite at both commits, streaming
                      →  probe: behaviour delta, public surface, lint delta
                      →  place each finding against the repo's charter: core,
                         supporting or peripheral to what the repository is for
                      →  attach the declared standard that makes each finding matter
                      →  rank by confidence × severity × centrality × relevance
                      →  read the top findings against the repo's charter, standards
                         and code; keep only what cites checkable evidence  [model]
```

The model can annotate a finding, demote one it shows to be misleading, and suggest a
change. It cannot add a finding, promote one, or state anything whose references do not
resolve: every citation must be a norm the repository has, a `path:line` that exists with
the quoted text on it, a test the run produced, or a file the pull request changes. An
answer that fails that is discarded, and the report says so.

Every step emits events to an append-only log. The live view and the stored transcript are
that same log replayed — so a tab opened at the end of a run shows what a tab opened at the
start showed.

## What it remembers about a repository

Each watched repository has a **charter**: its name, summary, stated purpose, target
(runtimes, platforms, audience), capabilities, entry points, public API, dependencies and
the rules its own `CONTRIBUTING.md`, `CLAUDE.md` or `AGENTS.md` set. It is read from the
repository's own files — `pyproject.toml`, `package.json`, `go.mod`, `Cargo.toml`, the
README, module docstrings, the license — and every claim carries a `file:line` that exists.

Memory is **per repository**. One repository's charter is never consulted for another's
pull requests; the code refuses the comparison rather than relying on a caller not to ask.

When the default branch moves, the charter is rebuilt at the new commit and compared with
the last one. What changed decides how loudly you hear about it:

| Level | What causes it | How you hear |
|---|---|---|
| **major** | purpose rewritten; toolchain or license changed; a new major version; a way of running it removed; a large share of the public API removed | a banner on every page until someone acknowledges it, and the webhook if configured |
| **notable** | purpose reworded; public API grown or partly removed; dependencies dropped | an entry on the repository's *Changes* page |
| **minor** | a dependency added, a standard tool changed, small wording edits | history only |

A pull request that would itself cause a major change is flagged the same way: a chip in
the queue, a section in its report, and a major notification. The thresholds live under
`[charter]` in `config.toml`. The webhook address is a credential, so it is read only from
`PRFLAGGER_NOTIFY_WEBHOOK` in the environment; its payload carries a Slack-compatible
`text` field.

## What its reviewers enforce

Once a day per repository (and when one is first added), the service reads the pull
requests merged since it last looked, keeps each review comment the author then acted on,
and groups those comments into standards. A standard needs three comments from at least two
reviewers. Grouping is by meaning when `sentence-transformers` is installed and by shared
words otherwise; a standard is stated by the model when one is configured and as the
group's most representative comment, quoted, when not. The repository's page says which.

Standards are per repository, like the charter. Repositories not on GitHub keep only the
standards their configuration declares.

## Any repository

A **toolchain pack** answers four questions for an ecosystem: what image, how to install,
how to test, how to lint. Python, JavaScript/TypeScript and Go ship as packs. A repository
nothing recognises falls back to the commands its own CI declares — filtered, because a
workflow belongs to whoever opened the pull request, and `curl | sh` is not run on its
behalf even inside the sandbox.

Where a pack has no exact symbol extractor, the repository still gets sizes, churn, test
topology and a real sandboxed run; the map says `analysis_depth: surface` rather than
implying a depth it never reached.

## What it needs

| Capability | Used for | How it is configured |
|---|---|---|
| Docker | every sandboxed run | a running daemon; `.claude/hooks/session-start.sh` starts one |
| A GitHub token | polling pull requests, reading review history | `GITHUB_TOKEN` in the environment (public repos work without one, at 60 calls/hour); for GitHub Enterprise Server also set `PRFLAGGER_GITHUB_API=https://<host>/api/v3` |
| A Bedrock model | reading findings against the repository, suggestions, naming standards | `AWS_BEARER_TOKEN_BEDROCK`, or an AWS access key / profile / instance role; then `python -m prflagger.cli llm check` |
| `all-MiniLM-L6-v2` | grouping review comments by meaning | `pip install -e ".[embed]"`; downloaded on first use |

**No credential is ever read from, or written to, this repository.** Configure them the way
your machine normally does.

Every one of these degrades explicitly rather than silently. A run that cannot reach a model
still produces a report, and that report names what it could not verify.

## Cost

Model spend is measured from the provider's own usage counts, never estimated, and written
to a ledger (`llm_spend`) with three hard caps from `[budget]`: per run, per repository per
day, and overall. Before each call its worst case is reserved against all three, so a call
that could breach one is never sent — even when several runs ask at once — and the run says
what it could not read. A model with no configured price is refused outright.

Naming standards routes to Haiku; reading findings routes to Sonnet, one call per file
group, at most `[models] max_adjudications` groups per run. The instructions and the
repository's charter and standards are identical for every pull request in a repository, so
they sit behind a prompt-cache breakpoint; a repeated request is answered from the disk cache
at $0 and recorded as such. `GET /api/budget` shows what has been spent.

If Bedrock refuses the credentials or does not know a model id, the client stops after that
one refusal and every run names the reason until it is fixed and the service restarted.

## At scale

The things that break first on a large repository or a busy one, and what each costs:

| Pressure | What it would have done | What happens now |
|---|---|---|
| A suite printing a lot | A database row per line — millions of rows, one writer | Lines stream live and land in a per-job file; `events` stays proportional to runs, not output |
| A large codebase | One symbol index per package, so cross-package calls never resolved | One index over a common root; an ambiguous name matching >8 symbols records nothing |
| A very large codebase | Minutes of blocking work | Past a file budget the symbol pass is skipped and the map says `analysis_depth: surface` |
| Many runs | A worktree per commit and an image per lockfile set, forever | Hourly sweep; `prflagger gc --dry-run` to see first; runs refuse to start below 5% free disk |
| Many pull requests at once | An unbounded queue, and one busy repo starving the others | Bounded admission with a stated reason, round-robin dispatch per repository |

Measured on a synthetic 3,000-file, 87k-line repository: the atlas builds in about three
seconds using 24 MB, and the call graph carries 21k edges rather than the 723k that
unbounded name matching produced.

## Tests

```bash
pytest tests/ -q                 # nothing here mocks Docker, git, or the repo under test
ruff check . && mypy prflagger/
```

The `test_v2_*` suites run real containers against a real two-commit git repository built by
`tests/v2_fixtures.py`. The older `test_cli`, `test_differential`, `test_probes` and
`test_report` suites additionally need the seeded branches:

```bash
python -m scripts.seed_target    # requires network access to the target repo
```
