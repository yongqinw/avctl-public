"""`python -m api` -- run the control app.

host is never 0.0.0.0 by default; `tailscale serve` handles TLS and
reachability. See docs/remote-access.md.
"""

from __future__ import annotations

import uvicorn

from . import settings


def main() -> None:
    # EventSource connections are intentionally long-lived. Without a bound,
    # Uvicorn waits forever for them during the setup wizard's packaged-Core
    # restart while the phone itself is the connected SSE client.
    uvicorn.run(
        "api.main:app",
        host=settings.BIND,
        port=settings.PORT,
        timeout_graceful_shutdown=3,
    )


if __name__ == "__main__":
    main()
