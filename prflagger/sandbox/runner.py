"""Docker execution with typed outcomes.

`run_job` never raises for job failure. A timeout is not an error to swallow — it is a
PR that may have introduced an infinite loop, which is a finding. Same for OOM.

Worktrees: one bare clone per repo under `.cache/repos/`, `git worktree add` per commit.
Never re-clone. Never share a worktree between concurrent jobs.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import tomllib
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

import structlog

from prflagger.gitsafety import assert_safe_revision
from prflagger.models import Job, Outcome, TestResult

__all__ = [
    "ImageBuildError",
    "LOCKFILE_NAMES",
    "build_image",
    "bare_clone",
    "lockfile_image_key",
    "run_job",
    "worktree_for",
]

log = structlog.get_logger(__name__)

# The files whose contents define an image. Most commits do not change dependencies, so
# one build serves hundreds of runs — keying on the commit instead would destroy
# iteration speed.
LOCKFILE_NAMES = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "requirements-dev.txt",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
)

# The container carries its own `timeout`; this outer bound only stops a wedged docker
# client from hanging the run forever.
_OUTER_GRACE_S = 30

# Bumped whenever the invocation changes. `Job.idempotency_key` covers the job, not the
# runner, so without this a cached result outlives the flags that produced it.
_INVOCATION_VERSION = 2

# Bumped when the Dockerfile changes. The lockfile hash describes the repo's
# dependencies, not our recipe, so without this a stale image would be reused.
_IMAGE_RECIPE_VERSION = 2

_DOCKERFILE = """\
FROM python:3.11-slim
{ca_layer}{binary_layer}\
RUN pip install --no-cache-dir --disable-pip-version-check \
    pytest pytest-json-report pytest-cov coverage ruff mypy
COPY . /build
RUN pip install --no-cache-dir --disable-pip-version-check /build
ENV PYTHONPATH={pythonpath}
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /src
"""

# Only emitted when this machine terminates TLS with its own CA. Without it pip inside
# the build sees a self-signed chain and every install fails.
_CA_LAYER = """\
COPY --from=prflagger_ca ca-bundle.crt /usr/local/share/ca-certificates/prflagger-proxy.crt
RUN update-ca-certificates
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
"""

_CA_ENV_VARS = ("PRFLAGGER_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE")
_CA_FALLBACK = Path("/root/.ccr/ca-bundle.crt")

# Real binaries the target's suite shells out to, staged from this machine because the
# image cannot always reach a package archive. Declared in config.toml.
_BINARY_LAYER = """\
COPY --from=prflagger_bin bin/ /usr/local/bin/
COPY --from=prflagger_bin lib/ /usr/local/lib/prflagger/
ENV LD_LIBRARY_PATH=/usr/local/lib/prflagger
"""

# Never stage the C runtime: LD_LIBRARY_PATH would then point every binary in the image
# at a foreign glibc.
_CORE_LIBS = ("libc.", "libm.", "libpthread.", "libdl.", "librt.", "ld-linux")


class ImageBuildError(RuntimeError):
    """A build that failed. `run_job` turns this into `Outcome.INSTALL_FAILED`."""

    def __init__(self, tag: str, stdout: str, stderr: str) -> None:
        super().__init__(f"image build failed: {tag}")
        self.tag = tag
        self.stdout = stdout
        self.stderr = stderr


def cache_root() -> Path:
    """`.cache`, or `PRFLAGGER_CACHE_DIR` when that is set.

    Always absolute. A relative cache path handed to `git -C <repo>` or `docker -v`
    resolves against *that* directory, which silently plants worktrees inside the repo
    under analysis.
    """
    return Path(os.environ.get("PRFLAGGER_CACHE_DIR", ".cache")).resolve()


# ----------------------------------------------------------------------------------
# Images
# ----------------------------------------------------------------------------------


def lockfile_image_key(repo_path: Path) -> str:
    """sha256 over the repo's lockfile set. Two commits with equal deps share an image."""
    digest = hashlib.sha256()
    for name in LOCKFILE_NAMES:
        candidate = repo_path / name
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        if candidate.is_file():
            digest.update(candidate.read_bytes())
        digest.update(b"\0")
    for name in _configured_system_binaries():
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
    digest.update(str(_IMAGE_RECIPE_VERSION).encode("utf-8"))
    return digest.hexdigest()


