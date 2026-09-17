"""The report: one self-contained HTML file.

No server, no build step, no CDN. It opens from disk and works when the venue wifi does
not. Every finding shows all four contract fields, and the coverage statement says what
was not checked — a tool that states the limits of its own coverage can be trusted; one
that quietly checks less and still shows green manufactures false confidence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, select_autoescape

from prflagger.models import Finding

__all__ = ["TEMPLATE", "citation", "render"]


def citation(evidence_prs: tuple[int, ...]) -> str:
    """The sentence that turns a finding into the maintainers' own words."""
    numbers = ", ".join(f"#{number}" for number in evidence_prs)
    return f"This repo required this in {numbers}."

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PR Flagger — {{ profile.repo or "report" }}</title>
<style>
  :root {
    --bg: #ffffff; --fg: #16181d; --muted: #5b6270; --line: #e2e5ea;
    --card: #f7f8fa; --accent: #1f6feb; --code: #f0f2f5;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #0f1116; --fg: #e6e8ec; --muted: #9aa3b2; --line: #272b34;
      --card: #161920; --accent: #6ea8ff; --code: #1b1f27;
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.6 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
  .wrap { max-width: 860px; margin: 0 auto; padding: 40px 16px 80px; }
  h1 { font-size: 24px; margin: 0 0 4px; letter-spacing: -0.01em; }
  .sub { color: var(--muted); margin: 0 0 32px; font-size: 14px; }
  .finding { border: 1px solid var(--line); border-radius: 10px; padding: 18px 20px;
    margin: 0 0 14px; background: var(--card); }
  .head { display: flex; flex-wrap: wrap; gap: 10px; align-items: baseline;
    justify-content: space-between; margin-bottom: 10px; }
  .kind { font-size: 11px; font-weight: 600; letter-spacing: .09em;
    text-transform: uppercase; color: var(--accent); }
  .conf { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .sym { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 14px;
    margin: 0 0 12px; word-break: break-all; }
  dl { margin: 0; display: grid; grid-template-columns: 116px 1fr; gap: 6px 14px; }
  dt { color: var(--muted); font-size: 12px; text-transform: uppercase;
    letter-spacing: .06em; padding-top: 2px; }
  dd { margin: 0; }
  pre { background: var(--code); border-radius: 6px; padding: 10px 12px; margin: 0;
    overflow-x: auto; font-size: 12.5px; white-space: pre-wrap; word-break: break-word; }
  .cite { font-style: italic; }
  .coverage { border: 1px solid var(--line); border-radius: 10px; padding: 18px 20px;
    margin-top: 32px; }
  .coverage h2 { font-size: 15px; margin: 0 0 10px; }
  table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
  td { padding: 5px 8px 5px 0; vertical-align: top; border-bottom: 1px solid var(--line); }
  td.name { font-family: ui-monospace, Menlo, monospace; white-space: nowrap; }
  .skip { color: var(--muted); }
  .empty { color: var(--muted); padding: 24px 0; }
  @media (max-width: 620px) { dl { grid-template-columns: 1fr; gap: 2px 0; }
    dt { margin-top: 8px; } }
</style>
</head>
<body>
<div class="wrap">
  <h1>{{ profile.repo or "PR Flagger" }}</h1>
  <p class="sub">
    {{ findings|length }} observation{{ "" if findings|length == 1 else "s" }}, each with
    evidence. Profile generated {{ profile.generated_at or "unknown" }} from
    {{ profile.prs_analyzed or 0 }} pull request{{ "" if profile.prs_analyzed == 1 else "s" }}.
  </p>

  {% if not findings %}
    <p class="empty">No observations. This is not an approval — see the coverage
    statement below for what was and was not checked.</p>
  {% endif %}

  {% for f in findings %}
  <div class="finding">
    <div class="head">
      <span class="kind">{{ f.kind.replace("_", " ") }}</span>
      <span class="conf">confidence {{ "%.2f"|format(f.confidence) }}
        &middot; severity {{ "%.2f"|format(f.severity) }}</span>
    </div>
    <p class="sym">{{ f.symbol }}</p>
    <dl>
      <dt>What changed</dt><dd>{{ f.what_changed }}</dd>
      <dt>How we know</dt><dd><pre>{{ f.how_we_know }}</pre></dd>
      <dt>Repo standard</dt>
      <dd>
        {% if f.norm %}
          &ldquo;{{ f.norm.statement }}&rdquo;
          {% if f.norm.evidence_prs %}
            <div class="cite">{{ citation(f.norm.evidence_prs) }}</div>
          {% else %}
            <div class="cite">Declared by the repository&rsquo;s own configuration.</div>
          {% endif %}
        {% else %}
          Not applicable: a behaviour change the pull request did not declare needs no
          external standard to matter.
        {% endif %}
      </dd>
    </dl>
  </div>
  {% endfor %}

  <div class="coverage">
    <h2>Coverage statement</h2>
    {% if coverage.verified or coverage.skipped %}
      <p class="sub" style="margin-bottom:12px">{{ coverage_line }}</p>
      <table>
        {% for row in coverage.verified %}
          <tr><td class="name">{{ row.module }}</td><td>{{ row.detail }}</td></tr>
        {% endfor %}
        {% for row in coverage.skipped %}
          <tr><td class="name skip">{{ row.module }}</td>
              <td class="skip">skipped — {{ row.reason }}</td></tr>
        {% endfor %}
      </table>
    {% else %}
      <p class="sub">No coverage statement was supplied for this run, so nothing here
      should be read as a claim about what was verified.</p>
    {% endif %}
  </div>
</div>
</body>
</html>
"""


def render(findings: list[Finding], profile: dict[str, Any], out: Path) -> None:
    """Jinja2 -> single self-contained HTML file. Every finding shows all four contract
    fields. Norm citations render as 'This repo required this in #412, #457, #490'.
    A findings list containing an invalid Finding must raise, not silently skip."""
    for index, finding in enumerate(findings):
        _validate(index, finding)

    environment = Environment(autoescape=select_autoescape(["html"]))
    coverage = _coverage(profile)
    total = len(coverage["verified"]) + len(coverage["skipped"])
    html = environment.from_string(TEMPLATE).render(
        findings=findings,
        profile=profile,
        coverage=coverage,
        citation=citation,
        coverage_line=(
            f"Verified {len(coverage['verified'])} of {total} changed modules."
        ),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")


def _validate(index: int, finding: Finding) -> None:
    """A finding missing any of the four fields is a bug, not something to skip past."""
    if not isinstance(finding, Finding):
        raise TypeError(f"findings[{index}] is not a Finding: {type(finding).__name__}")
    if not finding.what_changed:
        raise ValueError(f"findings[{index}] ({finding.symbol}) has no what_changed")
    if not finding.how_we_know:
        raise ValueError(f"findings[{index}] ({finding.symbol}) has no how_we_know")
    if finding.norm is None and finding.kind != "behavior_change":
        raise ValueError(
            f"findings[{index}] ({finding.symbol}) is a {finding.kind} with no norm"
        )
    if not 0.0 <= finding.confidence <= 1.0:
        raise ValueError(f"findings[{index}] has confidence {finding.confidence}")


def _coverage(profile: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    raw = profile.get("coverage") or {}
    return {
        "verified": list(raw.get("verified", [])),
        "skipped": list(raw.get("skipped", [])),
    }
