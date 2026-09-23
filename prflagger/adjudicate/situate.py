"""Everything a model needs to read an observation against this repository.

No model is involved here. Context is assembled from the charter, the norms, the
two worktrees and git, in two parts:

* the **repository context** — charter and standards — which is identical for
  every pull request in a repository until either changes, and so goes behind a
  prompt-cache breakpoint: after the first call it is billed at a tenth;
* one **packet** per group of observations touching the same file — numbered
  source excerpts, the diff, callers — which changes with every pull request.

Line numbers in excerpts are the file's own, so a citation of ``path:line``
means what it says and can be checked.
"""

from __future__ import annotations

import re
import subprocess
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from prflagger.core.models import Charter, Norm, Observation, PullRequest
from prflagger.storage.repos import Store

__all__ = ["Packet", "changed_paths", "group", "render_packet", "repo_context"]

_WINDOW = 12  # lines either side of a cited line
_MAX_EXCERPT_CHARS = 9000
_MAX_DIFF_CHARS = 6000
_MAX_CONTEXT_CHARS = 9000
_FILE_REF = re.compile(r"^([\w./-]+\.[\w]+):(\d+)")


@dataclass(frozen=True)
class Packet:
    key: str  # the file the group concerns, or "(repo)"
    observations: tuple[Observation, ...]
    excerpts: tuple[tuple[str, str], ...]  # (label, numbered lines)
    diff: str
    callers: tuple[str, ...]
    norms: tuple[Norm, ...]  # mined norms that may bear on this file


# ----------------------------------------------------------------------------------
# The stable part
# ----------------------------------------------------------------------------------


def repo_context(slug: str, charter: Charter | None, norms: list[Norm]) -> str:
    """The repository as it describes itself, and what it holds itself to.

    Byte-identical for every pull request until the charter or the norms change,
    which is what makes it cacheable. Every line carries the reference a
    citation of it would use.
    """
    lines = [f"REPOSITORY {slug}"]
    if charter is not None:
        lines.append(f"Charter #{charter.number} at {charter.sha[:12]}: {charter.summary}")
        sections = (
            ("purpose", "What it is for"),
            ("target", "Who and what it targets"),
            ("constraint", "Rules and non-goals it states"),
        )
        for kind, title in sections:
            claims = charter.claims_of(kind)
            if claims:
                lines.append(f"{title} (cite as code, by the source shown):")
                lines += [f"- [{c.source}] {c.text}" for c in claims[:12]]
        if charter.entry_points:
            lines.append("Entry points: " + "; ".join(charter.entry_points[:8]))
    else:
        lines.append("No charter has been read for this repository yet.")

    declared = [n for n in norms if n.source == "declared"]
    mined = [n for n in norms if n.source == "mined"][:15]
    if declared or mined:
        lines.append("Standards (cite as norm, by the id in brackets):")
    for norm in declared:
        where = ", ".join(e[2] for e in norm.evidence)
        lines.append(f"- [{norm.id}] {norm.statement} (configured at {where})")
    for norm in mined:
        prs = ", ".join(f"#{n}" for n in norm.evidence_prs[:6])
        lines.append(
            f"- [{norm.id}] {norm.statement} (enforced in review: {norm.support} comments "
            f"from {norm.distinct_reviewers} reviewers; {prs})"
        )
    text = "\n".join(lines)
    return text if len(text) <= _MAX_CONTEXT_CHARS else text[:_MAX_CONTEXT_CHARS] + "\n…"


# ----------------------------------------------------------------------------------
# The per-pull-request part
# ----------------------------------------------------------------------------------


def changed_paths(head_tree: Path, base_sha: str, head_sha: str) -> set[str]:
    completed = subprocess.run(  # noqa: S603
        ["git", "-C", str(head_tree), "diff", "--name-only", base_sha, head_sha],  # noqa: S607
        capture_output=True, text=True, check=False, timeout=60,
    )
    return {line.strip() for line in completed.stdout.splitlines() if line.strip()}


def group(
    observations: list[Observation],
    *,
    repo: str,
    store: Store,
    base_tree: Path,
    head_tree: Path,
    base_sha: str,
    head_sha: str,
    limit: int,
) -> list[Packet]:
    """Observations grouped by the file they concern, highest-ranked group first.

    One model call per group rather than per observation: the file's excerpts and
    diff are shared, and so is the cost of reading them.
    """
    groups: OrderedDict[str, list[Observation]] = OrderedDict()
    for observation in sorted(observations, key=lambda o: (-o.rank_score, o.id)):
        groups.setdefault(_path_of(observation, repo, store), []).append(observation)

    mined = [n for n in store.norms(repo) if n.source == "mined"]
    packets = []
    for key, members in list(groups.items())[:limit]:
        members = members[:6]
        packets.append(Packet(
            key=key,
            observations=tuple(members),
            excerpts=tuple(_excerpts(members, key, repo, store, base_tree, head_tree)),
            diff=_diff(head_tree, base_sha, head_sha, key) if key != "(repo)" else "",
            callers=tuple(_callers(members, repo, store)),
            norms=tuple(_related(mined, key, members)),
        ))
    return packets


