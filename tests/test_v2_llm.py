"""The model layer: real SDK, real HTTP, real ledger — pointed at a local server.

What matters here is money and honesty: a call that could breach a cap is never
sent, the recorded cost is the provider's own usage priced exactly, a cache hit
costs nothing and says so, and a provider that refuses the credentials is
stopped after one refusal rather than retried on every run.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from prflagger.core.config import BudgetConfig, Config, ModelConfig
from prflagger.llm.client import ModelClient, build_client
from prflagger.llm.ledger import BudgetExceeded, Ledger, UnpricedModel
from prflagger.llm.provider import Completion, Request, bedrock_mantle
from prflagger.storage.db import Database
from tests.model_fixture import RecordedModel, message, refusal, serve

HAIKU = "anthropic.claude-haiku-4-5"


@pytest.fixture(autouse=True)
def _cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path / "cache"))


def _client(tmp_path: Path, url: str, budget: BudgetConfig | None = None) -> ModelClient:
    config = Config(budget=budget or BudgetConfig(), models=ModelConfig())
    provider = bedrock_mantle("us-east-1", base_url=url, skip_auth=True)
    return build_client(config, Database(tmp_path / "llm.db"), provider=provider)


# ----------------------------------------------------------------------------------
# The provider speaks the real Messages API
# ----------------------------------------------------------------------------------


def test_the_stable_prefix_is_marked_for_caching_and_usage_is_read_back(
    tmp_path: Path,
) -> None:
    model = RecordedModel(lambda body: message(
        "ready", input_tokens=40, output_tokens=2, cache_read=900, cache_write=0
    ))
    with serve(model):
        provider = bedrock_mantle("us-east-1", base_url=model.base_url, skip_auth=True)
        completion = provider(Request(
            model=HAIKU, prompt="per-observation packet",
            system=(("repository charter and norms", True), ("volatile note", False)),
            max_tokens=64, cache_ttl="1h",
        ))
    body = model.requests[0]
    assert body["model"] == HAIKU and body["max_tokens"] == 64
    cached, volatile = body["system"]
    assert cached["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in volatile, "only the stable prefix is cached"
    assert completion == Completion(
        text="ready", model=HAIKU, input_tokens=40, output_tokens=2,
        cache_read_tokens=900, cache_write_tokens=0, stop_reason="end_turn",
    )


# ----------------------------------------------------------------------------------
# Money
# ----------------------------------------------------------------------------------


def test_cost_is_the_provider_usage_priced_exactly(tmp_path: Path) -> None:
    ledger = Ledger(Database(tmp_path / "l.db"), BudgetConfig(), ModelConfig())
    usd = ledger.cost(Completion(
        text="", model=HAIKU, input_tokens=1_000_000, output_tokens=200_000,
        cache_read_tokens=2_000_000, cache_write_tokens=100_000,
    ))
    # $1/M in, $5/M out, reads at a tenth of input, 1-hour writes at twice input.
    assert usd == pytest.approx(1.0 + 1.0 + 0.2 + 0.2)


def test_a_call_that_could_breach_a_cap_is_never_sent(tmp_path: Path) -> None:
    model = RecordedModel(lambda body: message("x"))
    with serve(model):
        client = _client(tmp_path, model.base_url, BudgetConfig(per_run_usd=0.0001))
        with pytest.raises(BudgetExceeded, match="run r1"):
            client.ask(Request(model=HAIKU, prompt="hello", max_tokens=200),
                       stage="adjudication", run_id="r1", repo="a/b")
    assert model.requests == [], "a refused call must not reach the provider"
    assert client.ledger.spent() == 0.0


def test_each_cap_is_enforced_where_it_applies(tmp_path: Path) -> None:
    # Each call reserves ~$0.00063 worst case and actually costs $0.00051.
    model = RecordedModel(lambda body: message("ok", input_tokens=10, output_tokens=100))
    request = Request(model=HAIKU, prompt="short", max_tokens=100)
    with serve(model):
        daily = _client(tmp_path, model.base_url,
                        BudgetConfig(per_run_usd=5, per_repo_daily_usd=0.001))
        daily.ask(request, stage="s", run_id="r1", repo="a/b", use_cache=False)
        with pytest.raises(BudgetExceeded, match="daily a/b"):
            daily.ask(request, stage="s", run_id="r2", repo="a/b", use_cache=False)
        # Another repository's day is its own.
        daily.ask(request, stage="s", run_id="r3", repo="c/d", use_cache=False)


def test_concurrent_reservations_cannot_overspend(tmp_path: Path) -> None:
    ledger = Ledger(Database(tmp_path / "c.db"), BudgetConfig(total_usd=0.01), ModelConfig())
    request = Request(model=HAIKU, prompt="x" * 3000, max_tokens=1000)  # ~$0.0072 worst case
    granted: list[object] = []
    refused: list[BudgetExceeded] = []
    barrier = threading.Barrier(8)

    def attempt() -> None:
        barrier.wait()
        try:
            granted.append(ledger.reserve(request, run_id=None, repo=None))
        except BudgetExceeded as error:
            refused.append(error)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(granted) == 1 and len(refused) == 7


def test_a_model_without_a_price_is_refused(tmp_path: Path) -> None:
    model = RecordedModel(lambda body: message("x"))
    with serve(model):
        client = _client(tmp_path, model.base_url)
        with pytest.raises(UnpricedModel, match="models.prices"):
            client.ask(Request(model="anthropic.some-future-model", prompt="hi"), stage="s")
    assert model.requests == []


def test_a_repeated_request_is_free_and_recorded_as_such(tmp_path: Path) -> None:
    model = RecordedModel(lambda body: message("same answer", input_tokens=500))
    request = Request(model=HAIKU, prompt="identical", max_tokens=50)
    with serve(model):
        client = _client(tmp_path, model.base_url)
        first = client.ask(request, stage="s", repo="a/b")
        second = client.ask(request, stage="s", repo="a/b")
    assert len(model.requests) == 1
    assert (first.cached, second.cached) == (False, True)
    assert second.text == first.text and second.usd == 0.0
    summary = client.ledger.summary()
    assert summary["calls"] == 2 and summary["cached"] == 1


def test_personal_data_never_leaves_the_machine(tmp_path: Path) -> None:
    model = RecordedModel(lambda body: message("ok"))
    with serve(model):
        client = _client(tmp_path, model.base_url)
        client.ask(Request(model=HAIKU, prompt="author is jane.doe@example.com",
                           system=(("contact ops@example.org", True),)), stage="s")
    sent = str(model.requests[0])
    assert "jane.doe@example.com" not in sent and "ops@example.org" not in sent


# ----------------------------------------------------------------------------------
# Failing well
# ----------------------------------------------------------------------------------


def test_refused_credentials_stop_the_client_after_one_call(tmp_path: Path) -> None:
    model = RecordedModel(lambda body: refusal(
        401, "The security token included in the request is invalid."
    ))
    with serve(model):
        client = _client(tmp_path, model.base_url)
        with pytest.raises(Exception, match="401"):
            client.ask(Request(model=HAIKU, prompt="a"), stage="s")
        assert not client.available
        assert "security token" in client.unavailable_reason
        assert "llm check" in client.unavailable_reason
        with pytest.raises(RuntimeError, match="no model provider"):
            client.ask(Request(model=HAIKU, prompt="b"), stage="s")
    assert len(model.requests) == 1, "the second call must not be sent"
    assert client.ledger.spent() == 0.0


def test_without_credentials_the_client_says_what_to_set(tmp_path: Path) -> None:
    client = build_client(Config(), Database(tmp_path / "n.db"))
    assert not client.available
    assert "AWS_BEARER_TOKEN_BEDROCK" in client.unavailable_reason
    off = build_client(Config(models=ModelConfig(enabled="off")), Database(tmp_path / "o.db"))
    assert "disabled" in off.unavailable_reason


def test_llm_check_proves_the_whole_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
                                         ) -> None:
    """The real command, the real credential lookup, the real SDK — a local endpoint."""
    seen: list[dict[str, Any]] = []

    def respond(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        seen.append(body)
        return message("ready", input_tokens=14, output_tokens=2)

    model = RecordedModel(respond)
    with serve(model):
        env = {
            **os.environ,
            "AWS_BEARER_TOKEN_BEDROCK": "test-key-not-a-secret",
            "ANTHROPIC_BEDROCK_MANTLE_BASE_URL": model.base_url,
            "PRFLAGGER_CACHE_DIR": str(tmp_path / "cache"),
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
        }
        completed = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "prflagger.cli", "llm", "check"],
            capture_output=True, text=True, env=env, cwd=tmp_path, timeout=60, check=False,
        )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Bedrock API key (AWS_BEARER_TOKEN_BEDROCK)" in completed.stdout
    assert "'ready'" in completed.stdout and "recorded in the ledger" in completed.stdout
    assert seen and seen[0]["max_tokens"] == 8
    assert model.headers[0].get("x-api-key") == "test-key-not-a-secret" or (
        "test-key-not-a-secret" in model.headers[0].get("authorization", "")
    ), "the API key from the environment is what authenticates the call"
