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
                "fail_after": fail_after, "steps": ["retrieve", "summarize", "extract"],
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
    Route("/healthz", health),
])
