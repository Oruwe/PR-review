"""Scripted model answers for the adjudication tests.

Each responder reads the real request the real SDK sent — the numbered source
excerpts, the observation ids, the standards list — and answers the way the
instructions ask. `honest` cites things that exist; `fabricating` cites things
that do not, which is the failure the citation gate exists to catch.
"""

from __future__ import annotations

import json
import re
from typing import Any

from tests.model_fixture import message

_OBSERVATION = re.compile(r"^- id=(\S+) kind=(\S+) symbol=.*?\n(?:.*\n)*?  evidence: (.*)$",
                          re.MULTILINE)
_SOURCE = re.compile(r"^SOURCE (base:)?(\S+?):\d+-\d+ at the \w+ commit\n((?:\s*\d+  .*\n?)+)",
                     re.MULTILINE)
_LINE = re.compile(r"^\s*(\d+)  (.*\S.*)$", re.MULTILINE)
_NORM = re.compile(r"^- \[([a-z][\w-]*)\]", re.MULTILINE)


def _prompt(body: dict[str, Any]) -> str:
    content = body["messages"][0]["content"]
    return content if isinstance(content, str) else content[0]["text"]


def _code_citation(prompt: str) -> dict[str, str] | None:
    for match in _SOURCE.finditer(prompt):
        prefix, path, block = match.group(1) or "", match.group(2), match.group(3)
        for number, text in _LINE.findall(block):
            if len(text.strip()) > 8:
                ref = f"{prefix}{path}:{number}"
                return {"type": "code", "ref": ref, "quote": text.strip()}
    return None


def honest(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    prompt = _prompt(body)
    norms = _NORM.findall(body["system"][1]["text"])
    code = _code_citation(prompt)
    items = []
    for oid, kind, ref in _OBSERVATION.findall(prompt):
        citations: list[dict[str, str]] = []
        if code:
            citations.append(code)
        if norms:
            citations.append({"type": "norm", "ref": norms[0]})
        if "::" in ref:
            citations.append({"type": "test", "ref": ref.strip()})
        items.append({
            "observation_id": oid,
            "assessment": "diverges_from_repo",
            "reasoning": f"The {kind} departs from what the cited standard and code establish.",
            "citations": citations,
            "suggestion": {
                "summary": "Keep the previous behaviour for this case, or state the change.",
                "rationale": "The repository's own code treats this case differently.",
                "patch_sketch": "-    if percent < 0:\n+    if percent <= 0:",
                "confidence": 0.6,
                "citations": citations[:1],
            } if code else None,
        })
    return message(json.dumps({"adjudications": items}), model=body["model"],
                   input_tokens=1800, output_tokens=240, cache_write=900)


def fabricating(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    prompt = _prompt(body)
    items = [{
        "observation_id": oid,
        "assessment": "diverges_from_repo",
        "reasoning": "It breaks the team's long-standing rule.",
        "citations": [
            {"type": "norm", "ref": "review-always-use-decimal-for-money"},
            {"type": "code", "ref": "shoplib/pricing.py:9999", "quote": "return amount"},
            {"type": "code", "ref": "shoplib/pricing.py:1", "quote": "import decimal"},
        ],
        "suggestion": {"summary": "Use Decimal.", "rationale": "", "patch_sketch": "",
                       "confidence": 0.9, "citations": [
                           {"type": "diff", "ref": "shoplib/money.py"}]},
    } for oid, _, _ in _OBSERVATION.findall(prompt)]
    return message(json.dumps({"adjudications": items}), model=body["model"])
