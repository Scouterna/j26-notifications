"""Periodically refresh every registered user's full Keycloak group membership.

Message sends only reconcile the one channel being sent to (see
_sync_channel_members in notifications.py) — that keeps delivery to the target
channel accurate without a per-member Keycloak round-trip, but leaves a user's
*other* channel memberships stale until something else refreshes them. This
background loop is that something else: every USER_SYNC_INTERVAL_MINUTES it
walks every row in `users` and refreshes their full channel list from Keycloak,
so a user who joined a channel that nobody has messaged since still eventually
shows up as a member (and sees history for it via GET /notifications).

Users can also force an immediate refresh of their own row by hitting
/register again (the j26-app prompts this if someone reports a missing
notification).
"""

import asyncio
import logging
import time

from .config import get_settings
from .db import db_execute, db_fetch
from .keycloak import get_user_groups

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None


async def _sync_all_users() -> None:
    t0 = time.perf_counter()
    rows = await db_fetch("SELECT user_id, channels FROM users")
    updates = 0
    for row in rows:
        username = row["user_id"]
        try:
            kc_channels = await get_user_groups(username)
        except Exception as exc:
            logger.warning("Keycloak sync failed for user %s, skipping: %s", username, exc)
            continue
        kc_channels.append(username)
        kc_set = set(kc_channels)
        if set(row["channels"]) == kc_set:
            continue  # no change, skip DB write
        updates += 1
        await db_execute("UPDATE users SET channels = $1 WHERE user_id = $2", kc_channels, username)
    logger.info(
        "Full user sync: %d users, %d updated, %.0fms",
        len(rows),
        updates,
        (time.perf_counter() - t0) * 1000,
    )


async def _run_periodically() -> None:
    interval = get_settings().USER_SYNC_INTERVAL_MINUTES * 60
    while True:
        try:
            await asyncio.sleep(interval)
            await _sync_all_users()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Full user sync failed, will retry next interval")


def start() -> None:
    """Start the periodic sync loop. Call once at app startup."""
    global _task
    if _task is None:
        _task = asyncio.create_task(_run_periodically())


async def shutdown() -> None:
    """Cancel the periodic sync loop, e.g. on application shutdown."""
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