def build_image(repo_path: Path, image_key: str) -> str:
    """Build (or reuse) a Docker image with the repo's dependencies installed.

    image_key = sha256 of the lockfile set. Returns the image tag.
    """
    tag = f"prflagger:{image_key[:32]}"
    if _image_exists(tag):
        log.debug("image.reused", tag=tag)
        return tag

    ca_dir = _stage_ca_bundle()
    binary_dir = _stage_system_binaries(image_key)
    dockerfile = cache_root() / "images" / f"{image_key[:32]}.Dockerfile"
    dockerfile.parent.mkdir(parents=True, exist_ok=True)
    dockerfile.write_text(
        _DOCKERFILE.format(
            pythonpath=_pythonpath_for(repo_path),
            ca_layer=_CA_LAYER if ca_dir else "",
            binary_layer=_BINARY_LAYER if binary_dir else "",
        ),
        encoding="utf-8",
    )

    # --network=host: the build installs dependencies, so it is the one step that needs
    # the network. Every *run* is --network=none.
    argv = ["build", "--network=host", "-f", str(dockerfile), "-t", tag]
    if ca_dir:
        argv += ["--build-context", f"prflagger_ca={ca_dir}"]
    if binary_dir:
        argv += ["--build-context", f"prflagger_bin={binary_dir}"]
    argv.append(str(repo_path))
    completed = _docker(argv, timeout_s=1800)
    if completed.returncode != 0:
        raise ImageBuildError(tag, completed.stdout, completed.stderr)
    log.info("image.built", tag=tag)
    return tag


def _readable_file(path: Path) -> bool:
    """`path.is_file()`, but a permission error means "not usable," not a crash.

    `_CA_FALLBACK` is this one machine's proxy CA path. On any other machine
    (including a normal CI runner) that directory can exist but be owned by a
    different user entirely — `.is_file()` itself raises `PermissionError`
    rather than returning False. Either way the answer this function needs is
    the same: this system has no CA bundle to stage.
    """
    try:
        return path.is_file()
    except OSError:
        return False


def _stage_ca_bundle() -> Path | None:
    """Put this machine's CA where the build can COPY it, or None if there is no CA."""
    for variable in _CA_ENV_VARS:
        raw = os.environ.get(variable)
        if raw and _readable_file(Path(raw)):
            source = Path(raw)
            break
    else:
        if not _readable_file(_CA_FALLBACK):
            return None
        source = _CA_FALLBACK

    staged = cache_root() / "images" / "ca"
    staged.mkdir(parents=True, exist_ok=True)
    (staged / "ca-bundle.crt").write_bytes(source.read_bytes())
    return staged


def _stage_system_binaries(image_key: str) -> Path | None:
    """Stage the binaries config.toml declares, with the shared libraries they need."""
    names = _configured_system_binaries()
    if not names:
        return None

    staged = cache_root() / "images" / f"{image_key[:32]}-bin"
    (staged / "bin").mkdir(parents=True, exist_ok=True)
    (staged / "lib").mkdir(parents=True, exist_ok=True)

    found = False
    for name in names:
        located = shutil.which(name)
        if located is None:
            log.warning("system_binary.missing", binary=name)
            continue
        source = Path(located)
        shutil.copy2(source, staged / "bin" / source.name)
        for library in _shared_libraries(source):
            shutil.copy2(library, staged / "lib" / library.name)
        found = True
    return staged if found else None


