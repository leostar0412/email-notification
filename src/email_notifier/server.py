# Copyright (c) 2026 Leo Chen <leo.chen0412@outlook.com>

"""HTTP server that receives mailbox push notifications.

Gmail: Pub/Sub POSTs a JSON envelope to ``/gmail/push``. Its ``message.data``
field is base64-encoded JSON like ``{"emailAddress": ..., "historyId": ...}``.

Outlook: Microsoft Graph POSTs to ``/outlook/push``. A create or revalidation
sends ``?validationToken=`` and expects the token echoed as ``text/plain``.
Change notifications are a ``{"value": [...]}`` batch. Each item's
``clientState`` is ``{secret}:{account name}``.

Either handler asks the account's provider for every message that arrived
since the stored cursor, and forwards each one to Slack.

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
from typing import Any

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


def _account_from_client_state(client_state: object, secret: str) -> str | None:
    """Return the account name when ``client_state`` is ``secret:name``."""
    if not isinstance(client_state, str):
        return None
    prefix = f"{secret}:".encode()
    raw = client_state.encode()
    if len(raw) < len(prefix) or not hmac.compare_digest(raw[: len(prefix)], prefix):
        return None
    return raw[len(prefix) :].decode() or None


def _decode_graph_notifications(body: object) -> list[dict[str, Any]] | None:
    """Return the ``value`` array from a Graph notification, or ``None`` if unusable."""
    if not isinstance(body, dict):
        logger.warning("Ignoring Graph notification whose body is not a JSON object")
        return None
    value = body.get("value")
    if not isinstance(value, list):
        logger.warning("Ignoring Graph notification without a value array")
        return None
    return [item for item in value if isinstance(item, dict)]


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
    gmail_settings = config.gmail
    outlook_settings = config.outlook

    if gmail_settings is not None and not gmail_settings.pubsub_verification_token:
        logger.warning(
            "No pubsub_verification_token configured: /gmail/push will accept "
            "pushes from anyone who can reach it"
        )

    app = FastAPI(title="email-notifier", version=__version__)

    def provider_for(account: AccountConfig) -> EmailProvider:
        if account.name not in providers:
            providers[account.name] = factory(account, config)
        return providers[account.name]

    def deliver(account: AccountConfig, cursor_if_missing: Callable[[], str]) -> None:
        with locks[account.name]:
            cursor = store.get(account.name)
            if cursor is None:
                # First push for this account (its watch predates this state
                # file): adopt "now" and notify from the next change onward.
                store.set(account.name, cursor_if_missing())
                return
            messages, new_cursor = provider_for(account).fetch_messages_since(cursor)
            for message in messages:
                notifier.notify(message)
            store.set(account.name, new_cursor)

    async def run_delivery(account_name: str, work: Callable[[], None]) -> None:
        try:
            await run_in_threadpool(work)
        except (ProviderError, SlackError) as exc:
            logger.error("Failed to process push for %s: %s", account_name, exc)
            # Static detail: the specifics are in the log, and this endpoint
            # may be reachable by more than the push service.
            raise HTTPException(status_code=500, detail="processing failed") from exc

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    if gmail_settings is not None:

        @app.post("/gmail/push")
        async def gmail_push(request: Request, token: str | None = None) -> Response:
            expected = gmail_settings.pubsub_verification_token
            # Compare as bytes: str-mode compare_digest raises on non-ASCII,
            # and the token query parameter is sender-controlled.
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
            if account is None or account.name not in locks:
                logger.warning("Push for unconfigured account %s; ignoring", email_address)
                return Response(status_code=204)

            await run_delivery(
                account.name,
                lambda: deliver(account, lambda: pushed_history_id),
            )
            return Response(status_code=204)

    if outlook_settings is not None:
        outlook_secret = outlook_settings.client_state

        @app.post("/outlook/push")
        async def outlook_push(request: Request) -> Response:
            # Graph's subscription handshake. It must be answered before any
            # attempt to read a notification body, and within a few seconds.
            if "validationToken" in request.query_params:
                return Response(
                    content=request.query_params["validationToken"],
                    media_type="text/plain",
                    status_code=200,
                )
            try:
                body = await request.json()
            except ValueError:
                logger.warning("Ignoring Outlook push with an unparsable body")
                return Response(status_code=202)
            notifications = _decode_graph_notifications(body)
            if notifications is None:
                return Response(status_code=202)

            accounts: list[AccountConfig] = []
            seen: set[str] = set()
            for item in notifications:
                if item.get("lifecycleEvent"):
                    logger.info("Acking Graph lifecycle event %s", item.get("lifecycleEvent"))
                    continue
                account_name = _account_from_client_state(item.get("clientState"), outlook_secret)
                if account_name is None:
                    logger.warning("Ignoring Graph notification with a bad clientState")
                    continue
                account = config.account_by_name(account_name)
                if account is None or account.provider != "outlook" or account.name not in locks:
                    logger.warning("Graph push for unconfigured account %s; ignoring", account_name)
                    continue
                if account.name in seen:
                    continue
                seen.add(account.name)
                accounts.append(account)

            if not accounts:
                return Response(status_code=202)

            for account in accounts:
                await run_delivery(
                    account.name,
                    lambda account=account: deliver(
                        account,
                        lambda account=account: provider_for(account).current_cursor(),
                    ),
                )
            return Response(status_code=202)

    return app
