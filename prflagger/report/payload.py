"""The findings document: the one artifact the interface renders from.

The web interface has no backend. It is a single HTML file with this document inlined,
so the document is the whole contract between the pipeline and the product surface.

Nothing here invents a shape. `models.py` owns the domain types; this module projects
them into JSON and adds only what the interface needs to draw them: which pipeline node
produced a finding, which sandbox run it can be traced to, and which line of that run's
log is the evidence.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prflagger.models import Finding, Job, Norm, Outcome, Symbol, TestResult

__all__ = [
    "NODES",
    "NODE_FOR_KIND",
    "PipelineNode",
    "PrRecord",
    "SandboxRun",
    "SCHEMA_VERSION",
    "ValidationRound",
    "build_payload",
    "finding_dict",
    "norm_dict",
    "write_payload",
]

SCHEMA_VERSION = 1

# The pipeline as it really runs, in data-flow order. `component` is the SPEC.md label so
# a reader can go from a node on screen to the module that produced it.
NODES: tuple[dict[str, str], ...] = (
    {"id": "pr", "label": "Pull request", "component": "input", "module": "cli.py"},
    {
        "id": "c2",
        "label": "Blast radius",
        "component": "C2",
        "module": "analysis/blast.py",
    },
    {
        "id": "c3",
        "label": "Generate",
        "component": "C3",
        "module": "characterize/generate.py",
    },
    {
        "id": "c1base",
        "label": "Sandbox · base",
        "component": "C1",
        "module": "sandbox/runner.py",
    },
    {
        "id": "filter",
        "label": "Validation filter",
        "component": "C3",
        "module": "characterize/validate.py",
    },
    {
        "id": "c1head",
        "label": "Sandbox · head",
        "component": "C1",
        "module": "sandbox/runner.py",
    },
    {
        "id": "c4",
        "label": "Differential",
        "component": "C4",
        "module": "characterize/differential.py",
    },
    {"id": "c5", "label": "Harvest", "component": "C5", "module": "brain/harvest.py"},
    {"id": "c6", "label": "Enforce", "component": "C6", "module": "brain/enforce.py"},
    {"id": "c7", "label": "Cluster", "component": "C7", "module": "brain/norms.py"},
    {"id": "c8", "label": "Repo brain", "component": "C8", "module": "brain/store.py"},
    {"id": "c9", "label": "Probes", "component": "C9", "module": "probes/"},
    {"id": "c10", "label": "Report", "component": "C10", "module": "report/"},
)

# `kind` is a plain string on Finding, so this map is the only place that knows which
# component emits which kind. Adding a probe means adding a line here, not a node type.
NODE_FOR_KIND: dict[str, str] = {
    "behavior_change": "c4",
    "coverage_gap": "c9",
    "lint_regression": "c9",
    "api_change": "c9",
    "timeout": "c1head",
    "oom": "c1head",
}

EDGES: tuple[dict[str, str], ...] = (
    {"from": "pr", "to": "c2", "kind": "flow"},
    {"from": "c2", "to": "c3", "kind": "flow"},
    {"from": "c3", "to": "c1base", "kind": "flow"},
    {"from": "c1base", "to": "filter", "kind": "filter"},
    {"from": "filter", "to": "c1head", "kind": "flow"},
    {"from": "c1head", "to": "c4", "kind": "flow"},
    {"from": "c4", "to": "c10", "kind": "flow"},
    {"from": "c5", "to": "c6", "kind": "flow"},
    {"from": "c6", "to": "c7", "kind": "flow"},
    {"from": "c7", "to": "c8", "kind": "flow"},
    {"from": "c8", "to": "c10", "kind": "oracle"},
    {"from": "c9", "to": "c10", "kind": "flow"},
)


@dataclass(frozen=True)
class PipelineNode:
    """One node's state for this run. `status` is observed, never assumed."""

    id: str
    status: str  # "idle" | "active" | "complete" | "failed" | "timeout" | "oom"
    detail: str = ""
    count: int | None = None
    duration_s: float | None = None

    def as_dict(self) -> dict[str, Any]:
        node = next((n for n in NODES if n["id"] == self.id), None)
        if node is None:
            raise ValueError(f"unknown pipeline node: {self.id!r}")
        return {
            **node,
            "status": self.status,
            "detail": self.detail,
            "count": self.count,
            "duration_s": self.duration_s,
        }


