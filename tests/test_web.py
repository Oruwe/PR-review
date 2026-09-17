"""The interface: the document it renders from, and the page itself.

Two things are checked here that a screenshot cannot: that the findings document
carries the four-field contract for every finding it projects, and that the page's
colour pairs actually reach WCAG AA rather than being assumed to.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from prflagger.models import Finding, Job, Norm, Outcome, Symbol, TestResult
from prflagger.report.payload import (
    NODE_FOR_KIND,
    FindingLink,
    PipelineNode,
    PrRecord,
    SandboxRun,
    ValidationRound,
    build_payload,
    finding_dict,
    write_payload,
)
from prflagger.report.web import PAYLOAD_MARKER, TEMPLATE_PATH, render_app, template

NORM = Norm(
    id="declared-tests-exist",
    statement="Every changed symbol must be executed by a test.",
    scope="repo",
    support=7,
    distinct_reviewers=3,
    confidence=0.7,
    evidence_prs=(3695, 3637),
)

PROFILE = {"repo": "pallets/click", "generated_at": "2026-09-17", "prs_analyzed": 15}


def finding(**overrides: object) -> Finding:
    fields: dict[str, object] = {
        "kind": "coverage_gap",
        "symbol": "click.core.Option.get_help_spec",
        "what_changed": "changed and no test executes it",
        "how_we_know": "coverage.py: 0 of 7 body lines executed",
        "norm": NORM,
        "confidence": 1.0,
        "severity": 0.5,
    }
    fields.update(overrides)
    return Finding(**fields)  # type: ignore[arg-type]


def job() -> Job:
    return Job(
        repo_path="/tmp/wt",
        commit="4295457ddc7b5ca9a733d59ca40d9651b02b9b1d",
        image_key="a" * 64,
        command=("pytest", "-v"),
        timeout_s=17,
        memory_mb=275,
    )


def run(outcome: Outcome, lines: tuple[tuple[float, str], ...]) -> SandboxRun:
    return SandboxRun(
        id="run-x",
        label="a run",
        job=job(),
        result=TestResult(
            outcome=outcome,
            per_test={"t.py::a": "passed", "t.py::b": "failed", "t.py::c": "skipped"},
            duration_s=1.25,
            peak_rss_mb=None,
            stdout="",
            stderr="",
        ),
        lines=lines,
        image="prflagger:abc",
        exit_code=1,
    )


# --------------------------------------------------------------------------------------
# the findings document
# --------------------------------------------------------------------------------------


def test_every_projected_finding_carries_the_four_fields() -> None:
    payload = build_payload([finding(), finding(kind="behavior_change", norm=None)], PROFILE)
    assert len(payload["findings"]) == 2
    for projected in payload["findings"]:
        assert projected["what_changed"]
        assert projected["how_we_know"]
        assert "norm" in projected
        assert 0.0 <= projected["confidence"] <= 1.0
        assert projected["node"] in {node["id"] for node in payload["pipeline"]["nodes"]}


def test_a_finding_with_no_norm_is_refused_unless_it_is_a_behaviour_change() -> None:
    broken = finding()
    object.__setattr__(broken, "norm", None)
    with pytest.raises(ValueError, match="no norm"):
        finding_dict(broken, 0)


def test_a_finding_with_an_unknown_kind_has_nowhere_to_come_from() -> None:
    odd = finding()
    object.__setattr__(odd, "kind", "vibes")
    with pytest.raises(ValueError, match="no node"):
        finding_dict(odd, 0)


def test_the_document_preserves_the_ranked_order() -> None:
    # Ranking is the pipeline's decision; the interface renders it, it does not re-sort.
    first = finding(symbol="click.a")
    second = finding(symbol="click.b")
    payload = build_payload([first, second], PROFILE)
    assert [row["symbol"] for row in payload["findings"]] == ["click.a", "click.b"]


def test_each_kind_names_the_component_that_emits_it() -> None:
    assert NODE_FOR_KIND["behavior_change"] == "c4"
    assert NODE_FOR_KIND["coverage_gap"] == "c9"
    assert NODE_FOR_KIND["timeout"] == "c1head"


def test_a_finding_keeps_its_provenance_links() -> None:
    payload = build_payload(
        [finding()],
        PROFILE,
        links={
            0: FindingLink(
                pr=3821,
                run="run-x",
                log_line=12,
                file="src/click/core.py",
                line_start=2054,
                line_end=2060,
                url="https://github.com/pallets/click/blob/abc/src/click/core.py#L2054-L2060",
            )
        },
    )
    projected = payload["findings"][0]
    assert projected["pr"] == 3821
    assert projected["run"] == "run-x"
    assert projected["log_line"] == 12
    assert projected["where"]["file"] == "src/click/core.py"
    assert projected["where"]["url"].endswith("#L2054-L2060")


def test_timeout_and_oom_survive_projection_as_outcomes() -> None:
    for outcome in (Outcome.TIMEOUT, Outcome.OOM, Outcome.INSTALL_FAILED):
        projected = run(outcome, ()).as_dict()
        assert projected["outcome"] == outcome.value
        assert projected["network"] == "none"
        assert projected["read_only_root"] is True


def test_skipped_tests_are_not_counted_as_failures() -> None:
    projected = run(Outcome.FAILED, ()).as_dict()
    assert projected["tests_total"] == 3
    assert projected["tests_passed"] == 1
    assert projected["tests_failed"] == 1
    assert projected["tests_skipped"] == 1
    assert projected["failed_nodeids"] == ["t.py::b"]
    # The whole per-test map would outweigh the log it belongs to; the counts and the
    # failing nodeids are what the page reads, and the job cache keeps the rest.
    assert "per_test" not in projected


def test_the_machine_report_line_is_replaced_rather_than_replayed() -> None:
    report = '{"created": 1.0, "tests": [' + "x" * 5000 + "]}"
    projected = run(Outcome.PASSED, ((0.1, "collected 2 items"), (0.2, report))).as_dict()
    assert projected["lines"][0]["text"] == "collected 2 items"
    assert projected["lines"][1]["omitted"] is True
    assert "pytest json report" in projected["lines"][1]["text"]
    assert "xxxx" not in projected["lines"][1]["text"]


def test_the_report_is_split_off_even_when_welded_to_a_test_line() -> None:
    # pytest writes the report without a leading newline, so it normally arrives stuck
    # to the end of the last test's line. Left alone it would be a megabyte of one line.
    report = '{"created": 1.0, "tests": [' + "x" * 5000 + "]}"
    welded = "tests/test_style.py::test_ansi PASSED [100%]" + report
    projected = run(Outcome.PASSED, ((6.27, welded),)).as_dict()
    assert projected["lines"][0]["text"] == "tests/test_style.py::test_ansi PASSED [100%]"
    assert projected["lines"][0]["t"] == 6.27
    assert projected["lines"][1]["omitted"] is True
    assert "xxxx" not in json.dumps(projected["lines"])


def test_line_times_are_kept_so_the_replay_can_be_a_replay() -> None:
    projected = run(Outcome.PASSED, ((0.0, "a"), (1.5, "b"), (6.25, "c"))).as_dict()
    assert [row["t"] for row in projected["lines"]] == [0.0, 1.5, 6.25]


def test_an_unknown_pipeline_node_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown pipeline node"):
        PipelineNode("c99", "complete").as_dict()


def test_the_validation_round_reports_what_was_discarded() -> None:
    round_ = ValidationRound(
        symbol="click._utils.Sentinel",
        attempts=({"attempt": 1, "ran": 9, "passed": 6, "failed": 3, "where": "base"},),
        generated=("a", "b"),
        base_failed=({"name": "b", "reason": "failed"},),
        survivors=("a",),
        head_failed=({"name": "a", "reason": "failed", "evidence": "assert"},),
        discard_rate=0.25,
    )
    projected = round_.as_dict()
    assert projected["discard_rate"] == 0.25
    assert projected["base_failed"][0]["name"] == "b"
    assert projected["head_failed"][0]["evidence"] == "assert"


def test_the_pr_record_carries_the_structural_facts() -> None:
    symbol = Symbol(
        fqn="click._utils.Sentinel",
        kind="class",
        file="src/click/_utils.py",
        line_start=7,
        line_end=30,
    )
    projected = PrRecord(
        number=3805,
        title="Fix copy, deepcopy and pickle of Sentinel members",
        author="Kevin Deldycke",
        branch="fix-sentinel",
        base="3cbcf9b115",
        head="4295457ddc",
        merged_at="2026-08-29T08:12:36-07:00",
        url="https://github.com/pallets/click/pull/3805",
        changed_symbols=(symbol,),
        edges=(("click.core.Option.consume_value", "click._utils.Sentinel"),),
        insertions=60,
        deletions=0,
    ).as_dict()
    assert projected["changed_symbols"][0]["line_start"] == 7
    assert projected["edges"] == [["click.core.Option.consume_value", "click._utils.Sentinel"]]
    assert projected["insertions"] == 60


def test_write_payload_round_trips(tmp_path: Path) -> None:
    payload = build_payload([finding()], PROFILE, runs=[run(Outcome.PASSED, ((0.0, "a"),))])
    path = write_payload(payload, tmp_path / "nested" / "findings.json")
    assert json.loads(path.read_text(encoding="utf-8")) == payload


# --------------------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------------------


def test_the_page_opens_standalone_with_no_network_requests(tmp_path: Path) -> None:
    out = tmp_path / "index.html"
    render_app(build_payload([finding()], PROFILE), out)
    html = out.read_text(encoding="utf-8")

    assert "<style>" in html, "styling must be inline"
    assert not re.search(r"<script[^>]+\bsrc\s*=", html, re.IGNORECASE)
    assert not re.search(r"<link[^>]+stylesheet", html, re.IGNORECASE)
    assert "@import" not in html
    assert not re.search(r"<img[^>]+src\s*=\s*[\"']https?://", html, re.IGNORECASE)
    # Citations are links, which is the point of a citation; they are never fetched.
    assert not re.search(r"\bfetch\s*\(|XMLHttpRequest|EventSource", html)


def test_content_cannot_close_the_script_tag_it_is_inlined_in(tmp_path: Path) -> None:
    hostile = finding(
        kind="behavior_change",
        norm=None,
        what_changed="</script><script>alert(1)</script>",
        how_we_know="  line separator  ",
    )
    out = tmp_path / "index.html"
    render_app(build_payload([hostile], PROFILE), out)
    html = out.read_text(encoding="utf-8")

    # Script data only ends at `</`, so escaping that is what keeps a quoted diff hunk
    # inside the element it lives in. The page's own closing tag must be the only one.
    assert html.count("</script>") == 1
    assert "<\\/script>" in html
    assert "\\u2028" in html

    # And the content survives intact: escaped for the parser, not mangled for the reader.
    inlined = html.split("const DATA = ", 1)[1].split(";\n", 1)[0].replace("<\\/", "</")
    document = json.loads(inlined)
    assert document["findings"][0]["what_changed"] == "</script><script>alert(1)</script>"


def test_the_payload_marker_is_substituted_not_left_behind(tmp_path: Path) -> None:
    out = tmp_path / "index.html"
    render_app(build_payload([finding()], PROFILE), out)
    html = out.read_text(encoding="utf-8")
    assert PAYLOAD_MARKER not in html
    assert '"pallets/click"' in html


def test_a_template_without_the_marker_is_a_bug_not_a_silent_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("prflagger.report.web.template", lambda: "<html></html>")
    with pytest.raises(ValueError, match="payload marker"):
        render_app(build_payload([], PROFILE), tmp_path / "index.html")


def test_the_page_issues_no_verdicts() -> None:
    """CLAUDE.md applies to every string in the interface, empty states included."""
    text = template()
    # Words that would turn an observation into a judgement.
    for banned in ("approve", "reject", "LGTM", "looks good", "looks fine", "risky", "score"):
        for match in re.finditer(re.escape(banned), text, re.IGNORECASE):
            line = text[: match.start()].count("\n") + 1
            context = text.splitlines()[line - 1]
            # The only permitted mentions are the ones denying that this page does it.
            assert re.search(r"never|not |no |does not|without", context, re.IGNORECASE), (
                f"{TEMPLATE_PATH.name}:{line} says {banned!r} "
                f"outside a denial: {context.strip()}"
            )


# --------------------------------------------------------------------------------------
# colour: verified, not assumed
# --------------------------------------------------------------------------------------


def _tokens() -> dict[str, str]:
    root = re.search(r":root\s*\{(.*?)\}", template(), re.DOTALL)
    assert root, "the page must define its palette on :root"
    return {
        name: value.strip()
        for name, value in re.findall(r"--([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", root.group(1))
    }


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(foreground: str, background: str) -> float:
    light, dark = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


@pytest.mark.parametrize(
    ("foreground", "background", "minimum"),
    [
        ("text", "bg", 4.5),
        ("text", "surface", 4.5),
        ("text", "surface-2", 4.5),
        ("text-dim", "bg", 4.5),
        ("text-dim", "surface", 4.5),
        ("text-dim", "surface-2", 4.5),
        ("orange", "bg", 4.5),
        ("orange", "surface", 4.5),
        ("orange", "surface-2", 4.5),
        ("orange-mid", "surface", 4.5),
        ("orange-mid", "surface-2", 4.5),
        ("red", "bg", 4.5),
        ("red", "surface", 4.5),
        ("green", "bg", 4.5),
        ("green", "surface", 4.5),
    ],
)
def test_every_text_pair_reaches_wcag_aa(
    foreground: str, background: str, minimum: float
) -> None:
    tokens = _tokens()
    ratio = contrast(tokens[foreground], tokens[background])
    assert ratio >= minimum, f"--{foreground} on --{background} is {ratio:.2f}:1"


def test_neither_pure_black_nor_pure_white_is_used_for_page_or_text() -> None:
    tokens = _tokens()
    assert tokens["bg"] not in {"#000000", "#ffffff", "#FFFFFF"}
    assert tokens["text"] not in {"#000000", "#ffffff", "#FFFFFF"}


# SVG paints text with `fill`, so a token that is fine for a wire is not fine here.
TEXT_CLASSES = ("comp", "lab", "det", "label", "vitals", "marker-more", "name", "note")


def test_the_idle_wiring_is_dim_enough_to_recede_and_carries_no_text() -> None:
    """--orange-dim is the idle wiring. It is deliberately below AA, so no text uses it."""
    tokens = _tokens()
    assert contrast(tokens["orange-dim"], tokens["bg"]) < 3.0

    offenders = []
    for selector, body in re.findall(r"([^{}]+)\{([^}]*)\}", template()):
        if "var(--orange-dim)" not in body:
            continue
        paints_text = re.search(r"(?<![a-z-])color\s*:\s*var\(--orange-dim\)", body) or (
            "fill" in body and any(name in selector for name in TEXT_CLASSES)
        )
        if paints_text:
            offenders.append(selector.strip())
    assert not offenders, f"--orange-dim must not paint text: {offenders}"


def test_every_css_variable_used_is_defined() -> None:
    text = template()
    defined = set(_tokens()) | {
        name for name, _ in re.findall(r"--([a-z0-9-]+):\s*([^;]+);", text)
    }
    used = set(re.findall(r"var\(--([a-z0-9-]+)\)", text))
    assert used <= defined, f"undefined tokens: {sorted(used - defined)}"
