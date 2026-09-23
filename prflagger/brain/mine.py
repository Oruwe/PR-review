"""Norms mined from enforced review comments, for the always-on service.

The v1 path (`norms.cluster_norms`) needs a local embedding model and a language
model, or it produces nothing. A service that watches arbitrary repositories
cannot assume either, so each of the two steps has an explicit fallback — and
every norm records which one produced it, so the interface can say so:

* **Grouping.** `semantic` uses sentence embeddings when they are installed.
  Otherwise `lexical` groups comments by the words they share (TF-IDF cosine).
  Lexical grouping finds "add a test" said three ways only when the three share
  words; it is weaker, it is labelled, and it never pretends to be the other.
* **Naming.** With a model configured, one call per cluster writes the standard
  as an imperative sentence. Without one, the norm is stated as the cluster's
  most representative comment, quoted verbatim — which cannot misstate what the
  reviewers said, because it is what one of them said.

Either way the norm carries its evidence: every supporting comment's pull
request, link and file. No step here invents a norm the history does not show.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import structlog

from prflagger.core.models import Norm

__all__ = [
    "Vectoriser",
    "agglomerate",
    "clean_comment",
    "lexical_vectoriser",
    "mine_norms",
    "semantic_vectoriser",
]

log = structlog.get_logger(__name__)

Namer = Callable[[Sequence[str]], str]


@dataclass(frozen=True)
class Vectoriser:
    name: str  # "semantic" | "lexical"
    vectorise: Callable[[list[str]], np.ndarray]
    threshold: float  # average-linkage cosine similarity at which clusters merge


# ----------------------------------------------------------------------------------
# What counts as a comment worth grouping
# ----------------------------------------------------------------------------------

_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`]*`")
_URL = re.compile(r"https?://\S+")
_MENTION = re.compile(r"@[\w-]+")
_ACKNOWLEDGEMENT = re.compile(
    r"^(lgtm|looks good|thanks?|thank you|done|fixed|ok(ay)?|nice|\+1|good catch|agreed|"
    r"sounds good|will do|ack|yes|no)\W*$",
    re.IGNORECASE,
)


def clean_comment(body: str) -> str:
    """The prose of a review comment: no quoted replies, code, links or mentions.

    Returns "" for a comment with nothing to learn from — an acknowledgement, or
    fewer than four words once code and quotes are removed.
    """
    lines = [line for line in body.splitlines() if not line.lstrip().startswith(">")]
    text = _CODE_BLOCK.sub(" ", "\n".join(lines))
    text = _URL.sub(" ", _MENTION.sub(" ", text))
    text = " ".join(text.split())
    if _ACKNOWLEDGEMENT.match(text) or len(_INLINE_CODE.sub(" ", text).split()) < 4:
        return ""
    return text


# ----------------------------------------------------------------------------------
# Grouping
# ----------------------------------------------------------------------------------

_WORD = re.compile(r"[a-z][a-z0-9_]+")
_STOPWORDS = (
    "a an the this that these those is are was were be been being it its of to in on "
    "for with as at by from or and but if then than so we you i me my our your they them "
    "can could would should will just also here there please maybe might do does did "
    "not no yes have has had what which who when where why how all any some more most "
    "very too into out up about over only same new"
)
_STOP = frozenset(_STOPWORDS.split(" "))


def _terms(text: str) -> list[str]:
    words = [w for w in _WORD.findall(_INLINE_CODE.sub(" ", text).lower()) if w not in _STOP]
    # Light stemming, so "tests", "tested" and "testing" count as one term.
    stems = [re.sub(r"(ing|ed|es|s)$", "", w) if len(w) > 4 else w for w in words]
    return stems + [f"{a} {b}" for a, b in zip(stems, stems[1:], strict=False)]


def lexical_vectoriser() -> Vectoriser:
    """TF-IDF over stemmed words and word pairs. Deterministic, local, no model."""

    def vectorise(texts: list[str]) -> np.ndarray:
        documents = [Counter(_terms(text)) for text in texts]
        frequency: Counter[str] = Counter()
        for document in documents:
            frequency.update(document.keys())
        # Inverse document frequency below discounts a term every comment uses: it
        # says nothing about which comments belong together.
        vocabulary = {term: index for index, term in enumerate(sorted(frequency))}
        matrix = np.zeros((len(texts), max(1, len(vocabulary))), dtype=np.float32)
        total = len(texts)
        for row, document in enumerate(documents):
            for term, count in document.items():
                idf = math.log((1 + total) / (1 + frequency[term])) + 1.0
                matrix[row, vocabulary[term]] = (1.0 + math.log(count)) * idf
        return matrix

    # 0.2 keeps unrelated comments apart on short review text; it also splits
    # some groups a semantic model would join ("add a test" phrased three ways).
    # That trade is deliberate: a missed standard costs less than an invented one.
    return Vectoriser(name="lexical", vectorise=vectorise, threshold=0.2)


def semantic_vectoriser() -> Vectoriser | None:
    """Sentence embeddings through `llm.embed`, when the local model loads."""
    from prflagger.llm import embed, embeddings_available

    if not embeddings_available():
        return None
    return Vectoriser(
        name="semantic",
        vectorise=lambda texts: np.asarray(embed(texts), dtype=np.float32),
        threshold=0.75,
    )


def agglomerate(vectors: Any, threshold: float) -> list[list[int]]:
    """Average-linkage agglomerative clustering over cosine similarity.

    Lance-Williams updates keep each merge O(n) in numpy rather than recomputing
    every pair of clusters, so a few thousand comments cluster in well under a
    second. The result is the same as recomputing the mean pairwise similarity
    after every merge — average linkage updates exactly this way.
    """
    matrix = np.asarray(vectors, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[0] == 0:
        return []
    count = matrix.shape[0]
    normalised = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-12)
    similarity = normalised @ normalised.T
    np.fill_diagonal(similarity, -np.inf)
    sizes = np.ones(count)
    members: list[list[int]] = [[index] for index in range(count)]

    while True:
        flat = int(np.argmax(similarity))
        left, right = divmod(flat, count)
        best = similarity[left, right]
        if not np.isfinite(best) or best < threshold:
            break
        a, b = (left, right) if left < right else (right, left)
        merged = (sizes[a] * similarity[a] + sizes[b] * similarity[b]) / (sizes[a] + sizes[b])
        similarity[a, :] = merged
        similarity[:, a] = merged
        similarity[a, a] = -np.inf
        similarity[b, :] = -np.inf
        similarity[:, b] = -np.inf
        sizes[a] += sizes[b]
        members[a].extend(members[b])
        members[b] = []
    return [sorted(group) for group in members if group]


# ----------------------------------------------------------------------------------
# From clusters to norms
# ----------------------------------------------------------------------------------


def mine_norms(
    comments: list[dict[str, Any]],
    *,
    vectoriser: Vectoriser,
    namer: Namer | None = None,
    namer_id: str = "",
    min_support: int = 3,
    min_reviewers: int = 2,
    max_comments: int = 1500,
) -> list[Norm]:
    """Group enforced comments and keep the groups enough reviewers stood behind.

    `comments` are enforced review comments, newest first, as `Store.review_comments`
    returns them. Only the newest `max_comments` are used: a standard nobody has
    enforced in the last fifteen hundred comments is not one the repository holds
    today.
    """
    usable = [
        (comment, text)
        for comment in comments[:max_comments]
        if (text := clean_comment(str(comment.get("body", ""))))
    ]
    if len(usable) < min_support:
        return []

    texts = [text for _, text in usable]
    vectors = vectoriser.vectorise(texts)
    groups = agglomerate(vectors, vectoriser.threshold)
    normalised = vectors / (np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-12)

    norms: list[Norm] = []
    taken: set[str] = set()
    for group in groups:
        members = [usable[index][0] for index in group]
        reviewers = {str(m.get("reviewer_login", "")) for m in members} - {""}
        if len(members) < min_support or len(reviewers) < min_reviewers:
            continue

        # The member closest to all the others speaks for the group.
        block = normalised[group]
        centrality = (block @ block.T).mean(axis=1)
        representative = texts[group[int(np.argmax(centrality))]]
        quote = representative if len(representative) <= 300 else representative[:297] + "…"

        statement, named_by = "", "quote"
        if namer is not None:
            try:
                statement = namer([texts[index] for index in group]).strip()
                named_by = namer_id or "model"
            except Exception as error:  # noqa: BLE001 - naming is optional, grouping is not
                log.warning("norm.naming_failed", error=str(error)[:200])
                statement = ""
        if not statement:
            statement, named_by = _first_sentence(representative), "quote"

        norm_id = _unique(_slug(statement), taken)
        prs = sorted({int(m["pr_number"]) for m in members}, reverse=True)
        norms.append(Norm(
            id=norm_id,
            statement=statement,
            scope="repo",
            support=len(members),
            distinct_reviewers=len(reviewers),
            # One loud maintainer is a preference; four people is a standard.
            confidence=round(min(1.0, len(members) / 10) * min(1.0, len(reviewers) / 4), 3),
            evidence_prs=tuple(prs),
            source="mined",
            quote=quote,
            evidence=tuple(
                (int(m["pr_number"]), str(m.get("html_url", "")), str(m.get("path", "")))
                for m in sorted(members, key=lambda m: -int(m["pr_number"]))[:20]
            ),
            clustered_by=vectoriser.name,
            named_by=named_by,
        ))
    log.info(
        "norms.mined", comments=len(usable), clusters=len(groups), norms=len(norms),
        clustered_by=vectoriser.name,
    )
    return sorted(norms, key=lambda n: (-n.confidence, -n.support, n.id))


def _first_sentence(text: str) -> str:
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    sentence = match.group(1) if match else text
    return sentence if len(sentence) <= 200 else sentence[:197] + "…"


def _slug(statement: str) -> str:
    words = re.findall(r"[a-z0-9]+", statement.lower())
    return "review-" + ("-".join(words[:6]) or "norm")


def _unique(candidate: str, taken: set[str]) -> str:
    name, suffix = candidate, 2
    while name in taken:
        name, suffix = f"{candidate}-{suffix}", suffix + 1
    taken.add(name)
    return name
