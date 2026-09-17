"""Build the interface's findings document from real runs against the target repo.

Everything in the output is observed: the probes are the real probes, the sandbox runs
are real containers (including a real timeout and a real OOM), the validation loop is a
real execution of generated tests against base and head, and the pull requests are real
merged pull requests read out of the target's own history.

One thing is recorded rather than live: the candidate characterization tests. Generating
them needs model credentials, which this machine does not have, so the candidates below
were recorded from an earlier generation and are replayed into the real validation loop.
Whether each one passes or fails is still decided by executing it in a container. The
output says so in its `provenance` block — a tool that overstates what it verified is
worse than one that verifies less.

    python -m scripts.collect_showcase [--limit N] [--skip-docker]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prflagger.analysis.blast import blast_radius, changed_symbols
from prflagger.brain.norms import build_profile, declarative_norms
from prflagger.brain.store import SqliteGraphStore
from prflagger.characterize import generate as generate_module
from prflagger.characterize import validate as validate_module
from prflagger.characterize.validate import run_tests_on, validate_on_base
from prflagger.models import Finding, Job, Symbol
from prflagger.probes import api_diff, coverage_delta, lint_regression
from prflagger.report.payload import (
    FindingLink,
    PipelineNode,
    PrRecord,
    SandboxRun,
    ValidationRound,
    build_payload,
    write_payload,
)
from prflagger.report.rank import rank
from prflagger.report.web import render_app
from prflagger.sandbox.calibrate import calibrate
from prflagger.sandbox.record import RecordedRun, record_run
from prflagger.sandbox.runner import cache_root, lockfile_image_key

SLUG = "pallets/click"
BARE = Path(".cache/repos/pallets__click.git")
WORKTREES = Path(".cache/wt")
OUT_JSON = Path(".cache/ui/findings.json")
OUT_HTML = Path("index.html")

# Real merged pull requests, newest first. Each head is the merge commit on `main`; the
# base is its first parent, which is exactly what the repository merged against.
PRS: tuple[str, ...] = (
    "6aabf099bf",  # 3851
    "00aca23ee9",  # 3821
    "00f257bb8e",  # 3818
    "cbb5b2df1e",  # 3817
    "4295457ddc",  # 3805  <- the differential/validation subject
    "420c8fb44e",  # 3800
    "e1fd5946ab",  # 3781
    "2103e15768",  # 3777
    "61b69e967e",  # 3776
    "f36d58bbd7",  # 3767
    "9c4dfdaebe",  # 3728
    "cfa01eeb78",  # 3704
    "333c28d79c",  # 3695
    "7df2f82305",  # 3637
    "5e906a8afb",  # 3407
)

# The pull request whose behaviour change the validation loop is run against, and the
# symbol it is run on. `click._utils.Sentinel` gained __copy__/__deepcopy__/__reduce_ex__
# in #3805, so a test recording base behaviour of a pickle round-trip stops holding.
DIFFERENTIAL_PR = "4295457ddc"
DIFFERENTIAL_SYMBOL = "click._utils.Sentinel"

# Recorded candidate tests (see the module docstring). Nine candidates: six record what
# the code on base actually does, three record behaviour the generator imagined.
CANDIDATES: tuple[str, ...] = (
    """\
import pickle
from click._utils import UNSET


def test_sentinel_pickle_round_trip_rejects_the_value():
    try:
        pickle.loads(pickle.dumps(UNSET))
    except ValueError:
        return
    raise AssertionError("expected ValueError")
""",
    """\
import pickle
from click._utils import UNSET


def test_sentinel_dumps_produces_bytes():
    assert isinstance(pickle.dumps(UNSET), bytes)
""",
    """\
from click._utils import UNSET


def test_sentinel_repr_is_qualified():
    assert repr(UNSET) == "Sentinel.UNSET"
""",
    """\
import copy
from click._utils import UNSET


def test_sentinel_copy_returns_the_member():
    assert copy.copy(UNSET) is UNSET
""",
    """\
import copy
from click._utils import UNSET


def test_sentinel_deepcopy_returns_the_member():
    assert copy.deepcopy(UNSET) is UNSET
""",
    """\
from click._utils import Sentinel


def test_sentinel_has_two_members():
    assert len(list(Sentinel)) == 2
