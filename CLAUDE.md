# PR Flagger

An always-on PR verification service. It does **not** review code or give opinions. It runs
every pull request in a sandbox, compares the result against the repository's own code and
its own mined knowledge, and reports facts — each citing evidence. `SPEC.md` is the
authoritative design.

## Commands

```bash
pytest tests/ -x -q              # test
pytest tests/test_<name>.py -x   # single component
ruff check . && mypy prflagger/  # lint + types

python -m prflagger.cli serve    # the service and its web interface
python -m prflagger.cli watch --repo owner/name
python -m prflagger.cli gc       # drop old events and orphaned transcripts
python -m prflagger.cli check --repo <path> --base <sha> --head <sha>
```

## Non-negotiable rules

- **IMPORTANT: Never mock Docker, git, or the target repo.** Tests run against real
  artifacts. A mocked test here proves nothing.
- **Never use an LLM for structure.** Symbols, call graphs and diffs come from `ast` and
  `git`. LLM calls are only for natural language: generating tests, clustering comments.
- **Never let an LLM decide storage.** Schema is fixed in `models.py`. Extraction fills a
  known shape; it does not invent one.
- **Every LLM call goes through `prflagger.llm`.** The service uses `llm.client.ModelClient`:
  redacted, content-addressed and cached to disk, reserved against the budget caps before it
  is sent, priced from the provider's own usage after. Never call Bedrock or the Anthropic
  SDK directly — an uncached, unmetered call blows the budget.
- **Sandbox runs return typed outcomes, never raise.** `TIMEOUT` and `OOM` are findings, not
  errors.
- **The agent never emits a verdict on a pull request.** No "approve", "reject", "looks
  risky", "LGTM". The reader decides.
- **A suggestion must cite evidence or it is not emitted.** Per-observation suggestions are
  permitted — they are the point of the adjudication stage — but only when they cite at
  least one artifact that resolves: a norm that exists, a `path:line` that exists at the
  run's sha, or a nodeid the run actually produced. `Adjudication` and `Suggestion` raise on
  construction when they carry no citation, so no code path can forget to check. An
  adjudicator may demote, annotate or suggest; it can never invent a finding.
- **A model's claims are checked before they are kept.** `adjudicate.citations.resolve`
  checks every reference: a norm id the repository has, a `path:line` that exists with the
  quoted text on it, a test id the run produced, a file the PR changes. The model may
  annotate, demote or suggest; it may not add, promote, or speak about an observation it was
  not shown.
- **Norms come from the repository.** Declared norms cite the config line that sets them;
  mined norms need three enforced comments from two reviewers and link every one. How a
  norm was grouped and stated (`clustered_by`, `named_by`) is recorded and shown.
- **Partial verification is never presented as complete.** Every run carries a coverage
  statement naming what it checked and what it could not, with the reason. A probe that
  failed to run is a gap to report, not a silence.
- **Repository memory comes only from the repository.** A `Charter` is built from the
  repo's own files; every `Claim` cites a `file:line` that exists. No model writes it, and
  nothing from outside the repo is added to it.
- **Memory is per repository.** A charter only ever judges, or is compared with, its own
  repository; `compare()` and `classify()` raise on a mismatch rather than trusting callers.
- **Credentials come from the environment, and exposure needs a token.** Tokens, keys and
  webhook URLs are read from environment variables only (`deploy/.env.example` lists them
  all). `serve` refuses to listen beyond loopback without `PRFLAGGER_ADMIN_TOKEN`; viewers
  may read and never write, and the middleware enforces it whatever the UI shows. A
  GitHub token reaches git only as an HTTP header in one process's environment — never in
  argv, a remote URL or `.git/config`.
- **Notification level follows what changed, not how much.** Only `major` interrupts (a
  banner on every page until acknowledged, plus the webhook); `notable` is a feed entry;
  `minor` is history only. The webhook URL comes from `PRFLAGGER_NOTIFY_WEBHOOK`, never
  from a file in the repository.

## The four-field contract

Every `Finding` must carry all four or it is not emitted:

1. `what_changed` — the observation
2. `how_we_know` — the failing test nodeid, the number, the diff hunk
3. `norm` — which repo standard makes it matter (may be None only for `behavior_change`)
4. `confidence` — 0.0–1.0

- **Language support is a pack, never a special case.** A toolchain pack answers what image,
  how to install, how to test, how to lint. No tool name (`pytest`, `ruff`, `go test`) is
  hardcoded anywhere else. A repo nothing recognises falls back to the commands its own CI
  declares — filtered, because a workflow belongs to whoever opened the pull request.
- **Every state change is an event.** The live view and the stored transcript are the same
  append-only log replayed, not two implementations. There is no second code path for
  "watching" a run.
- **Nothing unbounded goes in the events table.** A record whose volume scales with the
  repository under test — a log line — is `publish`ed, not `emit`ted: fanned out live, with
  its durable copy on disk. The `events` table must stay proportional to the number of runs,
  never to how much those runs printed.
- **Every accumulating resource has a reclaimer.** Worktrees, images, transcripts and cached
  job results all grow per run; `engine/janitor.py` removes what no recent or in-flight run
  needs, and the service sweeps hourly. A job refuses to start below 5% free disk rather
  than failing halfway.
- **The queue is bounded and fair.** Admission stops at `server.max_queue_depth`, and
  dispatch is round-robin per repository, so one busy repository cannot starve the rest.

## Conventions

- Python 3.11+, full type hints, `from __future__ import annotations`
- Frozen dataclasses for all domain types; they live in `core/models.py` and are never
  redefined (`prflagger.models` re-exports them for compatibility)
- `pathlib.Path` over strings for paths
- `structlog` for logging; no bare `print` outside `cli.py`
- Commits: `feat(<component>): <imperative one line>`

## Watched repos

Repositories are listed in `config.toml` under `[[repos]]`. Each is cloned bare under
`.cache/repos/` and checked out via git worktrees — never re-cloned per run. A repo with a
`clone_url` that is not a GitHub URL is never polled for pull requests; its runs start on
request.
