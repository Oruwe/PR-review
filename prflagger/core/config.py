"""Configuration: every knob in one place, loaded once.

v1 scattered its settings across module constants (`_MAX_SYMBOLS`,
`SIMILARITY_THRESHOLD`, `_OUTER_GRACE_S`) and a single-target `config.toml`.
A service that watches many repos cannot work that way, so everything tunable
lives here and is read from `config.toml` with documented defaults.

No credential is ever read from, or written to, this file or this repository.
Tokens come from the environment, the way the host machine normally supplies
them.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

__all__ = ["BudgetConfig", "Config", "RepoConfig", "ServerConfig", "load", "cache_root"]

_CACHE_ENV = "PRFLAGGER_CACHE_DIR"


def cache_root() -> Path:
    """`.cache`, or `PRFLAGGER_CACHE_DIR` when set. Always absolute.

    A relative cache path handed to `git -C <repo>` or `docker -v` resolves
    against *that* directory, which silently plants worktrees inside the repo
    under analysis. Resolving here is what stops that.
    """
    return Path(os.environ.get(_CACHE_ENV, ".cache")).resolve()


@dataclass(frozen=True)
class RepoConfig:
    """One watched repository."""

    slug: str
    default_branch: str = "main"
    #: Where to clone from. Empty means https://github.com/<slug>. Set it for a
    #: self-hosted server, a mirror, or a local path.
    clone_url: str = ""
    toolchain: str = ""  # "" -> detect from marker files
    package_roots: tuple[str, ...] = ()
    system_binaries: tuple[str, ...] = ()
    watch: bool = True
    max_prs: int = 25  # how many open PRs the watcher tracks


@dataclass(frozen=True)
class BudgetConfig:
    """Hard spending caps. A call that would breach one is refused, not trimmed.

    `total_usd` defaults to the credit this project was built against. Spend is
    measured from the provider's own `usage` object, never estimated.
    """

    total_usd: float = 100.0
    per_run_usd: float = 0.25
    per_repo_daily_usd: float = 2.00
    warn_at_fraction: float = 0.8


@dataclass(frozen=True)
class ModelConfig:
    """Which model does what. Routing is the main cost lever after caching."""

    # Cheap and fast: extraction, classification, norm naming.
    light: str = "anthropic.claude-haiku-4-5"
    # Judgement: adjudication and characterization-test generation.
    heavy: str = "anthropic.claude-sonnet-5"
    region: str = "us-east-1"
    max_tokens: int = 8192
    cache_ttl: str = "1h"


@dataclass(frozen=True)
class SandboxConfig:
    """Isolation and resource bounds for every containerised run."""

    max_concurrent: int = 0  # 0 -> min(2, cpu_count // 2)
    default_timeout_s: int = 300
    default_memory_mb: int = 1024
    pids_limit: int = 256
    cpus: float = 1.0
    tmpfs_size_mb: int = 256
    outer_grace_s: int = 30
    max_log_lines: int = 50_000
    max_log_bytes: int = 8 * 1024 * 1024
    sample_interval_s: float = 1.0

    def resolved_concurrency(self) -> int:
        if self.max_concurrent > 0:
            return self.max_concurrent
        return max(1, min(2, (os.cpu_count() or 2) // 2))


@dataclass(frozen=True)
class ServerConfig:
    """The web surface."""

    host: str = "127.0.0.1"
    port: int = 8000
    poll_interval_s: int = 120
    event_retention_days: int = 7
    debounce_s: int = 30


@dataclass(frozen=True)
class Config:
    """The whole configuration."""

    repos: tuple[RepoConfig, ...] = ()
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    models: ModelConfig = field(default_factory=ModelConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    source: Path | None = None

    def repo(self, slug: str) -> RepoConfig:
        """The config for `slug`, or a default one if it is not declared.

        An undeclared repo is not an error: the service can be pointed at
        anything at runtime, and detection fills in what config would have.
        """
        for entry in self.repos:
            if entry.slug == slug:
                return entry
        return RepoConfig(slug=slug)

    def with_repo(self, entry: RepoConfig) -> Config:
        """This config plus (or replacing) one repo."""
        others = tuple(r for r in self.repos if r.slug != entry.slug)
        return replace(self, repos=(*others, entry))

    @property
    def db_path(self) -> Path:
        return cache_root() / "prflagger.db"


def _as_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return ()


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    section = data.get(name, {})
    return section if isinstance(section, dict) else {}


def _build(cls: type, data: dict[str, Any]) -> Any:
    """Construct `cls` from `data`, ignoring keys it does not declare.

    An unknown key is skipped rather than raised: a config written for a newer
    version should still start an older binary.
    """
    fields = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return cls(**{k: v for k, v in data.items() if k in fields})


def load(path: Path | str = "config.toml") -> Config:
    """Read the configuration. A missing file yields documented defaults."""
    source = Path(path)
    if not source.is_file():
        return Config(source=None)
    try:
        data = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"could not read {source}: {error}") from error

    repos: list[RepoConfig] = []
    for entry in data.get("repos", []) or []:
        if not isinstance(entry, dict) or "slug" not in entry:
            continue
        repos.append(
            RepoConfig(
                slug=str(entry["slug"]),
                default_branch=str(entry.get("default_branch", "main")),
                clone_url=str(entry.get("clone_url", "")),
                toolchain=str(entry.get("toolchain", "")),
                package_roots=_as_tuple(entry.get("package_roots")),
                system_binaries=_as_tuple(entry.get("system_binaries")),
                watch=bool(entry.get("watch", True)),
                max_prs=int(entry.get("max_prs", 25)),
            )
        )

    # v1 wrote a single [target]; keep reading it so an existing config still works.
    target = _section(data, "target")
    if target.get("slug") and not any(r.slug == target["slug"] for r in repos):
        repos.append(
            RepoConfig(
                slug=str(target["slug"]),
                package_roots=_as_tuple(target.get("package_root")),
                system_binaries=_as_tuple(target.get("system_binaries")),
            )
        )

    return Config(
        repos=tuple(repos),
        budget=_build(BudgetConfig, _section(data, "budget")),
        models=_build(ModelConfig, _section(data, "models")),
        sandbox=_build(SandboxConfig, _section(data, "sandbox")),
        server=_build(ServerConfig, _section(data, "server")),
        source=source,
    )
