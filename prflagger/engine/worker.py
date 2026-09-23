"""Driving one run from queued to done.

The worker is the only thing that moves a run through its states, and every move
it makes is an event. That is what makes the live view and the stored transcript
the same thing rather than two implementations that drift.

A stage that cannot run degrades rather than aborting: if the base suite will
not build, the run still reports what it could establish and names what it could
not. SPEC.md's rule that partial verification is never presented as complete is
enforced by the coverage statement this assembles, not by hoping each probe
behaves.
"""

from __future__ import annotations

import asyncio
import functools
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import structlog

from prflagger.charter.drift import compare, evidence_lines, headline
from prflagger.charter.extract import extract_charter
from prflagger.charter.relevance import WEIGHT, grounded
from prflagger.core.config import Config
from prflagger.core.ids import job_id
from prflagger.core.models import (
    Charter,
    CharterDrift,
    Observation,
    Outcome,
    Run,
    RunState,
    TestResult,
)
from prflagger.engine.notify import Notifier
from prflagger.engine.states import can_transition, progress_of
from prflagger.lang.base import Toolchain
from prflagger.lang.detect import for_repo
from prflagger.probes.differential import differential_observations, outcome_observations
from prflagger.probes.surface import lint_observations, public_index, surface_observations
from prflagger.sandbox.pool import JobSpec, SandboxPool
from prflagger.storage.events import EventBus
from prflagger.storage.repos import Store
from prflagger.vcs.worktrees import worktree_for

__all__ = ["RunWorker"]

log = structlog.get_logger(__name__)


@dataclass
class _Verification:
    """What this run managed to check, and what it did not.

    Assembled as the run goes so the report can state its own limits precisely
    instead of implying it verified everything it attempted.
    """

    verified: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def ok(self, what: str) -> None:
        self.verified.append(what)

    def skip(self, what: str, why: str) -> None:
        self.skipped.append((what, why))

    def as_dict(self) -> dict[str, Any]:
        return {
            "verified": list(self.verified),
            "skipped": [{"what": w, "why": y} for w, y in self.skipped],
            "complete": not self.skipped,
        }


