"""One model call per group of observations; nothing kept that does not check out.

The model is told the repository's charter and standards (a cached prefix) and
one packet of observations with their source, diff and callers. It must answer
in a fixed JSON shape, per observation it was shown:

    assessment   consistent_with_repo | diverges_from_repo |
                 insufficient_evidence | probe_false_positive
    reasoning    one or two sentences
    citations    >= 1, each resolved by `citations.resolve`
    suggestion   optional, with its own resolvable citations

It cannot add an observation, speak about one it was not shown, or keep a claim
whose references do not resolve. Everything it could not do is returned as a
reason, so the run's coverage statement can name it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import structlog

from prflagger.adjudicate.citations import Evidence, resolve
from prflagger.adjudicate.situate import Packet, render_packet
from prflagger.core.models import Adjudication, Citation, PullRequest, Suggestion
from prflagger.llm.client import ModelClient
from prflagger.llm.ledger import BudgetExceeded
from prflagger.llm.provider import Request

__all__ = ["INSTRUCTIONS", "Outcome", "adjudicate"]

log = structlog.get_logger(__name__)

INSTRUCTIONS = """\
You are the adjudication stage of a pull-request verification service. Probes have
already produced observations: mechanical facts, each with its evidence. You do not
review the code, and you never judge the pull request as a whole — no approve, reject,
LGTM, "looks risky" or any verdict on the change.

For each observation you are shown, decide how it reads against THIS repository — its
charter, its standards, its own code — and nothing else. Choose exactly one assessment:
- consistent_with_repo: it matches how this repository already does things.
- diverges_from_repo: it departs from a standard, rule or established pattern this
  repository states or follows.
- insufficient_evidence: what you were shown does not settle it.
- probe_false_positive: the fact is real but misleading here, and the material shows why.

Every assessment needs at least one citation, each exactly one of:
  {"type": "norm", "ref": "<a norm id in brackets from the standards list>"}
  {"type": "code", "ref": "path:line" or "path:start-end" or "base:path:line",
   "quote": "<text copied verbatim from those lines>"}
  (a charter line is cited as code by the source shown in brackets, e.g. README.md:3)
  {"type": "test", "ref": "<a test id exactly as it appears in the observations>"}
  {"type": "diff", "ref": "<path of a file this pull request changes>"}
Take line numbers from the numbered SOURCE excerpts. Every citation is checked; one
that does not resolve is discarded, and an assessment left without a valid citation is
discarded with it. Never invent a norm id, a path, a line or a quote.

A suggestion is optional. Give one only when a specific change would bring the code in
line with a standard or pattern of this repository that you cite. It needs its own
citations. Keep patch_sketch to a short unified diff or code fragment.

Answer with JSON only, no prose around it:
{"adjudications": [{"observation_id": "...", "assessment": "...",
  "reasoning": "...", "citations": [...],
  "suggestion": null or {"summary": "...", "rationale": "...", "patch_sketch": "...",
                         "confidence": 0.0 to 1.0, "citations": [...]}}]}
