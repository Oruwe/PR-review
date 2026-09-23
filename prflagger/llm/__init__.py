"""The one door to any LLM.

`complete` and `embed` keep the names and signatures they had in v1 —
`prflagger.llm.complete(...)` still works — but the layer behind them is now
split: `cache.py` holds the content-addressed disk cache, `provider.py` talks to
Bedrock, and `ledger.py` records what every call actually cost.

CLAUDE.md's rule stands: every LLM call in this system goes through here, and
nothing else may call a model API directly.
"""

from __future__ import annotations

from prflagger.llm.cache import (
    EMBED_MODEL,
    EmbeddingUnavailable,
    Provider,
    cache_dir,
    complete,
    embed,
    embeddings_available,
    usage_path,
)

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
