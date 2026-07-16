"""The dashboard: same demo, streamed to a browser instead of a terminal.

Everything that matters is imported, not reimplemented. `agent.py`, `client.py`
and `lanes.py` are byte-identical to what `make demo` runs -- this file only
arms the injector, runs the two lanes, and forwards their decision log over SSE.
If the web build had its own agent, the demo's central claim ("same agent code")
would be a lie the moment you opened a browser.

One container runs all three pieces (dashboard + injector + mock upstream),
because App Platform deploys one container and a demo that needs a process
manager is a demo that breaks on stage.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import time
from pathlib import Path

import httpx

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

import agent
import config
import mock.server
import proxy.server
from client import AgentHalted, Client, Event
from lanes import LANES

HERE = Path(__file__).parent

# Only one run at a time. Two concurrent runs would share the injector's
# counters and desync each other -- the exact thing per-lane counting prevents
# *within* a run. A demo has one presenter; this is not a limitation.
RUN_LOCK = threading.Lock()


def _lane_worker(lane, api_key: str, q: "queue.Queue", results: dict) -> None:
    c = Client(lane, api_key, q.put)
    t0 = time.time()
    record, err = None, None
    try:
        record = agent.run(c)
    except AgentHalted as e:
        err = str(e)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    finally:
        c.close()

    completed = err is None and agent.is_complete(record)
    q.put(Event(time.time(), lane.name, "task", "done" if completed else "halt",
                "TASK COMPLETE" if completed else "TASK FAILED"))
    results[lane.name] = {
        "type": "result", "lane": lane.name, "completed": completed,
        "wall_s": round(time.time() - t0, 2), "requests": c.usage.requests,
        "tokens_in": c.usage.tokens_in, "tokens_out": c.usage.tokens_out,
        "cost_usd_est": round(c.usage.cost_usd, 6),
        "models_used": list(c.usage.by_model), "record": record, "error": err,
    }


async def run_stream(request: Request) -> StreamingResponse:
    p = request.query_params
    fail_mode = p.get("fail_mode", "429")
    mock_mode = p.get("mock", "0") == "1"
    fail_after = int(p.get("fail_after", config.FAIL_AFTER))

    if fail_mode not in ("429", "timeout", "5xx", "none"):
        return JSONResponse({"error": "bad fail_mode"}, status_code=400)

    key = os.environ.get(config.API_KEY_ENV, "").strip()
    if not mock_mode and not key:
        return JSONResponse(
            {"error": f"{config.API_KEY_ENV} not set on the server. "
                      f"Use the MOCK toggle, or set the env var."},
            status_code=400)

    upstream = config.MOCK_BASE_URL if mock_mode else config.DO_INFERENCE_BASE_URL

    async def gen():
        if not RUN_LOCK.acquire(blocking=False):
            yield _sse({"type": "busy"})
            return
        try:
            proxy.server.arm(proxy.server.Injection(
                upstream_primary=upstream, upstream_alt=upstream,
                fail_after=fail_after, fail_mode=fail_mode,
                fail_duration=config.FAIL_DURATION_S,
            ))

            q: "queue.Queue[Event]" = queue.Queue()
            results: dict = {}
            yield _sse({
                "type": "run_start", "fail_mode": fail_mode, "mock": mock_mode,
                "fail_after": fail_after, "task": agent.TASK,
                "steps": ["retrieve", "summarize", "extract"],
                "lanes": [{"name": l.name, "blurb": l.blurb,
                           "models": [c.model for c in l.candidates]} for l in LANES],
            })

            threads = [threading.Thread(target=_lane_worker,
                                        args=(l, key or "mock", q, results),
                                        daemon=True) for l in LANES]
            for t in threads:
                t.start()

            while any(t.is_alive() for t in threads) or not q.empty():
                try:
                    e = q.get_nowait()
                    yield _sse({"type": "event", **e.as_json()})
                except queue.Empty:
                    await asyncio.sleep(0.05)  # let the loop breathe

            for r in results.values():
                yield _sse(r)
            yield _sse({"type": "run_end"})
        finally:
            RUN_LOCK.release()

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",  # or the proxy in front will swallow the stream
    })


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


# --------------------------------------------------------------------------
# Act 2: model adoption via the real Inference Router.
#
# Two lanes stream the same requests. "pinned" calls one hard-coded model --
# adopting a new model there means an edit and a redeploy. "routed" calls
# `router:<name>`; mid-stream we reorder the router's ranking through the
# control-plane API and the very next responses come from the new model.
# No client change, no deploy, no failed request. The ranking is restored at
# the end so the demo re-runs cleanly.
# --------------------------------------------------------------------------

def _router_headers() -> dict:
    return {"Authorization": f"Bearer {os.environ[config.API_TOKEN_ENV]}",
            "Content-Type": "application/json"}


def _router_body(models: list[str]) -> dict:
    return {
        "name": config.ROUTER_NAME,
        "description": "agent-resilience-demo: customer feedback insights",
        "policies": [{
            "custom_task": {
                "name": "feedback-insights",
                "description": "Review customer feedback: select docs, "
                               "summarize, emit a structured product insight"},
            "models": models}],
        "fallback_models": ["mistral-3-14B"],
    }


# Sync httpx run via asyncio.to_thread, matching the failover scenario's
# battle-tested path -- one HTTP stack for the whole repo. (An earlier version
# used AsyncClient and got blamed for a wave of ReadTimeouts; the real culprit
# turned out to be the upstream model flapping -- async calls sampled its bad
# minutes, a sync probe sampled a good one. The uniform stack is still the
# right call, just not for the reason first written here.)

def _ensure_router(models: list[str]) -> str:
    """Find the demo router, creating it if absent. Returns its uuid.
    Self-provisioning keeps a fork of this repo runnable without a setup doc."""
    r = httpx.get(f"{config.DO_API_BASE}/gen-ai/models/routers?per_page=200",
                  headers=_router_headers(), timeout=30)
    r.raise_for_status()
    for router in r.json().get("model_routers") or []:
        if router["name"] == config.ROUTER_NAME:
            return router["uuid"]
    r = httpx.post(f"{config.DO_API_BASE}/gen-ai/models/routers",
                   headers=_router_headers(), json=_router_body(models), timeout=30)
    r.raise_for_status()
    return r.json()["model_router"]["uuid"]


def _set_ranking(uuid: str, models: list[str]) -> int:
    r = httpx.put(f"{config.DO_API_BASE}/gen-ai/models/routers/{uuid}",
                  headers=_router_headers(), json=_router_body(models), timeout=30)
    return r.status_code


def _mig_call(api_key: str, model: str, i: int) -> dict:
    """One request, one honest retry. Any production client retries a transient
    timeout; hiding that would be dishonest, so the retry is flagged in the
    result and the UI shows it on the cell."""
    t0 = time.time()
    last: dict = {}
    for attempt in (1, 2):
        try:
            r = httpx.post(
                f"{config.DO_INFERENCE_BASE_URL}/chat/completions",
                json={"model": model,
                      "messages": [{"role": "user",
                                    "content": f"Feedback item #{i}: enterprise "
                                               f"prospects keep requesting SSO/SAML. "
                                               f"One sentence: what should we "
                                               f"prioritize?"}],
                      "temperature": 0, "max_tokens": 60},
                headers={"Authorization": f"Bearer {api_key}"}, timeout=45)
            if r.status_code == 200:
                return {"ok": True, "served": r.json().get("model"),
                        "status": 200, "ms": int((time.time() - t0) * 1000),
                        "retried": attempt > 1,
                        # Server-side attribution: which router task matched.
                        # This is the receipt that the ROUTER picked the model
                        # -- the console has no per-request router log, so the
                        # response headers are the audit trail.
                        "route": r.headers.get("x-model-router-selected-route")}
            last = {"ok": False, "served": None, "status": r.status_code,
                    "ms": int((time.time() - t0) * 1000), "retried": attempt > 1}
        except httpx.HTTPError as e:
            last = {"ok": False, "served": None, "status": 0,
                    "ms": int((time.time() - t0) * 1000),
                    "err": type(e).__name__, "retried": attempt > 1}
    return last


async def migrate_stream(request: Request) -> StreamingResponse:
    mock = request.query_params.get("mock", "0") == "1"
    api_key = os.environ.get(config.API_KEY_ENV, "").strip()
    api_token = os.environ.get(config.API_TOKEN_ENV, "").strip()

    OLD, NEW = config.MIGRATE_OLD_MODEL, config.MIGRATE_NEW_MODEL
    N, FLIP = config.MIGRATE_REQUESTS, config.MIGRATE_FLIP_AFTER

    async def gen():
        # Errors travel inside the stream: an EventSource onerror carries no
        # body, and having the client re-fetch the endpoint to read the reason
        # would quietly start a second run.
        if not mock and not (api_key and api_token):
            missing = config.API_KEY_ENV if not api_key else config.API_TOKEN_ENV
            yield _sse({"type": "mig_error",
                        "error": f"{missing} not set on the server. Use the MOCK "
                                 f"toggle, or set it (the adoption scenario drives "
                                 f"the real Inference Router, which needs both "
                                 f"credentials)."})
            return
        if not RUN_LOCK.acquire(blocking=False):
            yield _sse({"type": "busy"})
            return
        try:
            yield _sse({"type": "mig_start", "n": N, "old": OLD, "new": NEW,
                        "router": config.ROUTER_NAME, "mock": mock})

            uuid = None
            if mock:
                yield _sse({"type": "mig_status",
                            "msg": "offline mock — canned rhythm, no network"})
            else:
                # The setup round-trips take a few seconds; say so, or the
                # page looks dead exactly when the presenter needs it alive.
                yield _sse({"type": "mig_status",
                            "msg": "verifying router config on the control plane…"})
                uuid = await asyncio.to_thread(_ensure_router, [OLD, NEW])
                await asyncio.to_thread(_set_ranking, uuid, [OLD, NEW])
                yield _sse({"type": "mig_status",
                            "msg": "baseline ranking set — streaming live requests"})

            # The lanes deliberately do NOT run in lockstep: pairing them
            # would clamp both to the slowest request, and the divergence --
            # routed racing ahead on the new model while pinned grinds -- is
            # the story.
            q: asyncio.Queue = asyncio.Queue()
            MIG_FLIPPED = asyncio.Event()

            async def call(lane: str, i: int) -> dict:
                if mock:
                    await asyncio.sleep(0.5 if lane == "pinned" else 0.4)
                    flipped = MIG_FLIPPED.is_set() and lane == "routed"
                    return {"ok": True, "served": NEW if flipped else OLD,
                            "ms": 500 if lane == "pinned" else 400}
                model = OLD if lane == "pinned" else f"router:{config.ROUTER_NAME}"
                return await asyncio.to_thread(_mig_call, api_key, model, i)

            async def lane_task(lane: str):
                for i in range(1, N + 1):
                    r = await call(lane, i)
                    await q.put({"type": "mig_req", "lane": lane, "i": i, **r})
                    if lane == "routed" and i == FLIP:
                        code = 200 if mock else \
                            await asyncio.to_thread(_set_ranking, uuid, [NEW, OLD])
                        MIG_FLIPPED.set()
                        await q.put({"type": "mig_flip", "http": code,
                                     "ranking": [NEW, OLD]})

            tasks = [asyncio.ensure_future(lane_task("pinned")),
                     asyncio.ensure_future(lane_task("routed"))]

            failed, adopted_at = 0, None
            while not (all(t.done() for t in tasks) and q.empty()):
                try:
                    item = await asyncio.wait_for(q.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                if item["type"] == "mig_req":
                    failed += not item["ok"]
                    if (item["lane"] == "routed" and item["ok"]
                            and item["served"] == NEW and adopted_at is None):
                        adopted_at = item["i"]
                yield _sse(item)
            for t in tasks:
                t.result()  # surface lane-task crashes instead of hiding them

            restored = True if mock else \
                (await asyncio.to_thread(_set_ranking, uuid, [OLD, NEW])) == 200
            yield _sse({"type": "mig_end", "adopted_at": adopted_at,
                        "failed": failed, "restored": restored})
        except Exception as e:
            yield _sse({"type": "mig_error",
                        "error": f"{type(e).__name__}: {e}"})
        finally:
            RUN_LOCK.release()

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def health(request: Request) -> JSONResponse:
    """App Platform health check. Deliberately does NOT call the upstream --
    a rate-limited model must not make the container look unhealthy."""
    return JSONResponse({"ok": True, "mock_available": True,
                         "key_configured": bool(os.environ.get(config.API_KEY_ENV))})


async def index(request: Request) -> FileResponse:
    return FileResponse(HERE / "index.html")


def _boot() -> None:
    config.assert_deterministic()
    mock.server.serve(background=True)
    proxy.server.serve(background=True)


_boot()

app = Starlette(routes=[
    Route("/", index),
    Route("/api/run", run_stream),
    Route("/api/migrate", migrate_stream),
    Route("/healthz", health),
])


class _BasicAuth:
    """Password gate for the public deployment -- real API credentials sit
    behind these endpoints and every Run spends real tokens.

    - The password comes from $DEMO_PASSWORD (an App Platform SECRET, never
      this repo). Unset means open: local dev and forks stay frictionless.
    - /healthz stays unauthenticated, or App Platform's health check would
      401 and restart the container forever.
    - Plain HTTP Basic: the browser asks once per session and then attaches
      credentials to every request, EventSource included -- no login page to
      build, nothing for a presenter to fumble on stage.
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        pw = os.environ.get("DEMO_PASSWORD", "").strip()
        if scope["type"] != "http" or not pw or scope["path"] == "/healthz":
            return await self.inner(scope, receive, send)

        import base64
        import secrets as sec
        auth = dict(scope.get("headers") or []).get(b"authorization", b"")
        ok = False
        if auth.startswith(b"Basic "):
            try:
                userpass = base64.b64decode(auth[6:]).decode()
                candidate = userpass.split(":", 1)[1] if ":" in userpass else ""
                ok = sec.compare_digest(candidate, pw)
            except Exception:
                ok = False
        if ok:
            return await self.inner(scope, receive, send)

        await send({"type": "http.response.start", "status": 401, "headers": [
            (b"www-authenticate", b'Basic realm="agent-resilience-demo"'),
            (b"content-type", b"text/plain; charset=utf-8"),
        ]})
        await send({"type": "http.response.body",
                    "body": b"password required (any username)"})


app = _BasicAuth(app)
