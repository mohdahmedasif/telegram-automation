"""
Relay — personal automation status hub.

Starts registered automations automatically with the app, then serves a
read-only status UI. Individual bots live under `automations/`.
"""

from __future__ import annotations

import logging
import os

import uvicorn
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("relay")


def main() -> None:
    host = os.getenv("RELAY_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.getenv("RELAY_PORT", "8765").strip() or "8765")
    logger.info("Starting Relay at http://%s:%s", host, port)
    uvicorn.run(
        "app.web.server:create_app",
        factory=True,
        host=host,
        port=port,
        reload=os.getenv("RELAY_RELOAD", "").lower() in {"1", "true", "yes"},
    )


if __name__ == "__main__":
    main()
