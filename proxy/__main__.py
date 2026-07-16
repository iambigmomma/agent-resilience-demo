"""CLI wrapper around the injector, for the terminal demo (`make demo`).

The web UI (`make web`) imports proxy.server directly and runs it in-process
instead -- same injector, same schedule, no subprocess.

Usage:
    python -m proxy --upstream-primary URL --upstream-alt URL \
                    [--fail-after N] [--fail-duration S] \
                    [--fail-mode {429,timeout,5xx,none}] [--fail-alias primary]
"""

from __future__ import annotations

import argparse

import config
from proxy.server import Injection, arm, serve


def main() -> None:
    p = argparse.ArgumentParser(prog="proxy")
    p.add_argument("--upstream-primary", required=True)
    p.add_argument("--upstream-alt", required=True)
    p.add_argument("--fail-after", type=int, default=config.FAIL_AFTER,
                   help="start failing AFTER this many requests, per lane")
    p.add_argument("--fail-duration", type=float, default=config.FAIL_DURATION_S)
    p.add_argument("--fail-mode", choices=("429", "timeout", "5xx", "none"),
                   default=config.FAIL_MODE)
    p.add_argument("--fail-alias", default=config.FAIL_ALIAS)
    p.add_argument("--port", type=int, default=config.PROXY_PORT)
    a = p.parse_args()

    # The proxy owns the effective schedule, so the proxy is what must vouch
    # for it. Fail here, loudly, rather than produce a run that only sometimes
    # tells the story.
    if a.fail_mode != "none":
        config.assert_deterministic(a.fail_duration)

    arm(Injection(
        upstream_primary=a.upstream_primary, upstream_alt=a.upstream_alt,
        fail_after=a.fail_after, fail_duration=a.fail_duration,
        fail_mode=a.fail_mode, fail_alias=a.fail_alias,
    ))
    print(f"proxy: :{a.port} fail-mode={a.fail_mode} "
          f"fail-after={a.fail_after} alias={a.fail_alias}", flush=True)
    serve(port=a.port)


if __name__ == "__main__":
    main()
