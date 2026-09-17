"""Create the three seeded branches in the target repo's cached clone.

They are real commits on a real repository, so the pipeline that reads them is reading
git rather than a fixture. Writing them ourselves is what makes the demo deterministic
and rehearsable — the same reason a stage magician does not improvise.

Idempotent: re-running resets each branch to the same content.

    python -m scripts.seed_target
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET_FILE = "src/click/shell_completion.py"
SILENT_FILE = "src/click/formatting.py"
ANCHOR = "def split_arg_string(string: str) -> list[str]:"

# The stated fix: a null byte is passed straight through on base
# (split_arg_string("a\\x00b") -> ["a\\x00b"]) and is rejected after. Deterministic and
# hermetic, so it can actually be characterized — None input cannot, because shlex reads
# stdin when handed None.
STATED_FIX = '''\
    if "\\x00" in string:
        raise ValueError("argument string contains a null byte")

'''

# The undeclared change, in a function the PR body never mentions: measure_table([])
# returns () on base and (0,) after. Nothing in the description says so.
SILENT_ANCHOR = "    return tuple(y for x, y in sorted(widths.items()))"
SILENT_EDGE = '''\
    if not widths:
        return (0,)

'''

NEW_PUBLIC_FUNCTION = '''

def join_arg_string(parts: list[str]) -> str:
    import shlex

    return " ".join(shlex.quote(part) for part in parts)
'''

SEEDS: dict[str, dict[str, str]] = {
    "seed/honest-fix": {
        "title": "Reject null bytes in split_arg_string",
        "body": (
            "split_arg_string passed a null byte straight through into the token list, "
            "which then blew up further down in the completion machinery.\n\n"
            "It now raises ValueError. Only split_arg_string changes; no other "
            "behaviour is affected."
        ),
    },
    "seed/silent-edge": {
        "title": "Reject null bytes in split_arg_string",
        "body": (
            "split_arg_string passed a null byte straight through into the token list, "
            "which then blew up further down in the completion machinery.\n\n"
            "It now raises ValueError."
        ),
    },
    "seed/untested-api": {
        "title": "Add join_arg_string",
        "body": "Adds join_arg_string, the inverse of split_arg_string.",
    },
}


def _git_bare(bare: Path, *argv: str) -> str:
    return _run(["git", f"--git-dir={bare}", *argv], REPO_ROOT)


def _run(argv: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, check=True
    )
    return completed.stdout.strip()


def _slug() -> str:
    data = tomllib.loads((REPO_ROOT / "config.toml").read_text(encoding="utf-8"))
    return str(data["target"]["slug"])


def _apply(worktree: Path, branch: str) -> None:
    path = worktree / TARGET_FILE
    source = path.read_text(encoding="utf-8")

    if branch == "seed/silent-edge":
        silent = worktree / SILENT_FILE
        text = silent.read_text(encoding="utf-8")
        assert SILENT_ANCHOR in text, "measure_table moved; reseeding needs a new anchor"
        silent.write_text(
            text.replace(SILENT_ANCHOR, SILENT_EDGE + SILENT_ANCHOR, 1), encoding="utf-8"
        )

    if branch == "seed/untested-api":
        # Appended, so git attributes the added lines to the new function rather than
        # aligning them with the one that used to follow.
        path.write_text(source.rstrip("\n") + "\n" + NEW_PUBLIC_FUNCTION, encoding="utf-8")
        return

    # Insert after the docstring's `import shlex`, inside the function body.
    anchor = f'{ANCHOR}\n    """'
    assert anchor in source, "target function moved; reseeding needs an updated anchor"
    body_start = source.index("    import shlex", source.index(ANCHOR))
    path.write_text(source[:body_start] + STATED_FIX + source[body_start:], encoding="utf-8")


def main() -> int:
    slug = _slug()
    bare = REPO_ROOT / ".cache" / "repos" / f"{slug.replace('/', '__')}.git"
    if not bare.is_dir():
        print(f"no clone at {bare}; run the test suite once to create it", file=sys.stderr)
        return 1

    base = _git_bare(bare, "rev-parse", "HEAD")
    staging = REPO_ROOT / ".cache" / "seeding"
    if staging.is_dir():
        _git_bare(bare, "worktree", "remove", "--force", str(staging))
    _git_bare(bare, "worktree", "add", "--detach", "--quiet", str(staging), base)

    manifest: dict[str, dict[str, str]] = {}
    try:
        for branch, meta in SEEDS.items():
            _run(["git", "checkout", "--quiet", "--detach", base], staging)
            _run(["git", "checkout", "--quiet", "--", "."], staging)
            _apply(staging, branch)
            _run(["git", "add", "-A"], staging)
            _run(
                ["git", "-c", "user.email=seed@prflagger", "-c", "user.name=PR Flagger",
                 "commit", "--quiet", "-m", f"{meta['title']}\n\n{meta['body']}"],
                staging,
            )
            head = _run(["git", "rev-parse", "HEAD"], staging)
            _git_bare(bare, "branch", "--force", branch, head)
            manifest[branch] = {"base": base, "head": head, **meta}
            print(f"{branch}: {head[:12]}")
    finally:
        _git_bare(bare, "worktree", "remove", "--force", str(staging))

    out = REPO_ROOT / ".cache" / "seeds.json"
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
