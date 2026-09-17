---
name: component
description: Implement one component from SPEC.md end to end, with its acceptance test, until green
disable-model-invocation: true
---

Implement the component identified by $ARGUMENTS (e.g. `C1`, `runner`, `brain/norms`).

## Procedure

1. Read that component's section in `SPEC.md`. **That section is authoritative.** Do not
   invent, rename, widen or "improve" any interface it defines.
2. Read `prflagger/models.py`. Never redefine a type that already exists there. If you need
   a new shared type, add it to `models.py` rather than defining it locally.
3. Implement the public interface exactly as specified. Private helpers are your choice.
4. Write the component's **Acceptance** block into `tests/test_<component>.py` as real
   assertions. The acceptance criteria are the test — do not substitute weaker ones.
5. Run `pytest tests/test_<component>.py -x -q`.
6. On failure, diagnose and fix, then rerun. Up to 5 attempts.
7. Run `ruff check . && mypy prflagger/` and fix what they report.
8. Commit: `feat(<component>): <imperative one-line summary>`.

## Stop conditions — report, do not work around

- Still failing after 5 attempts → stop and report the actual blocker.
- The spec's interface appears wrong or impossible → stop and say exactly why. Do not
  change it unilaterally.
- An acceptance criterion cannot be met with real artifacts → stop. **Do not weaken the
  test, mock the dependency, or mark it xfail.**

## Hard rules

- **Never mock Docker, git, the network, or the target repo.** If a test needs the target
  repo, use the real one from `config.toml`. A passing mocked test is worse than no test
  because it reports false confidence.
- **Every LLM call goes through `prflagger.llm.complete` or `.embed`.** Never call boto3
  bedrock directly. Uncached calls cost real money and there is a fixed budget.
- **Structure comes from `ast` and `git`, never from an LLM.** If you find yourself prompting
  a model to find function definitions or callers, you have taken a wrong turn.
- **Sandbox execution returns typed `Outcome` values and never raises** for job failure.
  `TIMEOUT` and `OOM` are results, not exceptions.
- **No verdicts anywhere.** This system emits observations with evidence. No code path may
  produce "approve", "reject", "looks good", "risky", or a quality score.
- Type hints on every public function. `from __future__ import annotations` at the top of
  every module.

## Before you finish

State in one line: what you implemented, which acceptance assertions now pass, and anything
you deliberately left out.
