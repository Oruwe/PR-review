"""C3 acceptance.

SPEC.md § C3: pick one pure function in the target repo with no I/O. `generate_tests`
returns >= 5 syntactically valid test functions (assert via `ast.parse`). After
`validate_on_base`, >= 1 survives and `discard_rate` is recorded. A deliberately wrong
test (asserting a value the function does not return) is discarded.

The validation loop runs for real: the tests below execute against the real target repo
inside the real Docker sandbox. Only the text generator is stubbed, which is the provider
boundary the spec permits — the discard decision is made by execution, never by a stub.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from prflagger.characterize import generate, validate
from prflagger.characterize.generate import generate_tests, split_test_functions
from prflagger.characterize.validate import validate_on_base
from prflagger.models import Symbol

# A pure function in the target repo with no I/O.
SUBJECT = Symbol(
    fqn="click.shell_completion.split_arg_string",
    kind="function",
    file="src/click/shell_completion.py",
    line_start=603,
    line_end=620,
)

# Real observed behaviour: split_arg_string("a b") == ["a", "b"], ("") == [].
TRUE_SIMPLE = '''\
from click.shell_completion import split_arg_string


def test_split_arg_string_simple():
    assert split_arg_string("a b") == ["a", "b"]
'''

TRUE_EMPTY = '''\
from click.shell_completion import split_arg_string


def test_split_arg_string_empty():
    assert split_arg_string("") == []
'''

TRUE_QUOTED = '''\
from click.shell_completion import split_arg_string


def test_split_arg_string_quoted():
    assert split_arg_string("'x y'") == ["x y"]
'''

# Asserts a value the function does not return: it returns [], not None.
DELIBERATELY_WRONG = '''\
from click.shell_completion import split_arg_string


def test_split_arg_string_empty_is_none():
    assert split_arg_string("") is None
'''


@pytest.fixture
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh cache so job results and metrics belong to this test alone."""
    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path))
    return tmp_path


# --------------------------------------------------------------------------------------
# The validation loop — execution is the filter
# --------------------------------------------------------------------------------------


def test_a_deliberately_wrong_test_is_discarded(
    head_worktree: Path, head_sha: str, isolated_cache: Path
) -> None:
    survivors, discard_rate = validate_on_base(
        [TRUE_SIMPLE, TRUE_EMPTY, DELIBERATELY_WRONG],
        head_worktree,
        head_sha,
        max_attempts=1,
    )

    assert DELIBERATELY_WRONG not in survivors, "a test that fails on base is not evidence"
    assert TRUE_SIMPLE in survivors
    assert TRUE_EMPTY in survivors
    assert len(survivors) >= 1
    assert discard_rate == pytest.approx(1 / 3)


def test_discard_rate_is_recorded(
    head_worktree: Path, head_sha: str, isolated_cache: Path
) -> None:
    _, discard_rate = validate_on_base(
        [TRUE_SIMPLE, DELIBERATELY_WRONG], head_worktree, head_sha, max_attempts=1
    )

    records = [
        json.loads(line)
        for line in validate.metrics_path().read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records, "the discard rate must be recorded per run"
    entry = records[-1]
    assert entry["discard_rate"] == pytest.approx(discard_rate)
    assert entry["tests_generated"] == 2
    assert entry["tests_discarded"] == 1
    assert entry["stage"] == "validate_on_base"


def test_every_wrong_test_discarded_leaves_nothing(
    head_worktree: Path, head_sha: str, isolated_cache: Path
) -> None:
    survivors, discard_rate = validate_on_base(
        [DELIBERATELY_WRONG], head_worktree, head_sha, max_attempts=1
    )
    assert survivors == []
    assert discard_rate == pytest.approx(1.0)


def test_failure_output_is_fed_back_and_the_fix_survives(
    head_worktree: Path, head_sha: str, isolated_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry loop: the model sees what the code really returned, and the corrected
    test is kept only because it genuinely passes in the sandbox."""
    seen: list[str] = []

    def corrected(prompt: str, **kwargs: object) -> str:
        seen.append(prompt)
        return TRUE_EMPTY

    monkeypatch.setattr(validate, "complete", corrected)

    survivors, discard_rate = validate_on_base(
        [DELIBERATELY_WRONG], head_worktree, head_sha, max_attempts=2
    )

    assert seen, "a failing test must trigger regeneration"
    # The prompt carries the real failure, not just the test that failed.
    assert "assert split_arg_string" in seen[0]
    assert "Rewrite them to match the real behavior" in seen[0]
    assert TRUE_EMPTY in survivors
    assert discard_rate == pytest.approx(0.5)  # 2 seen, 1 survived


def test_a_syntactically_broken_test_is_discarded_not_crashed(
    head_worktree: Path, head_sha: str, isolated_cache: Path
) -> None:
    survivors, _ = validate_on_base(
        ["def test_broken(:\n    pass\n", TRUE_SIMPLE], head_worktree, head_sha, max_attempts=1
    )
    assert survivors == [TRUE_SIMPLE]


# --------------------------------------------------------------------------------------
# generate_tests — the splitter is real; the model is stubbed at the provider boundary
# --------------------------------------------------------------------------------------


MODEL_RESPONSE = '''```python
from click.shell_completion import split_arg_string


def test_split_arg_string_simple():
    assert split_arg_string("a b") == ["a", "b"]


def test_split_arg_string_empty():
    assert split_arg_string("") == []


def test_split_arg_string_quoted():
    assert split_arg_string("'x y'") == ["x y"]


def test_split_arg_string_whitespace_only():
    assert split_arg_string("   ") == []


def test_split_arg_string_single():
    assert split_arg_string("solo") == ["solo"]


def test_split_arg_string_trailing_backslash():
    assert split_arg_string("a\\\\") == ["a"]
```'''


def test_generate_tests_returns_parseable_single_function_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def stub(prompt: str, **kwargs: object) -> str:
        captured["prompt"] = prompt
        return MODEL_RESPONSE

    monkeypatch.setattr(generate, "complete", stub)

    tests = generate_tests(SUBJECT, "def split_arg_string(string): ...", n=8)

    assert len(tests) >= 5
    for source in tests:
        tree = ast.parse(source)  # syntactically valid
        functions = [
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        ]
        assert len(functions) == 1, "one test function each"
        assert "from click.shell_completion import split_arg_string" in source


def test_the_prompt_asks_for_observed_behaviour_not_correct_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def stub(prompt: str, **kwargs: object) -> str:
        captured["prompt"] = prompt
        return MODEL_RESPONSE

    monkeypatch.setattr(generate, "complete", stub)
    generate_tests(SUBJECT, "def split_arg_string(string): ...", n=8)

    prompt = captured["prompt"]
    assert "RECORD ITS CURRENT BEHAVIOR" in prompt
    assert "Do not test what the function should do" in prompt
    assert SUBJECT.fqn in prompt


def test_split_handles_a_bare_response_without_fences() -> None:
    tests = split_test_functions(MODEL_RESPONSE.strip("`").replace("python\n", "", 1))
    assert len(tests) >= 5


def test_split_ignores_helpers_that_are_not_tests() -> None:
    response = '''\
import pytest


def _helper(value):
    return value


def test_one():
    assert _helper(1) == 1
'''
    tests = split_test_functions(response)
    assert len(tests) == 1
    assert "def test_one" in tests[0]


def test_split_returns_nothing_for_unparseable_output() -> None:
    assert split_test_functions("this is not python (((") == []
