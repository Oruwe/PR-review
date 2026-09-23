"""A local HTTP server that answers the Anthropic Messages API.

Not a mock of our code: `llm.provider.bedrock_mantle` builds the real
`AnthropicBedrockMantle` client, which serialises the real request and parses
the real response shape — pointed at this server instead of AWS. What it cannot
prove is that Bedrock accepts our credentials; `prflagger llm check` proves that
against the real endpoint.
"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

Responder = Callable[[dict[str, Any]], "tuple[int, dict[str, Any]]"]


@dataclass
class RecordedModel:
    """`respond` maps a request body to (status, response body)."""

    respond: Responder
    requests: list[dict[str, Any]] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)
    base_url: str = ""


def message(text: str, *, model: str = "anthropic.claude-haiku-4-5", input_tokens: int = 120,
            output_tokens: int = 30, cache_read: int = 0, cache_write: int = 0
            ) -> tuple[int, dict[str, Any]]:
    return 200, {
        "id": "msg_local", "type": "message", "role": "assistant", "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_write,
        },
    }


def refusal(status: int, text: str, kind: str = "authentication_error"
            ) -> tuple[int, dict[str, Any]]:
    return status, {"type": "error", "error": {"type": kind, "message": text}}


@contextmanager
def serve(model: RecordedModel) -> Iterator[RecordedModel]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server's naming
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            model.requests.append(body)
            model.headers.append({k.lower(): v for k, v in self.headers.items()})
            status, payload = model.respond(body)
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    model.base_url = f"http://127.0.0.1:{server.server_port}/anthropic"
    try:
        yield model
    finally:
        server.shutdown()
        server.server_close()
