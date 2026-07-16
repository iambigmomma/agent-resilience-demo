"""The agent. Three steps, ~60 seconds to read, zero framework.

Note what is NOT in this file: any mention of a lane, an endpoint, a model, a
retry, or a failover. It is handed a `client` and it makes three calls. Both
lanes execute this exact function. Swap the CORPUS and the three prompts and you
have your own scenario.
"""

from __future__ import annotations

import json
import re

from client import Client

TASK = "Triage the on-call incident reports and emit a structured record."

# A tiny fake retrieval corpus, inline so the repo has no data dependencies.
CORPUS = {
    "doc-1": "Marketing site CSS regression: footer misaligned on Safari 17.",
    "doc-2": "checkout-api p99 latency 340ms -> 4.2s at 02:14 UTC. Connection "
             "pool saturated against payments-db. 12% of carts abandoned.",
    "doc-3": "Weekly analytics batch finished 6 minutes late. No user impact.",
    "doc-4": "payments-db failover event 02:11 UTC. Replica promoted. Apps "
             "using the stale writer endpoint reconnected over ~9 minutes.",
}

REQUIRED_KEYS = {"severity", "root_cause", "affected_service", "action"}


def run(client: Client) -> dict:
    """retrieve -> summarize -> extract. Raises AgentHalted if a step can't finish."""

    catalog = "\n".join(f"{k}: {v[:60]}..." for k, v in CORPUS.items())
    picked = client.complete("retrieve", [
        {"role": "system", "content": "You select relevant documents. Reply with "
                                      "ONLY a comma-separated list of doc ids."},
        {"role": "user", "content": f"Task: {TASK}\n\nCatalog:\n{catalog}\n\n"
                                    f"Which docs describe a real production incident?"},
    ])
    ids = [d for d in re.findall(r"doc-\d", picked) if d in CORPUS] or list(CORPUS)
    docs = "\n\n".join(f"[{i}] {CORPUS[i]}" for i in dict.fromkeys(ids))

    summary = client.complete("summarize", [
        {"role": "system", "content": "You are a terse incident analyst."},
        {"role": "user", "content": f"Summarize the incident in 2 sentences, "
                                    f"naming the likely root cause.\n\n{docs}"},
    ])

    raw = client.complete("extract", [
        {"role": "system", "content": "Reply with ONLY a JSON object, no prose, "
                                      "no markdown fence. Keys: severity "
                                      "(SEV1|SEV2|SEV3), root_cause (string), "
                                      "affected_service (string), action (string)."},
        {"role": "user", "content": f"Incident summary:\n{summary}"},
    ])

    return _parse(raw)


def _parse(raw: str) -> dict:
    """Models like to wrap JSON in prose. Grab the first object and move on."""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("no JSON object in model output")
    return json.loads(m.group(0))


def is_complete(record: dict | None) -> bool:
    """The task counts as done only if the artifact is actually usable."""
    return bool(record) and REQUIRED_KEYS.issubset(record.keys())
