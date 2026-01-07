import json
import logging
from typing import TYPE_CHECKING

from firebase_admin import credentials, get_app, initialize_app, messaging
from starlette.concurrency import run_in_threadpool

from .config import get_settings

if TYPE_CHECKING:
    from .notifications import Notification

settings = get_settings()
logger = logging.getLogger(__name__)


async def firebase_init():
    cred_data = json.loads(settings.FCM_CREDENTIALS_JSON)
    cred = credentials.Certificate(cred_data)
    initialize_app(cred)
    return


async def firebase_send(tokens: list[str], notification: "Notification") -> None:
    if not tokens:
        return
    multicast_message = messaging.MulticastMessage(
        tokens=tokens,
        notification=messaging.Notification(
            title=notification.title,
            body=notification.body,
        ),
        data=None,
    )
    response = await run_in_threadpool(messaging.send_each_for_multicast, multicast_message)
    # I want to catch all failed notifications that are due to invalid tokens and save the tokens in a list
    # for later asynchronous removal from database
    pass
