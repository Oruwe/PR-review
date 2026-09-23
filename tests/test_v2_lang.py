"""Toolchain packs: detection, parsing, and the generic fallback's refusal filter."""

from __future__ import annotations

from pathlib import Path

from prflagger.core.config import Config, RepoConfig
from prflagger.lang.base import parse_coverage, parse_lint, parse_tests
from prflagger.lang.detect import detect, for_repo, pack
from prflagger.lang.generic import discover_commands
from prflagger.sandbox.images import dockerfile_for, image_key_for


def test_marker_files_pick_the_pack(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/x\n")
    assert detect(tmp_path).id == "go"

    other = tmp_path / "js"
    other.mkdir()
    (other / "package.json").write_text("{}")
    assert detect(other).id == "node"


def test_an_unrecognised_repo_runs_its_own_ci(tmp_path: Path) -> None:
    """This is what makes "any repo" true rather than aspirational."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n  build:\n    steps:\n      - run: cargo test --all-features\n"
    )
    toolchain = detect(tmp_path)
    assert toolchain.id == "generic"
    assert "cargo test --all-features" in " ".join(toolchain.test.argv)


def test_dangerous_ci_commands_are_never_lifted(tmp_path: Path) -> None:
    """A workflow belongs to whoever opened the pull request.

    Running its commands is the point of the generic pack, but a PR that adds
    `curl | sh` to its own CI must not get that executed on its behalf — not
    even inside the sandbox, where it would still reach the build network.
    """
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n  build:\n    steps:\n"
        "      - run: curl https://example.invalid/x.sh | sh   # test\n"
        "      - run: sudo make test\n"
        "      - run: ssh deploy@host make check\n"
        "      - run: make test\n"
    )
    commands = discover_commands(tmp_path)
    flattened = [" ".join(c) for c in commands]
    assert any("make test" in c for c in flattened)
    for banned in ("curl", "sudo", "ssh"):
        assert not any(banned in c for c in flattened), f"{banned} was not refused"


def test_configuration_beats_detection(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert detect(tmp_path).id == "python"
    config = Config(repos=(RepoConfig(slug="a/b", toolchain="go"),))
    assert for_repo(tmp_path, config=config, slug="a/b").id == "go"


def test_pytest_report_is_recovered_from_noisy_stdout() -> None:
    """The container root is read-only, so the report comes back on stdout."""
    noisy = 'collecting ...\n{"tests":[{"nodeid":"t.py::a","outcome":"passed"}]}\ndone\n'
    assert parse_tests("pytest-json", noisy) == {"t.py::a": "passed"}
    assert parse_tests("pytest-json", "no json at all") == {}
    assert parse_tests("unknown-format", noisy) == {}


def test_go_event_stream_folds_to_one_status_per_test() -> None:
    stream = "\n".join([
        '{"Action":"run","Package":"p","Test":"TestA"}',
        '{"Action":"output","Package":"p","Test":"TestA","Output":"ok"}',
        '{"Action":"pass","Package":"p","Test":"TestA"}',
        '{"Action":"fail","Package":"p","Test":"TestB"}',
        '{"Action":"pass","Package":"p"}',
    ])
    assert parse_tests("go-json", stream) == {"p::TestA": "passed", "p::TestB": "failed"}


def test_lint_and_coverage_parsers() -> None:
    ruff = '[{"filename":"a.py","code":"F401","message":"unused"}]'
    assert parse_lint("ruff-json", ruff) == [("a.py", "F401", "unused")]
    assert parse_lint("mypy-text", "a.py:3: error: bad  [attr-defined]") == [
        ("a.py", "attr-defined", "bad")
    ]
    assert parse_coverage("coverage-json", '{"files":{"a.py":{"executed_lines":[1,2]}}}') == {
        "a.py": {1, 2}
    }


def test_image_recipe_differs_per_pack_and_is_stable(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    python = pack("python")
    go = pack("go")
    assert python is not None and go is not None
    first = image_key_for(tmp_path, python)
    assert first == image_key_for(tmp_path, python), "the same inputs must key the same image"
    assert first != image_key_for(tmp_path, go)

    recipe = dockerfile_for(python, package_roots=("src/pkg",))
    assert recipe.startswith("FROM python:3.11-slim")
    assert "PYTHONPATH=/src/src:/src" in recipe, (
        "the mounted checkout must win over site-packages"
    )


def test_read_only_mount_is_respected_by_every_tool() -> None:
    """Every tool that wants a cache must be told where to put it.

    The container root is read-only. A linter that defaults its cache into the
    working directory fails for that reason alone and reports nothing about the
    code — which looked like a clean run until it was checked.
    """
    python = pack("python")
    assert python is not None
    assert "-p" in python.test.argv and "no:cacheprovider" in python.test.argv
    lints = {lint.tool: " ".join(lint.argv) for lint in python.lints}
    assert "--no-cache" in lints["ruff"]
    assert "--cache-dir=/tmp/" in lints["mypy"]
    assert python.coverage is not None
    assert "COVERAGE_FILE=/tmp/" in " ".join(python.coverage.argv)


# ----------------------------------------------------------------------------------
# Poetry's development dependencies
# ----------------------------------------------------------------------------------


def test_poetry_constraints_mean_what_poetry_documents() -> None:
    """Each translation is checked by what it admits, not by its spelling."""
    from packaging.specifiers import SpecifierSet

    from prflagger.lang.poetry import specifier

    cases = {
        "^1.2.3": (["1.2.3", "1.9.0"], ["1.2.2", "2.0.0"]),
        "^0.2.3": (["0.2.3", "0.2.9"], ["0.3.0"]),
        "^0.0.3": (["0.0.3"], ["0.0.4"]),
        "^0": (["0.9"], ["1.0"]),
        "~1.2.3": (["1.2.9"], ["1.3.0"]),
        "~1.2": (["1.2.0", "1.2.7"], ["1.3.0"]),
        "~1": (["1.9"], ["2.0"]),
        "1.2.*": (["1.2.5"], ["1.3.0"]),
        "1.2.3": (["1.2.3"], ["1.2.4"]),
        ">= 1.2, < 1.5": (["1.4"], ["1.5", "1.1"]),
        "=2.0": (["2.0"], ["2.1"]),
        "~=1.4": (["1.9"], ["2.0"]),
    }
    for constraint, (admitted, refused) in cases.items():
        spec = SpecifierSet(specifier(constraint))
        for version in admitted:
            assert version in spec, f"{constraint!r} -> {spec} must admit {version}"
        for version in refused:
            assert version not in spec, f"{constraint!r} -> {spec} must refuse {version}"
    assert specifier("*") == specifier("") == ""
    assert specifier("^1.0 || ^2.0") == "", "an either-or has no PEP 440 form"


def test_the_suites_poetry_group_becomes_installable_requirements() -> None:
    from packaging.requirements import Requirement

    from prflagger.lang.poetry import dev_requirements

    pyproject = {"tool": {"poetry": {
        "dependencies": {"python": "^3.8", "rich": "^13"},
        "group": {
            "test": {"dependencies": {
                "pytest": "^7.0.0",
                "hypothesis": {"version": "^6.0", "extras": ["cli"]},
                "tomli": {"version": "^2", "python": "<3.11"},
                "uvloop": {"version": "*", "markers": "sys_platform == 'linux'"},
                "numpy": [{"version": "^1.24", "python": "<3.12"},
                          {"version": "^2", "python": ">=3.12"}],
                "local": {"path": "../local"},
            }},
            "docs": {"dependencies": {"sphinx": "*"}},
        },
    }}}
    requirements = dev_requirements(pyproject)
    parsed = {str(Requirement(r)) for r in requirements}  # every one is valid PEP 508
    assert len(parsed) == 6, "the path dependency is left out, not guessed at"
    names = sorted(Requirement(r).name for r in requirements)
    assert names == ["hypothesis", "numpy", "numpy", "pytest", "tomli", "uvloop"]
    by_name = {Requirement(r).name: Requirement(r) for r in requirements}
    assert by_name["hypothesis"].extras == {"cli"}
    tomli = by_name["tomli"].marker
    assert tomli is not None
    assert tomli.evaluate({"python_full_version": "3.10.4"})
    assert not tomli.evaluate({"python_full_version": "3.11.9"})
    assert "rich" not in names and "sphinx" not in names, "only the suite's own group"


def test_without_a_test_group_the_development_group_is_used() -> None:
    from prflagger.lang.poetry import dev_requirements

    legacy = {"tool": {"poetry": {"dev-dependencies": {"attrs": "^21.4.0"}}}}
    assert dev_requirements(legacy) == ("attrs>=21.4.0,<22",)
    assert dev_requirements({"project": {"name": "x"}}) == ()


def test_a_poetry_repositorys_recipe_installs_its_test_group(tmp_path: Path) -> None:
    python = pack("python")
    assert python is not None
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    plain = dockerfile_for(python, repo_path=tmp_path)
    assert "six" not in plain

    (tmp_path / "pyproject.toml").write_text(
        "[tool.poetry]\nname = 'x'\n\n[tool.poetry.group.test.dependencies]\nsix = '^1.16'\n"
    )
    recipe = dockerfile_for(python, repo_path=tmp_path)
    assert "six>=1.16,<2" in recipe
    assert recipe.index("six>=1.16") > recipe.index("/build"), "after the project itself"