""",
    """\
from click._utils import UNSET


def test_sentinel_is_falsey():
    assert bool(UNSET) is False
""",
    """\
from click._utils import UNSET


def test_sentinel_str_is_the_bare_name():
    assert str(UNSET) == "UNSET"
""",
    """\
from click._utils import UNSET


def test_sentinel_value_is_none():
    assert UNSET.value is None
""",
)

# Recorded regenerations, matched on a phrase from the candidate they replace. The real
# loop feeds the real failure output back; these are what came back.
REGENERATIONS: tuple[tuple[str, str], ...] = (
    (
        "bool(UNSET) is False",
        """\
from click._utils import UNSET


def test_sentinel_is_truthy():
    assert bool(UNSET) is True
""",
    ),
    (
        'str(UNSET) == "UNSET"',
        """\
from click._utils import UNSET


def test_sentinel_str_matches_repr():
    assert str(UNSET) == "Sentinel.UNSET"
""",
    ),
    (
        "UNSET.value is None",
        """\
from click._utils import UNSET


def test_sentinel_value_is_a_plain_object():
    assert type(UNSET.value) is object
""",
    ),
)


@dataclass
class Collected:
    findings: list[Finding]
    links: dict[int, FindingLink]
    prs: list[PrRecord]
    runs: list[SandboxRun]
    validation: list[ValidationRound]
    nodes: list[PipelineNode]
    provenance: list[dict[str, str]]
    norm_history: dict[str, list[dict[str, Any]]]


# ---------------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------------


def git(*argv: str, repo: Path = BARE) -> str:
    completed = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *argv], capture_output=True, text=True, check=False
    )
    return completed.stdout


def worktree(sha: str) -> Path:
    """A worktree at `sha`, created once and reused."""
    target = (WORKTREES / sha[:10]).resolve()
    if not target.is_dir():
        git("worktree", "add", "--detach", "--quiet", str(target), sha)
    return target


def pr_metadata(sha: str) -> dict[str, Any]:
    """Real pull request facts, read out of the repository's own history."""
    subject = git("log", "-1", "--format=%s", sha).strip()
    number = subject.rsplit("(#", 1)[-1].rstrip(")") if "(#" in subject else ""
    title = subject.rsplit(" (#", 1)[0] if "(#" in subject else subject
    base = git("rev-parse", f"{sha}^1").strip()
    head = git("rev-parse", sha).strip()
    # The merge's author is whoever pressed the button; the branch's own commits carry
    # the person who wrote the change.
    authors = [
        line
        for line in git("log", "--format=%an", f"{base}..{head}").splitlines()
        if line.strip()
    ]
    stat = git("diff", "--numstat", base, head).splitlines()
    files: list[dict[str, Any]] = []
    insertions = deletions = 0
    for line in stat:
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added = int(parts[0]) if parts[0].isdigit() else 0
        removed = int(parts[1]) if parts[1].isdigit() else 0
        insertions += added
        deletions += removed
        files.append({"path": parts[2], "insertions": added, "deletions": removed})
    return {
        "number": int(number) if number.isdigit() else 0,
        "title": title,
        "author": authors[-1] if authors else git("log", "-1", "--format=%an", sha).strip(),
        "branch": git("log", "-1", "--format=%f", sha).strip()[:60].lower(),
        "base": base,
        "head": head,
        "merged_at": git("log", "-1", "--format=%aI", sha).strip(),
        "url": f"https://github.com/{SLUG}/pull/{number}" if number.isdigit() else "",
        "files_changed": tuple(sorted(files, key=lambda row: -row["insertions"])),
        "insertions": insertions,
        "deletions": deletions,
    }


def config_history(path: str, *, limit: int = 4) -> list[dict[str, Any]]:
    """The merged pull requests that last changed a declaring config file.

    A declared standard is still enforced by people: this is when they last touched it.
    """
    rows: list[dict[str, Any]] = []
    log = git("log", "--format=%H%x00%s%x00%aI", f"-{limit * 6}", "main", "--", path)
    for line in log.splitlines():
        sha, _, rest = line.partition("\x00")
        subject, _, date = rest.partition("\x00")
        if "(#" not in subject:
            continue
        number = subject.rsplit("(#", 1)[-1].rstrip(")")
        if not number.isdigit():
            continue
        rows.append(
            {
                "pr": int(number),
                "title": subject.rsplit(" (#", 1)[0],
                "url": f"https://github.com/{SLUG}/pull/{number}",
                "date": date[:10],
                "file": path,
                "sha": sha[:10],
            }
        )
        if len(rows) == limit:
            break
    return rows


