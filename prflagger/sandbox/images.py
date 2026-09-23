"""Building the image a repo's tests run in.

v1 had one hardcoded `FROM python:3.11-slim` recipe, which is the single biggest
reason it only worked on Python repos. The recipe now comes from the toolchain
pack, but everything careful about v1's build survives unchanged:

  * the image is keyed on the repo's **lockfiles**, not its commit, so one build
    serves hundreds of runs;
  * this machine's TLS-terminating CA is staged in, or `pip`/`npm` inside the
    build sees a self-signed chain and every install fails;
  * binaries the suite shells out to are copied from the host with their shared
    libraries, because the build cannot always reach a package archive.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import structlog

from prflagger.core.config import cache_root
from prflagger.core.errors import ImageBuildError
from prflagger.lang.base import Toolchain

__all__ = ["build_image", "dockerfile_for", "image_key_for"]

log = structlog.get_logger(__name__)

#: Bumped when the recipe below changes. The lockfile hash describes the repo's
#: dependencies, not our recipe, so without this a stale image would be reused.
_RECIPE_VERSION = 4

_CA_LAYER = """\
COPY --from=prflagger_ca ca-bundle.crt /usr/local/share/ca-certificates/prflagger-proxy.crt
RUN update-ca-certificates
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt
"""

_BINARY_LAYER = """\
COPY --from=prflagger_bin bin/ /usr/local/bin/
COPY --from=prflagger_bin lib/ /usr/local/lib/prflagger/
ENV LD_LIBRARY_PATH=/usr/local/lib/prflagger
"""

_CA_ENV_VARS = ("PRFLAGGER_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "AWS_CA_BUNDLE")
_CA_FALLBACK = Path("/root/.ccr/ca-bundle.crt")

#: Never stage the C runtime: LD_LIBRARY_PATH would then point every binary in
#: the image at a foreign glibc.
_CORE_LIBS = ("libc.", "libm.", "libpthread.", "libdl.", "librt.", "ld-linux")


def image_key_for(
    repo_path: Path, toolchain: Toolchain, *, system_binaries: Sequence[str] = ()
) -> str:
    """sha256 over the pack, the repo's lockfiles, and the staged binaries.

    Two commits with identical dependencies share an image. Keying on the commit
    instead would destroy iteration speed — most commits do not touch deps.
    """
    digest = hashlib.sha256()
    digest.update(toolchain.id.encode("utf-8"))
    digest.update(toolchain.base_image.encode("utf-8"))
    for line in toolchain.setup_lines:
        digest.update(line.encode("utf-8"))
    for command in toolchain.install:
        digest.update(shlex.join(command).encode("utf-8"))
    for name in toolchain.lockfiles:
        candidate = repo_path / name
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        if candidate.is_file():
            digest.update(candidate.read_bytes())
        digest.update(b"\0")
    for name in sorted(system_binaries):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
    digest.update(str(_RECIPE_VERSION).encode("utf-8"))
    return digest.hexdigest()


def dockerfile_for(
    toolchain: Toolchain,
    *,
    package_roots: Sequence[str] = (),
    has_ca: bool = False,
    has_binaries: bool = False,
    repo_path: Path | None = None,
) -> str:
    """The recipe. Deterministic, so the same inputs rebuild the same image."""
    lines = [f"FROM {toolchain.base_image}"]
    if has_ca:
        lines.append(_CA_LAYER.rstrip())
    if has_binaries:
        lines.append(_BINARY_LAYER.rstrip())
    lines.extend(toolchain.setup_lines)

    if toolchain.install:
        # Dependencies are installed from a build-time copy so the layer caches;
        # the worktree under test is mounted read-only at run time instead.
        lines.append("COPY . /build")
        lines.append("WORKDIR /build")
        for command in toolchain.install:
            # `|| true`: a repo whose install is partially broken should still get
            # a usable image and an honest INSTALL_FAILED from the test run, not a
            # build failure that reports nothing at all.
            lines.append(f"RUN {shlex.join(command)} || true")

    for key, value in toolchain.env:
        lines.append(f"ENV {key}={value}")

    if toolchain.grammar == "python" or toolchain.id == "python":
        lines.append(f"ENV PYTHONPATH={_pythonpath(package_roots, repo_path)}")

    lines.append("WORKDIR /src")
    return "\n".join(lines) + "\n"


def _pythonpath(package_roots: Sequence[str], repo_path: Path | None) -> str:
    """Put the mounted worktree ahead of site-packages so the checkout wins.

    The image installs the project to get its dependencies; without this the
    tests import that build-time copy and base and head are indistinguishable.
    """
    for root in package_roots:
        parent = Path(root).parent
        if parent != Path("."):
            return f"/src/{parent.as_posix()}:/src"
    if repo_path is not None and (repo_path / "src").is_dir():
        return "/src/src:/src"
    return "/src"


def build_image(
    repo_path: Path,
    toolchain: Toolchain,
    *,
    image_key: str | None = None,
    package_roots: Sequence[str] = (),
    system_binaries: Sequence[str] = (),
    timeout_s: int = 1800,
) -> str:
    """Build, or reuse, the image for this repo. Returns the tag."""
    key = image_key or image_key_for(repo_path, toolchain, system_binaries=system_binaries)
    tag = f"prflagger:{key[:32]}"
    if _image_exists(tag):
        log.debug("image.reused", tag=tag, pack=toolchain.id)
        return tag

    ca_dir = _stage_ca_bundle()
    binary_dir = _stage_system_binaries(key, system_binaries)
    recipe = dockerfile_for(
        toolchain,
        package_roots=package_roots,
        has_ca=ca_dir is not None,
        has_binaries=binary_dir is not None,
        repo_path=repo_path,
    )
    path = cache_root() / "images" / f"{key[:32]}.Dockerfile"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(recipe, encoding="utf-8")

    # --network=host: the build installs dependencies, so it is the one step that
    # needs the network. Every *run* is --network=none.
    argv = ["build", "--network=host", "-f", str(path), "-t", tag]
    if ca_dir:
        argv += ["--build-context", f"prflagger_ca={ca_dir}"]
    if binary_dir:
        argv += ["--build-context", f"prflagger_bin={binary_dir}"]
    argv.append(str(repo_path))

    completed = _docker(argv, timeout_s=timeout_s)
    if completed.returncode != 0:
        raise ImageBuildError(tag, completed.stdout, completed.stderr)
    log.info("image.built", tag=tag, pack=toolchain.id)
    return tag


# ----------------------------------------------------------------------------------
# Staging
# ----------------------------------------------------------------------------------


def _readable_file(path: Path) -> bool:
    """`path.is_file()`, but a permission error means "not usable," not a crash."""
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


def _stage_system_binaries(key: str, names: Sequence[str]) -> Path | None:
    """Stage declared binaries with the shared libraries they need."""
    if not names:
        return None
    staged = cache_root() / "images" / f"{key[:32]}-bin"
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


def _image_exists(tag: str) -> bool:
    return _docker(["image", "inspect", tag], timeout_s=60).returncode == 0


def _docker(argv: Sequence[str], *, timeout_s: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603
            ["docker", *argv], capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired as expired:
        return subprocess.CompletedProcess(
            args=["docker", *argv],
            returncode=124,
            stdout=expired.stdout.decode("utf-8", "replace") if expired.stdout else "",
            stderr=expired.stderr.decode("utf-8", "replace") if expired.stderr else "",
        )
