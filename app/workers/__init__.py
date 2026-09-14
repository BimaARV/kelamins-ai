"""KELA worker process.

PHASE 1 note: collection runs inside the scheduler; this worker is a liveness
placeholder. Phase 5 introduces a real Redis-backed job queue (spec section 13,
23) and workers will consume collector/event/AI/document/alert jobs from here.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.cache import set_heartbeat
from app.config import settings


async def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("kela.worker")
    logger.info("kela worker starting (env=%s) - queue backend arrives in Phase 5", settings.app_env)
    while True:
        await set_heartbeat("worker:heartbeat", datetime.now(timezone.utc).isoformat())
        await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())