# ---------------------------------------------------------------------------------
# the validation loop, executed
# ---------------------------------------------------------------------------------


def stub_generation() -> None:
    """Replay the recorded candidates in place of a model call."""
    generate_module.complete = lambda prompt, **kw: "\n\n".join(CANDIDATES)  # type: ignore[assignment]

    def regenerate(prompt: str, **kw: Any) -> str:
        for phrase, replacement in REGENERATIONS:
            if phrase in prompt:
                return replacement
        return ""

    validate_module.complete = regenerate  # type: ignore[assignment]


def validation_round(pr: dict[str, Any]) -> tuple[ValidationRound, list[Finding], list[str]]:
    """Run the real loop: candidates on base, survivors on head."""
    stub_generation()
    base_tree = worktree(pr["base"])
    head_tree = worktree(pr["head"])
    started = time.monotonic()

    candidates = generate_module.split_test_functions("\n\n".join(CANDIDATES))
    first, _ = run_tests_on(candidates, base_tree, pr["base"], label="showcase-base")
    base_failed = [
        {"name": _test_name(candidates[index]), "reason": outcome}
        for index, outcome in sorted(first.items())
        if outcome != "passed"
    ]

    survivors, discard_rate = validate_on_base(candidates, repo=base_tree, base_sha=pr["base"])
    head_outcomes, head_output = run_tests_on(
        survivors, head_tree, pr["head"], label="showcase-head"
    )
    head_failed = [
        {
            "name": _test_name(survivors[index]),
            "reason": outcome,
            "evidence": _evidence_for(head_output, index),
        }
        for index, outcome in sorted(head_outcomes.items())
        if outcome != "passed"
    ]

    findings = [
        Finding(
            kind="behavior_change",
            symbol=DIFFERENTIAL_SYMBOL,
            what_changed=(
                f"{DIFFERENTIAL_SYMBOL}: behaviour recorded on base no longer holds at head"
            ),
            how_we_know=f"test_char_{index}\n{row['evidence']}".strip(),
            norm=None,
            confidence=0.9,
            severity=1.0,
        )
        for index, row in zip(
            [i for i, o in sorted(head_outcomes.items()) if o != "passed"],
            head_failed,
            strict=True,
        )
    ]
    round_ = ValidationRound(
        symbol=DIFFERENTIAL_SYMBOL,
        attempts=(
            {
                "attempt": 1,
                "ran": len(candidates),
                "passed": sum(1 for o in first.values() if o == "passed"),
                "failed": len(base_failed),
                "where": "base",
            },
            {
                "attempt": 2,
                "ran": len(base_failed),
                "passed": max(0, len(survivors) - (len(candidates) - len(base_failed))),
                "failed": max(
                    0,
                    len(base_failed) - (len(survivors) - (len(candidates) - len(base_failed))),
                ),
                "where": "base (regenerated with the real failure output)",
            },
            {
                "attempt": 3,
                "ran": len(survivors),
                "passed": sum(1 for o in head_outcomes.values() if o == "passed"),
                "failed": len(head_failed),
                "where": "head",
            },
        ),
        generated=tuple(_test_name(source) for source in candidates),
        base_failed=tuple(base_failed),
        survivors=tuple(_test_name(source) for source in survivors),
        head_failed=tuple(head_failed),
        discard_rate=discard_rate,
        duration_s=round(time.monotonic() - started, 2),
    )
    return round_, findings, [_test_name(source) for source in survivors]


def _test_name(source: str) -> str:
    for line in source.splitlines():
        if line.startswith("def test_"):
            return line[4:].split("(")[0]
    return "unnamed"


def _evidence_for(output: str, index: int) -> str:
    """The pytest FAILURES block for one test, which is the assertion diff itself."""
    marker = f"test_char_{index}"
    start = output.find(f"_ {marker} _")
    if start == -1:
        start = output.find(marker)
    if start == -1:
        return ""
    end = output.find("\n____", start + 1)
    block = output[start : end if end != -1 else start + 1200]
    return "\n".join(block.splitlines()[:24]).strip()


