"""A canned OpenAI-compatible upstream so the demo runs on conference WiFi.

MOCK=1 points the proxy here instead of at DigitalOcean. No key, no network, no
egress. The responses are fixed strings, which also means MOCK=1 runs are
byte-identical -- useful when you want to diff two out/*.jsonl files and see
only the thing you changed.

This mock is deliberately dumb: it pattern-matches the prompt to pick a canned
reply. It is a stand-in for an upstream, not a model.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config

RETRIEVE = "doc-2, doc-4"

SUMMARIZE = (
    "Enterprise buyers are blocked on SSO/SAML: four prospects raised it this "
    "week and two called it their only blocker to signing. Batch API users are "
    "also asking for higher rate limits for overnight jobs, and one paying "
    "customer is already evaluating competitors over it."
)

EXTRACT = json.dumps({
    "priority": "P1",
    "theme": "Enterprise readiness: SSO/SAML and API rate limits",
    "affected_area": "auth / API platform",
    "action": "Ship SAML SSO this quarter and offer a raised rate-limit tier "
              "for batch workloads.",
}, indent=2)


def _reply_for(messages: list[dict]) -> str:
    sys = " ".join(m.get("content", "") for m in messages if m.get("role") == "system")
    if "select relevant documents" in sys:
        return RETRIEVE
    if "product analyst" in sys:
        return SUMMARIZE
    return EXTRACT


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        text = _reply_for(body.get("messages", []))

        # A touch of latency so the terminal reads like a real run rather than a
        # wall of instantaneous text. Well under any timeout.
        time.sleep(0.25)

        payload = json.dumps({
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": body.get("model", "mock"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            # Rough but stable token counts so the summary block has real numbers.
            "usage": {
                "prompt_tokens": sum(len(m.get("content", "")) for m in
                                     body.get("messages", [])) // 4,
                "completion_tokens": len(text) // 4,
            },
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def serve(port: int = config.MOCK_PORT, background: bool = False) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((config.MOCK_HOST, port), Handler)
    if background:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    else:
        srv.serve_forever()
    return srv