@dataclass(frozen=True)
class SandboxRun:
    """A recorded container execution, with per-line arrival times for replay.

    `lines` carries `(offset_seconds, text)` pairs measured while the container was
    running. The replay is a replay: nothing is interpolated or invented.
    """

    id: str
    label: str
    job: Job
    result: TestResult
    lines: tuple[tuple[float, str], ...]
    image: str
    exit_code: int | None = None
    pr: int | None = None
    argv: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        per_test = dict(self.result.per_test)
        # pytest reports skipped and xfailed alongside passed and failed; counting them
        # as failures would overstate what the run found.
        failed = sorted(
            nodeid
            for nodeid, outcome in per_test.items()
            if outcome in {"failed", "error"}
        )
        return {
            "id": self.id,
            "label": self.label,
            "pr": self.pr,
            "commit": self.job.commit,
            "command": list(self.job.command),
            "argv": list(self.argv),
            "image": self.image,
            "image_key": self.job.image_key,
            "memory_mb": self.job.memory_mb,
            "timeout_s": self.job.timeout_s,
            "network": "none",
            "read_only_root": True,
            "user": "1000:1000",
            "capabilities": "all dropped",
            "pids_limit": 256,
            "cpus": 1,
            "outcome": _outcome_value(self.result.outcome),
            "exit_code": self.exit_code,
            "wall_s": round(self.result.duration_s, 3),
            "peak_rss_mb": self.result.peak_rss_mb,
            "tests_total": len(per_test),
            "tests_passed": sum(1 for v in per_test.values() if v == "passed"),
            "tests_failed": len(failed),
            "tests_skipped": sum(
                1 for v in per_test.values() if v in {"skipped", "xfailed", "xpassed"}
            ),
            "failed_nodeids": failed,
            "per_test": per_test,
            "lines": _replayable(self.lines),
        }


@dataclass(frozen=True)
class ValidationRound:
    """One symbol's trip through the validation loop, as executed.

    Every count here is the result of running code in a container. `base_failed` is the
    behaviour the generator imagined: discarded, never reported.
    """

    symbol: str
    attempts: tuple[dict[str, Any], ...]
    generated: tuple[str, ...]
    base_failed: tuple[dict[str, str], ...]
    survivors: tuple[str, ...]
    head_failed: tuple[dict[str, str], ...]
    discard_rate: float
    base_run: str | None = None
    head_run: str | None = None
    duration_s: float | None = None
    finding_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "attempts": [dict(attempt) for attempt in self.attempts],
            "generated": list(self.generated),
            "base_failed": [dict(row) for row in self.base_failed],
            "survivors": list(self.survivors),
            "head_failed": [dict(row) for row in self.head_failed],
            "discard_rate": round(self.discard_rate, 4),
            "base_run": self.base_run,
            "head_run": self.head_run,
            "duration_s": self.duration_s,
            "finding_ids": list(self.finding_ids),
        }


@dataclass(frozen=True)
class PrRecord:
    """An analysed pull request and the structural facts behind it.

    `structure` is built from the blast radius and the diff — the real edges and line
    counts. No prose summary of a change is generated here or anywhere else.
    """

    number: int
    title: str
    author: str
    branch: str
    base: str
    head: str
    merged_at: str
    url: str
    files_changed: tuple[dict[str, Any], ...] = ()
    changed_symbols: tuple[Symbol, ...] = ()
    callers: tuple[Symbol, ...] = ()
    hops: int = 2
    insertions: int = 0
    deletions: int = 0
    run_ids: tuple[str, ...] = ()
    finding_ids: tuple[str, ...] = ()
    outcome: str = "passed"
    analysed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "author": self.author,
            "branch": self.branch,
            "base": self.base,
            "head": self.head,
            "merged_at": self.merged_at,
            "url": self.url,
            "analysed_at": self.analysed_at,
            "insertions": self.insertions,
            "deletions": self.deletions,
            "files_changed": [dict(row) for row in self.files_changed],
            "changed_symbols": [_symbol_dict(s) for s in self.changed_symbols],
            "callers": [_symbol_dict(s) for s in self.callers],
            "hops": self.hops,
            "run_ids": list(self.run_ids),
            "finding_ids": list(self.finding_ids),
            "outcome": self.outcome,
        }


@dataclass
class FindingLink:
    """Where a finding came from, beyond what the Finding itself carries.

    `file`/`line_start`/`line_end` locate the symbol in the head revision, and `url`
    points at those exact lines on the forge. A citation a reader cannot open is a
    citation they have to take on trust.
    """

    pr: int | None = None
    run: str | None = None
    log_line: int | None = None
    node: str | None = None
    declared: bool = False
    evidence_urls: dict[int, str] = field(default_factory=dict)
    file: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    excerpt: str | None = None
    url: str | None = None


def norm_dict(norm: Norm) -> dict[str, Any]:
    return {
        "id": norm.id,
        "statement": norm.statement,
        "scope": norm.scope,
        "support": norm.support,
        "distinct_reviewers": norm.distinct_reviewers,
        "confidence": round(norm.confidence, 4),
        "evidence_prs": list(norm.evidence_prs),
    }