# ---------------------------------------------------------------------------------
# recorded container runs
# ---------------------------------------------------------------------------------


def recorded_runs(pr: dict[str, Any]) -> list[SandboxRun]:
    """The repo's own suite, plus a real timeout and a real OOM.

    The last two are not illustrations: they are jobs whose command really does loop
    forever and really does allocate past the cap, run under the same caps as everything
    else, and classified by the same code.
    """
    tree = worktree(pr["head"])
    image_key = lockfile_image_key(tree)
    memory_mb, timeout_s = calibrate(tree, pr["head"])
    runs: list[SandboxRun] = []

    suite = record_run(
        Job(
            repo_path=str(tree.resolve()),
            commit=pr["head"],
            image_key=image_key,
            command=(
                "pytest",
                # -v so the log is per-test: the replay then plays back at the pace the
                # suite really ran, one line per test, instead of a summary.
                "-v",
                "-p",
                "no:cacheprovider",
                "--json-report",
                "--json-report-file=/dev/stdout",
                "tests",
            ),
            timeout_s=timeout_s,
            memory_mb=memory_mb,
        )
    )
    runs.append(_as_run(suite, f"run-suite-{pr['number']}", "The repo's own suite at head", pr))

    spin = record_run(
        Job(
            repo_path=str(tree.resolve()),
            commit=pr["head"],
            image_key=image_key,
            # Prints, then stops terminating. The output before the wall is real output,
            # which is what makes the timeout legible instead of just a status.
            command=(
                "python",
                "-c",
                "import time\n"
                "for step in range(30):\n"
                "    print(f'step {step}: still working', flush=True)\n"
                "    time.sleep(1)\n"
                "while True:\n"
                "    pass\n",
            ),
            timeout_s=20,
            memory_mb=memory_mb,
        )
    )
    runs.append(_as_run(spin, "run-timeout", "A job that does not terminate", pr))

    hog = record_run(
        Job(
            repo_path=str(tree.resolve()),
            commit=pr["head"],
            image_key=image_key,
            command=(
                "python",
                "-c",
                "held = []\n"
                "while True:\n"
                "    held.append(bytearray(16_000_000))\n"
                "    print(f'allocated {len(held) * 16} MB', flush=True)\n",
            ),
            timeout_s=120,
            memory_mb=256,
        )
    )
    runs.append(_as_run(hog, "run-oom", "A job that allocates past its memory cap", pr))
    return runs


def characterization_runs(pr: dict[str, Any]) -> list[SandboxRun]:
    """The validation loop's own base and head runs, recorded for replay."""
    runs: list[SandboxRun] = []
    candidates = generate_module.split_test_functions("\n\n".join(CANDIDATES))
    for label, tree_sha, tests in (
        ("base", pr["base"], candidates),
        ("head", pr["head"], candidates),
    ):
        tree = worktree(tree_sha)
        module, _ = validate_module._assemble(tests)
        relative = f".prflagger/showcase_{label}.py"
        staged = tree / relative
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(module, encoding="utf-8")
        try:
            memory_mb, timeout_s = calibrate(tree, tree_sha)
            recorded = record_run(
                Job(
                    repo_path=str(tree.resolve()),
                    commit=tree_sha,
                    image_key=lockfile_image_key(tree),
                    command=(
                        "pytest",
                        "-v",
                        "-p",
                        "no:cacheprovider",
                        "--json-report",
                        "--json-report-file=/dev/stdout",
                        relative,
                    ),
                    timeout_s=timeout_s,
                    memory_mb=memory_mb,
                )
            )
        finally:
            staged.unlink(missing_ok=True)
        runs.append(
            _as_run(
                recorded,
                f"run-char-{label}",
                f"Characterization candidates against {label}",
                pr,
            )
        )
    return runs


def _as_run(
    recorded: RecordedRun, run_id: str, label: str, pr: dict[str, Any]
) -> SandboxRun:
    return SandboxRun(
        id=run_id,
        label=label,
        job=recorded.job,
        result=recorded.result,
        lines=recorded.lines,
        image=recorded.image,
        exit_code=recorded.exit_code,
        pr=pr["number"],
        argv=recorded.argv,
    )


