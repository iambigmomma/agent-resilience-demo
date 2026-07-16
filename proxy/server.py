"""Deterministic fault injection. Runs as a CLI process or in-process (web).

Sits between the agent and the upstream, forwards everything, and manufactures
the failure itself when the schedule says the primary backend is sick. We inject
rather than provoke a real 429 because a demo that depends on a stranger's rate
limiter firing on cue is not a demo, it's a prayer.

    /u/<alias>/chat/completions  ->  <upstream for alias>/chat/completions

Determinism comes from two choices:
  1. The counter is keyed on (lane, alias), read from the X-Demo-Lane header.
     Both lanes run concurrently; per-lane counters mean neither can perturb the
     other's schedule no matter how the requests interleave.
  2. "After N requests" is a count, not a clock. The only clock is the failure
     window, and config.assert_deterministic() forces that window to outlast the
     single lane's whole retry budget, so it can never close mid-demo.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

import config


@dataclass
class Injection:
    """One run's fault schedule. Rebuilt per run so the web UI can re-arm it."""
    upstream_primary: str
    upstream_alt: str
    fail_after: int = config.FAIL_AFTER
    fail_duration: float = config.FAIL_DURATION_S
    fail_mode: str = config.FAIL_MODE  # 429 | timeout | 5xx | none
    fail_alias: str = config.FAIL_ALIAS

    _counts: dict = field(default_factory=dict, repr=False)
    _windows: dict = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def upstream(self, alias: str) -> str | None:
        return {"primary": self.upstream_primary, "alt": self.upstream_alt}.get(alias)

    def should_fail(self, lane: str, alias: str) -> bool:
        """Same call sequence -> same verdicts, every run."""
        if self.fail_mode == "none" or alias != self.fail_alias:
            return False  # the alternate backend is healthy; that is the point
        with self._lock:
            key = (lane, alias)
            self._counts[key] = self._counts.get(key, 0) + 1
            if self._counts[key] <= self.fail_after:
                return False
            now = time.time()
            start = self._windows.setdefault(lane, now)
            return (now - start) < self.fail_duration


# The live schedule. Swapped wholesale by arm(); never mutated in place.
STATE: Injection | None = None
_state_lock = threading.Lock()


def arm(inj: Injection) -> None:
    """Install a fresh schedule. Counters start at zero, so every run is run #1."""
    global STATE
    with _state_lock:
        STATE = inj


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a) -> None:
        pass  # the agent's decision log is the product; proxy chatter is noise

    def do_POST(self) -> None:
        inj = STATE
        if inj is None:
            return self._send(503, {"error": "injector not armed"})

        parts = self.path.strip("/").split("/", 2)
        if len(parts) < 3 or parts[0] != "u":
            return self._send(404, {"error": "expected /u/<alias>/<path>"})
        alias, rest = parts[1], parts[2]
        up = inj.upstream(alias)
        if not up:
            return self._send(404, {"error": f"unknown alias {alias}"})

        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        lane = self.headers.get("X-Demo-Lane", "unknown")

        if inj.should_fail(lane, alias):
            return self._inject(inj.fail_mode)

        try:
            r = httpx.post(
                f"{up.rstrip('/')}/{rest}",
                content=body,
                headers={
                    # Pass the caller's credential straight through. We never
                    # parse, store, or log it.
                    "Authorization": self.headers.get("Authorization", ""),
                    "Content-Type": "application/json",
                },
                timeout=config.REQUEST_TIMEOUT_S * 2,
            )
            self._send_raw(r.status_code, r.content)
        except httpx.HTTPError as e:
            self._send(502, {"error": f"upstream unreachable: {type(e).__name__}"})

    def _inject(self, mode: str) -> None:
        if mode == "429":
            self._send(429, {"error": {"message": "Rate limit exceeded",
                                       "type": "rate_limit_error"}},
                       extra={"Retry-After": "20"})
        elif mode == "5xx":
            self._send(503, {"error": {"message": "Service unavailable",
                                       "type": "server_error"}})
        elif mode == "timeout":
            # Go silent past the client's patience, then answer into the void.
            time.sleep(config.REQUEST_TIMEOUT_S + 1.5)
            try:
                self._send(504, {"error": {"message": "Gateway timeout"}})
            except (BrokenPipeError, ConnectionResetError):
                pass  # client already gave up; that was the intent

    def _send(self, code: int, payload: dict, extra: dict | None = None) -> None:
        self._send_raw(code, json.dumps(payload).encode(), extra)

    def _send_raw(self, code: int, body: bytes, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


def serve(port: int = config.PROXY_PORT, background: bool = False) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((config.PROXY_HOST, port), Handler)
    if background:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    else:
        srv.serve_forever()
    return srv
