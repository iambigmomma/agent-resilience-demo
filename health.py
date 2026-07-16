"""Pre-flight: do the pinned models actually answer?

`make models` only proves an ID appears in the catalog. That is not the same
question, and the difference cost us a broken demo: openai-gpt-oss-120b was
listed, pinned, and serving real 429 "Platform overloaded" -- so the routed lane
failed over into a backend that was itself down.

Run this before you present. It is 4 seconds and it is the check that matters.
"""

from __future__ import annotations

import os
import sys
import time

import httpx

import config


def probe(model: str, key: str) -> tuple[bool, str]:
    t = time.time()
    try:
        r = httpx.post(
            f"{config.DO_INFERENCE_BASE_URL}/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": "Say OK."}],
                  "max_tokens": 5},
            headers={"Authorization": f"Bearer {key}"},
            timeout=30,
        )
        dt = time.time() - t
        if r.status_code == 200:
            return True, f"{r.status_code}  {dt:.2f}s"
        # Surface the upstream's own words -- "Platform overloaded" is the tell.
        msg = r.json().get("error", {}).get("message", "")[:48]
        return False, f"{r.status_code}  {dt:.2f}s  {msg}"
    except Exception as e:
        return False, f"--   {time.time() - t:.2f}s  {type(e).__name__}"


def main() -> int:
    key = os.environ.get(config.API_KEY_ENV, "").strip()
    if not key:
        print(f"  ${config.API_KEY_ENV} not set (health needs a real key)")
        return 1

    ok = True
    for label, model in (("primary", config.PRIMARY_MODEL), ("alt", config.ALT_MODEL)):
        good, detail = probe(model, key)
        ok &= good
        print(f"  {'OK  ' if good else 'DOWN'}  {label:8} {model:26} {detail}")

    if not ok:
        print("\n  A degraded backend will break the demo. Swap the pin in "
              "config.py\n  (mistral-3-14B is a known-good spare) or run: "
              "make demo MOCK=1")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
