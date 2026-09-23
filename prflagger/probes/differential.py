"""Observations from comparing a suite's behaviour at base and at head.

This is the probe that matters most and the one that needs no model at all: a
test that passed before the change and fails after it is a behaviour change,
full stop. The evidence is the test's own nodeid, which the reader can run.

Everything here is a comparison of two `TestResult`s the sandbox already
produced, so it is pure and instant. The expensive part — running the suites —
happened in the sandbox.
"""

from __future__ import annotations

from prflagger.core.ids import observation_id
from prflagger.core.models import Observation, Outcome, TestResult

__all__ = ["SEVERITY", "differential_observations", "outcome_observations"]

#: SPEC.md § C9's severity table. A behaviour change outranks everything because
#: it is the only one that proves the program now does something different.
SEVERITY = {
    "behavior_change": 1.0,
    "test_removed": 0.7,
    "api_change": 0.8,
    "coverage_gap": 0.5,
    "lint_regression": 0.3,
    "timeout": 0.9,
    "oom": 0.9,
    "install_failed": 0.6,
    "collection_error": 0.7,
}


def differential_observations(
    run_id: str, base: TestResult, head: TestResult
) -> list[Observation]:
    """Tests whose verdict changed between the two commits.

    Only base-passed/head-failed is reported as a behaviour change. The reverse —
    a test that was failing and now passes — is a fix, and this system does not
    editorialise about good news.
    """
    observations: list[Observation] = []

    for nodeid, base_status in sorted(base.per_test.items()):
        if base_status != "passed":
            continue
        head_status = head.per_test.get(nodeid)

        if head_status is None:
            observations.append(
                _observation(
                    run_id,
                    kind="test_removed",
                    symbol=_symbol_of(nodeid),
                    what_changed=(
                        f"{nodeid} passed at base and is no longer collected at head"
                    ),
                    how_we_know=(
                        f"present in the base run's report, absent from the head run's; "
                        f"base collected {len(base.per_test)} tests, head collected "
                        f"{len(head.per_test)}"
                    ),
                    evidence_ref=nodeid,
                    confidence=0.75,
                )
            )
            continue

        if head_status in ("failed", "error"):
            observations.append(
                _observation(
                    run_id,
                    kind="behavior_change",
                    symbol=_symbol_of(nodeid),
                    what_changed=f"{nodeid} passed at base and {head_status} at head",
                    how_we_know=f"pytest nodeid {nodeid}: passed -> {head_status}",
                    evidence_ref=nodeid,
                    confidence=0.9,
                )
            )

    return observations


def outcome_observations(
    run_id: str, base: TestResult, head: TestResult
) -> list[Observation]:
    """Whole-suite outcomes that are findings in their own right.

    SPEC.md is explicit that a timeout and an OOM are results, not errors. They
    are only reported when base did *not* have the same problem — a suite that
    already timed out before the change tells you nothing about the change.
    """
    observations: list[Observation] = []
    interesting = {
        Outcome.TIMEOUT: (
            "timeout",
            "the suite did not finish within its time limit at head",
        ),
        Outcome.OOM: ("oom", "the suite was killed for exceeding its memory limit at head"),
        Outcome.COLLECTION_ERROR: (
            "collection_error",
            "the suite could not be collected at head",
        ),
        Outcome.INSTALL_FAILED: (
            "install_failed",
            "the environment for this repo could not be built",
        ),
    }
    entry = interesting.get(head.outcome)
    if entry is None or head.outcome == base.outcome:
        return observations

    kind, description = entry
    observations.append(
        _observation(
            run_id,
            kind=kind,
            symbol="(suite)",
            what_changed=description,
            how_we_know=(
                f"base run outcome {base.outcome.value}, head run outcome "
                f"{head.outcome.value}; head ran for {head.duration_s:.1f}s"
                + (f", peak memory {head.peak_rss_mb} MB" if head.peak_rss_mb else "")
            ),
            evidence_ref=f"outcome:{head.outcome.value}",
            confidence=0.95,
        )
    )
    return observations


def _observation(
    run_id: str,
    *,
    kind: str,
    symbol: str,
    what_changed: str,
    how_we_know: str,
    evidence_ref: str,
    confidence: float,
) -> Observation:
    return Observation(
        id=observation_id(run_id, kind, symbol, evidence_ref),
        run_id=run_id,
        kind=kind,
        symbol=symbol,
        what_changed=what_changed,
        how_we_know=how_we_know,
        evidence_ref=evidence_ref,
        severity=SEVERITY.get(kind, 0.5),
        confidence=confidence,
    )


def _symbol_of(nodeid: str) -> str:
    """A readable symbol for a nodeid, without pretending to resolve it.

    `tests/test_x.py::TestThing::test_case` becomes `TestThing.test_case`. The
    symbol the *change* touched is attached later, by the blast-radius pass —
    inventing a mapping here would be a guess wearing a fact's clothing.
    """
    _, _, rest = nodeid.partition("::")
    return rest.replace("::", ".") if rest else nodeid
