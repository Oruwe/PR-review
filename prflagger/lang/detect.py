"""Which toolchain a repo uses.

Detection is marker-file scoring, never an LLM: CLAUDE.md is explicit that
structure comes from the filesystem and `git`. Configuration always beats
detection, so a repo that does something unusual is a config line rather than a
patch to this module.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from prflagger.core.config import Config, RepoConfig
from prflagger.lang.base import TestCommand, Toolchain
from prflagger.lang.generic import GENERIC, discover_commands
from prflagger.lang.go import GO
from prflagger.lang.node import NODE
from prflagger.lang.python import PYTHON

__all__ = ["PACKS", "detect", "for_repo", "pack", "packs"]

log = structlog.get_logger(__name__)

#: Order matters only for display; scoring picks the winner.
PACKS: tuple[Toolchain, ...] = (PYTHON, NODE, GO, GENERIC)


def packs() -> tuple[Toolchain, ...]:
    return PACKS


def pack(pack_id: str) -> Toolchain | None:
    for candidate in PACKS:
        if candidate.id == pack_id:
            return candidate
    return None


def _score(candidate: Toolchain, repo_path: Path) -> int:
    """How strongly this repo looks like `candidate`.

    A marker at the repo root counts double: a `package.json` beside the code is
    the project's own, while one three levels down is probably a fixture.
    """
    score = 0
    for marker in candidate.markers:
        if (repo_path / marker).is_file():
            score += 2
        elif any(repo_path.glob(f"*/{marker}")):
            score += 1
    return score


def detect(repo_path: Path) -> Toolchain:
    """The best-matching pack, or the generic one wired to this repo's own CI."""
    best, best_score = GENERIC, 0
    for candidate in PACKS:
        if candidate is GENERIC:
            continue
        score = _score(candidate, repo_path)
        if score > best_score:
            best, best_score = candidate, score

    if best_score > 0:
        log.debug("toolchain.detected", pack=best.id, score=best_score)
        return best

    return _generic_for(repo_path)


def _generic_for(repo_path: Path) -> Toolchain:
    """The generic pack, pointed at whatever this repo's CI already runs."""
    from dataclasses import replace

    commands = discover_commands(repo_path)
    if not commands:
        log.warning("toolchain.undetected", path=str(repo_path))
        return GENERIC
    log.info("toolchain.generic", commands=len(commands))
    return replace(GENERIC, test=TestCommand(argv=commands[0]))


def for_repo(
    repo_path: Path, *, config: Config | None = None, slug: str | None = None
) -> Toolchain:
    """The pack for this repo. A configured `toolchain` wins over detection."""
    entry: RepoConfig | None = None
    if config is not None and slug:
        entry = config.repo(slug)
    if entry and entry.toolchain:
        chosen = pack(entry.toolchain)
        if chosen is not None:
            return chosen
        log.warning("toolchain.unknown_configured", requested=entry.toolchain)
    return detect(repo_path)
