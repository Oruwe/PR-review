"""Concrete LLM providers.

A provider is the network boundary: ``(prompt, max_tokens, temperature) -> text``.
Keeping it injectable means `llm.complete` can be built and tested before AWS
credentials exist, and swapping models later is one line.

Nothing outside this module talks to bedrock.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from prflagger.llm import Provider

__all__ = ["DEFAULT_MODEL", "bedrock_provider"]

# Bedrock model ids carry an "anthropic." prefix. Override per call via
# `llm.complete(model=...)`, or environment-wide via PRFLAGGER_BEDROCK_MODEL.
#
# Credentials come from boto3's normal chain: an access key pair, or a Bedrock API key
# in AWS_BEARER_TOKEN_BEDROCK. Nothing is read from this repository.
DEFAULT_MODEL = "anthropic.claude-opus-5"

_ANTHROPIC_BEDROCK_VERSION = "bedrock-2023-05-31"


def bedrock_provider(model: str | None = None, *, region: str | None = None) -> Provider:
    """Return a `Provider` bound to one bedrock model.

    The returned callable performs a single `invoke_model` request per call. It is
    never called on a cache hit — `llm.complete` owns the cache.
    """
    model_id = model or os.environ.get("PRFLAGGER_BEDROCK_MODEL") or DEFAULT_MODEL
    # Leave the region to boto3's own resolution chain when nothing is given, so
    # ~/.aws/config is honoured rather than silently overridden.
    aws_region = region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")

    def _invoke(prompt: str, max_tokens: int, temperature: float) -> str:
        import boto3  # imported lazily so importing this module needs no AWS deps

        client = boto3.client("bedrock-runtime", region_name=aws_region)
        body: dict[str, Any] = {
            "anthropic_version": _ANTHROPIC_BEDROCK_VERSION,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        }
        # Current Anthropic models reject sampling parameters outright, so only send
        # `temperature` when the caller actually asked for something other than the
        # deterministic default.
        if temperature != 0.0:
            body["temperature"] = temperature

        response = client.invoke_model(modelId=model_id, body=json.dumps(body))
        payload = json.loads(_read_body(response["body"]))
        return "".join(
            block.get("text", "")
            for block in payload.get("content", [])
            if block.get("type") == "text"
        )

    return _invoke


def _read_body(body: Any) -> str:
    """Bedrock returns a streaming body; tolerate an already-materialised one."""
    raw = body.read() if hasattr(body, "read") else body
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return str(raw)
