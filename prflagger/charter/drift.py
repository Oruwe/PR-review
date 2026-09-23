"""How far a repository moved from what it was.

Drift is measured on the charter, not the code. A change is major when it
changes what the repository *is* — its public surface, its entry points, its
stated purpose, its version line, its license, its ecosystem — and never merely
because it is large. A thousand-line refactor that keeps every promise is minor;
a three-line change that removes the command users run is major.

Every signal is mechanical and carries its evidence, so a person told "this
update is major" can see exactly why and disagree with specifics.
"""

from __future__ import annotations

import difflib
import re

from prflagger.charter.extract import NON_GOAL
from prflagger.core.config import CharterConfig
from prflagger.core.models import Charter, CharterDrift, DriftSignal

__all__ = ["compare", "evidence_lines", "headline"]

_SEMVER = re.compile(r"^v?(\d+)(?:\.(\d+))?")


def _n(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _sample(items: list[str], limit: int = 6) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" (+{len(items) - limit} more)" if len(items) > limit else "")


def _purpose_text(charter: Charter) -> str:
    parts = [charter.summary, *(c.text for c in charter.claims_of("purpose"))]
    return " ".join(p for p in parts if p).strip().lower()


def _version(value: str) -> tuple[int, int] | None:
    match = _SEMVER.match(value.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2) or 0)


