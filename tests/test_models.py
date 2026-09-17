"""C0 acceptance.

The three criteria from SPEC.md § C0:
  1. `Job.idempotency_key` is stable across two constructions with equal fields and
     differs when any field differs.
  2. `Finding` with `norm=None` and `kind="coverage_gap"` raises `ValueError`.
  3. Calling `complete()` twice with the same prompt performs exactly one network
     call — asserted with a counter on the boto3 client, not on `complete` itself.

Criterion 3 stubs the *provider boundary* only. Docker, git and the target repo are
never mocked anywhere in this suite.
"""

from __future__ import annotations

import dataclasses
import io
import json
from pathlib import Path
from typing import Any

import boto3
import pytest

from prflagger import llm
from prflagger.models import Finding, Job, Norm, Outcome, Symbol

# --------------------------------------------------------------------------------------
# Job.idempotency_key
# --------------------------------------------------------------------------------------

JOB_FIELDS: dict[str, Any] = {
    "repo_path": "/srv/repos/requests",
    "commit": "9a1b2c3d4e5f60718293a4b5c6d7e8f901234567",
    "image_key": "py311-requests",
    "command": ("python", "-m", "pytest", "-q"),
    "timeout_s": 120,
    "memory_mb": 512,
}

ALTERED_FIELDS: dict[str, Any] = {
    "repo_path": "/srv/repos/other",
    "commit": "0000000000000000000000000000000000000000",
    "image_key": "py312-requests",
    "command": ("python", "-m", "pytest", "-x"),
    "timeout_s": 121,
    "memory_mb": 513,
}


def test_idempotency_key_is_stable_across_equal_constructions() -> None:
    first = Job(**JOB_FIELDS)
    second = Job(**JOB_FIELDS)

    assert first.idempotency_key == second.idempotency_key
    # Stable across runs, not merely within one object: it is a hash of the fields.
    assert first.idempotency_key == Job(**JOB_FIELDS).idempotency_key
    assert len(first.idempotency_key) == 64
    assert first.idempotency_key != Job(**JOB_FIELDS).repo_path


@pytest.mark.parametrize("field", sorted(JOB_FIELDS))
def test_idempotency_key_differs_when_any_field_differs(field: str) -> None:
    baseline = Job(**JOB_FIELDS)
    altered = Job(**{**JOB_FIELDS, field: ALTERED_FIELDS[field]})

    assert altered.idempotency_key != baseline.idempotency_key, (
        f"changing {field} did not change the idempotency key"
    )


def test_idempotency_key_covers_every_field_of_job() -> None:
    # If a field is ever added to Job, the parametrised test above must cover it.
    declared = {f.name for f in dataclasses.fields(Job)}
    assert declared == set(JOB_FIELDS)


# --------------------------------------------------------------------------------------
# The four-field contract
# --------------------------------------------------------------------------------------

NORM = Norm(
    id="tests-accompany-behaviour-changes",
    statement="Add a regression test with any behavioural change.",
    scope="repo",
    support=7,
    distinct_reviewers=3,
    confidence=0.82,
    evidence_prs=(412, 457, 490),
)


def test_finding_without_norm_raises_for_coverage_gap() -> None:
    with pytest.raises(ValueError):
        Finding(
            kind="coverage_gap",
            symbol="requests.sessions.Session.send",
            what_changed="send() gained an early return that no test exercises",
            how_we_know="tests/test_sessions.py collected 0 tests covering lines 701-709",
            norm=None,
            confidence=0.7,
            severity=0.5,
        )


@pytest.mark.parametrize(
    "kind", ["coverage_gap", "lint_regression", "api_change", "timeout", "oom"]
)
def test_every_non_behaviour_kind_requires_a_norm(kind: str) -> None:
    with pytest.raises(ValueError):
        Finding(
            kind=kind,
            symbol="requests.sessions.Session.send",
            what_changed="something observable changed",
            how_we_know="tests/test_sessions.py::test_send",
            norm=None,
            confidence=0.7,
            severity=0.5,
        )

    # The same finding is constructible once it cites a norm.
    cited = Finding(
        kind=kind,
        symbol="requests.sessions.Session.send",
        what_changed="something observable changed",
        how_we_know="tests/test_sessions.py::test_send",
        norm=NORM,
        confidence=0.7,
        severity=0.5,
    )
    assert cited.norm is NORM


def test_behaviour_change_may_omit_the_norm() -> None:
    finding = Finding(
        kind="behavior_change",
        symbol="requests.models.Response.json",
        what_changed="json() now raises ValueError instead of returning None on empty body",
        how_we_know="tests/generated/test_response_json.py::test_empty_body",
        norm=None,
        confidence=0.9,
        severity=0.6,
    )
    assert finding.norm is None


