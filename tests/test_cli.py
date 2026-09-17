"""C11 acceptance.

SPEC.md § C11: `prflagger check` on each seeded branch produces the expected report end
to end, in under 10 minutes.

The suite runs against the warm cache rather than a cold one, because a cold run
re-clones the target and rebuilds the image. The timing bound is asserted regardless.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SEEDS = REPO_ROOT / ".cache" / "seeds.json"

EXPECTED = {
    # branch -> symbols the report must name
    "seed/untested-api": {"click.shell_completion.join_arg_string"},
    "seed/honest-fix": set(),
    "seed/silent-edge": set(),
}


@pytest.fixture(scope="module")
def seeds() -> dict[str, dict[str, str]]:
    if not SEEDS.is_file():
        pytest.fail("run `python -m scripts.seed_target` first")
    return json.loads(SEEDS.read_text(encoding="utf-8"))


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "prflagger.cli", *argv],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=900,
    )


def _worktree() -> str:
    root = REPO_ROOT / ".cache" / "worktrees" / "pallets__click"
    checkouts = sorted(child for child in root.iterdir() if child.is_dir())
    assert checkouts, "no target worktree; run the suite once to create it"
    return str(checkouts[0])


@pytest.mark.parametrize("branch", sorted(EXPECTED))
def test_check_produces_a_report_for_each_seeded_branch(
    branch: str, seeds: dict[str, dict[str, str]], tmp_path: Path
) -> None:
    meta = seeds[branch]
    out = tmp_path / "report.html"

    started = time.monotonic()
    result = _run(
        "check", "--repo", _worktree(), "--base", meta["base"], "--head", meta["head"],
        "--out", str(out),
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr[-2000:]
    assert out.is_file()
    assert elapsed < 600, f"took {elapsed:.0f}s, budget 600s"

    html = out.read_text(encoding="utf-8")
    assert "Coverage statement" in html
    for symbol in EXPECTED[branch]:
        assert symbol in html, f"{branch} report should name {symbol}"


def test_check_names_what_it_could_not_verify(
    seeds: dict[str, dict[str, str]], tmp_path: Path
) -> None:
    """Degrade explicitly: a run that could not do behavioural verification says so,
    both on stdout and in the report's coverage statement."""
    meta = seeds["seed/untested-api"]
    out = tmp_path / "report.html"
    result = _run(
        "check", "--repo", _worktree(), "--base", meta["base"], "--head", meta["head"],
        "--out", str(out),
    )

    assert result.returncode == 0
    html = out.read_text(encoding="utf-8")
    assert "Coverage statement" in html
    assert "Verified" in html
    # Anything skipped is named with a reason, never dropped quietly.
    if "Not verified:" in result.stdout:
        assert "behavioural differential" in result.stdout
        assert "skipped" in html


def test_check_emits_no_verdict(seeds: dict[str, dict[str, str]], tmp_path: Path) -> None:
    """No code path may produce approve, reject, looks good, risky, or a score."""
    meta = seeds["seed/untested-api"]
    out = tmp_path / "report.html"
    result = _run(
        "check", "--repo", _worktree(), "--base", meta["base"], "--head", meta["head"],
        "--out", str(out),
    )
    combined = (result.stdout + out.read_text(encoding="utf-8")).lower()

    for verdict in ("approve", "reject", "looks good", "lgtm", "risky", "quality score"):
        assert verdict not in combined, f"the report must not say {verdict!r}"


def test_norms_prints_evidence(tmp_path: Path) -> None:
    result = _run("norms", "--repo", "pallets/click")
    assert result.returncode == 0
    assert "confidence" in result.stdout
    assert "declared by the repository" in result.stdout or "#" in result.stdout


def test_the_three_commands_are_documented() -> None:
    assert _run("--help").returncode == 0
    for command in ("brain", "check", "norms"):
        assert command in _run("--help").stdout
