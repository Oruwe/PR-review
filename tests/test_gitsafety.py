"""The git-argument-injection boundary.

`analysis/`, `characterize/` and `sandbox/` all shell out to git on the host,
before anything attacker-supplied reaches the sandboxed container — so this
is the one guard standing between a hostile PR branch name and a git flag
(`--output=<path>`) or a cache path escape (`../..`).
"""

from __future__ import annotations

import pytest

from prflagger.gitsafety import assert_safe_revision


def test_accepts_a_real_sha() -> None:
    sha = "b92b0945b468ef36e68840a2cfbc339acae5866a"
    assert assert_safe_revision(sha) == sha


def test_accepts_an_ordinary_branch_name() -> None:
    branch = "feature/fix-shlex-none"
    assert assert_safe_revision(branch) == branch


def test_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        assert_safe_revision("")


def test_rejects_leading_dash() -> None:
    # git treats a bare argv token starting with '-' as an option. Several
    # subcommands (log, show, diff) accept --output=<path>, which writes
    # their output to a file the caller names.
    with pytest.raises(ValueError, match="flag"):
        assert_safe_revision("--output=/tmp/pwned")


def test_rejects_dash_o_short_flag() -> None:
    with pytest.raises(ValueError, match="flag"):
        assert_safe_revision("-oplugin.so")


def test_rejects_path_traversal() -> None:
    # base_sha and commit are also spliced into cache_root()/worktrees/checkouts/<value>.
    with pytest.raises(ValueError, match="traversal"):
        assert_safe_revision("../../etc/passwd")


def test_rejects_null_byte() -> None:
    with pytest.raises(ValueError, match="null byte"):
        assert_safe_revision("main\x00--upload-pack=evil")


def test_error_names_the_field() -> None:
    with pytest.raises(ValueError, match="base_sha"):
        assert_safe_revision("--evil", what="base_sha")
