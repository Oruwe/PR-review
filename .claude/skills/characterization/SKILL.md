---
name: characterization
description: How to generate and validate characterization tests that record current behavior
---

# Characterization tests

A characterization test **records what code does today**. It does not assert what the code
*should* do. If the current behavior is a bug, the test asserts the bug. That is correct and
intended — the test exists to detect *change*, not to judge correctness.

This is the single most common mistake when generating these. A model asked to "write tests"
will write tests for correct behavior. It must be asked to write tests for *observed*
behavior.

## Generation rules

Tests must be:

- **Pure observation.** Call the symbol, assert the exact returned value, raised exception
  type, or mutation. No "should", no correctness judgement.
- **Deterministic.** No `datetime.now()`, no `random`, no ordering assumptions over sets or
  dicts, no dependence on locale or platform paths.
- **Hermetic.** No network, no filesystem writes, no environment variables, no subprocesses.
  The sandbox has `--network=none`; a test that needs the network will fail for the wrong
  reason and be discarded as a false signal.
- **Public API only.** Never reach into names with a leading underscore. Private behavior is
  allowed to change and flagging it produces noise.
- **Single-behavior.** One assertion concern per test function, so a failure names exactly
  what changed.
- **Self-contained.** All imports at the top of the test function's module. No fixtures from
  the target repo's conftest.

Aim for roughly half ordinary inputs and half edge cases: empty, `None`, zero, negative,
boundary values, wrong types, very large inputs. Edge cases are where undeclared behavior
changes hide.

Name tests `test_<fqn_with_underscores>_<short_case>`.

## The validation loop

Generated tests are untrusted. Execution is the filter:

1. Run every generated test against the **base** commit.
2. A test that **fails on base** described behavior the model imagined. Discard it.
3. Regenerate discarded ones, feeding the actual failure output back into the prompt so the
   model sees what the code really returned.
4. Up to 3 attempts, then give up on that symbol.
5. Only tests that **pass on base** proceed to run against head.

This is the project's own thesis applied to itself: do not trust the model's output, verify
it by running it. Say so in the demo — it is one sentence and it is the most sophisticated
thing in the architecture.

## Interpreting the result

- Passed on base, **failed on head** → a behavior change. This is a finding.
- Passed on both → no observable change in that behavior.
- Failed on base → a bad test, discarded. Never a finding.
- Flaky (differs across repeated runs on the same commit) → discard. Run suspicious tests
  twice on base before trusting them.

## Metrics to record

Append to `.cache/metrics.jsonl` per run: symbols attempted, tests generated, tests discarded,
discard rate, attempts used, wall time. The discard rate is a headline number for the writeup
— a high rate means either the symbol is hard to characterize or the generation prompt needs
work, and knowing which is the difference between a finished project and a stuck one.

## Prompt template

```
Here is a Python function and its module context.

<source>

Write {n} pytest test functions that RECORD ITS CURRENT BEHAVIOR.

Do not test what the function should do. Test what it does. If it returns None for
empty input, assert it returns None. If it raises KeyError, assert KeyError.

Rules: public API only; deterministic; no network, filesystem, or environment access;
one behavior per test; all imports inside the module you emit.

Cover roughly {n//2} ordinary inputs and {n//2} edge cases (empty, None, zero, negative,
boundary, wrong type).

Return only Python code, no prose, no markdown fences.
```

On a retry, append:

```
These tests failed against the unchanged code, which means the behavior you assumed was
wrong. Here is what actually happened:

<failure output>

Rewrite them to match the real behavior shown above.
```
