"""Every tunable in the demo lives here.

If you fork this repo to show your own scenario, this file plus `agent.py`'s
CORPUS/PROMPTS should be the only things you need to touch.
"""

from __future__ import annotations

# --- Python -----------------------------------------------------------------
# Pinned because the demo must behave identically on a stranger's laptop.
# 3.11 is the floor: we rely on tomllib-era stdlib and modern typing syntax.
PYTHON_MIN = (3, 11)

# --- Upstream ---------------------------------------------------------------
# DigitalOcean Serverless Inference speaks the OpenAI chat-completions wire
# format, which is why this whole demo is ~400 lines of httpx and no SDK.
DO_INFERENCE_BASE_URL = "https://inference.do-ai.run/v1"
API_KEY_ENV = "DIGITALOCEAN_INFERENCE_KEY"

# --- Model pins -------------------------------------------------------------
# Pinned to exact IDs, not aliases: an alias that silently re-points would make
# the demo non-reproducible, which is the one thing this repo is selling.
#
# PRIMARY: llama3.3-70b-instruct
#   Chosen because it is DO-hosted, open-weight (no commercial gating, so anyone
#   can run this cold), and strong enough that the 3-step task genuinely works.
#   It plays the role of "the one endpoint you bet the whole product on."
PRIMARY_MODEL = "llama3.3-70b-instruct"
#
# ALT: openai-gpt-oss-20b
#   Chosen because it is a *different model family from a different lab*, also
#   DO-hosted. That matters: failing over to a second copy of the same model
#   behind the same vendor is not resilience, it is a retry with extra steps.
#   The routed lane's whole claim is that it can cross a backend boundary.
#
#   NOT gpt-oss-120b, which was the original pin: on 2026-07-16 it was serving
#   real 429 "Platform overloaded" and took the routed lane down with it. A
#   failover target that is itself degraded is not a failover target. `make
#   models` only proves an ID exists -- `make health` proves it answers.
#   Spare, verified healthy the same day, if 20b ever sulks: mistral-3-14B.
ALT_MODEL = "openai-gpt-oss-20b"

# Cost estimate only. DigitalOcean does not publish per-model token pricing in
# the model catalog, so these are ROUGH placeholders so the summary block has a
# number in it. Edit before you quote this to a customer.
PRICE_PER_MTOK = {
    PRIMARY_MODEL: {"in": 0.65, "out": 0.65},
    ALT_MODEL: {"in": 0.10, "out": 0.10},
}
PRICING_IS_ESTIMATE = True

# --- Act 2: Inference Router (model adoption without downtime) --------------
# The adoption scenario drives the REAL DigitalOcean Inference Router: the
# routed lane calls `router:<ROUTER_NAME>` and mid-stream the server reorders
# the router's model ranking via the control-plane API. Measured propagation
# on 2026-07-16: ~2s from PUT to first response served by the new model.
#
# Requires DIGITALOCEAN_API_TOKEN (control plane, same team as the inference
# key -- a token from the wrong team makes the router invisible and everything
# 404s; we learned this the hard way). The router is created on first use if
# it doesn't exist, so a fork of this repo self-provisions.
ROUTER_NAME = "agent-demo-router"
API_TOKEN_ENV = "DIGITALOCEAN_API_TOKEN"
DO_API_BASE = "https://api.digitalocean.com/v2"
MIGRATE_REQUESTS = 6   # stream length per lane
MIGRATE_FLIP_AFTER = 2  # flip the ranking after this many routed responses

# Act 2's cast, separate knobs from act 1's PRIMARY/ALT on purpose: when a
# model has a genuinely bad day you recast this act without touching the
# failover story. Any two healthy models tell it fine.
#
# mistral rather than PRIMARY_MODEL (llama) as the "old" model: the evening
# this was written, llama degraded from 1s to 10-18s to hard ReadTimeouts --
# live proof of this repo's thesis, and exactly why act 2 must not depend on
# any one model's mood. Flip back to PRIMARY_MODEL if you prefer the
# older-generation-llama story and `make health` shows it healthy.
MIGRATE_OLD_MODEL = "mistral-3-14B"
MIGRATE_NEW_MODEL = ALT_MODEL

# --- Local demo plumbing ----------------------------------------------------
PROXY_HOST, PROXY_PORT = "127.0.0.1", 8900
MOCK_HOST, MOCK_PORT = "127.0.0.1", 8901
PROXY_BASE_URL = f"http://{PROXY_HOST}:{PROXY_PORT}"
MOCK_BASE_URL = f"http://{MOCK_HOST}:{MOCK_PORT}/v1"

# --- Fault injection defaults (overridable from the Makefile) ---------------
FAIL_AFTER = 1  # per lane: request #1 (retrieve) succeeds, #2 (summarize) breaks
FAIL_DURATION_S = 60.0
FAIL_MODE = "429"  # one of: 429 | timeout | 5xx
FAIL_ALIAS = "primary"  # only the primary backend is sick; the alt is healthy

# --- Retry / routing policy -------------------------------------------------
# ONE policy, shared by both lanes. Read client.py: there is no `if lane ==`
# anywhere. The lanes differ only in how many candidate endpoints they hand in.
# 1200, not 400. openai-gpt-oss-20b is a *reasoning* model: it spends tokens on
# reasoning_content before it writes a single character of the answer. At 400 it
# hit finish_reason=length mid-JSON, the closing brace never arrived, and the
# agent died with a baffling "no JSON object in model output" -- a truncation
# bug wearing a parsing bug's clothes. If you swap in another reasoning model,
# check finish_reason before you trust this number.
MAX_TOKENS = 1200

MAX_ATTEMPTS = 3
BACKOFF_S = (1.0, 2.0)  # waits between attempts 1->2 and 2->3
# 15s, not 4s. Against the mock a 70B model looks instant; against the real
# thing it took 2.4s for ten tokens, and 4s turned normal latency into a fake
# ReadTimeout that silently ate a fault-injector count and desynced the demo.
# Tune this to your slowest real model, not to your mock.
REQUEST_TIMEOUT_S = 15.0

# Determinism guard, asserted at startup by runner.py.
#
# The failure window is wall-clock (`--fail-duration`), but the outcome must not
# be. So we require the window to strictly outlast the single lane's ENTIRE
# retry budget. If the window can't close mid-retry, the single lane always
# halts and the routed lane always survives -- on any machine, at any speed.
_WORST_CASE_SINGLE_LANE_S = MAX_ATTEMPTS * REQUEST_TIMEOUT_S + sum(BACKOFF_S)


def assert_deterministic(fail_duration: float | None = None) -> None:
    """Check the duration ACTUALLY in force, not just this module's default.

    Checking the default was a bug: `make demo FAIL_DURATION=20` sailed past it
    and then raced the clock anyway. Whoever owns the real value passes it here.
    """
    d = FAIL_DURATION_S if fail_duration is None else fail_duration
    if d <= _WORST_CASE_SINGLE_LANE_S:
        raise SystemExit(
            f"\n  fail-duration={d}s must exceed the single lane's worst-case "
            f"retry budget ({_WORST_CASE_SINGLE_LANE_S}s = {MAX_ATTEMPTS} attempts "
            f"x {REQUEST_TIMEOUT_S}s timeout + {sum(BACKOFF_S)}s backoff).\n"
            f"  Otherwise the fault window can close mid-retry and the single "
            f"lane sometimes survives -- the demo stops being reproducible.\n"
        )


# --- Artifacts --------------------------------------------------------------
OUT_DIR = "out"
