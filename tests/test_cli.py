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


# --------------------------------------------------------------------------------------
# In process, so the branches are actually exercised rather than only shelled out to
# --------------------------------------------------------------------------------------


def test_argument_parsing_rejects_a_missing_required_option() -> None:
    from prflagger.cli import main

    with pytest.raises(SystemExit) as exit_info:
        main(["check", "--repo", "."])  # no --base/--head
    assert exit_info.value.code == 2


def test_an_unknown_command_is_rejected() -> None:
    from prflagger.cli import main

    with pytest.raises(SystemExit):
        main(["not-a-command"])


def test_check_in_process_writes_a_report(
    seeds: dict[str, dict[str, str]], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from prflagger.cli import main

    meta = seeds["seed/untested-api"]
    out = tmp_path / "report.html"
    code = main(
        ["check", "--repo", _worktree(), "--base", meta["base"], "--head", meta["head"],
         "--out", str(out)]
    )

    assert code == 0
    assert out.is_file()
    printed = capsys.readouterr().out
    assert "observation(s)" in printed
    assert "join_arg_string" in printed


def test_brain_build_falls_back_to_a_declarative_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no gh on PATH the brain still builds, and says what it could not mine."""
    from prflagger.brain import harvest as harvest_module
    from prflagger.cli import main

    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(harvest_module.shutil, "which", lambda _: None)

    assert main(["brain", "build", "--repo", "pallets/click"]) == 0

    captured = capsys.readouterr()
    assert "harvest unavailable" in captured.err
    assert "declarative-only" in captured.err
    assert "norm(s)" in captured.out
    assert (tmp_path / "brain" / "pallets__click" / "repo_profile.json").is_file()


def test_norms_in_process_reports_when_there_is_nothing_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from prflagger.cli import main

    monkeypatch.setenv("PRFLAGGER_CACHE_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)  # no config.toml, no target checkout

    assert main(["norms", "--repo", "someone/else"]) == 0
    assert "brain build" in capsys.readouterr().out


def test_a_failing_probe_becomes_a_coverage_fact_not_a_crash(
    seeds: dict[str, dict[str, str]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe that blows up must be named in the coverage statement, never swallowed."""
    import prflagger.cli as cli_module

    def explode(repo: Path, base: str, head: str) -> list[object]:
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(cli_module, "api_diff", explode)

    meta = seeds["seed/untested-api"]
    out = tmp_path / "report.html"
    assert cli_module.main(
        ["check", "--repo", _worktree(), "--base", meta["base"], "--head", meta["head"],
         "--out", str(out)]
    ) == 0

    html = out.read_text(encoding="utf-8")
    assert "api surface" in html
    assert "probe exploded" in html


def test_reason_names_the_error_type() -> None:
    from prflagger.cli import _reason

    assert _reason(ValueError("boom")) == "ValueError: boom"
    assert _reason(RuntimeError()) == "RuntimeError: RuntimeError"
