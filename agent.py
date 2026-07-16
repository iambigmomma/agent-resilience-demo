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

TASK = "Review this week's customer feedback and file a product insight report."

# A tiny fake retrieval corpus, inline so the repo has no data dependencies.
# Deliberately NOT an incident/outage scenario: on stage, an agent triaging
# "our" incidents reads as us having incidents. Feedback analysis is neutral.
CORPUS = {
    "doc-1": "Onboarding praise: three new users called the setup flow the "
             "easiest they have tried this year.",
    "doc-2": "Enterprise deals blocked: four prospects asked for SSO/SAML this "
             "week; two said it is the only blocker to signing.",
    "doc-3": "Typo report: the shipping-notification email says 'you order' "
             "instead of 'your order'.",
    "doc-4": "Batch users hitting API rate limits: three paying customers asked "
             "for higher limits for overnight jobs; one is evaluating competitors.",
}

REQUIRED_KEYS = {"priority", "theme", "affected_area", "action"}


def run(client: Client) -> dict:
    """retrieve -> summarize -> extract. Raises AgentHalted if a step can't finish."""

    catalog = "\n".join(f"{k}: {v[:60]}..." for k, v in CORPUS.items())
    picked = client.complete("retrieve", [
        {"role": "system", "content": "You select relevant documents. Reply with "
                                      "ONLY a comma-separated list of doc ids."},
        {"role": "user", "content": f"Task: {TASK}\n\nCatalog:\n{catalog}\n\n"
                                    f"Which docs carry an actionable product "
                                    f"signal (not trivia)?"},
    ])
    ids = [d for d in re.findall(r"doc-\d", picked) if d in CORPUS] or list(CORPUS)
    docs = "\n\n".join(f"[{i}] {CORPUS[i]}" for i in dict.fromkeys(ids))

    summary = client.complete("summarize", [
        {"role": "system", "content": "You are a terse product analyst."},
        {"role": "user", "content": f"Summarize the strongest customer signal in "
                                    f"2 sentences, naming what customers are "
                                    f"asking for.\n\n{docs}"},
    ])

    raw = client.complete("extract", [
        {"role": "system", "content": "Reply with ONLY a JSON object, no prose, "
                                      "no markdown fence. Keys: priority "
                                      "(P1|P2|P3), theme (string), "
                                      "affected_area (string), action (string)."},
        {"role": "user", "content": f"Customer feedback summary:\n{summary}"},
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
