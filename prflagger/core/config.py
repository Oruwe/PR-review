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

__all__ = [
    "BrainConfig",
    "BudgetConfig",
    "CharterConfig",
    "Config",
    "RepoConfig",
    "ServerConfig",
    "cache_root",
    "load",
]

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
    #: "auto" uses a model when AWS credentials are present; "off" never does,
    #: whatever the environment holds; "on" treats missing credentials as an error
    #: to report on every run rather than a quiet skip.
    enabled: str = "auto"
    #: Most symbol groups adjudicated per run, highest-ranked first. The rest are
    #: named in the coverage statement as not adjudicated.
    max_adjudications: int = 8
    #: USD per million tokens: (model id, input, output). A model with no price
    #: here is refused, because a budget cannot be enforced on an unknown price.
    #: These defaults are list prices at the time of writing — check current
    #: Bedrock pricing for your region and override under [models.prices].
    prices: tuple[tuple[str, float, float], ...] = (
        ("anthropic.claude-haiku-4-5", 1.0, 5.0),
        ("anthropic.claude-sonnet-5", 3.0, 15.0),
        ("anthropic.claude-opus-5", 5.0, 25.0),
    )


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
class CharterConfig:
    """When a change to a repository counts as major.

    Every threshold is a statement about the repository's charter — what it
    exposes, what it says it is for — never about code style or size alone. A
    thousand-line refactor that leaves the public surface and the stated purpose
    intact is not a major update; deleting a fifth of the public API is.
    """

    api_removed_major_fraction: float = 0.2
    api_removed_major_count: int = 10
    api_added_notable_fraction: float = 0.3
    purpose_major_similarity: float = 0.6
    purpose_notable_similarity: float = 0.9
    #: Lowest level sent to the outbound webhook. Everything is recorded; this only
    #: decides what interrupts someone.
    webhook_min_level: str = "major"
    refresh_on_branch_move: bool = True


@dataclass(frozen=True)
class BrainConfig:
    """How much review history to read, and how often.

    Each merged pull request costs three API requests (listed, comments,
    commits). Without a token GitHub allows sixty an hour, so an unauthenticated
    first harvest reads only a handful and says so rather than stalling the
    watcher for an hour.
    """

    harvest_limit: int = 200  # closed PRs examined on a repository's first harvest
    incremental_limit: int = 100  # at most this many per refresh after that
    unauthenticated_limit: int = 15
    refresh_hours: float = 24.0
    min_support: int = 3  # comments behind a mined norm
    min_reviewers: int = 2  # distinct people behind it
    max_comments: int = 1500  # newest enforced comments considered when mining


@dataclass(frozen=True)
class ServerConfig:
    """The web surface."""

    host: str = "127.0.0.1"
    port: int = 8000
    poll_interval_s: int = 120
    event_retention_days: int = 7
    debounce_s: int = 30
    #: How many runs may wait at once, across all repositories. An unbounded
    #: queue under sustained load is a slower way to run out of memory, and a
    #: run admitted now that starts in six hours helps nobody.
    max_queue_depth: int = 200


@dataclass(frozen=True)
class Config:
    """The whole configuration."""

    repos: tuple[RepoConfig, ...] = ()
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    models: ModelConfig = field(default_factory=ModelConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    charter: CharterConfig = field(default_factory=CharterConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
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
    values = {k: v for k, v in data.items() if k in fields}
    if cls is ModelConfig and isinstance(values.get("prices"), dict):
        # [models.prices] "anthropic.x" = [input, output]; merged over the defaults.
        merged = {model: (inp, out) for model, inp, out in ModelConfig.prices}
        for model, pair in values["prices"].items():
            if isinstance(pair, list | tuple) and len(pair) == 2:
                merged[str(model)] = (float(pair[0]), float(pair[1]))
        values["prices"] = tuple((m, i, o) for m, (i, o) in sorted(merged.items()))
    return cls(**values)


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
        charter=_build(CharterConfig, _section(data, "charter")),
        brain=_build(BrainConfig, _section(data, "brain")),
        source=source,
    )
