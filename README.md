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

**The map** — what a repository *is*, before any pull request is considered. Modules sized
by lines and coloured by churn, the dependency graph, hotspots with their factors broken
out, the language split, which modules carry no tests, and the exact commands the repository
will be run with.

**The queue** — open pull requests ranked by the weight of what was found, not the count.
One behaviour change outranks six lint nits.

**The run** — the sandbox as it executes. Stage timeline, live CPU, memory against its
limit, process count, per-test results as they land, and a searchable console. The verbatim
`docker run` invocation is shown, so the isolation is auditable rather than asserted.

**The report** — each observation with all four contract fields: what changed, how we know,
what in this repository makes it matter, and a confidence. Plus a coverage statement naming
what the run could *not* check, and why.

## How a pull request is verified

```
worktree base + head  →  build image (keyed on lockfiles, not commits)
                      →  run the suite at both commits, streaming
                      →  probe: behaviour delta, public surface, lint delta
                      →  rank by confidence × severity × centrality
                      →  adjudicate against the repo's own norms   [needs a model]
```

Every step emits events to an append-only log. The live view and the stored transcript are
that same log replayed — so a tab opened at the end of a run shows what a tab opened at the
start showed.

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
| A GitHub token | polling pull requests | `GITHUB_TOKEN` in the environment (public repos work without one, at 60 calls/hour) |
| A Bedrock model | adjudication, test generation, norm naming | boto3's normal credential chain |
| `all-MiniLM-L6-v2` | norm clustering | downloaded by `sentence-transformers` on first use |

**No credential is ever read from, or written to, this repository.** Configure them the way
your machine normally does.

Every one of these degrades explicitly rather than silently. A run that cannot reach a model
still produces a report, and that report names what it could not verify.

## Cost

Model spend is measured from the provider's own usage counts, never estimated, and written
to a ledger with three hard caps: per run, per repository per day, and overall. A call that
would breach one is refused and the run says so. Cheap work (extraction, classification,
norm naming) routes to Haiku; judgement routes to Sonnet. The repository context block is
identical across every pull request in a repository, so it sits behind a cache breakpoint.

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
