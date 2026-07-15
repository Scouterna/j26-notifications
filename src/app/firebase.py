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


async def firebase_init():
    cred_data = json.loads(settings.FCM_CREDENTIALS_JSON)
    cred = credentials.Certificate(cred_data)
    try:
        get_app()
    except ValueError:
        initialize_app(cred)


async def firebase_send(
    tokens: list[str], title: str, body: str, data_json: str, link: str | None = None
) -> None:
    if not tokens:
        return
    multicast_message = messaging.MulticastMessage(
        tokens=tokens,
        notification=messaging.Notification(title=title, body=body),
        data={"payload": data_json},
        webpush=messaging.WebpushConfig(
            notification=messaging.WebpushNotification(
                icon=urljoin(settings.APP_BASE_URL, NOTIFICATION_ICON_PATH),
                badge=urljoin(settings.APP_BASE_URL, NOTIFICATION_BADGE_PATH),
            ),
            fcm_options=messaging.WebpushFCMOptions(link=urljoin(settings.APP_BASE_URL, link))
            if link
            else None,
        ),
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
