"""Real artifacts for the v2 tests.

CLAUDE.md is non-negotiable: Docker, git and the repo under analysis are never
mocked. So these helpers build an actual git repository on disk with an actual
regression in it, and the tests run actual containers against it. A mocked test
here would prove nothing about the thing being tested.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

__all__ = ["REGRESSION_TEST", "advance_base", "build_repo"]

#: The test that passes at base and fails at head. Named so assertions can
#: reference it without restating the string.
REGRESSION_TEST = "tests/test_pricing.py::test_negative_discount_is_identity"

_BASE_SOURCE = '''\
"""Order pricing."""


def apply_discount(amount: float, percent: float) -> float:
    """Reduce `amount` by `percent`."""
    if percent <= 0:
        return amount
    return round(amount * (1 - percent / 100), 2)


def total(items: list[float], discount: float = 0.0) -> float:
    """Sum `items`, then discount."""
    return apply_discount(sum(items), discount)
'''

#: Fixes the stated bug and also changes an edge case without mentioning it, and
#: adds a public function. That is the shape of change this system exists to find.
_HEAD_SOURCE = '''\
"""Order pricing."""


def apply_discount(amount: float, percent: float) -> float:
    """Reduce `amount` by `percent`."""
    if percent < 0:
        raise ValueError("percent must not be negative")
    return round(amount * (1 - percent / 100), 2)


def total(items: list[float], discount: float = 0.0) -> float:
    """Sum `items`, then discount."""
    return apply_discount(sum(items), discount)


def bulk_total(orders: list[list[float]]) -> float:
    """Total across several orders."""
    return sum(total(o) for o in orders)
'''

_TESTS = '''\
from shoplib.pricing import apply_discount, total


def test_discount_applies():
    assert apply_discount(100.0, 10) == 90.0


def test_zero_discount_is_identity():
    assert apply_discount(100.0, 0) == 100.0


def test_negative_discount_is_identity():
    assert apply_discount(100.0, -5) == 100.0


def test_total_sums_then_discounts():
    assert total([50.0, 50.0], 10) == 90.0


def test_total_of_empty_is_zero():
    assert total([]) == 0
'''

_PYPROJECT = """\
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "shoplib"
version = "0.1.0"

[tool.setuptools]
packages = ["shoplib"]
"""


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *argv], check=True, capture_output=True, text=True
    )


def build_repo(root: Path) -> tuple[Path, str, str]:
    """A real two-commit repository. Returns (path, base_sha, head_sha)."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root.parent, "init", "-q", "-b", "main", str(root))
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "user.name", "Fixture")

    (root / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (root / "shoplib").mkdir(exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "shoplib" / "__init__.py").write_text(
        "from shoplib.pricing import apply_discount, total\n"
        '__all__ = ["apply_discount", "total"]\n',
        encoding="utf-8",
    )
    (root / "shoplib" / "pricing.py").write_text(_BASE_SOURCE, encoding="utf-8")
    (root / "tests" / "test_pricing.py").write_text(_TESTS, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "feat: pricing")
    base = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    (root / "shoplib" / "pricing.py").write_text(_HEAD_SOURCE, encoding="utf-8")
    (root / "shoplib" / "__init__.py").write_text(
        "from shoplib.pricing import apply_discount, bulk_total, total\n"
        '__all__ = ["apply_discount", "bulk_total", "total"]\n',
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "fix: validate the discount percentage")
    head = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return root, base, head


def advance_base(root: Path, base: str) -> str:
    """Move the base branch on after the pull request branched. Returns the new tip.

    What every busy repository looks like: other work lands on the base branch
    while a pull request is open. Here that work adds a public function and a
    test the pull request's branch has never seen, so a run that compared head
    with this tip would report both as removed by the pull request.
    """
    _git(root, "checkout", "-q", "-b", "moved-on", base)
    (root / "shoplib" / "rounding.py").write_text(
        '"""Rounding."""\n\n\ndef round_price(amount: float) -> float:\n'
        '    """Round to cents."""\n    return round(amount, 2)\n',
        encoding="utf-8",
    )
    (root / "tests" / "test_rounding.py").write_text(
        "from shoplib.rounding import round_price\n\n\n"
        "def test_round_price_keeps_cents():\n    assert round_price(1.234) == 1.23\n",
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "feat: rounding")
    tip = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    _git(root, "checkout", "-q", "main")
    return tip