def render_packet(packet: Packet, pull: PullRequest | None) -> str:
    """The volatile part of the prompt: this pull request, this group."""
    parts = []
    if pull is not None:
        body = (pull.body or "").strip()
        parts.append(
            f"PULL REQUEST #{pull.number}: {pull.title}\n"
            f"Its description (the author's stated intent):\n{body[:2000] or '(none)'}"
        )
    parts.append(f"OBSERVATIONS concerning {packet.key}:")
    for o in packet.observations:
        parts.append(
            f"- id={o.id} kind={o.kind} symbol={o.symbol}\n"
            f"  what changed: {o.what_changed}\n  how we know: {o.how_we_know}\n"
            f"  evidence: {o.evidence_ref}"
            + (f"\n  relevance to the charter: {o.relevance} — {o.relevance_note}"
               if o.relevance else "")
        )
    for label, text in packet.excerpts:
        parts.append(f"SOURCE {label}\n{text}")
    if packet.diff:
        parts.append(f"DIFF of {packet.key} (base → head)\n{packet.diff}")
    if packet.callers:
        parts.append("CALLERS (from this repository's call graph):\n"
                     + "\n".join(f"- {c}" for c in packet.callers))
    if packet.norms:
        parts.append("REVIEW STANDARDS that were enforced on this area before:\n" + "\n".join(
            f"- [{n.id}] {n.statement} — e.g. “{n.quote[:200]}”" for n in packet.norms
        ))
    return "\n\n".join(parts)


# ----------------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------------


def _path_of(observation: Observation, repo: str, store: Store) -> str:
    ref = observation.evidence_ref
    if "::" in ref:
        return ref.split("::", 1)[0]
    match = _FILE_REF.match(ref)
    if match:
        return match.group(1)
    if ":" in ref and "/" in ref.split(":", 1)[0]:
        return ref.split(":", 1)[0]
    symbol = store.symbol(repo, observation.symbol)
    if symbol is not None and symbol.file:
        return symbol.file
    if "/" in observation.symbol or observation.symbol.endswith((".py", ".ts", ".js", ".go")):
        return observation.symbol
    return "(repo)"


def _numbered(lines: list[str], start: int, end: int) -> str:
    return "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(start, end + 1))


def _read(tree: Path, path: str) -> list[str] | None:
    target = tree / path
    if not target.is_file():
        return None
    try:
        return target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def _excerpts(
    members: list[Observation], key: str, repo: str, store: Store,
    base_tree: Path, head_tree: Path,
) -> list[tuple[str, str]]:
    if key == "(repo)":
        return []
    windows: dict[str, set[int]] = {"head": set(), "base": set()}
    for observation in members:
        line = _line_of(observation, key, repo, store, head_tree)
        side = "head" if _read(head_tree, key) is not None else "base"
        if line is not None:
            windows[side].update(range(max(1, line - _WINDOW), line + _WINDOW + 1))

    out: list[tuple[str, str]] = []
    budget = _MAX_EXCERPT_CHARS
    for side, tree in (("head", head_tree), ("base", base_tree)):
        lines = _read(tree, key)
        if lines is None:
            continue
        wanted = sorted(n for n in windows[side] if n <= len(lines))
        if not wanted and side == "head" and not windows["base"]:
            wanted = list(range(1, min(len(lines), 60) + 1))  # no line known: the top
        for start, end in _runs(wanted):
            text = _numbered(lines, start, end)
            if len(text) > budget:
                break
            budget -= len(text)
            prefix = "" if side == "head" else "base:"
            out.append((f"{prefix}{key}:{start}-{end} at the {side} commit", text))
    return out


def _line_of(observation: Observation, key: str, repo: str, store: Store, head_tree: Path
             ) -> int | None:
    match = _FILE_REF.match(observation.evidence_ref)
    if match and match.group(1) == key:
        return int(match.group(2))
    if "::" in observation.evidence_ref:
        name = observation.evidence_ref.split("::")[-1].split("[", 1)[0]
        for index, text in enumerate(_read(head_tree, key) or []):
            if re.match(rf"\s*(async\s+)?def\s+{re.escape(name)}\b", text):
                return index + 1
    symbol = store.symbol(repo, observation.symbol)
    if symbol is not None and symbol.file == key:
        return symbol.line_start
    return None


def _runs(numbers: list[int]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    for n in numbers:
        if runs and n == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], n)
        else:
            runs.append((n, n))
    return runs


def _diff(head_tree: Path, base_sha: str, head_sha: str, path: str) -> str:
    completed = subprocess.run(  # noqa: S603
        ["git", "-C", str(head_tree), "diff", "--unified=3", base_sha, head_sha,  # noqa: S607
         "--", path],
        capture_output=True, text=True, check=False, timeout=60,
    )
    text = completed.stdout
    return text if len(text) <= _MAX_DIFF_CHARS else text[:_MAX_DIFF_CHARS] + "\n… (truncated)"


def _callers(members: list[Observation], repo: str, store: Store) -> list[str]:
    out: list[str] = []
    for observation in members:
        for caller in store.callers_of(repo, observation.symbol, limit=5):
            entry = f"{caller.fqn} ({caller.file}:{caller.line_start})"
            if entry not in out:
                out.append(entry)
    return out[:8]


_TOKEN = re.compile(r"[a-z][a-z0-9_]{2,}")


def _related(mined: list[Norm], key: str, members: list[Observation]) -> list[Norm]:
    """Mined norms that were enforced on this file or directory, or share its words."""
    directory = key.rsplit("/", 1)[0] if "/" in key else ""
    words = set(_TOKEN.findall(" ".join(o.what_changed + " " + o.symbol for o in members)
                               .lower()))
    scored = []
    for norm in mined:
        paths = {where for _, _, where in norm.evidence}
        score = 2.0 if key in paths else 1.0 if directory and any(
            p.startswith(directory + "/") for p in paths) else 0.0
        score += len(words & set(_TOKEN.findall(norm.statement.lower()))) * 0.25
        if score > 0:
            scored.append((score, norm))
    return [norm for _, norm in sorted(scored, key=lambda pair: (-pair[0], pair[1].id))[:3]]