"""


@dataclass
class Outcome:
    adjudications: list[Adjudication] = field(default_factory=list)
    suggestions: list[Suggestion] = field(default_factory=list)
    #: (observation id, why its adjudication or suggestion was discarded)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    #: (group, why it was not adjudicated at all)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    usd: float = 0.0
    calls: int = 0
    cached: int = 0


def adjudicate(
    *,
    run_id: str,
    repo: str,
    pull: PullRequest | None,
    packets: list[Packet],
    context: str,
    evidence: Evidence,
    client: ModelClient,
    model: str,
    cache_ttl: str = "1h",
    max_tokens: int = 2000,
) -> Outcome:
    outcome = Outcome()
    for packet in packets:
        if not client.available:
            outcome.skipped.append((packet.key, client.unavailable_reason))
            continue
        request = Request(
            model=model,
            system=((INSTRUCTIONS, True), (context, True)),
            prompt=render_packet(packet, pull),
            max_tokens=max_tokens,
            cache_ttl=cache_ttl,
        )
        try:
            answer = client.ask(request, stage="adjudication", run_id=run_id, repo=repo)
        except BudgetExceeded as error:
            outcome.skipped.append((packet.key, f"refused before sending: {error}"))
            continue
        except Exception as error:  # noqa: BLE001 - a failed call is a gap, not a crash
            outcome.skipped.append((packet.key, f"the model call failed: {str(error)[:200]}"))
            continue
        outcome.calls += 1
        outcome.cached += 1 if answer.cached else 0
        outcome.usd += answer.usd

        items = _parse(answer.text)
        if items is None:
            for o in packet.observations:
                outcome.rejected.append((o.id, "the answer was not the required JSON"))
            continue
        shown = {o.id for o in packet.observations}
        share = answer.usd / max(1, len(items))
        answered: set[str] = set()
        for item in items:
            _take(item, shown, answered, evidence, model, share, outcome)
        for missing in sorted(shown - answered):
            outcome.rejected.append((missing, "the answer did not address it"))
    log.info(
        "adjudication.done", run=run_id, groups=len(packets), calls=outcome.calls,
        kept=len(outcome.adjudications), suggestions=len(outcome.suggestions),
        rejected=len(outcome.rejected), skipped=len(outcome.skipped), usd=round(outcome.usd, 5),
    )
    return outcome


def _take(
    item: dict[str, Any],
    shown: set[str],
    answered: set[str],
    evidence: Evidence,
    model: str,
    usd: float,
    outcome: Outcome,
) -> None:
    oid = str(item.get("observation_id", ""))
    if oid not in shown:
        # Speaking about an observation it was not shown is how an invented finding
        # would enter; it is dropped without a trace in the report.
        log.warning("adjudication.unknown_observation", observation=oid[:80])
        return
    if oid in answered:
        return
    answered.add(oid)

    assessment = str(item.get("assessment", ""))
    reasoning = str(item.get("reasoning", "")).strip()
    if assessment not in Adjudication.ASSESSMENTS or not reasoning:
        outcome.rejected.append((oid, f"unusable assessment {assessment[:40]!r}"))
        return
    offered = _citations(item.get("citations"))
    kept = [c for c in (resolve(c, evidence) for c in offered) if c is not None]
    if not kept:
        outcome.rejected.append(
            (oid, f"none of its {len(offered)} citation(s) could be checked"
                  if offered else "it cited nothing")
        )
        return
    outcome.adjudications.append(Adjudication(
        observation_id=oid, assessment=assessment, reasoning=reasoning[:1200],
        citations=tuple(kept), model=model, usd=round(usd, 6),
    ))

    raw = item.get("suggestion")
    if not isinstance(raw, dict) or not str(raw.get("summary", "")).strip():
        return
    offered = _citations(raw.get("citations"))
    backing = [c for c in (resolve(c, evidence) for c in offered) if c is not None]
    if not backing:
        outcome.rejected.append((oid, "its suggestion cited nothing that could be checked"))
        return
    try:
        confidence = min(1.0, max(0.0, float(raw.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5
    outcome.suggestions.append(Suggestion(
        observation_id=oid,
        summary=str(raw["summary"]).strip()[:300],
        rationale=str(raw.get("rationale", "")).strip()[:1200],
        patch_sketch=str(raw.get("patch_sketch", "")).strip()[:4000],
        confidence=confidence,
        citations=tuple(backing),
    ))


def _citations(raw: Any) -> list[Citation]:
    out: list[Citation] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(Citation(
                type=str(entry.get("type", "")), ref=str(entry.get("ref", "")),
                quote=str(entry.get("quote", "") or "")[:400],
            ))
        except ValueError:
            continue  # an unknown type or an empty ref is not a citation
    return out


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _parse(text: str) -> list[dict[str, Any]] | None:
    """The `adjudications` list from the answer, or None if there is none."""
    body = _FENCE.sub("", text.strip())
    start = body.find("{")
    if start < 0:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(body[start:])
    except json.JSONDecodeError:
        return None
    items = payload.get("adjudications") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return None
    return [item for item in items if isinstance(item, dict)]
