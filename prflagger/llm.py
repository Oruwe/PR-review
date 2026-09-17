"""The one door to any LLM.

Every LLM call in this system goes through `complete` or `embed`. Both are
content-addressed and cached to disk, so a repeated prompt costs nothing and a
run is reproducible. Nothing else may call bedrock directly.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = [
    "EMBED_MODEL",
    "EmbeddingUnavailable",
    "Provider",
    "cache_dir",
    "complete",
    "embed",
    "embeddings_available",
    "usage_path",
]


class EmbeddingUnavailable(RuntimeError):
    """The local embedding model could not be loaded.

    Raised rather than silently substituting a different vectoriser: a norm match
    computed by some other algorithm is not the match this system claims to make.
    """ 

Provider = Callable[[str, int, float], str]  # prompt, max_tokens, temperature -> text

EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

_CACHE_ROOT_ENV = "PRFLAGGER_CACHE_DIR"


def cache_dir() -> Path:
    """`.cache/llm`, or `<PRFLAGGER_CACHE_DIR>/llm` when that is set. Always absolute."""
    root = Path(os.environ.get(_CACHE_ROOT_ENV, ".cache")).resolve()
    return root / "llm"


def usage_path() -> Path:
    """The jsonl ledger of everything that actually crossed the network."""
    return cache_dir() / "usage.jsonl"


def complete(
    prompt: str,
    *,
    model: str,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    provider: Provider | None = None,
) -> str:
    """Content-addressed cache at .cache/llm/<sha256>.json.

    On a cache hit the provider is never called. Records token counts to
    .cache/llm/usage.jsonl. `provider` defaults to `providers.bedrock_provider()`,
    bound to `model`.
    """
    key = _key(
        {
            "op": "complete",
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "prompt": prompt,
        }
    )
    cached = _read(key)
    if cached is not None:
        return str(cached["text"])

    if provider is None:
        from prflagger.providers import bedrock_provider

        provider = bedrock_provider(model)

    text = provider(prompt, max_tokens, temperature)
    _write(
        key,
        {
            "op": "complete",
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "prompt": prompt,
            "text": text,
        },
    )
    _record_usage(
        {
            "op": "complete",
            "key": key,
            "model": model,
            "prompt_chars": len(prompt),
            "completion_chars": len(text),
            "prompt_tokens": _estimate_tokens(prompt),
            "completion_tokens": _estimate_tokens(text),
            # The Provider boundary hands back text only, so exact usage counts
            # are not observable here.
            "token_source": "estimate",
        }
    )
    return text


def embed(texts: list[str]) -> list[list[float]]:
    """sentence-transformers all-MiniLM-L6-v2, local. Cached the same way."""
    vectors: list[list[float]] = [[] for _ in texts]
    pending: list[tuple[int, str, str]] = []  # index, text, cache key

    for index, text in enumerate(texts):
        key = _key({"op": "embed", "model": EMBED_MODEL, "text": text})
        cached = _read(key)
        if cached is None:
            pending.append((index, text, key))
        else:
            vectors[index] = [float(value) for value in cached["vector"]]

    if pending:
        model = _embed_model()
        encoded = model.encode([text for _, text, _ in pending])
        for (index, text, key), vector in zip(pending, encoded, strict=True):
            values = [float(value) for value in vector]
            vectors[index] = values
            _write(key, {"op": "embed", "model": EMBED_MODEL, "text": text, "vector": values})
        _record_usage(
            {
                "op": "embed",
                "model": EMBED_MODEL,
                "texts": len(pending),
                "prompt_tokens": sum(_estimate_tokens(text) for _, text, _ in pending),
                "completion_tokens": 0,
                "token_source": "estimate",
            }
        )

    return vectors


_EMBED_MODEL_CACHE: Any = None


def _embed_model() -> Any:
    """Load the local embedding model once per process."""
    global _EMBED_MODEL_CACHE
    if _EMBED_MODEL_CACHE is None:
        try:
            from sentence_transformers import SentenceTransformer

            _EMBED_MODEL_CACHE = SentenceTransformer(EMBED_MODEL)
        except Exception as error:  # noqa: BLE001 - any load failure means unavailable
            raise EmbeddingUnavailable(
                f"could not load {EMBED_MODEL}: {error}"
            ) from error
    return _EMBED_MODEL_CACHE


def embeddings_available() -> bool:
    """Whether `embed` can run. Callers use this to degrade explicitly."""
    try:
        _embed_model()
    except EmbeddingUnavailable:
        return False
    return True


def _key(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read(key: str) -> dict[str, Any] | None:
    path = cache_dir() / f"{key}.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        entry = json.loads(raw)
    except json.JSONDecodeError:
        return None  # a truncated entry is a miss, not a crash
    return entry if isinstance(entry, dict) else None


def _write(key: str, entry: dict[str, Any]) -> None:
    directory = cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{key}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps({"key": key, "created_at": time.time(), **entry}, ensure_ascii=True),
        encoding="utf-8",
    )
    temporary.replace(path)  # atomic: a reader never sees a half-written entry


def _record_usage(record: dict[str, Any]) -> None:
    path = usage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": time.time(), **record}, ensure_ascii=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _estimate_tokens(text: str) -> int:
    """Rough token count. The provider boundary returns text, not usage."""
    return max(1, len(text) // 4) if text else 0
