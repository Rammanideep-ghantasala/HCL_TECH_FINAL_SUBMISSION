"""Entry point: `python -m app`. One process, one worker, one copy of the model."""

from __future__ import annotations

import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        workers=1,
        access_log=False,
        log_config=None,
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_keep_alive=65,
    )


if __name__ == "__main__":
    main()
