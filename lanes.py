"""The entire difference between the two lanes is in this file.

Not "mostly". Not "except for a flag". This file is it. `agent.py` cannot tell
which lane it is running in, and `client.py` has no lane-specific branch --
it just walks the candidate list it was handed.

    single lane  -> candidates = (primary,)            len 1, nowhere to go
    routed lane  -> candidates = (primary, alt)        len 2, somewhere to go

That is the whole argument the demo makes, expressed as a tuple length.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import config


@dataclass(frozen=True)
class Endpoint:
    alias: str  # proxy route key; also what the fault injector targets
    model: str

    def url(self, proxy_base: str) -> str:
        # Both lanes always dial the local proxy, never the upstream directly.
        # The proxy is what makes "the upstream got sick" reproducible.
        return f"{proxy_base}/u/{self.alias}/chat/completions"


@dataclass(frozen=True)
class Lane:
    name: str
    blurb: str
    candidates: tuple[Endpoint, ...]


PRIMARY = Endpoint(alias="primary", model=config.PRIMARY_MODEL)
ALT = Endpoint(alias="alt", model=config.ALT_MODEL)

SINGLE = Lane(
    name="single",
    blurb="one fixed endpoint (single-vendor)",
    candidates=(PRIMARY,),
)

ROUTED = Lane(
    name="routed",
    blurb="DO Serverless Inference + routing",
    candidates=(PRIMARY, ALT),
)

LANES = (SINGLE, ROUTED)


def require_api_key(mock: bool) -> str:
    """Fail fast and loudly. Never return or log the key's contents."""
    if mock:
        return "mock-no-key-needed"
    key = os.environ.get(config.API_KEY_ENV, "").strip()
    if not key:
        raise SystemExit(
            f"\n  Missing ${config.API_KEY_ENV}.\n\n"
            f"  Either:  cp .env.example .env   and put your key in it\n"
            f"  Or run fully offline with no key at all:\n\n"
            f"      make demo MOCK=1\n"
        )
    return key