class RunWorker:
    """Executes runs. One instance, many runs, bounded by the sandbox pool."""

    def __init__(
        self,
        config: Config,
        store: Store,
        bus: EventBus,
        pool: SandboxPool,
        notifier: Notifier | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._bus = bus
        self._pool = pool
        self._notifier = notifier

    # -- state ----------------------------------------------------------------

    def _advance(self, run: Run, state: RunState, **payload: Any) -> Run:
        if not can_transition(run.state, state):
            log.warning(
                "run.illegal_transition",
                run=run.id, frm=run.state.value, to=state.value,
            )
            return run
        self._store.set_run_state(run.id, state, error=str(payload.get("error", "")))
        self._bus.emit(
            "run.state", run_id=run.id, repo=run.repo, state=state.value,
            progress=progress_of(state), **payload,
        )
        return self._store.run(run.id) or run

    # -- the pipeline ---------------------------------------------------------

    async def execute(self, run: Run) -> Run:
        """Take `run` from wherever it is to a terminal state. Never raises."""
        started = time.monotonic()
        coverage = _Verification()
        try:
            return await self._pipeline(run, coverage, started)
        except Exception as error:  # noqa: BLE001 - a worker must not die on one run
            log.exception("run.failed", run=run.id)
            self._bus.emit(
                "run.state", run_id=run.id, repo=run.repo, state=RunState.FAILED.value,
                progress=1.0, error=str(error)[:500],
            )
            self._store.set_run_state(run.id, RunState.FAILED, error=str(error)[:500])
            return self._store.run(run.id) or run

    async def _pipeline(self, run: Run, coverage: _Verification, started: float) -> Run:
        entry = self._config.repo(run.repo)

        # -- prepare: get both commits on disk --------------------------------
        run = self._advance(run, RunState.PREPARING)
        url = entry.clone_url or None
        base_tree = await _thread(worktree_for, run.repo, run.base_sha, url=url)
        head_tree = await _thread(worktree_for, run.repo, run.head_sha, url=url)
        toolchain = await _thread(
            for_repo, head_tree, config=self._config, slug=run.repo
        )
        self._bus.emit(
            "run.toolchain", run_id=run.id, repo=run.repo,
            pack=toolchain.id, display=toolchain.display, detail=toolchain.as_dict(),
        )

        # -- run the suite at both commits ------------------------------------
        run = self._advance(run, RunState.BASE_RUN)
        base = await self._suite(run, "base_run", base_tree, run.base_sha, toolchain, entry)

        run = self._advance(run, RunState.HEAD_RUN)
        head = await self._suite(run, "head_run", head_tree, run.head_sha, toolchain, entry)

        # -- probe -------------------------------------------------------------
        run = self._advance(run, RunState.PROBING)
        observations = await self._probe(
            run, coverage, base, head, base_tree, head_tree, toolchain, entry
        )

        # -- rank and persist ---------------------------------------------------
        run = self._advance(run, RunState.RENDERING, observations=len(observations))
        ranked = self._rank(run, observations)
        self._store.put_observations(ranked)
        self._bus.emit(
            "run.observations", run_id=run.id, repo=run.repo,
            count=len(ranked),
            by_kind=_counts(ranked),
            coverage=coverage.as_dict(),
        )

        run = self._advance(
            run, RunState.DONE,
            observations=len(ranked),
            duration_s=round(time.monotonic() - started, 2),
            coverage=coverage.as_dict(),
        )
        log.info(
            "run.done", run=run.id, repo=run.repo, pr=run.pr_number,
            observations=len(ranked), seconds=round(time.monotonic() - started, 1),
        )
        return run

    # -- stages ---------------------------------------------------------------

    async def _suite(
        self, run: Run, stage: str, tree: Path, commit: str, toolchain: Toolchain, entry: Any
    ) -> TestResult:
        spec = JobSpec(
            run_id=run.id,
            job_id=job_id(run.id, stage),
            stage=stage,
            repo_path=tree,
            commit=commit,
            toolchain=toolchain,
            command=toolchain.test.argv,
            timeout_s=self._config.sandbox.default_timeout_s,
            memory_mb=self._config.sandbox.default_memory_mb,
            package_roots=tuple(entry.package_roots),
            system_binaries=tuple(entry.system_binaries),
        )
        result = await self._pool.run(spec)
        self._record_job(run, spec, result)
        return result

    def _record_job(self, run: Run, spec: JobSpec, result: TestResult) -> None:
        self._store.put_job(
            id=spec.job_id, run_id=run.id, stage=spec.stage,
            idempotency_key=spec.as_job("").idempotency_key,
            argv=list(spec.command), outcome=result.outcome.value,
            duration_s=result.duration_s, peak_rss_mb=result.peak_rss_mb,
            memory_mb=spec.memory_mb, timeout_s=spec.timeout_s,
            log_path=str(spec.job_id), finished_at=time.time(),
        )

    async def _probe(
        self,
        run: Run,
        coverage: _Verification,
        base: TestResult,
        head: TestResult,
        base_tree: Path,
        head_tree: Path,
        toolchain: Toolchain,
        entry: Any,
    ) -> list[Observation]:
        observations: list[Observation] = []

        # Behaviour: only meaningful when the base suite actually ran.
        if base.per_test:
            observations += differential_observations(run.id, base, head)
            coverage.ok(
                f"behavioural comparison over {len(base.per_test)} tests "
                f"({toolchain.display} suite at both commits)"
            )
        else:
            coverage.skip(
                "behavioural comparison",
                f"the base suite produced no per-test results "
                f"(outcome: {base.outcome.value}) — nothing to compare against",
            )
        observations += outcome_observations(run.id, base, head)

        # Public surface: structural, no sandbox needed. Each commit is indexed
        # once and the index serves both this probe and the charter below.
        roots = tuple(entry.package_roots)
        base_index: dict[str, Any] = {}
        head_index: dict[str, Any] = {}
        if toolchain.grammar == "python":
            base_index = await _thread(public_index, base_tree, roots)
            head_index = await _thread(public_index, head_tree, roots)
            surface = await _thread(
                surface_observations, run.id, base_tree, head_tree, roots,
                base_index=base_index, head_index=head_index,
            )
            observations += surface
            coverage.ok("public API surface compared by parsing both commits")
        else:
            coverage.skip(
                "public API surface",
                f"no exact symbol extractor for {toolchain.display} yet",
            )

        # Lint: run the repo's own configured linters at both commits.
        if toolchain.lints:
            lint, ran, failed = await self._lint(run, base_tree, head_tree, toolchain, entry)
            observations += lint
            if ran:
                coverage.ok(
                    "lint compared with the repo's own configuration ("
                    + ", ".join(sorted(ran))
                    + ")"
                )
            for tool, why in sorted(failed.items()):
                # Claiming a linter ran when it did not is the one thing the
                # coverage statement exists to prevent.
                coverage.skip(f"lint comparison ({tool})", why)
        else:
            coverage.skip("lint comparison", f"{toolchain.display} pack declares no linters")

        observations = await self._ground(
            run, coverage, observations, base_tree, head_tree, toolchain, roots,
            base_index, head_index,
        )

        coverage.skip(
            "adjudication against repo norms",
            "no model provider is configured for this run",
        )
        return observations

    async def _ground(
        self,
        run: Run,
        coverage: _Verification,
        observations: list[Observation],
        base_tree: Path,
        head_tree: Path,
        toolchain: Toolchain,
        roots: tuple[str, ...],
        base_index: dict[str, Any],
        head_index: dict[str, Any],
    ) -> list[Observation]:
        """Judge every observation against this repository's own charter, and
        measure how far the pull request would move that charter.

        The charter used is the one at the pull request's *base* — the repository
        as this change found it — built from that checkout alone. The stored
        default-branch charter is the fallback. No other repository's memory is
        reachable from here.
        """
        def build(tree: Path, sha: str, index: dict[str, Any]) -> Charter:
            return extract_charter(
                tree, slug=run.repo, sha=sha, toolchain=toolchain, package_roots=roots,
                public_api=list(index) if index else None,
            )

        try:
            base_charter: Charter | None = await _thread(
                build, base_tree, run.base_sha, base_index
            )
            head_charter: Charter | None = await _thread(
                build, head_tree, run.head_sha, head_index
            )
        except Exception as error:  # noqa: BLE001 - grounding is additive, never fatal
            log.warning("charter.run_extract_failed", run=run.id, error=str(error)[:200])
            base_charter = self._store.charter(run.repo)
            head_charter = None

        if base_charter is None:
            coverage.skip(
                "judging findings against the repository's charter",
                "no charter could be built for this repository",
            )
            return observations

        observations = grounded(observations, base_charter, repo=run.repo)
        coverage.ok(
            f"judged only against {run.repo}'s own charter at base "
            f"{run.base_sha[:10]} ({len(base_charter.claims)} cited claims)"
        )

        if head_charter is not None:
            drift = compare(base_charter, head_charter, self._config.charter)
            self._store.set_run_charter_impact(run.id, drift.level)
            self._bus.emit(
                "run.charter_impact", run_id=run.id, repo=run.repo, level=drift.level,
                signals=[s.__dict__ for s in drift.signals],
            )
            if drift.level != "none":
                coverage.ok(
                    f"charter impact measured: {drift.level} "
                    f"({len(drift.signals)} "
                    f"{'change' if len(drift.signals) == 1 else 'changes'} "
                    f"to what the repository is)"
                )
            self._announce(run, drift)
        return observations

    def _announce(self, run: Run, drift: CharterDrift) -> None:
        """A pull request that would change what the repository *is* is not a
        routine finding, so it is announced like a major update — distinctly, and
        once per head commit."""
        if self._notifier is None or drift.level != "major":
            return
        top = headline(drift)
        pull = self._store.pull(run.repo, run.pr_number)
        title = pull.title if pull else f"#{run.pr_number}"
        self._notifier.raise_(
            repo=run.repo,
            kind="pr.charter_change",
            level="major",
            title=f"PR #{run.pr_number} would change what {run.repo} is: {top.detail}",
            body=f"“{title}” — {run.base_sha[:10]} → {run.head_sha[:10]}",
            sha=run.head_sha,
            evidence=evidence_lines(drift),
        )

    async def _lint(
        self, run: Run, base_tree: Path, head_tree: Path, toolchain: Toolchain, entry: Any
    ) -> tuple[list[Observation], set[str], dict[str, str]]:
        """Run each linter at both commits.

        Returns the observations, the tools that ran at both commits, and why any
        others did not — a linter that ran at only one commit cannot produce a
        delta, and reporting one anyway would invent a regression.
        """
        results: dict[str, dict[str, str]] = {}
        outcomes: dict[str, dict[str, Outcome]] = {}
        for label, tree, commit in (
            ("base", base_tree, run.base_sha), ("head", head_tree, run.head_sha)
        ):
            for index, lint in enumerate(toolchain.lints):
                spec = JobSpec(
                    run_id=run.id,
                    job_id=job_id(run.id, f"lint_{label}_{lint.tool}", index),
                    stage=f"lint_{label}",
                    repo_path=tree,
                    commit=commit,
                    toolchain=toolchain,
                    command=lint.argv,
                    timeout_s=min(240, self._config.sandbox.default_timeout_s),
                    memory_mb=self._config.sandbox.default_memory_mb,
                    package_roots=tuple(entry.package_roots),
                    system_binaries=tuple(entry.system_binaries),
                    report_format="none",
                    ok_codes=lint.ok_codes,
                )
                result = await self._pool.run(spec)
                self._record_job(run, spec, result)
                results.setdefault(label, {})[lint.parser] = result.stdout + result.stderr
                outcomes.setdefault(label, {})[lint.tool] = result.outcome

        ran: set[str] = set()
        failed: dict[str, str] = {}
        for lint in toolchain.lints:
            base_outcome = outcomes.get("base", {}).get(lint.tool)
            head_outcome = outcomes.get("head", {}).get(lint.tool)
            if base_outcome is Outcome.PASSED and head_outcome is Outcome.PASSED:
                ran.add(lint.tool)
            else:
                failed[lint.tool] = (
                    f"{lint.tool} did not complete at both commits "
                    f"(base: {base_outcome.value if base_outcome else 'not run'}, "
                    f"head: {head_outcome.value if head_outcome else 'not run'})"
                )

        usable = {
            label: {
                parser: text
                for parser, text in parsers.items()
                if any(lint.parser == parser and lint.tool in ran for lint in toolchain.lints)
            }
            for label, parsers in results.items()
        }
        observations = lint_observations(
            run.id, toolchain, usable.get("base", {}), usable.get("head", {})
        )
        return observations, ran, failed

    def _rank(self, run: Run, observations: list[Observation]) -> list[Observation]:
        """Order by what the reader should look at first.

        score = confidence x severity x centrality x relevance

        Centrality is how many callers the touched symbol has, normalised.
        Relevance is how close the finding sits to what this repository says it
        is for (see `charter.relevance`). No model is involved — SPEC.md § C10
        requires ranking be deterministic, and the same input must produce the
        same order twice.
        """
        callers = self._store.caller_counts(run.repo)
        widest = max(callers.values(), default=0)
        ranked = []
        for observation in observations:
            count = callers.get(observation.symbol, 0)
            centrality = 0.5 if widest == 0 else 0.5 + 0.5 * (count / widest)
            weight = WEIGHT.get(observation.relevance, 1.0)
            score = observation.confidence * observation.severity * centrality * weight
            ranked.append(replace(observation, rank_score=round(score, 6)))
        # Tie-break on id so the order is total, not merely sorted.
        return sorted(ranked, key=lambda o: (-o.rank_score, o.id))


def _counts(observations: list[Observation]) -> dict[str, int]:
    out: dict[str, int] = {}
    for observation in observations:
        out[observation.kind] = out.get(observation.kind, 0) + 1
    return out


async def _thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking call off the loop, so streaming stays responsive."""
    return await asyncio.to_thread(functools.partial(fn, *args, **kwargs))
