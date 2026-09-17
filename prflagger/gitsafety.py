"""Guards against a hostile revision string, before it reaches git or a path.

Every SHA, branch, or ref this system hands to `git` can trace back to a pull
request an attacker authored — a fork PR's own branch name, for instance.
Two things can go wrong once that string reaches a subprocess call or a
filesystem path, and `analysis/`, `characterize/` and `sandbox/` all do both:

1. `git` treats a bare argv token starting with `-` as an option, not a
   revision. Several subcommands (`log`, `show`, `diff`) accept
   `--output=<path>`, which writes their output to a file the caller names —
   an attacker who controls the ref name controls that path.
2. Several of these same strings are also spliced straight into a cache path
   (`cache_root() / "worktrees" / "checkouts" / base_sha`). A value like
   `../../etc` walks that path outside `.cache/` entirely.

Both matter more here than in most tools: this git activity runs on the
*host*, before anything attacker-supplied ever reaches the sandboxed
container C1 builds. The sandbox's isolation buys nothing against a ref name
that never makes it that far.
"""

from __future__ import annotations

__all__ = ["assert_safe_revision"]


def assert_safe_revision(value: str, *, what: str = "revision") -> str:
    """Reject a revision/ref string shaped like a flag or a path escape.

    A real SHA or branch name never starts with `-`, never contains a `..`
    path segment, and never contains a null byte. Anything that does is
    either a mistake or an attempt to inject a git option or walk outside
    the cache directory this value is about to be used inside.
    """
    if not value:
        raise ValueError(f"empty {what}")
    if value.startswith("-"):
        raise ValueError(f"unsafe {what} (looks like a flag): {value!r}")
    if "\x00" in value:
        raise ValueError(f"unsafe {what} (contains a null byte): {value!r}")
    if any(segment == ".." for segment in value.split("/")):
        raise ValueError(f"unsafe {what} (path traversal): {value!r}")
    return value
