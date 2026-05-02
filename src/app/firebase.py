import json
import logging

from firebase_admin import credentials, get_app, initialize_app, messaging
from starlette.concurrency import run_in_threadpool

from .config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


async def firebase_init():
    cred_data = json.loads(settings.FCM_CREDENTIALS_JSON)
    cred = credentials.Certificate(cred_data)
    try:
        get_app()
    except ValueError:
        initialize_app(cred)


async def firebase_send(tokens: list[str], title: str, body: str) -> None:
    if not tokens:
        return
    multicast_message = messaging.MulticastMessage(
        tokens=tokens,
        notification=messaging.Notification(title=title, body=body),
        data=None,
    )
    response = await run_in_threadpool(messaging.send_each_for_multicast, multicast_message)
    # TODO: collect failed tokens from response.responses and remove them from DB
    pass
