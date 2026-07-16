"""Transport: retry, failover, and the decision log.

Read `complete()` once and the demo's claim should be self-evident: there is no
`if lane.name == "routed"` in here. There is a list of candidate endpoints and a
loop. The single lane's list has one element, so the `advance to next candidate`
branch is simply unreachable for it, and it burns its whole budget re-dialling a
backend that is already down.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

import config
from lanes import Endpoint, Lane


class AgentHalted(RuntimeError):
    """Retry/failover budget exhausted. The task does not complete."""


@dataclass
class Event:
    ts: float
    lane: str
    step: str
    kind: str  # attempt | routing | halt | done
    message: str
    attempt: int | None = None
    status: int | None = None
    model: str | None = None

    def as_json(self) -> dict:
        d = {
            "ts": round(self.ts, 3),
            "iso": time.strftime("%H:%M:%S", time.localtime(self.ts)),
            "lane": self.lane,
            "step": self.step,
            "kind": self.kind,
            "message": self.message,
        }
        for k in ("attempt", "status", "model"):
            if getattr(self, k) is not None:
                d[k] = getattr(self, k)
        return d


@dataclass
class Usage:
    # `requests` counts every HTTP attempt, including the ones that 429'd. A
    # failed request still costs you a connection, a timeout, and a customer --
    # counting only the successes would flatter the single lane.
    requests: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    by_model: dict[str, int] = field(default_factory=dict)

    def attempted(self, model: str) -> None:
        self.requests += 1
        self.by_model.setdefault(model, 0)

    def succeeded(self, model: str, tin: int, tout: int) -> None:
        self.tokens_in += tin
        self.tokens_out += tout
        self.by_model[model] = self.by_model.get(model, 0) + 1
        p = config.PRICE_PER_MTOK.get(model)
        if p:
            self.cost_usd += (tin * p["in"] + tout * p["out"]) / 1_000_000


# Errors we consider worth another shot -- either on this backend or a different
# one. A 400 (bad request) is our bug and retrying it is just noise.
def _is_retryable(status: int) -> bool:
    return status == 429 or status >= 500


class Client:
    def __init__(
        self,
        lane: Lane,
        api_key: str,
        emit: Callable[[Event], None],
        proxy_base: str = config.PROXY_BASE_URL,
    ) -> None:
        self.lane = lane
        self._api_key = api_key  # never logged, never echoed into an Event
        self.emit = emit
        self.proxy_base = proxy_base
        self.usage = Usage()
        self._http = httpx.Client(timeout=config.REQUEST_TIMEOUT_S)

    def close(self) -> None:
        self._http.close()

    def _post(self, ep: Endpoint, messages: list[dict]) -> httpx.Response:
        return self._http.post(
            ep.url(self.proxy_base),
            json={
                "model": ep.model,
                "messages": messages,
                "temperature": 0,  # determinism: same prompt -> same answer
                "max_tokens": config.MAX_TOKENS,
            },
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                # Demo instrumentation, not part of the real API. The proxy keys
                # its fault counter on the lane so the two lanes cannot race each
                # other's counters -- that's what keeps concurrent runs
                # reproducible.
                "X-Demo-Lane": self.lane.name,
            },
        )

    def complete(self, step: str, messages: list[dict]) -> str:
        """Run one model call to completion, or raise AgentHalted trying."""
        idx = 0  # index into lane.candidates

        for attempt in range(1, config.MAX_ATTEMPTS + 1):
            ep = self.lane.candidates[idx]
            self.usage.attempted(ep.model)
            try:
                r = self._post(ep, messages)
                status, err = r.status_code, None
            except (httpx.TimeoutException, httpx.TransportError) as e:
                status, r, err = 0, None, type(e).__name__

            if status == 200:
                body = r.json()
                u = body.get("usage", {})
                self.usage.succeeded(
                    ep.model,
                    u.get("prompt_tokens", 0),
                    u.get("completion_tokens", 0),
                )
                choice = body["choices"][0]

                # A 200 that ran out of tokens is a failure wearing a success's
                # status code. Say so here, at the source, rather than let a
                # half-written JSON object explode in the parser later.
                if choice.get("finish_reason") == "length":
                    self._ev(step, "halt",
                             f"200 but truncated at max_tokens="
                             f"{config.MAX_TOKENS}  ✗ AGENT HALTED",
                             attempt, 200, ep.model)
                    raise AgentHalted(
                        f"{step}: {ep.model} hit max_tokens={config.MAX_TOKENS} "
                        f"mid-answer. Raise config.MAX_TOKENS -- reasoning models "
                        f"spend tokens before they emit any answer.")

                self._ev(step, "attempt", "200 OK  ✓", attempt, 200, ep.model)
                return choice["message"]["content"]

            if not err and not _is_retryable(status):
                self._ev(step, "halt", f"{status} (not retryable)  ✗ AGENT HALTED",
                         attempt, status, ep.model)
                raise AgentHalted(f"{step}: {status}")

            label = err or _status_label(status)
            self._ev(step, "attempt", f"-> {label}", attempt, status or None, ep.model)

            if attempt == config.MAX_ATTEMPTS:
                break

            # --- the fork in the road ---------------------------------------
            # Somewhere else to go? Route around the failure. Nowhere to go?
            # Sit in a backoff and hope. Same code, different candidate list.
            if idx + 1 < len(self.lane.candidates):
                idx += 1
                nxt = self.lane.candidates[idx]
                self._ev(
                    step, "routing",
                    f"routing decision: failover -> {nxt.model}",
                    model=nxt.model,
                )
                continue  # no backoff: a healthy backend is not rate limiting us

            wait = config.BACKOFF_S[min(attempt - 1, len(config.BACKOFF_S) - 1)]
            self._ev(step, "attempt", f"   (backoff {wait:g}s, no alternate)",
                     attempt, status or None, ep.model)
            time.sleep(wait)

        self._ev(step, "halt", "✗ AGENT HALTED — retry budget exhausted",
                 config.MAX_ATTEMPTS, None, self.lane.candidates[idx].model)
        raise AgentHalted(f"{step}: exhausted {config.MAX_ATTEMPTS} attempts")

    def _ev(self, step, kind, message, attempt=None, status=None, model=None) -> None:
        self.emit(Event(time.time(), self.lane.name, step, kind, message,
                        attempt, status, model))


def _status_label(status: int) -> str:
    return {
        429: "429 Too Many Requests",
        500: "500 Internal Server Error",
        502: "502 Bad Gateway",
        503: "503 Service Unavailable",
        504: "504 Gateway Timeout",
    }.get(status, f"{status}")