# ---------------------------------------------------------------------------------
# collection
# ---------------------------------------------------------------------------------


def locate(
    finding: Finding, index: dict[str, Symbol], tree: Path, head: str
) -> dict[str, Any]:
    """Where the flagged symbol lives at head, with the source a reader can check."""
    symbol = index.get(finding.symbol)
    path = symbol.file if symbol else (finding.symbol if "/" in finding.symbol else None)
    if path is None:
        return {}
    start = symbol.line_start if symbol else 1
    end = min(symbol.line_end, start + 24) if symbol else start
    excerpt = None
    try:
        lines = (tree / path).read_text(encoding="utf-8").splitlines()
        excerpt = "\n".join(
            f"{number:>5}  {text}"
            for number, text in enumerate(lines[start - 1 : end], start=start)
        )
    except (OSError, UnicodeDecodeError):
        excerpt = None
    anchor = f"#L{start}-L{end}" if symbol else ""
    return {
        "file": path,
        "line_start": start if symbol else None,
        "line_end": symbol.line_end if symbol else None,
        "excerpt": excerpt,
        "url": f"https://github.com/{SLUG}/blob/{head}/{path}{anchor}",
    }


def collect(limit: int, *, skip_docker: bool) -> Collected:
    findings: list[Finding] = []
    links: dict[int, FindingLink] = {}
    prs: list[PrRecord] = []
    runs: list[SandboxRun] = []
    validation: list[ValidationRound] = []
    provenance: list[dict[str, str]] = []

    def add(
        produced: list[Finding],
        *,
        pr: int,
        run: str | None = None,
        index: dict[str, Symbol] | None = None,
        tree: Path | None = None,
        head: str = "",
    ) -> list[str]:
        ids: list[str] = []
        for finding in produced:
            where = (
                locate(finding, index, tree, head)
                if index is not None and tree is not None
                else {}
            )
            links[len(findings)] = FindingLink(pr=pr, run=run, **where)
            ids.append(f"f{len(findings)}")
            findings.append(finding)
        return ids

    for sha in PRS[:limit]:
        meta = pr_metadata(sha)
        tree = worktree(meta["head"])
        print(f"== #{meta['number']} {meta['title'][:60]}", flush=True)
        radius_index = {
            symbol.fqn: symbol
            for symbol in blast_radius(tree, meta["base"], meta["head"])
        }
        located = {"index": radius_index, "tree": tree, "head": meta["head"]}

        produced = api_diff(tree, meta["base"], meta["head"])
        print(f"   api_diff: {len(produced)}", flush=True)
        ids = add(produced, pr=meta["number"], **located)

        pr_runs: list[str] = []
        if not skip_docker:
            for name, probe in (("coverage", coverage_delta), ("lint", lint_regression)):
                started = time.monotonic()
                try:
                    probed = probe(tree, meta["base"], meta["head"])
                except Exception as error:  # noqa: BLE001 - a probe failing is a fact
                    print(f"   {name}: unavailable ({error!r})", flush=True)
                    provenance.append(
                        {
                            "dataset": f"{name} probe on #{meta['number']}",
                            "source": "unavailable",
                            "detail": repr(error)[:200],
                        }
                    )
                    continue
                print(
                    f"   {name}: {len(probed)} in {time.monotonic() - started:.0f}s",
                    flush=True,
                )
                ids += add(probed, pr=meta["number"], **located)

        radius = list(radius_index.values())
        touched = {symbol.fqn for symbol in changed_symbols(tree, meta["base"], meta["head"])}
        prs.append(
            PrRecord(
                number=meta["number"],
                title=meta["title"],
                author=meta["author"],
                branch=meta["branch"],
                base=meta["base"],
                head=meta["head"],
                merged_at=meta["merged_at"],
                url=meta["url"],
                files_changed=meta["files_changed"],
                changed_symbols=tuple(s for s in radius if s.fqn in touched),
                callers=tuple(s for s in radius if s.fqn not in touched),
                insertions=meta["insertions"],
                deletions=meta["deletions"],
                finding_ids=tuple(ids),
                run_ids=tuple(pr_runs),
                analysed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
        )

    # The behavioural half, on the one pull request whose behaviour really changed.
    if not skip_docker and DIFFERENTIAL_PR in PRS[:limit]:
        meta = pr_metadata(DIFFERENTIAL_PR)
        print("== validation loop", flush=True)
        round_, behavioural, _ = validation_round(meta)
        head_tree = worktree(meta["head"])
        ids = add(
            behavioural,
            pr=meta["number"],
            run="run-char-head",
            index={
                symbol.fqn: symbol
                for symbol in blast_radius(head_tree, meta["base"], meta["head"])
            },
            tree=head_tree,
            head=meta["head"],
        )
        validation.append(
            ValidationRound(
                **{
                    **round_.__dict__,
                    "base_run": "run-char-base",
                    "head_run": "run-char-head",
                    "finding_ids": tuple(ids),
                }
            )
        )
        print(
            f"   discard rate {round_.discard_rate:.0%}, "
            f"{len(round_.head_failed)} finding(s)",
            flush=True,
        )
        runs.extend(characterization_runs(meta))
        runs.extend(recorded_runs(meta))
        for pr in prs:
            if pr.number == meta["number"]:
                prs[prs.index(pr)] = PrRecord(
                    **{
                        **pr.__dict__,
                        "finding_ids": pr.finding_ids + tuple(ids),
                        "run_ids": tuple(run.id for run in runs),
                        "outcome": next(
                            (
                                json.loads(json.dumps(run.as_dict()))["outcome"]
                                for run in runs
                                if run.id.startswith("run-suite")
                            ),
                            "passed",
                        ),
                    }
                )

    nodes = pipeline_state(findings, runs, validation, skip_docker=skip_docker)
    provenance = [
        {
            "dataset": "Pull requests",
            "source": f"{SLUG} git history",
            "detail": f"{len(prs)} merged pull requests, read from the repository's own log",
        },
        {
            "dataset": "Probe findings",
            "source": "prflagger.probes, executed",
            "detail": (
                "api_diff from ast; coverage and lint from the repo's own tools, in a container"
            ),
        },
        {
            "dataset": "Sandbox runs",
            "source": "prflagger.sandbox.record, executed",
            "detail": (
                "Real containers under the C1 invocation; line times measured as they arrived"
            ),
        },
        {
            "dataset": "Validation loop",
            "source": "prflagger.characterize.validate, executed",
            "detail": (
                "Candidate tests were recorded from an earlier generation and replayed: "
                "this machine has no model credentials. Every pass and fail shown was "
                "decided by running the test in a container."
            ),
        },
        {
            "dataset": "Repo brain norms",
            "source": "declared configuration",
            "detail": (
                "Review-history mining needs the GitHub API, which is not reachable from "
                "this machine. The norms shown are the standards the repository declares "
                "in its own config, with the pull requests that last changed them."
            ),
        },
        *provenance,
    ]
    history = {
        "declared-ruff-clean": config_history("pyproject.toml"),
        "declared-types-clean": config_history("pyproject.toml"),
        "declared-tests-exist": config_history("tests"),
        "declared-changelog-for-api": config_history("CHANGES.md"),
    }
    return Collected(findings, links, prs, runs, validation, nodes, provenance, history)


def pipeline_state(
    findings: list[Finding],
    runs: list[SandboxRun],
    validation: list[ValidationRound],
    *,
    skip_docker: bool,
) -> list[PipelineNode]:
    """Each node's real state after this run. Nothing here is decorative."""
    by_kind: dict[str, int] = {}
    for finding in findings:
        by_kind[finding.kind] = by_kind.get(finding.kind, 0) + 1
    probe_count = sum(
        by_kind.get(kind, 0) for kind in ("api_change", "coverage_gap", "lint_regression")
    )
    generated = sum(len(round_.generated) for round_ in validation)
    discarded = sum(len(round_.base_failed) for round_ in validation)
    survivors = sum(len(round_.survivors) for round_ in validation)
    timeout_run = next((r for r in runs if r.id == "run-timeout"), None)
    oom_run = next((r for r in runs if r.id == "run-oom"), None)

    def run_seconds(prefix: str) -> float | None:
        matched = [r.result.duration_s for r in runs if r.id.startswith(prefix)]
        return round(sum(matched), 1) if matched else None

    head_status = "complete"
    head_detail = f"{survivors} survivor(s) run against head"
    if timeout_run is not None and oom_run is not None:
        head_detail += "; 1 timeout, 1 OOM recorded"

    return [
        PipelineNode("pr", "complete", "base..head resolved from the merge's first parent"),
        PipelineNode(
            "c2",
            "complete",
            "changed symbols plus callers, 2 hops",
            count=None,
        ),
        PipelineNode(
            "c3",
            "complete" if generated else "idle",
            f"{generated} candidate test(s) replayed from a recorded generation",
            count=generated or None,
        ),
        PipelineNode(
            "c1base",
            "complete" if generated else "idle",
            "candidates executed against base",
            duration_s=run_seconds("run-char-base"),
        ),
        PipelineNode(
            "filter",
            "complete" if generated else "idle",
            f"{discarded} discarded as imagined behaviour, {survivors} kept",
            count=discarded or None,
        ),
        PipelineNode(
            "c1head",
            head_status if generated else "idle",
            head_detail,
            duration_s=run_seconds("run-char-head"),
        ),
        PipelineNode(
            "c4",
            "complete" if by_kind.get("behavior_change") else "idle",
            f"{by_kind.get('behavior_change', 0)} behaviour change(s)",
            count=by_kind.get("behavior_change"),
        ),
        PipelineNode(
            "c5",
            "failed",
            "GitHub API unreachable from this machine: no review history harvested",
        ),
        PipelineNode("c6", "idle", "nothing to filter without harvested comments"),
        PipelineNode("c7", "idle", "no clusters: mining needs harvested comments"),
        PipelineNode(
            "c8",
            "complete",
            "declared standards only, with the pull requests that last changed them",
        ),
        PipelineNode(
            "c9",
            "complete" if not skip_docker else "idle",
            f"{probe_count} observation(s) from api, coverage and lint probes",
            count=probe_count or None,
        ),
        PipelineNode(
            "c10",
            "complete",
            f"{len(findings)} finding(s) ranked",
            count=len(findings),
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=len(PRS))
    parser.add_argument("--skip-docker", action="store_true")
    args = parser.parse_args()

    os.environ.setdefault("PRFLAGGER_MAX_SYMBOLS", "2")
    collected = collect(args.limit, skip_docker=args.skip_docker)

    store = SqliteGraphStore(cache_root() / "brain" / "store.sqlite")
    tree = worktree(pr_metadata(PRS[0])["head"])
    norms = declarative_norms(tree)
    store.put_norms(SLUG, norms)
    ordered = rank(collected.findings, norms, store)

    # rank returns the same findings, reordered; the links are keyed by original index.
    index_of = {id(finding): index for index, finding in enumerate(collected.findings)}
    links = {
        position: collected.links.get(index_of[id(finding)], FindingLink())
        for position, finding in enumerate(ordered)
    }

    profile = build_profile(SLUG, tree, norms, prs_analyzed=len(collected.prs))
    profile["coverage"] = {
        "verified": [
            {
                "module": "api surface (C9)",
                "detail": "ast diff of public symbols, both commits",
            },
            {"module": "coverage (C9)", "detail": "the repo's own suite under coverage.py"},
            {"module": "lint and types (C9)", "detail": "the repo's own ruff and mypy config"},
            {
                "module": "behaviour (C3/C4)",
                "detail": f"{len(collected.validation)} symbol(s) characterised and executed",
            },
        ],
        "skipped": [
            {
                "module": "review-history norms (C5-C7)",
                "reason": "the GitHub API is not reachable from this machine",
            },
            {
                "module": "behaviour on every other changed symbol",
                "reason": (
                    "characterisation is capped per run; what was skipped is counted, "
                    "not hidden"
                ),
            },
        ],
    }
    profile["norm_history"] = collected.norm_history

    payload = build_payload(
        ordered,
        profile,
        links=links,
        prs=collected.prs,
        runs=collected.runs,
        validation=collected.validation,
        nodes=collected.nodes,
        provenance=collected.provenance,
    )
    write_payload(payload, OUT_JSON)
    render_app(payload, OUT_HTML)
    print(f"\n{len(ordered)} finding(s) -> {OUT_JSON}")
    print(f"interface -> {OUT_HTML}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
