"""CLI wrapper for the canned upstream (`make demo MOCK=1`).

The web UI imports mock.server and runs it in-process instead.
"""

from __future__ import annotations

import config
from mock.server import serve

if __name__ == "__main__":
    print(f"mock upstream: :{config.MOCK_PORT}", flush=True)
    serve()
