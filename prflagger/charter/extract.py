"""Building a repository's charter from its own words.

A charter answers four questions — what is this repository's goal, who is it
for, what does it do, and what rules does it hold itself to — using nothing but
what the repository says about itself: its README, its package metadata, its
entry points, the docstrings at the top of its modules, its public API, its
declared dependencies and tooling, and its own contributing notes.

Nothing here is inferred or generated. Every claim carries the `path:line` it
came from, and `Claim` refuses to exist without one. That is what makes the
charter safe to judge against: it cannot contain an opinion the repository does
not hold, or a fact about some other repository.
"""

from __future__ import annotations

import ast
import json
import re
import time
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import structlog

from prflagger.core.models import Charter, Claim
from prflagger.lang.base import Toolchain

__all__ = ["extract_charter"]

log = structlog.get_logger(__name__)

_README_NAMES = ("README.md", "README.rst", "README.txt", "README", "readme.md", "Readme.md")
_GUIDANCE_NAMES = (
    "CONTRIBUTING.md", "docs/CONTRIBUTING.md", ".github/CONTRIBUTING.md",
    "CLAUDE.md", "AGENTS.md",
)

#: README headings, mapped to the kind of claim the section under them makes.
_SECTION_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("purpose", ("about", "overview", "introduction", "why", "motivation", "goal",
                 "goals", "purpose", "what is", "philosophy", "mission", "background")),
    ("capability", ("feature", "features", "capabilities", "highlights",
                    "functionality", "what it does", "what you get")),
    ("target", ("who is this for", "who it is for", "who it's for", "who should use",
                "who uses", "intended audience", "audience", "target", "use cases",
                "requirements", "supported", "compatibility", "platforms", "prerequisites")),
    ("constraint", ("non-goals", "non goals", "anti-goals", "what it is not",
                    "what this is not", "limitations", "scope", "out of scope",
                    "design principles", "principles", "rules", "conventions")),
)

#: Sections that list what a repository deliberately does *not* do. Their bullets
#: are constraints too, but quoted bare ("Currency conversion") they read as if
#: they were features, so each is labelled.
_NON_GOAL_KEYS = ("non-goals", "non goals", "anti-goals", "what it is not",
                  "what this is not", "out of scope")
NON_GOAL = "Not a goal: "

_CAPS = {"purpose": 12, "capability": 40, "target": 12, "constraint": 16, "summary": 1}

_STANDARD_FILES = {
    "ruff": ("ruff.toml", ".ruff.toml"),
    "mypy": ("mypy.ini", ".mypy.ini"),
    "pytest": ("pytest.ini",),
    "eslint": (".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.cjs",
               ".eslintrc.yml", "eslint.config.js", "eslint.config.mjs"),
    "typescript": ("tsconfig.json",),
    "prettier": (".prettierrc", ".prettierrc.json", "prettier.config.js"),
    "golangci-lint": (".golangci.yml", ".golangci.yaml"),
    "rustfmt": ("rustfmt.toml", ".rustfmt.toml"),
    "clippy": ("clippy.toml",),
    "pre-commit": (".pre-commit-config.yaml",),
    "editorconfig": (".editorconfig",),
}