def test_finding_carries_all_four_contract_fields() -> None:
    finding = Finding(
        kind="lint_regression",
        symbol="requests.adapters.HTTPAdapter.send",
        what_changed="ruff F841 introduced on line 512",
        how_we_know="ruff check . -> adapters.py:512:9 F841",
        norm=NORM,
        confidence=0.95,
        severity=0.3,
    )
    assert finding.what_changed
    assert finding.how_we_know
    assert finding.norm is not None
    assert 0.0 <= finding.confidence <= 1.0


def test_domain_types_are_frozen() -> None:
    symbol = Symbol(
        fqn="requests.models.Response.json",
        kind="method",
        file="src/requests/models.py",
        line_start=897,
        line_end=921,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        symbol.line_start = 1  # type: ignore[misc]


def test_outcome_values_are_the_spec_values() -> None:
    assert Outcome.TIMEOUT.value == "timeout"
    assert Outcome.OOM.value == "oom"
    assert {o.value for o in Outcome} == {
        "passed",
        "failed",
        "install_failed",
        "timeout",
        "collection_error",
        "oom",
    }


# --------------------------------------------------------------------------------------
# llm.complete caching — the counter sits on the boto3 client
# --------------------------------------------------------------------------------------


class _CountingBedrockClient:
    """Stands in for the bedrock-runtime client and counts invocations."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def invoke_model(self, *, modelId: str, body: str) -> dict[str, Any]:  # noqa: N803
        self.calls.append({"modelId": modelId, "body": json.loads(body)})
        payload = {
            "content": [{"type": "text", "text": "cached-answer"}],
            "usage": {"input_tokens": 11, "output_tokens": 3},
        }
        return {"body": io.BytesIO(json.dumps(payload).encode("utf-8"))}


@pytest.fixture
def bedrock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _CountingBedrockClient:
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path))
    client = _CountingBedrockClient()

    def _client(service_name: str, **kwargs: Any) -> _CountingBedrockClient:
        assert service_name == "bedrock-runtime"
        return client

    monkeypatch.setattr(boto3, "client", _client)
    return client


def test_complete_twice_performs_exactly_one_network_call(
    bedrock: _CountingBedrockClient,
) -> None:
    first = llm.complete("Summarise this diff hunk.", model="anthropic.claude-opus-5")
    second = llm.complete("Summarise this diff hunk.", model="anthropic.claude-opus-5")

    assert first == second == "cached-answer"
    assert len(bedrock.calls) == 1, f"expected one bedrock call, got {len(bedrock.calls)}"


def test_cache_hit_is_content_addressed(bedrock: _CountingBedrockClient) -> None:
    llm.complete("prompt A", model="anthropic.claude-opus-5")
    llm.complete("prompt B", model="anthropic.claude-opus-5")
    llm.complete("prompt A", model="anthropic.claude-opus-5")
    assert len(bedrock.calls) == 2

    # Any parameter that changes the request changes the cache key.
    llm.complete("prompt A", model="anthropic.claude-sonnet-5")
    llm.complete("prompt A", model="anthropic.claude-opus-5", max_tokens=100)
    llm.complete("prompt A", model="anthropic.claude-opus-5", temperature=0.7)
    assert len(bedrock.calls) == 5


def test_cache_entries_and_usage_land_under_the_cache_dir(
    bedrock: _CountingBedrockClient, tmp_path: Path
) -> None:
    llm.complete("ledger me", model="anthropic.claude-opus-5")
    llm.complete("ledger me", model="anthropic.claude-opus-5")

    entries = list((tmp_path / "llm").glob("*.json"))
    assert len(entries) == 1
    assert len(entries[0].stem) == 64  # sha256 of the request

    records = [
        json.loads(line)
        for line in llm.usage_path().read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert len(records) == 1, "the cache hit must not be billed"
    assert records[0]["model"] == "anthropic.claude-opus-5"
    assert records[0]["prompt_tokens"] > 0


def test_injected_provider_is_used_and_also_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path))
    calls: list[tuple[str, int, float]] = []

    def provider(prompt: str, max_tokens: int, temperature: float) -> str:
        calls.append((prompt, max_tokens, temperature))
        return "from-injected-provider"

    kwargs: dict[str, Any] = {"model": "anthropic.claude-opus-5", "provider": provider}
    assert llm.complete("hello", **kwargs) == "from-injected-provider"
    assert llm.complete("hello", **kwargs) == "from-injected-provider"
    assert calls == [("hello", 4096, 0.0)]


def test_bedrock_request_body_is_an_anthropic_messages_request(
    bedrock: _CountingBedrockClient,
) -> None:
    llm.complete("what changed?", model="anthropic.claude-opus-5", max_tokens=256)

    call = bedrock.calls[0]
    assert call["modelId"] == "anthropic.claude-opus-5"
    assert call["body"]["anthropic_version"] == "bedrock-2023-05-31"
    assert call["body"]["max_tokens"] == 256
    assert call["body"]["messages"][0]["role"] == "user"
    assert call["body"]["messages"][0]["content"][0]["text"] == "what changed?"
