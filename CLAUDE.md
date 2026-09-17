# PR Flagger

A PR verification agent. It does **not** review code or give opinions. It runs mechanical
probes and reports facts, each citing evidence. `SPEC.md` is the authoritative design.

## Commands

```bash
pytest tests/ -x -q              # test
pytest tests/test_<name>.py -x   # single component
ruff check . && mypy prflagger/  # lint + types
python -m prflagger.cli check --repo <path> --base <sha> --head <sha>
```

## Non-negotiable rules

- **IMPORTANT: Never mock Docker, git, or the target repo.** Tests run against real
  artifacts. A mocked test here proves nothing.
- **Never use an LLM for structure.** Symbols, call graphs and diffs come from `ast` and
  `git`. LLM calls are only for natural language: generating tests, clustering comments.
- **Never let an LLM decide storage.** Schema is fixed in `models.py`. Extraction fills a
  known shape; it does not invent one.
- **Every LLM call goes through `prflagger/llm.py`.** It is content-addressed and cached to
  disk. Never call boto3 bedrock directly — uncached calls blow the budget.
- **Sandbox runs return typed outcomes, never raise.** `TIMEOUT` and `OOM` are findings, not
  errors.
- **The agent never emits a verdict.** No "approve", "reject", "looks risky", "LGTM". Only
  observations with evidence.

## The four-field contract

Every `Finding` must carry all four or it is not emitted:

1. `what_changed` — the observation
2. `how_we_know` — the failing test nodeid, the number, the diff hunk
3. `norm` — which repo standard makes it matter (may be None only for `behavior_change`)
4. `confidence` — 0.0–1.0

## Conventions

- Python 3.11+, full type hints, `from __future__ import annotations`
- Frozen dataclasses for all domain types; they live in `models.py` and are never redefined
- `pathlib.Path` over strings for paths
- `structlog` for logging; no bare `print` outside `cli.py`
- Commits: `feat(<component>): <imperative one line>`

## Target repo

The repo under analysis is configured in `config.toml`. It is cloned bare under
`.cache/repos/` and checked out via git worktrees — never re-cloned per run.