def finding_dict(
    finding: Finding, index: int, link: FindingLink | None = None
) -> dict[str, Any]:
    """One finding, carrying the four-field contract and its provenance.

    Refuses to project a finding that breaks the contract: the interface renders what it
    is given, so a finding missing a field must not reach it.
    """
    link = link or FindingLink()
    if not finding.what_changed:
        raise ValueError(f"findings[{index}] ({finding.symbol}) has no what_changed")
    if not finding.how_we_know:
        raise ValueError(f"findings[{index}] ({finding.symbol}) has no how_we_know")
    if finding.norm is None and finding.kind != "behavior_change":
        raise ValueError(
            f"findings[{index}] ({finding.symbol}) is a {finding.kind} with no norm"
        )
    node = link.node or NODE_FOR_KIND.get(finding.kind)
    if node is None:
        raise ValueError(f"findings[{index}] has kind {finding.kind!r} with no node")
    return {
        "id": f"f{index}",
        "kind": finding.kind,
        "symbol": finding.symbol,
        "what_changed": finding.what_changed,
        "how_we_know": finding.how_we_know,
        "norm": norm_dict(finding.norm) if finding.norm else None,
        "confidence": round(finding.confidence, 4),
        "severity": round(finding.severity, 4),
        "node": node,
        "pr": link.pr,
        "run": link.run,
        "log_line": link.log_line,
        "declared": link.declared,
        "evidence_urls": {str(k): v for k, v in link.evidence_urls.items()},
        "where": {
            "file": link.file,
            "line_start": link.line_start,
            "line_end": link.line_end,
            "url": link.url,
            "excerpt": link.excerpt,
        },
    }


def build_payload(
    findings: list[Finding],
    profile: dict[str, Any],
    *,
    links: dict[int, FindingLink] | None = None,
    prs: list[PrRecord] | None = None,
    runs: list[SandboxRun] | None = None,
    validation: list[ValidationRound] | None = None,
    nodes: list[PipelineNode] | None = None,
    provenance: list[dict[str, str]] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Project a run into the document the interface renders.

    Findings arrive already ranked by `report.rank`: order is preserved, because the
    ranking is the pipeline's decision and the interface must not re-derive it.
    """
    links = links or {}
    projected = [
        finding_dict(finding, index, links.get(index))
        for index, finding in enumerate(findings)
    ]
    node_states = {node.id: node.as_dict() for node in (nodes or [])}
    pipeline = [
        node_states.get(
            node["id"],
            {**node, "status": "idle", "detail": "", "count": None, "duration_s": None},
        )
        for node in NODES
    ]
    return {
        "schema": SCHEMA_VERSION,
        "generated_at": generated_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "profile": profile,
        "findings": projected,
        "pipeline": {"nodes": pipeline, "edges": [dict(edge) for edge in EDGES]},
        "prs": [pr.as_dict() for pr in (prs or [])],
        "runs": [run.as_dict() for run in (runs or [])],
        "validation": [round_.as_dict() for round_ in (validation or [])],
        "provenance": [dict(row) for row in (provenance or [])],
        "counts": _counts(projected),
    }


def write_payload(payload: dict[str, Any], out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1, sort_keys=False), encoding="utf-8")
    return out


def _counts(findings: list[dict[str, Any]]) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    for finding in findings:
        by_kind[finding["kind"]] = by_kind.get(finding["kind"], 0) + 1
    return {
        "findings": len(findings),
        "by_kind": by_kind,
        "cited": sum(1 for f in findings if f["norm"]),
        "uncited": sum(1 for f in findings if not f["norm"]),
    }


def _replayable(lines: tuple[tuple[float, str], ...]) -> list[dict[str, Any]]:
    """The run's human-readable output, with its arrival times.

    The machine-readable pytest report shares the same stream (it is written to stdout
    so it survives a read-only container). It is one enormous line that nobody reads and
    it is already projected into `per_test`, so the log view leaves it out and says so.
    """
    projected: list[dict[str, Any]] = []
    for offset, text in lines:
        if text.startswith('{"created"') or text.startswith('{"meta"'):
            projected.append(
                {
                    "t": round(offset, 4),
                    "text": (
                        f"[pytest json report, {len(text)} bytes"
                        " \u2014 projected into per-test outcomes]"
                    ),
                    "omitted": True,
                }
            )
            continue
        projected.append({"t": round(offset, 4), "text": text})
    return projected


def _symbol_dict(symbol: Symbol) -> dict[str, Any]:
    return {
        "fqn": symbol.fqn,
        "kind": symbol.kind,
        "file": symbol.file,
        "line_start": symbol.line_start,
        "line_end": symbol.line_end,
    }


def _outcome_value(outcome: Outcome | str) -> str:
    return outcome.value if isinstance(outcome, Outcome) else str(outcome)