def _shared_libraries(binary: Path) -> list[Path]:
    completed = subprocess.run(  # noqa: S603
        ["ldd", str(binary)], capture_output=True, text=True, check=False
    )
    libraries: list[Path] = []
    for line in completed.stdout.splitlines():
        if "=>" not in line:
            continue
        target = line.split("=>", 1)[1].strip().split(" ")[0]
        if not target.startswith("/"):
            continue
        path = Path(target)
        if any(path.name.startswith(core) for core in _CORE_LIBS) or not path.is_file():
            continue
        libraries.append(path)
    return libraries


def _configured_system_binaries() -> tuple[str, ...]:
    raw = _target_config().get("system_binaries", [])
    return tuple(str(item) for item in raw) if isinstance(raw, list) else ()


def _pythonpath_for(repo_path: Path) -> str:
    """Put the mounted worktree ahead of site-packages so the checkout under test wins.

    The image installs the project to get its dependencies; without this the tests would
    import that build-time copy and base and head would be indistinguishable.
    """
    package_root = _configured_package_root(repo_path)
    if package_root and package_root.parent != Path("."):
        return f"/src/{package_root.parent.as_posix()}:/src"
    return "/src/src:/src" if (repo_path / "src").is_dir() else "/src"


def _configured_package_root(repo_path: Path) -> Path | None:
    raw = _target_config().get("package_root")
    return Path(str(raw)) if raw else None


def _target_config() -> dict[str, Any]:
    config = Path("config.toml")
    if not config.is_file():
        return {}
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    target = data.get("target", {})
    return target if isinstance(target, dict) else {}


def _image_exists(tag: str) -> bool:
    return _docker(["image", "inspect", tag], timeout_s=60).returncode == 0


# ----------------------------------------------------------------------------------
# Running
# ----------------------------------------------------------------------------------


