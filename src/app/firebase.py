import json
import logging
from urllib.parse import urljoin

from firebase_admin import credentials, get_app, initialize_app, messaging
from starlette.concurrency import run_in_threadpool

from .config import get_settings
from .db import db_execute

settings = get_settings()
logger = logging.getLogger(__name__)

# Must match the icon/badge assets in j26-app (src/notifications/notification-defaults.ts).
# Setting these here lets FCM's own Web SDK auto-display background notifications with the
# correct branding, instead of the app's SW needing to set them on a manual showNotification call.
NOTIFICATION_ICON_PATH = "/web-app-manifest-192x192.png"
NOTIFICATION_BADGE_PATH = "/notification-badge.png"


def _same_origin_link(base_url: str, link: str | None) -> str:
    # Clamp the click target to the app's own origin, mirroring j26-app's resolveLink:
    # relative paths and same-origin URLs pass through, anything else (external URLs,
    # scheme-relative //host, ...) falls back to the homepage. The startswith check is
    # only safe because base_url ends with "/" (request.base_url always does).
    target = urljoin(base_url, link or "/")
    return target if target.startswith(base_url) else base_url


async def firebase_init():
    cred_data = json.loads(settings.FCM_CREDENTIALS_JSON)
    cred = credentials.Certificate(cred_data)
    try:
        get_app()
    except ValueError:
        initialize_app(cred)


async def firebase_send(
    tokens: list[str],
    title: str,
    body: str,
    data_json: str,
    link: str | None = None,
    base_url: str | None = None,
) -> None:
    if not tokens:
        return
    # base_url comes from the triggering request; the API is served on the same origin
    # as the app shell, so it points at where the icon/badge assets live. firebase-admin
    # rejects a non-HTTPS fcm_options.link outright (and web push doesn't work over plain
    # HTTP anyway), so skip the whole webpush block in that case (local dev).
    webpush = None
    if base_url and base_url.startswith("https://"):
        webpush = messaging.WebpushConfig(
            notification=messaging.WebpushNotification(
                icon=urljoin(base_url, NOTIFICATION_ICON_PATH),
                badge=urljoin(base_url, NOTIFICATION_BADGE_PATH),
            ),
            # Fall back to the homepage rather than omitting fcm_options entirely: FCM's own
            # click handler (which now displays and handles clicks for background notifications
            # on our behalf) does nothing at all on click if there's no link, not even opening
            # the app.
            fcm_options=messaging.WebpushFCMOptions(link=_same_origin_link(base_url, link)),
        )
    multicast_message = messaging.MulticastMessage(
        tokens=tokens,
        notification=messaging.Notification(title=title, body=body),
        data={"payload": data_json},
        webpush=webpush,
    )
    response = await run_in_threadpool(messaging.send_each_for_multicast, multicast_message)

    stale_tokens = [
        tokens[i]
        for i, r in enumerate(response.responses)
        if not r.success and isinstance(r.exception, messaging.UnregisteredError)
    ]
    if stale_tokens:
        logger.info("Removing %d stale FCM tokens", len(stale_tokens))
        await db_execute(
            "UPDATE users SET tokens = array(SELECT unnest(tokens) EXCEPT SELECT unnest($1::text[]))",
            stale_tokens,
        )
