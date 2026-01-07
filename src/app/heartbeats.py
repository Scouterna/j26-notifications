import asyncio
import logging
import time
from datetime import datetime, timezone

from .config import get_settings
from .notifications import Notification, send_notification

settings = get_settings()
logger = logging.getLogger(__name__)


# --- Init function ---


async def heartbeats_init() -> None:
    asyncio.create_task(heartbeats_loop())
    return


# --- Heartbeat loop ---


async def heartbeats_loop():
    while True:
        seconds_until_next_minute = 60 - (time.time() % 60)
        await asyncio.sleep(seconds_until_next_minute)
        logger.info("Sending a heartbeat")
        msg = Notification(
            tenant_id=settings.DEFAULT_TENANT,
            channel_id="heartbeat",
            title="HeartBeat",
            body=f"Aktuell tid: {datetime.now(timezone.utc)}",
            sent_by="Heartbeat loop",
        )
        await send_notification(settings.DEFAULT_TENANT, "heartbeat", msg, save=False)