def compare(
    before: Charter, after: Charter, config: CharterConfig | None = None
) -> CharterDrift:
    """Every way `after` departs from `before`, each graded."""
    if before.repo != after.repo:
        # Drift between two different repositories is meaningless, and computing
        # it anyway is exactly the cross-repository leakage the charter forbids.
        raise ValueError(
            f"cannot compare charters of different repositories: "
            f"{before.repo!r} vs {after.repo!r}"
        )
    limits = config or CharterConfig()
    signals: list[DriftSignal] = []

    # -- identity --------------------------------------------------------------
    if before.toolchain and after.toolchain and before.toolchain != after.toolchain:
        signals.append(DriftSignal(
            "toolchain", "major",
            f"the repository moved from {before.toolchain} to {after.toolchain}",
            f"{before.toolchain} → {after.toolchain}",
        ))
    if before.license and after.license and before.license != after.license:
        signals.append(DriftSignal(
            "license", "major", "the license changed",
            f"{before.license} → {after.license}",
        ))
    old_version, new_version = _version(before.version), _version(after.version)
    if old_version and new_version and new_version != old_version:
        if new_version[0] > old_version[0]:
            level, detail = "major", "new major version — the project is signalling a break"
        elif new_version[0] == 0 and new_version[1] > old_version[1]:
            level, detail = "notable", "0.x minor bump, which semver allows to break"
        elif new_version < old_version:
            level, detail = "notable", "the version went backwards"
        else:
            level, detail = "minor", "version bump"
        signals.append(DriftSignal("version", level, detail,
                                   f"{before.version} → {after.version}"))

    # -- how the repository is used --------------------------------------------
    old_entries = {e.split(" = ")[0]: e for e in before.entry_points}
    new_entries = {e.split(" = ")[0]: e for e in after.entry_points}
    removed_entries = sorted(set(old_entries) - set(new_entries))
    added_entries = sorted(set(new_entries) - set(old_entries))
    if removed_entries:
        signals.append(DriftSignal(
            "entry_points", "major",
            f"{_n(len(removed_entries), 'way', 'ways')} of running the project removed",
            _sample([old_entries[name] for name in removed_entries]),
        ))
    if added_entries:
        signals.append(DriftSignal(
            "entry_points", "notable",
            f"{_n(len(added_entries), 'new way', 'new ways')} of running the project",
            _sample([new_entries[name] for name in added_entries]),
        ))

    # -- the public surface ----------------------------------------------------
    old_api, new_api = set(before.public_api), set(after.public_api)
    if old_api:
        removed = sorted(old_api - new_api)
        added = sorted(new_api - old_api)
        if removed:
            fraction = len(removed) / len(old_api)
            major = (
                fraction >= limits.api_removed_major_fraction
                or len(removed) >= limits.api_removed_major_count
            )
            signals.append(DriftSignal(
                "public_api_removed", "major" if major else "notable",
                f"{_n(len(removed), 'public symbol', 'public symbols')} removed "
                f"({fraction:.0%} of the {len(old_api)} it exposed)",
                _sample(removed),
            ))
        if added:
            fraction = len(added) / len(old_api)
            signals.append(DriftSignal(
                "public_api_added",
                "notable" if fraction >= limits.api_added_notable_fraction else "minor",
                f"{_n(len(added), 'public symbol', 'public symbols')} added "
                f"({fraction:.0%} growth)",
                _sample(added),
            ))

    # -- what the repository says it is for -------------------------------------
    old_purpose, new_purpose = _purpose_text(before), _purpose_text(after)
    if old_purpose and new_purpose and old_purpose != new_purpose:
        ratio = difflib.SequenceMatcher(None, old_purpose, new_purpose).ratio()
        if ratio < limits.purpose_major_similarity:
            level = "major"
        elif ratio < limits.purpose_notable_similarity:
            level = "notable"
        else:
            level = "minor"
        signals.append(DriftSignal(
            "purpose", level,
            f"how the repository describes its purpose changed "
            f"({ratio:.0%} similar to before)",
            f"before: {before.summary[:140]!r} · after: {after.summary[:140]!r}",
        ))
    elif old_purpose and not new_purpose:
        signals.append(DriftSignal(
            "purpose", "notable", "the repository no longer states its purpose",
            f"before: {before.summary[:140]!r}",
        ))

    # -- modules, dependencies, standards --------------------------------------
    old_modules = {m[0] for m in before.modules}
    new_modules = {m[0] for m in after.modules}
    gone = sorted(old_modules - new_modules)
    new = sorted(new_modules - old_modules)
    if gone:
        signals.append(DriftSignal(
            "modules_removed", "major" if len(gone) >= 3 else "notable",
            f"{_n(len(gone), 'documented module', 'documented modules')} removed",
            _sample(gone),
        ))
    if new:
        signals.append(DriftSignal(
            "modules_added", "notable" if len(new) >= 3 else "minor",
            f"{_n(len(new), 'documented module', 'documented modules')} added", _sample(new),
        ))

    dropped = sorted(set(before.dependencies) - set(after.dependencies))
    gained = sorted(set(after.dependencies) - set(before.dependencies))
    if dropped:
        signals.append(DriftSignal(
            "dependencies_removed", "notable",
            f"{_n(len(dropped), 'runtime dependency', 'runtime dependencies')} removed",
            _sample(dropped),
        ))
    if gained:
        signals.append(DriftSignal(
            "dependencies_added", "notable" if len(gained) >= 3 else "minor",
            f"{_n(len(gained), 'runtime dependency', 'runtime dependencies')} added",
            _sample(gained),
        ))

    if set(before.standards) != set(after.standards):
        changed = sorted(set(before.standards) ^ set(after.standards))
        signals.append(DriftSignal(
            "standards", "minor", "declared tooling changed", _sample(changed),
        ))

    # A dropped non-goal is the repository taking on something it said it would
    # not do; a dropped rule is a standard it no longer holds. Both loosen what it
    # promised, so both are notable. Adding either only narrows it: minor.
    def split(charter: Charter) -> tuple[set[str], set[str]]:
        texts = {c.text for c in charter.claims_of("constraint")}
        goals = {t.removeprefix(NON_GOAL) for t in texts if t.startswith(NON_GOAL)}
        return goals, {t for t in texts if not t.startswith(NON_GOAL)}

    (old_non_goals, old_rules), (new_non_goals, new_rules) = split(before), split(after)
    if lost := sorted(old_non_goals - new_non_goals):
        signals.append(DriftSignal(
            "non_goals", "notable",
            f"{_n(len(lost), 'stated non-goal', 'stated non-goals')} dropped — "
            f"it may now do what it said it would not",
            _sample(lost, 3),
        ))
    if found := sorted(new_non_goals - old_non_goals):
        signals.append(DriftSignal(
            "non_goals", "minor",
            f"{_n(len(found), 'new non-goal', 'new non-goals')} stated", _sample(found, 3),
        ))
    if lost := sorted(old_rules - new_rules):
        signals.append(DriftSignal(
            "constraints", "notable",
            f"{_n(len(lost), 'of its own rules', 'of its own rules')} dropped",
            _sample(lost, 3),
        ))
    if found := sorted(new_rules - old_rules):
        signals.append(DriftSignal(
            "constraints", "minor", f"{_n(len(found), 'new rule', 'new rules')} stated",
            _sample(found, 3),
        ))

    return CharterDrift(
        repo=after.repo, from_sha=before.sha, to_sha=after.sha, signals=tuple(signals),
    )


#: Which kind of change best explains an update, most telling first. A repository
#: whose stated purpose changed is best described by that, even if its version
#: number also moved.
_HEADLINE_ORDER = (
    "purpose", "toolchain", "entry_points", "public_api_removed", "license",
    "modules_removed", "version", "non_goals", "constraints", "dependencies_removed",
    "public_api_added", "modules_added", "dependencies_added", "standards",
)


def headline(drift: CharterDrift) -> DriftSignal:
    """The single signal that best explains an update, for a one-line title."""
    top = [s for s in drift.signals if s.level == drift.level]
    return min(
        top,
        key=lambda s: _HEADLINE_ORDER.index(s.kind) if s.kind in _HEADLINE_ORDER else 99,
    )


def evidence_lines(drift: CharterDrift) -> list[str]:
    """The notable and major signals, one line each, for a notification."""
    return [
        f"[{s.level}] {s.detail} — {s.evidence}" if s.evidence else f"[{s.level}] {s.detail}"
        for s in drift.signals
        if s.level in ("notable", "major")
    ]
