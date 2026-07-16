# agent-resilience-demo

**The same agent, the same task, the same fault. One finishes. One dies.**

The only difference between the two lanes is where they send their inference —
one fixed endpoint, versus DigitalOcean Serverless Inference behind routing.

![dashboard](docs/dashboard.png)

## Run it

```sh
make web                  # dashboard -> http://localhost:8080
```

No key? Tick the **MOCK** toggle in the UI — canned upstream, no network. Or:

```sh
cp .env.example .env      # add DIGITALOCEAN_INFERENCE_KEY
make health               # do the pinned models actually answer? RUN THIS FIRST
make web
```

Hit **⚡ 注入故障並開跑**. Left lane stalls, retries, dies. Right lane logs the
429, fails over to a different model, finishes. There's also a terminal version
(`make demo`) that prints the same decision log with `rich`.

## Vary it

| Control | Default | What it does |
|---|---|---|
| 故障模式 | `429` | `429` (~5s, use this live), `5xx` (~5s), `timeout` (~50s — the single lane really does sit there; dramatic, but don't open with it), `無故障` (clean run, ~5s — show this first so they see both lanes healthy) |
| MOCK | off | Local canned upstream. No key, no network, byte-identical runs. |
| `make demo FAIL_AFTER=2` | `1` | Start failing after N requests **per lane**. `1` breaks `summarize`. |
| `make demo FAIL_DURATION=90` | `60` | Seconds the fault persists. The injector rejects values too short to be deterministic. |

## Deploy

```sh
doctl apps create --spec .do/app.yaml     # edit the github.repo field first
```

Then set `DIGITALOCEAN_INFERENCE_KEY` as a **SECRET** env var in the App
Platform dashboard. Without it the app still boots and MOCK still works, which
is your stage fallback. `make docker` runs the same image locally first.

> **Untested:** the container build and the App Platform deploy have not been
> run end-to-end (the Docker daemon wouldn't start on the authoring machine).
> The app itself is verified against the real DO API via `make web`. Build the
> image once before you rely on it.

## How it works

```
                    ┌──────────────────────────────────────┐
   agent.py ───────►│ proxy/  (deterministic fault injector)│
   (identical       │   counts requests PER LANE            │
    for both        │   after N: inject 429/timeout/5xx     │
    lanes)          └───────┬──────────────────────┬────────┘
                            │ /u/primary           │ /u/alt
                            ▼                      ▼
                     llama3.3-70b-instruct   openai-gpt-oss-20b
                        (sick)                   (healthy)

   single lane  candidates = (primary,)       ← nowhere to go, burns retries, dies
   routed lane  candidates = (primary, alt)   ← routes around it, finishes
```

The agent code is **identical** for both lanes — `agent.py` never learns which
lane it's in, and `client.py` has no `if lane ==` in it. The lanes differ by a
tuple length in `lanes.py`, and nothing else. The web build imports those same
modules rather than reimplementing them, so opening a browser can't quietly turn
the claim into a lie.

Failure is **injected, not provoked**: a demo that waits for a real provider's
rate limiter to fire on cue isn't reproducible. The injector counts per lane, so
the two concurrent lanes can't perturb each other's schedule. Same flags → same
outcome, every run, any machine — verified by diffing three runs.
`config.assert_deterministic()` refuses flag combinations where the fault window
could close mid-retry and let the single lane survive by luck.

One container runs the dashboard, the injector, and the mock upstream, because
App Platform deploys one container and a demo that needs a process manager is a
demo that breaks on stage.

## What this does NOT claim

- **This is not a latency benchmark.** The routed lane is often *slower* — it
  makes an extra failed request before failing over. Ignore the wall clock. If
  you quote timings from this repo, you have misread it.
- **Not a model quality comparison.** Two models are configured as different
  backends to make failover meaningful, not to rank them.
- **The 429s are synthetic.** They say nothing about any provider's real limits.
- **Cost figures are placeholder estimates** from `config.PRICE_PER_MTOK`. DO
  doesn't publish per-model token pricing in its catalog. Not billing figures.

The one claim: **when an endpoint degrades, an agent with somewhere else to go
completes the task and an agent without one does not.**

## Before you present

Run `make health`. It takes 4 seconds and it is the check that matters.

`make models` only proves a model ID still *exists* in the catalog. That is a
different question from whether it will *answer*, and the gap is not academic:
during development the original alt pin (`openai-gpt-oss-120b`) was listed,
valid, and serving real `429 Platform overloaded` — so the routed lane failed
over into a backend that was itself down, and both lanes died. A failover target
that is degraded is not a failover target. `make health` catches that; `make
models` doesn't.

## Fork it

Swap `CORPUS` and the three prompts in `agent.py` for your scenario; adjust the
pins in `config.py`. Everything else holds. ~20 minutes.

## Next: Fusion integration

*(stub)* Fusion would replace the hand-rolled candidate list in `lanes.py` with
real policy — health-aware routing, circuit breaking, and cost/latency-based
model selection — so the failover decision comes from the platform rather than
from a tuple. The demo's seam is deliberately at `Lane.candidates`: that's the
single place a Fusion-backed router would plug in. Note that the routed lane
currently restarts at `primary` for every step; a real router would circuit-break
and stop dialling a backend it already knows is sick.

---
`make demo` also writes `out/run-<timestamp>.jsonl` — one JSON object per
decision, diffable across runs and pasteable into a doc.