def run_job(job: Job) -> TestResult:
    """Execute job in a container. Never raises for job failure."""
    cached = _read_result(job)
    if cached is not None:
        log.debug("job.cache_hit", key=job.idempotency_key[:12])
        return cached

    try:
        image = build_image(Path(job.repo_path), job.image_key)
    except ImageBuildError as error:
        # Cannot verify this repo — say so explicitly rather than skipping silently.
        result = TestResult(
            outcome=Outcome.INSTALL_FAILED,
            per_test={},
            duration_s=0.0,
            peak_rss_mb=None,
            stdout=error.stdout,
            stderr=error.stderr,
        )
        _write_result(job, result)
        return result

    started = time.monotonic()
    completed = _docker(_run_argv(job, image), timeout_s=job.timeout_s + _OUTER_GRACE_S)
    duration = time.monotonic() - started

    report = _extract_report(completed.stdout)
    result = TestResult(
        outcome=_classify(completed.returncode, report),
        per_test=_per_test(report),
        duration_s=duration,
        peak_rss_mb=None,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    _write_result(job, result)
    log.info(
        "job.done",
        key=job.idempotency_key[:12],
        outcome=result.outcome.value,
        tests=len(result.per_test),
    )
    return result


def _run_argv(job: Job, image: str) -> list[str]:
    """The exact invocation SPEC.md § C1 mandates."""
    return [
        "run",
        "--rm",
        "--network=none",
        f"--memory={job.memory_mb}m",
        f"--memory-swap={job.memory_mb}m",
        "--pids-limit=256",
        "--cpus=1",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,exec,size=256m",
        "--user",
        "1000:1000",
        "--security-opt",
        "no-new-privileges",
        "--cap-drop=ALL",
        "-v",
        f"{job.repo_path}:/src:ro",
        image,
        "timeout",
        str(job.timeout_s),
        *job.command,
    ]


def _classify(returncode: int, report: dict[str, Any] | None) -> Outcome:
    """SPEC.md § C1's mapping. A timeout and an OOM are results, not errors."""
    if returncode == 0:
        return Outcome.PASSED
    if returncode == 124:
        return Outcome.TIMEOUT
    if returncode == 137:
        return Outcome.OOM
    if returncode == 2:
        return Outcome.COLLECTION_ERROR
    if returncode == 125:
        # docker itself could not start the container.
        return Outcome.INSTALL_FAILED
    if returncode == 1:
        return Outcome.FAILED
    return Outcome.FAILED if report is not None else Outcome.FAILED


def _per_test(report: dict[str, Any] | None) -> dict[str, str]:
    if report is None:
        return {}
    tests = report.get("tests")
    if not isinstance(tests, list):
        return {}
    return {
        str(entry["nodeid"]): str(entry.get("outcome", "error"))
        for entry in tests
        if isinstance(entry, dict) and "nodeid" in entry
    }


def _extract_report(stdout: str) -> dict[str, Any] | None:
    """Pull the pytest-json-report object out of stdout.

    The container root is read-only, so the report is written to /dev/stdout rather than
    a file that would vanish with the container.
    """
    decoder = json.JSONDecoder()
    for index, character in enumerate(stdout):
        if character != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(stdout, index)
        except ValueError:
            continue
        if isinstance(candidate, dict) and "tests" in candidate:
            return candidate
    return None


def _docker(argv: Sequence[str], *, timeout_s: float) -> subprocess.CompletedProcess[str]:
    """The single point where this process shells out to Docker."""
    try:
        return subprocess.run(  # noqa: S603
            ["docker", *argv],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as expired:
        return subprocess.CompletedProcess(
            args=["docker", *argv],
            returncode=124,
            stdout=_as_text(expired.stdout),
            stderr=_as_text(expired.stderr),
        )


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


# ----------------------------------------------------------------------------------
# Result cache — identical work deduplicates, so re-runs during iteration are free
# ----------------------------------------------------------------------------------


def _result_path(job: Job) -> Path:
    return cache_root() / "jobs" / f"{job.idempotency_key}.json"


def _read_result(job: Job) -> TestResult | None:
    try:
        raw = _result_path(job).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        entry = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if entry.get("invocation_version") != _INVOCATION_VERSION:
        return None  # produced by a different invocation; re-run rather than mislead
    try:
        return TestResult(
            outcome=Outcome(entry["outcome"]),
            per_test=dict(entry["per_test"]),
            duration_s=float(entry["duration_s"]),
            peak_rss_mb=entry["peak_rss_mb"],
            stdout=entry["stdout"],
            stderr=entry["stderr"],
        )
    except (KeyError, TypeError, ValueError):
        return None


def _write_result(job: Job, result: TestResult) -> None:
    path = _result_path(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(result)
    payload["outcome"] = result.outcome.value
    payload["job"] = {**asdict(job), "command": list(job.command)}
    payload["invocation_version"] = _INVOCATION_VERSION
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


# ----------------------------------------------------------------------------------
# Worktrees
# ----------------------------------------------------------------------------------


def bare_clone(slug: str, *, url: str | None = None) -> Path:
    """One bare clone per repo under `.cache/repos/`. Never re-cloned."""
    path = cache_root() / "repos" / f"{slug.replace('/', '__')}.git"
    if path.is_dir():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    source = url or f"https://github.com/{slug}"
    _git(["clone", "--bare", source, str(path)], cwd=None, remote=source)
    return path


def worktree_for(slug: str, commit: str, *, url: str | None = None) -> Path:
    """`git worktree add` per commit. Never shared between concurrent jobs."""
    assert_safe_revision(commit)
    bare = bare_clone(slug, url=url)
    path = cache_root() / "worktrees" / slug.replace("/", "__") / commit
    if path.is_dir():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    if _git(["cat-file", "-e", f"{commit}^{{commit}}"], cwd=bare).returncode != 0:
        _git(["fetch", "origin", "--quiet"], cwd=bare, remote=url or f"https://github.com/{slug}")
    _git(["worktree", "add", "--detach", "--quiet", str(path), commit], cwd=bare)
    return path


def _git(
    argv: Sequence[str], *, cwd: Path | None, remote: str | None = None
) -> subprocess.CompletedProcess[str]:
    from prflagger.vcs.credentials import run_git

    command = ["git"] if cwd is None else ["git", f"--git-dir={cwd}"]
    return run_git([*command, *argv], remote=remote, timeout_s=1800)
