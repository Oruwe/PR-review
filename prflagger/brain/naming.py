"""Naming a group of review comments with a model, when one is available.

One call per group, on the light model, through the budgeted client. The
instructions are a cached prefix, identical for every group in every
repository. A refused or failed call falls back to quoting the group's most
representative comment (see `mine.mine_norms`), so naming can never block
learning.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from prflagger.core.config import Config
from prflagger.llm.client import ModelClient
from prflagger.llm.provider import Request

__all__ = ["model_namer"]

_INSTRUCTIONS = """\
You are given code review comments that reviewers of one repository wrote on
different pull requests, and that a clustering step grouped because they ask for
the same thing. Every one of them was acted on: the author changed the code
afterwards and the pull request merged.

Write the single standard they enforce as one imperative sentence a maintainer of
that repository would recognise. Be as specific as the comments are; do not
generalise beyond them, and do not add anything they do not say. Return only the
sentence.
"""


def model_namer(
    config: Config, client: ModelClient
) -> Callable[[str], tuple[Callable[[Sequence[str]], str], str] | None]:
    def factory(slug: str) -> tuple[Callable[[Sequence[str]], str], str] | None:
        if not client.available:
            return None

        def name(comments: Sequence[str]) -> str:
            listed = "\n".join(f"- {text.strip()[:400]}" for text in list(comments)[:25])
            answer = client.ask(
                Request(
                    model=config.models.light,
                    system=((_INSTRUCTIONS, True),),
                    prompt=f"{len(comments)} comments:\n{listed}",
                    max_tokens=120,
                    cache_ttl=config.models.cache_ttl,
                ),
                stage="norm_naming",
                repo=slug,
            )
            line = answer.text.strip().strip('"').splitlines()
            return line[0].strip() if line else ""

        return name, config.models.light

    return factory
