"""The one way the service talks to a model.

    redact → cache lookup → reserve budget → provider → price real usage → cache

A repeated request is answered from the disk cache and recorded at $0. A new one
is reserved against every cap first, so a call that could breach one is never
sent. Personal data is redacted before the request is hashed, cached or sent.

`ModelClient.available` is false when no provider is configured — no AWS
credentials, or `[models] enabled = "off"` — and every caller treats that as a
stage that did not run, named in the coverage statement, never as an error.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

import structlog

from prflagger.core.config import Config
from prflagger.llm.cache import _key, _read, _write
from prflagger.llm.ledger import Ledger
from prflagger.llm.provider import (
    Completion,
    ModelProvider,
    Request,
    bedrock_mantle,
    credential_source,
)
from prflagger.privacy import redact
from prflagger.storage.db import Database

__all__ = ["Answer", "ModelClient", "build_client"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Answer:
    text: str
    model: str
    usd: float
    cached: bool
    completion: Completion


class ModelClient:
    def __init__(
        self,
        ledger: Ledger,
        provider: ModelProvider | None,
        *,
        unavailable_reason: str = "",
        credentials: str = "",
    ) -> None:
        self.ledger = ledger
        self._provider = provider
        self.unavailable_reason = unavailable_reason
        self.credentials = credentials

    @property
    def available(self) -> bool:
        return self._provider is not None

    def ask(
        self,
        request: Request,
        *,
        stage: str,
        run_id: str | None = None,
        repo: str | None = None,
        use_cache: bool = True,
    ) -> Answer:
        """Send `request`, or answer it from the cache. Raises `BudgetExceeded`."""
        request = replace(
            request,
            prompt=redact(request.prompt),
            system=tuple((redact(text), cache) for text, cache in request.system),
        )
        key = _key({
            "op": "ask", "model": request.model, "max_tokens": request.max_tokens,
            "system": [list(block) for block in request.system], "prompt": request.prompt,
        })
        if use_cache and (hit := _read(key)) is not None:
            completion = Completion(
                text=str(hit["text"]), model=str(hit.get("model", request.model)),
                input_tokens=int(hit.get("input_tokens", 0)),
                output_tokens=int(hit.get("output_tokens", 0)),
            )
            self.ledger.record(completion, model=request.model, stage=stage, run_id=run_id,
                               repo=repo, cached=True)
            return Answer(completion.text, request.model, 0.0, True, completion)

        if self._provider is None:
            raise RuntimeError(f"no model provider: {self.unavailable_reason}")
        reservation = self.ledger.reserve(request, run_id=run_id, repo=repo)
        started = time.monotonic()
        try:
            completion = self._provider(request)
        except BaseException as error:
            self.ledger.release(reservation)
            self._trip(error, request.model)
            raise
        usd = self.ledger.record(completion, model=request.model, stage=stage, run_id=run_id,
                                 repo=repo, cached=False, reservation=reservation)
        _write(key, {
            "op": "ask", "model": request.model, "text": completion.text,
            "input_tokens": completion.input_tokens, "output_tokens": completion.output_tokens,
        })
        log.info(
            "llm.call", stage=stage, model=request.model, run=run_id, repo=repo,
            input=completion.input_tokens, output=completion.output_tokens,
            cache_read=completion.cache_read_tokens, cache_write=completion.cache_write_tokens,
            usd=usd, seconds=round(time.monotonic() - started, 2),
        )
        return Answer(completion.text, request.model, usd, False, completion)


    def _trip(self, error: BaseException, model: str) -> None:
        """Stop calling a provider that will refuse every call.

        Refused credentials or an unknown model id fail identically on every
        request, so after the first one the client reports itself unavailable,
        with the provider's own reason, until the service is restarted with a
        fix. A rate limit or an overloaded endpoint is transient and does not trip.
        """
        status = getattr(error, "status_code", None)
        if status in (401, 403):
            reason = f"Bedrock refused the credentials ({self.credentials}): {_message(error)}"
        elif status == 404:
            reason = f"Bedrock does not know the model {model!r}: {_message(error)}"
        else:
            return
        self._provider = None
        self.unavailable_reason = reason + " — fix it, then run `prflagger llm check`"
        log.error("llm.disabled", reason=self.unavailable_reason)


def _message(error: BaseException) -> str:
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict) and inner.get("message"):
            return str(inner["message"])[:200]
    return str(error)[:200]


def build_client(
    config: Config, db: Database, *, provider: ModelProvider | None = None
) -> ModelClient:
    """The client the service runs with, decided once at start-up.

    `provider` is for tests at the network boundary; without it, a Bedrock
    provider is built only when credentials are present and models are enabled.
    """
    ledger = Ledger(db, config.budget, config.models)
    mode = config.models.enabled
    if provider is not None:
        return ModelClient(ledger, provider, credentials="injected")
    if mode == "off":
        return ModelClient(
            ledger, None, unavailable_reason="models are disabled in config.toml"
        )
    source = credential_source()
    if source is None:
        reason = ("no AWS credentials are configured — set AWS_BEARER_TOKEN_BEDROCK or an "
                  "access key, then run `prflagger llm check`")
        if mode == "on":
            log.error("llm.unavailable", reason=reason)
        return ModelClient(ledger, None, unavailable_reason=reason)
    try:
        built = bedrock_mantle(config.models.region)
    except Exception as error:  # noqa: BLE001 - a client that cannot be built is unavailable
        return ModelClient(ledger, None, unavailable_reason=f"Bedrock client: {error}")
    return ModelClient(ledger, built, credentials=source)
