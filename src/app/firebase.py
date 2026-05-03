import json
import logging

from firebase_admin import credentials, get_app, initialize_app, messaging
from starlette.concurrency import run_in_threadpool

from .config import get_settings
from .db import db_execute

settings = get_settings()
logger = logging.getLogger(__name__)


async def firebase_init():
    cred_data = json.loads(settings.FCM_CREDENTIALS_JSON)
    cred = credentials.Certificate(cred_data)
    try:
        get_app()
    except ValueError:
        initialize_app(cred)


async def firebase_send(tokens: list[str], title: str, body: str, data_json: str) -> None:
    if not tokens:
        return
    multicast_message = messaging.MulticastMessage(
        tokens=tokens,
        notification=messaging.Notification(title=title, body=body),
        data={"payload": data_json},
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
