"""Generate characterization tests.

A characterization test records what code does today. It does not assert what the code
should do — if current behaviour is a bug, the test asserts the bug. The test exists to
detect change, not to judge correctness.
"""

from __future__ import annotations

import ast

from prflagger.llm import complete
from prflagger.models import Symbol
from prflagger.providers import DEFAULT_MODEL

__all__ = ["PROMPT", "RETRY_SUFFIX", "generate_tests", "split_test_functions"]

PROMPT = """\
Here is a Python function and its module context.

{source}

Write {n} pytest test functions that RECORD ITS CURRENT BEHAVIOR.

Do not test what the function should do. Test what it does. If it returns None for
empty input, assert it returns None. If it raises KeyError, assert KeyError.

Rules: public API only; deterministic; no network, filesystem, or environment access;
one behavior per test; all imports inside the module you emit.

Cover roughly {ordinary} ordinary inputs and {edge} edge cases (empty, None, zero,
negative, boundary, wrong type).

The symbol under test is `{fqn}`.
Name tests test_{slug}_<short_case>.

Return only Python code, no prose, no markdown fences.
"""

RETRY_SUFFIX = """\

These tests failed against the unchanged code, which means the behavior you assumed was
wrong. Here is what actually happened:

{failure_output}

Rewrite them to match the real behavior shown above.
"""


def generate_tests(symbol: Symbol, source: str, *, n: int = 8) -> list[str]:
    """LLM-generate pytest functions recording CURRENT behavior of `symbol`.

    Returns source strings, one test function each.
    """
    prompt = PROMPT.format(
        source=source,
        n=n,
        ordinary=n // 2,
        edge=n - n // 2,
        fqn=symbol.fqn,
        slug=symbol.fqn.replace(".", "_"),
    )
    return split_test_functions(complete(prompt, model=DEFAULT_MODEL, max_tokens=8192))


def split_test_functions(response: str) -> list[str]:
    """One self-contained source string per test function, imports carried along.

    The model emits a module; downstream wants individually runnable units, because a
    test is kept or discarded on its own.
    """
    code = _strip_fences(response)
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    lines = code.splitlines()
    preamble: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            segment = ast.get_source_segment(code, node)
            if segment:
                preamble.append(segment)

    tests: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue
        start = min([node.lineno, *[d.lineno for d in node.decorator_list]]) - 1
        end = node.end_lineno or node.lineno
        body = "\n".join(lines[start:end])
        tests.append("\n".join([*preamble, "", "", body]).strip() + "\n")
    return tests


def _strip_fences(response: str) -> str:
    text = response.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)
