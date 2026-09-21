"""HTTP server that receives Gmail push notifications from Cloud Pub/Sub.

Pub/Sub POSTs a JSON envelope to ``/gmail/push``; its ``message.data`` field
is base64-encoded JSON like ``{"emailAddress": ..., "historyId": ...}``. The
handler looks up the matching account, asks its provider for every message
that arrived since the stored cursor, and forwards each one to Slack.

Responses drive Pub/Sub's retry behaviour:

- 2xx acknowledges the push. Malformed or unroutable notifications are
  acknowledged — redelivering them cannot fix anything.
- 5xx makes Pub/Sub redeliver. Provider/Slack failures return 500, and the
  cursor is only advanced after every notification is delivered, so mail is
  never silently dropped (a retry may repeat a notification: at-least-once).

The Gmail/Slack calls are blocking, so each push is processed on the
threadpool (keeping ``/healthz`` responsive) under a per-account lock that
serializes concurrent pushes for the same account. The lock is per process:
run a single server process (the default), not multiple workers/replicas.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import threading
from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool

from . import __version__
from .config import AccountConfig, AppConfig
from .notifier import SlackError, SlackNotifier
from .providers import create_provider
from .providers.base import EmailProvider, ProviderError
from .state import CursorStore

logger = logging.getLogger(__name__)

ProviderFactory = Callable[[AccountConfig, AppConfig], EmailProvider]


def _decode_pubsub_message(envelope: object) -> tuple[str, str] | None:
    """Extract ``(email_address, history_id)`` from a Pub/Sub push envelope."""
    message = envelope.get("message") if isinstance(envelope, dict) else None
    if not isinstance(message, dict) or "data" not in message:
        logger.warning("Ignoring malformed Pub/Sub envelope (no message.data)")
        return None
    try:
        payload = json.loads(base64.b64decode(message["data"]))
    except (ValueError, TypeError):
        logger.warning("Ignoring Pub/Sub message with undecodable data")
        return None
    if not isinstance(payload, dict):
        logger.warning("Ignoring Pub/Sub message whose data is not a JSON object")
        return None
    email_address = payload.get("emailAddress")
    history_id = payload.get("historyId")
    if not email_address or history_id is None:
        logger.warning("Ignoring Pub/Sub message without emailAddress/historyId")
        return None
    return str(email_address), str(history_id)


def create_app(
    config: AppConfig,
    *,
    notifier: SlackNotifier | None = None,
    store: CursorStore | None = None,
    provider_factory: ProviderFactory | None = None,
) -> FastAPI:
    """Build the FastAPI app. Collaborators are injectable for testing."""
    notifier = notifier or SlackNotifier(config.slack.webhook_url)
    store = store or CursorStore(config.state_file)
    factory = provider_factory or create_provider
    providers: dict[str, EmailProvider] = {}
    locks = {account.name: threading.Lock() for account in config.accounts}

    if not config.gmail.pubsub_verification_token:
        logger.warning(
            "No pubsub_verification_token configured: /gmail/push will accept "
            "pushes from anyone who can reach it"
        )

    app = FastAPI(title="email-notifier", version=__version__)

    def provider_for(account: AccountConfig) -> EmailProvider:
        if account.name not in providers:
            providers[account.name] = factory(account, config)
        return providers[account.name]

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/gmail/push")
    async def gmail_push(request: Request, token: str | None = None) -> Response:
        expected = config.gmail.pubsub_verification_token
        # Compare as bytes: str-mode compare_digest raises on non-ASCII, and
        # the token query parameter is sender-controlled.
        if expected and not hmac.compare_digest((token or "").encode(), expected.encode()):
            raise HTTPException(status_code=403, detail="Bad verification token")
        try:
            envelope = await request.json()
        except ValueError:
            # Covers both invalid JSON and non-UTF-8 bodies.
            logger.warning("Ignoring push with an unparsable body")
            return Response(status_code=204)
        notification = _decode_pubsub_message(envelope)
        if notification is None:
            return Response(status_code=204)
        email_address, pushed_history_id = notification
        account = config.account_for_email(email_address)
        if account is None:
            logger.warning("Push for unconfigured account %s; ignoring", email_address)
            return Response(status_code=204)

        def process() -> None:
            with locks[account.name]:
                cursor = store.get(account.name)
                if cursor is None:
                    # First push we have seen for this account (its watch was
                    # registered before this server had state): adopt the
                    # pushed position and notify from the next change onward.
                    store.set(account.name, pushed_history_id)
                    return
                messages, new_cursor = provider_for(account).fetch_messages_since(cursor)
                for message in messages:
                    notifier.notify(message)
                store.set(account.name, new_cursor)

        try:
            await run_in_threadpool(process)
        except (ProviderError, SlackError) as exc:
            logger.error("Failed to process push for %s: %s", account.name, exc)
            # Static detail: the specifics are in the log, and this endpoint
            # may be reachable by more than just Pub/Sub.
            raise HTTPException(status_code=500, detail="processing failed") from exc
        return Response(status_code=204)

    return app