_BADGE = re.compile(r"^\s*(\[!\[|!\[|<|\.\. image|\.\. \||\|)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_NOISE = re.compile(r"[*_`]{1,3}")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


class _Builder:
    """Accumulates claims, enforcing caps and dropping exact duplicates."""

    def __init__(self) -> None:
        self.claims: list[Claim] = []
        self._seen: set[tuple[str, str]] = set()
        self._counts: dict[str, int] = {}

    def add(self, kind: str, text: str, source: str) -> None:
        cleaned = _clean(text)
        if len(cleaned) < 3 or not source:
            return
        key = (kind, cleaned.lower())
        if key in self._seen or self._counts.get(kind, 0) >= _CAPS[kind]:
            return
        self._seen.add(key)
        self._counts[kind] = self._counts.get(kind, 0) + 1
        self.claims.append(Claim(kind=kind, text=cleaned, source=source))


def _clean(text: str) -> str:
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_NOISE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(":")
    return text[:300] + ("…" if len(text) > 300 else "")


def _line_of(text: str, needle: str) -> int:
    """1-based line of the first occurrence of `needle`, or 1 if absent."""
    index = text.find(needle)
    return text.count("\n", 0, index) + 1 if index >= 0 else 1


def _src(file: str, raw: str, needle: str) -> str:
    """`file:line` of `needle` in `raw` — the citation a metadata claim carries."""
    return f"{file}:{_line_of(raw, needle)}"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ----------------------------------------------------------------------------------
# The entry point
# ----------------------------------------------------------------------------------


def extract_charter(
    tree: Path,
    *,
    slug: str,
    sha: str,
    toolchain: Toolchain,
    package_roots: Sequence[str] = (),
    public_api: Sequence[str] | None = None,
    max_api_files: int = 25_000,
) -> Charter:
    """The charter of the repository checked out at `tree`.

    `public_api` may be supplied by a caller that already indexed the symbols —
    the atlas, or a run's surface probe — so the work is not done twice.
    """
    builder = _Builder()
    meta: dict[str, Any] = {
        "name": "", "summary": "", "summary_source": "", "version": "", "license": "",
        "entry_points": [], "dependencies": set(), "standards": set(),
    }

    _from_pyproject(tree, builder, meta)
    _from_setup_cfg(tree, builder, meta)
    _from_package_json(tree, builder, meta)
    _from_go_mod(tree, builder, meta)
    _from_cargo(tree, builder, meta)
    _from_readme(tree, builder, meta)
    _from_guidance(tree, builder)
    _from_standard_files(tree, meta)
    _from_license_file(tree, meta)
    _from_go_commands(tree, meta)

    modules = _module_docstrings(tree, toolchain, package_roots)
    for module, line, source in modules:
        builder.add("capability", f"{module}: {line}", source)

    if public_api is None:
        public_api = _public_api(tree, toolchain, package_roots, max_api_files)
    if (tree / "src").is_dir() or package_roots:
        for root in _python_roots(tree, package_roots):
            if (root / "__main__.py").is_file():
                meta["entry_points"].append(f"python -m {root.name}")

    name = meta["name"] or slug.rsplit("/", 1)[-1]
    summary = meta["summary"]
    if summary:
        builder.claims.insert(0, Claim(kind="summary", text=_clean(summary),
                                       source=meta["summary_source"]))

    charter = Charter(
        repo=slug,
        sha=sha,
        name=name,
        summary=_clean(summary) if summary else "",
        claims=tuple(builder.claims),
        version=str(meta["version"]),
        license=str(meta["license"]),
        toolchain=toolchain.id,
        entry_points=tuple(sorted(set(meta["entry_points"]))),
        modules=tuple(modules),
        public_api=tuple(sorted(set(public_api))),
        dependencies=tuple(sorted(meta["dependencies"])),
        standards=tuple(sorted(meta["standards"])),
        built_at=time.time(),
    )
    log.info(
        "charter.extracted", repo=slug, sha=sha[:12], claims=len(charter.claims),
        api=len(charter.public_api), entry_points=len(charter.entry_points),
    )
    return charter


# ----------------------------------------------------------------------------------
# Package metadata, one ecosystem at a time
# ----------------------------------------------------------------------------------


def _set_summary(meta: dict[str, Any], text: str, source: str) -> None:
    """Package metadata describes the project most authoritatively; keep the first."""
    if text and not meta["summary"]:
        meta["summary"] = text
        meta["summary_source"] = source


def _requirement_name(spec: str) -> str:
    match = _REQUIREMENT_NAME.match(spec)
    return match.group(1).lower().replace("_", "-") if match else ""


def _from_pyproject(tree: Path, builder: _Builder, meta: dict[str, Any]) -> None:
    path = tree / "pyproject.toml"
    raw = _read(path)
    if not raw:
        return
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError:
        return
    project = data.get("project") or {}

    def at(needle: str) -> str:
        return _src("pyproject.toml", raw, needle)

    poetry = (data.get("tool") or {}).get("poetry") or {}

    meta["name"] = meta["name"] or str(project.get("name") or poetry.get("name") or "")
    version = project.get("version") or poetry.get("version") or ""
    meta["version"] = meta["version"] or str(version)
    description = str(project.get("description") or poetry.get("description") or "")
    _set_summary(meta, description, at("description"))

    license_value = project.get("license") or poetry.get("license")
    if isinstance(license_value, dict):
        license_value = license_value.get("text") or license_value.get("file")
    if license_value and not meta["license"]:
        meta["license"] = str(license_value)

    python = project.get("requires-python") or (poetry.get("dependencies") or {}).get("python")
    if python:
        builder.add("target", f"Python {python}", at("requires-python"))
    for classifier in project.get("classifiers") or poetry.get("classifiers") or []:
        text = str(classifier)
        if text.startswith(("Intended Audience", "Environment", "Operating System",
                            "Framework", "Topic")):
            builder.add("target", text.replace(" :: ", ": "), at(text))
    keywords = project.get("keywords") or poetry.get("keywords") or []
    if keywords:
        builder.add("target", "Keywords: " + ", ".join(map(str, keywords)), at("keywords"))

    for spec in project.get("dependencies") or []:
        name = _requirement_name(str(spec))
        if name:
            meta["dependencies"].add(name)
    for name in (poetry.get("dependencies") or {}):
        if name.lower() != "python":
            meta["dependencies"].add(name.lower())

    scripts: dict[str, Any] = {}
    scripts.update(project.get("scripts") or {})
    scripts.update(project.get("gui-scripts") or {})
    scripts.update(((project.get("entry-points") or {}).get("console_scripts")) or {})
    scripts.update(poetry.get("scripts") or {})
    for name, target in scripts.items():
        meta["entry_points"].append(f"{name} = {target}")
        builder.add("capability", f"Command `{name}` runs {target}", at(f"{name} ="))

    tool = data.get("tool") or {}
    for standard in ("ruff", "mypy", "pytest", "black", "isort", "pylint", "coverage"):
        if standard in tool:
            meta["standards"].add(standard)


def _from_setup_cfg(tree: Path, builder: _Builder, meta: dict[str, Any]) -> None:
    import configparser

    path = tree / "setup.cfg"
    raw = _read(path)
    if not raw:
        return
    parser = configparser.ConfigParser()
    try:
        parser.read_string(raw)
    except configparser.Error:
        return
    if parser.has_section("metadata"):
        section = parser["metadata"]
        meta["name"] = meta["name"] or section.get("name", "")
        _set_summary(meta, section.get("description", ""),
                     f"setup.cfg:{_line_of(raw, 'description')}")
        meta["license"] = meta["license"] or section.get("license", "")
    if parser.has_section("options"):
        python = parser["options"].get("python_requires")
        if python:
            builder.add("target", f"Python {python}",
                        f"setup.cfg:{_line_of(raw, 'python_requires')}")
        for line in parser["options"].get("install_requires", "").splitlines():
            name = _requirement_name(line)
            if name:
                meta["dependencies"].add(name)


def _from_package_json(tree: Path, builder: _Builder, meta: dict[str, Any]) -> None:
    raw = _read(tree / "package.json")
    if not raw:
        return
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return

    def at(needle: str) -> str:
        return _src("package.json", raw, needle)

    meta["name"] = meta["name"] or str(data.get("name") or "")
    meta["version"] = meta["version"] or str(data.get("version") or "")
    _set_summary(meta, str(data.get("description") or ""), at('"description"'))
    if data.get("license") and not meta["license"]:
        meta["license"] = str(data["license"])
    for engine, version in (data.get("engines") or {}).items():
        builder.add("target", f"{engine} {version}", at(f'"{engine}"'))
    if data.get("keywords"):
        builder.add("target", "Keywords: " + ", ".join(map(str, data["keywords"])),
                    at('"keywords"'))
    for name in (data.get("dependencies") or {}):
        meta["dependencies"].add(str(name).lower())
    binary = data.get("bin")
    if isinstance(binary, str):
        meta["entry_points"].append(f"{meta['name'] or 'bin'} = {binary}")
    elif isinstance(binary, dict):
        for name, target in binary.items():
            meta["entry_points"].append(f"{name} = {target}")
            builder.add("capability", f"Command `{name}` runs {target}", at(f'"{name}"'))
    for key in ("main", "module", "exports"):
        if data.get(key) and isinstance(data[key], str):
            meta["entry_points"].append(f"{key} = {data[key]}")


def _from_go_mod(tree: Path, builder: _Builder, meta: dict[str, Any]) -> None:
    raw = _read(tree / "go.mod")
    if not raw:
        return
    in_require = False
    for number, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("module "):
            meta["name"] = meta["name"] or stripped.split()[1]
        elif stripped.startswith("go ") and len(stripped.split()) == 2:
            builder.add("target", f"Go {stripped.split()[1]}", f"go.mod:{number}")
        elif stripped.startswith("require ("):
            in_require = True
        elif in_require and stripped == ")":
            in_require = False
        elif (in_require or stripped.startswith("require ")) and "// indirect" not in stripped:
            parts = stripped.removeprefix("require ").split()
            if parts:
                meta["dependencies"].add(parts[0].lower())


def _from_cargo(tree: Path, builder: _Builder, meta: dict[str, Any]) -> None:
    raw = _read(tree / "Cargo.toml")
    if not raw:
        return
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError:
        return
    package = data.get("package") or {}

    def at(needle: str) -> str:
        return _src("Cargo.toml", raw, needle)

    meta["name"] = meta["name"] or str(package.get("name") or "")
    version = package.get("version")
    if isinstance(version, str):
        meta["version"] = meta["version"] or version
    _set_summary(meta, str(package.get("description") or ""), at("description"))
    if package.get("license") and not meta["license"]:
        meta["license"] = str(package["license"])
    if package.get("rust-version"):
        builder.add("target", f"Rust {package['rust-version']}", at("rust-version"))
    for name in (data.get("dependencies") or {}):
        meta["dependencies"].add(str(name).lower())
    for entry in data.get("bin") or []:
        if isinstance(entry, dict) and entry.get("name"):
            meta["entry_points"].append(f"{entry['name']} = {entry.get('path', 'src/main.rs')}")


def _from_go_commands(tree: Path, meta: dict[str, Any]) -> None:
    command_root = tree / "cmd"
    if not command_root.is_dir():
        return
    for child in sorted(command_root.iterdir()):
        if child.is_dir() and (child / "main.go").is_file():
            meta["entry_points"].append(f"{child.name} = cmd/{child.name}")


# ----------------------------------------------------------------------------------
# Prose: the README and the repository's own contributing notes
# ----------------------------------------------------------------------------------


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(index, level, title) for Markdown `#` headings and RST underlined titles."""
    found: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            title = stripped.lstrip("#").strip()
            if title and level <= 6:
                found.append((index, level, title))
        elif (
            index + 1 < len(lines)
            and stripped
            and re.fullmatch(r"([=\-~^*])\1{2,}", lines[index + 1].strip() or "x")
            and len(lines[index + 1].strip()) >= len(stripped) - 2
        ):
            level = {"=": 1, "-": 2}.get(lines[index + 1].strip()[0], 3)
            found.append((index, level, stripped))
    return found


def _heading_matches(title: str, keys: tuple[str, ...]) -> bool:
    lowered = title.lower().strip(" :?")
    return any(
        lowered == key or lowered.startswith(key + " ") or lowered.endswith(" " + key)
        for key in keys
    )


def _section_kind(title: str) -> str | None:
    for kind, keys in _SECTION_KINDS:
        if _heading_matches(title, keys):
            return kind
    return None


def _paragraphs(lines: list[str], start: int, stop: int) -> list[tuple[int, str]]:
    """(first line index, text) for each prose paragraph in a range, bullets split out."""
    out: list[tuple[int, str]] = []
    buffer: list[str] = []
    buffer_start = start
    in_code = False
    for index in range(start, stop):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")) or stripped.startswith(".. code"):
            in_code = not in_code if stripped.startswith(("```", "~~~")) else in_code
            if buffer:
                out.append((buffer_start, " ".join(buffer)))
                buffer = []
            continue
        if in_code or line.startswith(("    ", "\t")) and not _BULLET.match(line):
            continue
        bullet = _BULLET.match(line)
        if bullet:
            if buffer:
                out.append((buffer_start, " ".join(buffer)))
                buffer = []
            joined = _bullets(lines[index:stop])
            out.append((index, joined[0][1] if joined else bullet.group(1)))
            continue
        if not stripped or _BADGE.match(line) or stripped.startswith(("#", "|", ">")):
            if buffer:
                out.append((buffer_start, " ".join(buffer)))
                buffer = []
            continue
        if not buffer:
            buffer_start = index
        buffer.append(stripped)
    if buffer:
        out.append((buffer_start, " ".join(buffer)))
    return out


def _from_readme(tree: Path, builder: _Builder, meta: dict[str, Any]) -> None:
    readme = next((tree / name for name in _README_NAMES if (tree / name).is_file()), None)
    if readme is None:
        return
    relative = readme.relative_to(tree).as_posix()
    lines = _read(readme).splitlines()
    headings = _headings(lines)

    # The opening paragraph, before any second-level section, is how the project
    # introduces itself. It is the summary when metadata gave none, and a purpose
    # claim either way.
    first_section = next((i for i, level, _ in headings if level >= 2), len(lines))
    intro_start = headings[0][0] + 1 if headings and headings[0][1] == 1 else 0
    if headings and headings[0][1] == 1 and not meta["name"]:
        meta["name"] = _clean(headings[0][2])
    intro = [
        (index, text) for index, text in _paragraphs(lines, intro_start, first_section)
        if len(text) > 20
    ]
    if intro:
        index, text = intro[0]
        _set_summary(meta, text, f"{relative}:{index + 1}")
        for index, text in intro[:3]:
            builder.add("purpose", text, f"{relative}:{index + 1}")

    for position, (index, _, title) in enumerate(headings):
        kind = _section_kind(title)
        if kind is None:
            continue
        stop = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        label = NON_GOAL if _heading_matches(title, _NON_GOAL_KEYS) else ""
        for line_index, text in _paragraphs(lines, index + 1, stop)[:10]:
            builder.add(kind, label + text, f"{relative}:{line_index + 1}")


def _bullets(lines: list[str]) -> list[tuple[int, str]]:
    """(line index, full text) for each bullet, joining wrapped continuation lines."""
    out: list[tuple[int, str]] = []
    index = 0
    while index < len(lines):
        match = _BULLET.match(lines[index])
        if not match:
            index += 1
            continue
        indent = len(lines[index]) - len(lines[index].lstrip())
        parts = [match.group(1).strip()]
        follow = index + 1
        while follow < len(lines):
            line = lines[follow]
            if not line.strip() or _BULLET.match(line):
                break
            if len(line) - len(line.lstrip()) <= indent:
                break
            parts.append(line.strip())
            follow += 1
        out.append((index, " ".join(parts)))
        index = follow
    return out


def _from_guidance(tree: Path, builder: _Builder) -> None:
    """The rules a repository writes down for its own contributors.

    These are data about the repository — what it holds itself to — and are
    recorded as constraint claims. They are never treated as instructions to
    this system.
    """
    for name in _GUIDANCE_NAMES:
        path = tree / name
        if not path.is_file():
            continue
        for index, text in _bullets(_read(path).splitlines()):
            if len(text) > 20:
                builder.add("constraint", text, f"{name}:{index + 1}")


# ----------------------------------------------------------------------------------
# Standards, license, modules, public API
# ----------------------------------------------------------------------------------


def _from_standard_files(tree: Path, meta: dict[str, Any]) -> None:
    for standard, names in _STANDARD_FILES.items():
        if any((tree / name).is_file() for name in names):
            meta["standards"].add(standard)
    workflows = tree / ".github" / "workflows"
    if workflows.is_dir() and any(workflows.glob("*.y*ml")):
        meta["standards"].add("ci")


def _from_license_file(tree: Path, meta: dict[str, Any]) -> None:
    if meta["license"]:
        return
    for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"):
        text = _read(tree / name)
        if text:
            first = next((line.strip() for line in text.splitlines() if line.strip()), "")
            meta["license"] = first[:80]
            return


def _python_roots(tree: Path, package_roots: Sequence[str]) -> list[Path]:
    roots = [tree / root for root in package_roots if (tree / root).is_dir()]
    if roots:
        return roots
    found: list[Path] = []
    for parent in (tree / "src", tree):
        if not parent.is_dir():
            continue
        for child in sorted(parent.iterdir()):
            if (
                child.is_dir()
                and (child / "__init__.py").is_file()
                and child.name not in ("tests", "test", "docs", "examples", "benchmarks")
            ):
                found.append(child)
        if found:
            break
    return found


def _module_docstrings(
    tree: Path, toolchain: Toolchain, package_roots: Sequence[str]
) -> list[tuple[str, str, str]]:
    """(module, first docstring line, source) for each top-level module.

    A module's own docstring is the repository describing one of its parts in
    its own words — the most precise capability statement it makes.
    """
    if toolchain.grammar != "python":
        return []
    out: list[tuple[str, str, str]] = []
    for root in _python_roots(tree, package_roots):
        candidates = [root / "__init__.py"]
        candidates += sorted(p / "__init__.py" for p in root.iterdir() if p.is_dir())
        candidates += sorted(p for p in root.glob("*.py") if p.name != "__init__.py")
        for path in candidates:
            if not path.is_file():
                continue
            try:
                module = ast.parse(_read(path))
            except SyntaxError:
                continue
            docstring = ast.get_docstring(module, clean=True)
            if not docstring:
                continue
            first = docstring.strip().splitlines()[0].strip()
            if len(first) < 8:
                continue
            relative = path.relative_to(tree).as_posix()
            name = ".".join(path.relative_to(root.parent).with_suffix("").parts)
            name = name.removesuffix(".__init__")
            out.append((name, _clean(first), f"{relative}:1"))
            if len(out) >= 60:
                return out
    return out


def _public_api(
    tree: Path, toolchain: Toolchain, package_roots: Sequence[str], max_files: int
) -> list[str]:
    if toolchain.grammar != "python":
        return []
    from prflagger.analysis.callgraph import index_symbols, package_files

    names: list[str] = []
    for root in _python_roots(tree, package_roots):
        if len(package_files(root)) > max_files:
            log.warning("charter.api_skipped", root=str(root))
            continue
        for fqn in index_symbols(root):
            if not any(part.startswith("_") for part in fqn.split(".")[1:]):
                names.append(fqn)
    return names
