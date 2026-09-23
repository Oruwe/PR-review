"""The interface: one self-contained HTML file with the findings document inlined.

`render` (report/render.py) writes the static report — the thing you attach to a review.
This writes the product surface: the same facts, as an instrument you can interrogate.

There is no backend, no build step and no CDN. The page is `app.html` with the findings
document substituted in, so the file opens from disk and behaves identically offline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["PAYLOAD_MARKER", "TEMPLATE_PATH", "render_app", "template"]

TEMPLATE_PATH = Path(__file__).with_name("app.html")

PAYLOAD_MARKER = "/*__PRFLAGGER_PAYLOAD__*/null"


def template() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def render_app(payload: dict[str, Any], out: Path) -> Path:
    """Write the interface for `payload`. Returns the path written.

    The document is inlined as JSON inside a script tag, so `</script>` and the two
    line-separator characters JSON leaves raw have to be escaped — otherwise a diff hunk
    quoted in a finding could close the tag it is inside.
    """
    if PAYLOAD_MARKER not in (source := template()):
        raise ValueError(f"{TEMPLATE_PATH.name} has no payload marker to substitute")
    document = (
        json.dumps(payload, ensure_ascii=False)
        .replace("</", "<\\/")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(source.replace(PAYLOAD_MARKER, document), encoding="utf-8")
    return out
