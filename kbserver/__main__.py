"""服务入口：python -m kbserver"""

from __future__ import annotations

import uvicorn

from .app import create_app
from .config import Config, check_listen_security


def main() -> None:
    cfg = Config()
    try:
        check_listen_security(cfg)
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    app = create_app(cfg)
    uvicorn.run(
        app,
        host=str(cfg.data.get("host", "127.0.0.1")),
        port=int(cfg.data.get("port", 8765)),
        log_level="info",
    )


if __name__ == "__main__":
    main()
