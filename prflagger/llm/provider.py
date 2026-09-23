"""The network boundary to a model: one request in, one completion out.

v1's provider (`prflagger.providers`) called boto3's `invoke_model` and handed
back text only, so every token count downstream was an estimate — flying blind
against a hundred-dollar budget. This one uses the Anthropic SDK's Bedrock
client, which returns the provider's own `usage`: input, output, and the prompt
cache's reads and writes. The ledger prices exactly those numbers.

Credentials are never read from this repository. The SDK resolves them the AWS
way — a Bedrock API key in `AWS_BEARER_TOKEN_BEDROCK`, an access key pair, a
named profile, or the instance's role — and the region from `[models] region`,
`AWS_REGION` or `AWS_DEFAULT_REGION`.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "Completion",
    "ModelProvider",
    "Request",
    "bedrock_mantle",
    "credential_source",
]


@dataclass(frozen=True)
class Request:
    """One model call.

    `system` is a sequence of (text, cache) blocks. A block marked for caching
    ends a prefix the provider may reuse: the repository's charter and norms go
    there, identical for every pull request in that repository, and only the
    per-observation packet after it is paid for at the full input price.
    """

    model: str
    prompt: str
    system: tuple[tuple[str, bool], ...] = ()
    max_tokens: int = 1024
    cache_ttl: str = "1h"


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    stop_reason: str = ""


ModelProvider = Callable[[Request], Completion]


def bedrock_mantle(
    region: str | None = None,
    *,
    base_url: str | None = None,
    skip_auth: bool = False,
    timeout_s: float = 120.0,
) -> ModelProvider:
    """A provider backed by `anthropic.AnthropicBedrockMantle`.

    `base_url` and `skip_auth` exist for one reason: so the real SDK can be
    exercised end to end against a local server in tests, without AWS.
    """
    from anthropic import AnthropicBedrockMantle

    resolved = region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    client = AnthropicBedrockMantle(
        aws_region=resolved,
        base_url=base_url,
        skip_auth=skip_auth,
        timeout=timeout_s,
        max_retries=2,
    )

    def call(request: Request) -> Completion:
        system: list[dict[str, Any]] = []
        for text, cache in request.system:
            block: dict[str, Any] = {"type": "text", "text": text}
            if cache:
                block["cache_control"] = {"type": "ephemeral", "ttl": request.cache_ttl}
            system.append(block)
        message = client.messages.create(
            model=request.model,
            max_tokens=request.max_tokens,
            system=system,  # type: ignore[arg-type]
            messages=[{"role": "user", "content": request.prompt}],
        )
        usage = message.usage
        return Completion(
            text="".join(
                getattr(block, "text", "") for block in message.content
                if getattr(block, "type", "") == "text"
            ),
            model=message.model or request.model,
            input_tokens=int(usage.input_tokens or 0),
            output_tokens=int(usage.output_tokens or 0),
            cache_read_tokens=int(usage.cache_read_input_tokens or 0),
            cache_write_tokens=int(usage.cache_creation_input_tokens or 0),
            stop_reason=str(message.stop_reason or ""),
        )

    return call


def credential_source() -> str | None:
    """Which AWS credential the SDK would use, described — or None if there is none.

    Only the environment and the standard AWS credential chain are consulted;
    nothing in the repository or the service's own files is read.
    """
    for variable in ("AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_AWS_API_KEY"):
        if os.environ.get(variable, "").strip():
            return f"Bedrock API key ({variable})"
    if os.environ.get("AWS_ACCESS_KEY_ID", "").strip():
        return "access key (AWS_ACCESS_KEY_ID)"
    if os.environ.get("AWS_PROFILE", "").strip():
        return f"profile {os.environ['AWS_PROFILE']!r}"
    try:
        import boto3

        credentials = boto3.Session().get_credentials()
    except Exception:  # noqa: BLE001 - no usable chain means no credentials
        return None
    if credentials is None:
        return None
    method = getattr(credentials, "method", "") or "chain"
    return f"AWS credential chain ({method})"
