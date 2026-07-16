"""Runs both lanes concurrently, renders them side by side, writes the artifact.

The two lanes run in threads on purpose: watching them diverge in real time is
the demo. It is safe to run them concurrently because the fault injector counts
per lane (see proxy/__main__.py), so neither lane can shift the other's schedule.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import agent
import config
from client import AgentHalted, Client, Event, Usage
from lanes import LANES, Lane, require_api_key

console = Console()

# Below this, the full `step=… lane=… attempt=…` line cannot fit twice across.
# Measured, not guessed: ~73 chars per line, two panels, plus borders.
VERBOSE_MIN_WIDTH = 155

EVENTS: "queue.Queue[Event]" = queue.Queue()
LINES: dict[str, list[Text]] = {l.name: [] for l in LANES}
RAW: list[Event] = []  # ordered, for the diffable jsonl artifact


@dataclass
class Result:
    lane: str
    completed: bool
    wall_s: float
    usage: Usage
    record: dict | None = None
    error: str | None = None


def _style(e: Event) -> str:
    if e.kind == "routing":
        return "bold cyan"
    if e.kind == "halt":
        return "bold red"
    if e.status == 200:
        return "green"
    if e.status or e.kind == "attempt":
        return "yellow"
    return "white"


def _fmt(e: Event) -> Text:
    """One event, one line. Two panels side by side need ~120 cols to show the
    full `step=… lane=… attempt=…` form, so drop to a compact form rather than
    let rich wrap every line into three."""
    ts = time.strftime("%H:%M:%S", time.localtime(e.ts))
    verbose = console.width >= VERBOSE_MIN_WIDTH
    att = e.attempt if e.kind == "attempt" else None

    if verbose:
        head = f"[{ts}] step={e.step:<9} lane={e.lane:<7} "
        head += f"attempt={att} " if att else " " * 10
    else:
        head = f"[{ts}] {e.step:<9} "
        head += f"a{att} " if att else "   "
    # One event must stay one line, or the two columns stop lining up and the
    # side-by-side comparison -- the whole point -- gets hard to read.
    return Text(head + e.message, style=_style(e), no_wrap=True,
                overflow="ellipsis")


def _render(results: dict[str, Result]) -> Panel:
    grid = Table.grid(expand=True, padding=(0, 1))
    for _ in LANES:
        grid.add_column(ratio=1)

    cells = []
    for lane in LANES:
        body = Group(*(LINES[lane.name][-22:] or [Text("waiting…", style="dim")]))
        r = results.get(lane.name)
        border = "white"
        if r:
            border = "green" if r.completed else "red"
        cells.append(Panel(body, title=f"[bold]{lane.name}[/] — {lane.blurb}",
                           border_style=border, padding=(0, 1)))
    grid.add_row(*cells)
    return Panel(grid, title="[bold]same agent · same task · same fault[/]",
                 border_style="dim")


def _run_lane(lane: Lane, api_key: str, results: dict[str, Result]) -> None:
    c = Client(lane, api_key, EVENTS.put)
    t0 = time.time()
    record, err = None, None
    try:
        record = agent.run(c)
    except AgentHalted as e:
        err = str(e)
    except Exception as e:  # a malformed model reply is a task failure too
        err = f"{type(e).__name__}: {e}"
    finally:
        c.close()

    completed = err is None and agent.is_complete(record)
    EVENTS.put(Event(time.time(), lane.name, "task", "done" if completed else "halt",
                     "✓ TASK COMPLETE" if completed else "✗ TASK FAILED"))
    results[lane.name] = Result(lane.name, completed, time.time() - t0,
                                c.usage, record, err)


def _summary(results: dict[str, Result]) -> Table:
    t = Table(title="summary", header_style="bold", expand=False)
    t.add_column("")
    for lane in LANES:
        t.add_column(lane.name, justify="right")

    def row(label, fn):
        t.add_row(label, *(fn(results[l.name]) for l in LANES))

    row("task completed", lambda r: "[green]YES[/]" if r.completed else "[red]NO[/]")
    row("wall clock", lambda r: f"{r.wall_s:.1f}s")
    row("requests", lambda r: str(r.usage.requests))
    row("tokens in/out", lambda r: f"{r.usage.tokens_in}/{r.usage.tokens_out}")
    row("cost est. (USD)", lambda r: f"${r.usage.cost_usd:.5f}")
    row("models used", lambda r: ", ".join(r.usage.by_model) or "—")
    return t


def main() -> None:
    if sys.version_info < config.PYTHON_MIN:
        raise SystemExit(f"needs Python >= {'.'.join(map(str, config.PYTHON_MIN))}")
    config.assert_deterministic()

    mock = os.environ.get("MOCK") == "1"
    api_key = require_api_key(mock)

    results: dict[str, Result] = {}
    threads = [threading.Thread(target=_run_lane, args=(l, api_key, results),
                                daemon=True) for l in LANES]

    with Live(_render(results), console=console, refresh_per_second=12) as live:
        for th in threads:
            th.start()
        while any(t.is_alive() for t in threads) or not EVENTS.empty():
            try:
                e = EVENTS.get(timeout=0.1)
                LINES[e.lane].append(_fmt(e))
                RAW.append(e)
            except queue.Empty:
                pass
            live.update(_render(results))
        live.update(_render(results))

    console.print()
    console.print(_summary(results))
    if config.PRICING_IS_ESTIMATE:
        console.print("[dim]cost is a rough estimate from config.PRICE_PER_MTOK — "
                      "not a billing figure[/]")

    path = _write_artifact(results, mock)
    console.print(f"[dim]run log: {path}[/]")

    single, routed = results["single"], results["routed"]
    console.print()
    if not single.completed and routed.completed:
        console.print(Panel(
            "The single-endpoint lane and the routed lane ran [bold]identical "
            "agent code[/] against an [bold]identical fault[/].\n"
            "The routed lane had somewhere else to go. That is the entire "
            "difference — see lanes.py.",
            border_style="green", title="[bold]the point[/]"))
    else:
        console.print("[yellow]Lanes did not diverge as expected — check the "
                      "proxy flags and config.assert_deterministic().[/]")
    sys.exit(0)


def _write_artifact(results: dict[str, Result], mock: bool) -> str:
    os.makedirs(config.OUT_DIR, exist_ok=True)
    path = os.path.join(config.OUT_DIR,
                        f"run-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    with open(path, "w") as f:
        f.write(json.dumps({
            "type": "meta", "mock": mock,
            "fail_mode": os.environ.get("FAIL_MODE", config.FAIL_MODE),
            "fail_after": os.environ.get("FAIL_AFTER", config.FAIL_AFTER),
            "primary": config.PRIMARY_MODEL, "alt": config.ALT_MODEL,
        }) + "\n")
        for e in RAW:
            f.write(json.dumps({"type": "event", **e.as_json()}) + "\n")
        for name, r in results.items():
            f.write(json.dumps({
                "type": "result", "lane": name, "completed": r.completed,
                "wall_s": round(r.wall_s, 2), "requests": r.usage.requests,
                "tokens_in": r.usage.tokens_in, "tokens_out": r.usage.tokens_out,
                "cost_usd_est": round(r.usage.cost_usd, 6),
                "record": r.record, "error": r.error,
            }) + "\n")
    return path


if __name__ == "__main__":
    main()
