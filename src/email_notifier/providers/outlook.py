# Copyright (c) 2026 Leo Chen <leo.chen0412@outlook.com>

"""Outlook provider: Microsoft identity tokens, Graph subscriptions, delta reads.

Graph change notifications do not contain the new mail. ``start_watch`` creates
or renews a subscription on the inbox and stores a delta link meaning "now".
Each push then follows that link. The cursor also stores the time it was
issued so a later update of an older message (read, flagged) is not notified
again. A 410 from Graph means the delta link expired; the provider
resynchronizes to "now" and skips the gap, matching Gmail's stale-history
behavior.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import msal

from ..config import AccountConfig, AppConfig, OutlookSettings
from ..models import EmailMessage, WatchInfo
from .base import EmailProvider, ProviderError

SCOPES = (
    "https://graph.microsoft.com/Mail.Read",
    "https://graph.microsoft.com/User.Read",
)
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
INBOX_RESOURCE = "me/mailFolders/inbox/messages"
SUBSCRIPTION_MINUTES = 4230
_MAX_DELTA_PAGES = 50


class _DeltaExpired(Exception):
    """The stored delta link is no longer accepted (Graph HTTP 410)."""


def _authority(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant}"


def _expiration_stamp() -> str:
    when = datetime.now(UTC) + timedelta(minutes=SUBSCRIPTION_MINUTES)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def encode_cursor(delta_link: str, since: datetime) -> str:
    """Opaque cursor: a Graph delta link plus the moment it represents."""
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return json.dumps(
        {"delta": delta_link, "since": since.astimezone(UTC).isoformat()},
        separators=(",", ":"),
        sort_keys=True,
    )


def decode_cursor(cursor: str) -> tuple[str, datetime] | None:
    """Return ``(delta_link, since)``, or ``None`` when ``cursor`` is unusable."""
    try:
        data = json.loads(cursor)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    delta = data.get("delta")
    since_raw = data.get("since")
    if not isinstance(delta, str) or not delta or not isinstance(since_raw, str):
        return None
    try:
        since = _parse_time(since_raw)
    except ValueError:
        return None
    return delta, since


def _is_inbox_resource(resource: str) -> bool:
    text = resource.lower().replace("'", "").replace("(", "/").replace(")", "")
    return text.rstrip("/") == "me/mailfolders/inbox/messages"


def _error_detail(response: Any) -> str:
    try:
        body = response.json()
    except (ValueError, TypeError):
        return str(getattr(response, "text", ""))[:300]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or body)[:300]
    return str(getattr(response, "text", body))[:300]


def _sender(item: dict[str, Any]) -> str:
    sender = item.get("from")
    if not isinstance(sender, dict):
        return ""
    email = sender.get("emailAddress")
    if not isinstance(email, dict):
        return ""
    name = str(email.get("name") or "")
    address = str(email.get("address") or "")
    if name and address:
        return f"{name} <{address}>"
    return address or name


def _optional_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _parse_time(value)
    except ValueError:
        return None


def _authorized_email(access_token: str) -> str:
    response = httpx.get(
        f"{GRAPH_ROOT}/me",
        params={"$select": "mail,userPrincipalName"},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30.0,
    )
    if response.status_code >= 400:
        detail = _error_detail(response)
        raise ProviderError(
            f"Could not read the Outlook profile: HTTP {response.status_code} {detail}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise ProviderError("Outlook profile response was not JSON.") from exc
    if not isinstance(body, dict):
        raise ProviderError("Outlook profile response was not a JSON object.")
    email = body.get("mail") or body.get("userPrincipalName")
    if not email:
        raise ProviderError("Outlook profile did not include an email address.")
    return str(email)


def _default_app(settings: OutlookSettings, cache: msal.SerializableTokenCache) -> Any:
    return msal.PublicClientApplication(
        settings.client_id,
        authority=_authority(settings.tenant),
        token_cache=cache,
    )


def run_oauth_flow(
    settings: OutlookSettings,
    token_file: str | Path,
    *,
    app_factory: Callable[[], Any] | None = None,
    profile_reader: Callable[[str], str] | None = None,
) -> str:
    """Run the interactive Microsoft consent flow and save the token cache.

    Opens a browser, waits for consent on a localhost redirect, writes the
    MSAL cache to ``token_file``, and returns the email address that was
    actually authorized.
    """
    cache = msal.SerializableTokenCache()
    app = (app_factory or (lambda: _default_app(settings, cache)))()
    try:
        result = app.acquire_token_interactive(list(SCOPES))
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(f"Outlook authorization failed: {exc}") from exc
    if not isinstance(result, dict) or "access_token" not in result:
        detail = "unknown error"
        if isinstance(result, dict):
            detail = str(result.get("error_description") or result.get("error") or detail)
        raise ProviderError(f"Outlook authorization failed: {detail}")
    token_cache = getattr(app, "token_cache", cache)
    serialize = getattr(token_cache, "serialize", None)
    serialized = serialize() if serialize is not None else ""
    if not serialized:
        raise ProviderError("Outlook authorization did not produce a token cache.")
    path = Path(token_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized, encoding="utf-8")
    reader = profile_reader or _authorized_email
    return reader(str(result["access_token"]))


def access_token_from_cache(token_file: str | Path, settings: OutlookSettings) -> str:
    """Return a Graph access token, refreshing the stored cache when needed."""
    path = Path(token_file)
    if not path.is_file():
        raise ProviderError(
            f"No token found at {path}. Authorize the account first with "
            "`email-notifier auth <account>`."
        )
    cache = msal.SerializableTokenCache()
    try:
        cache.deserialize(path.read_text(encoding="utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ProviderError(
            f"Stored token at {path} is malformed: {exc}. Re-run `email-notifier auth <account>`."
        ) from exc
    app = msal.PublicClientApplication(
        settings.client_id,
        authority=_authority(settings.tenant),
        token_cache=cache,
    )
    accounts = app.get_accounts()
    if not accounts:
        raise ProviderError(
            f"Stored token at {path} has no Outlook account. "
            "Re-run `email-notifier auth <account>`."
        )
    result = app.acquire_token_silent(list(SCOPES), account=accounts[0])
    if not isinstance(result, dict) or "access_token" not in result:
        detail = ""
        if isinstance(result, dict):
            detail = str(result.get("error_description") or result.get("error") or "")
        raise ProviderError(
            f"Could not refresh the Outlook token at {path} (revoked or expired): {detail}. "
            "Re-run `email-notifier auth <account>`."
        )
    if cache.has_state_changed:
        path.write_text(cache.serialize(), encoding="utf-8")
    return str(result["access_token"])


def build_graph_client(token_file: str | Path, settings: OutlookSettings) -> httpx.Client:
    """Build an authorized Graph client from a stored MSAL token cache."""
    token = access_token_from_cache(token_file, settings)
    return httpx.Client(
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )


class OutlookProvider(EmailProvider):
    name = "outlook"

    def __init__(
        self,
        account: AccountConfig,
        settings: OutlookSettings,
        *,
        graph_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._account = account
        self._settings = settings
        self._graph_factory = graph_factory or (
            lambda: build_graph_client(account.token_file, settings)
        )
        self._client: Any = None

    @classmethod
    def from_config(cls, account: AccountConfig, config: AppConfig) -> OutlookProvider:
        if config.outlook is None:
            raise ProviderError(
                f"Account {account.name!r} uses provider 'outlook' but [outlook] is not configured."
            )
        return cls(account, config.outlook)

    @property
    def _graph(self) -> Any:
        if self._client is None:
            self._client = self._graph_factory()
        return self._client

    def start_watch(self) -> WatchInfo:
        try:
            existing = self._find_subscription()
            if existing is None:
                body = self._create_subscription()
            else:
                body = self._renew_subscription(str(existing["id"]))
            delta = self._latest_delta_link()
        except _DeltaExpired as exc:
            raise ProviderError(
                f"Outlook watch failed for account {self._account.name!r}: delta link expired"
            ) from exc
        except ProviderError as exc:
            raise ProviderError(
                f"Outlook watch failed for account {self._account.name!r}: {exc}"
            ) from exc
        return WatchInfo(
            cursor=encode_cursor(delta, datetime.now(UTC)),
            expires_at=_optional_time(body.get("expirationDateTime")),
        )

    def current_cursor(self) -> str:
        """A delta link meaning "now", so a missing cursor does not dump the mailbox."""
        try:
            delta = self._latest_delta_link()
        except _DeltaExpired as exc:
            raise ProviderError(
                f"Could not read the Outlook delta link for account {self._account.name!r}: "
                "delta link expired"
            ) from exc
        except ProviderError as exc:
            raise ProviderError(
                f"Could not read the Outlook delta link for account {self._account.name!r}: {exc}"
            ) from exc
        return encode_cursor(delta, datetime.now(UTC))

    def fetch_messages_since(self, cursor: str) -> tuple[list[EmailMessage], str]:
        decoded = decode_cursor(cursor)
        if decoded is None:
            return [], self.current_cursor()
        delta_url, since = decoded
        try:
            items, new_delta = self._collect_delta(delta_url)
        except _DeltaExpired:
            return [], self.current_cursor()
        messages = [
            message for item in items if (message := self._to_message(item, since)) is not None
        ]
        return messages, encode_cursor(new_delta, datetime.now(UTC))

    def _find_subscription(self) -> dict[str, Any] | None:
        url: str | None = f"{GRAPH_ROOT}/subscriptions"
        while url:
            body = self._request_json("GET", url)
            entries = body.get("value", [])
            if isinstance(entries, list):
                for item in entries:
                    if not isinstance(item, dict) or "id" not in item:
                        continue
                    if item.get("notificationUrl") != self._settings.notification_url:
                        continue
                    if _is_inbox_resource(str(item.get("resource", ""))):
                        return item
            nxt = body.get("@odata.nextLink")
            url = str(nxt) if isinstance(nxt, str) and nxt else None
        return None

    def _create_subscription(self) -> dict[str, Any]:
        return self._request_json(
            "POST",
            f"{GRAPH_ROOT}/subscriptions",
            json_body={
                "changeType": "created",
                "notificationUrl": self._settings.notification_url,
                "resource": INBOX_RESOURCE,
                "expirationDateTime": _expiration_stamp(),
                "clientState": f"{self._settings.client_state}:{self._account.name}",
            },
        )

    def _renew_subscription(self, subscription_id: str) -> dict[str, Any]:
        return self._request_json(
            "PATCH",
            f"{GRAPH_ROOT}/subscriptions/{subscription_id}",
            json_body={"expirationDateTime": _expiration_stamp()},
        )

    def _latest_delta_link(self) -> str:
        _items, delta = self._collect_delta(
            f"{GRAPH_ROOT}/me/mailFolders/inbox/messages/delta",
            params={"$deltatoken": "latest"},
        )
        return delta

    def _collect_delta(
        self, url: str, *, params: dict[str, str] | None = None
    ) -> tuple[list[dict[str, Any]], str]:
        items: list[dict[str, Any]] = []
        next_url = url
        next_params = params
        for _ in range(_MAX_DELTA_PAGES):
            body = self._request_json("GET", next_url, params=next_params)
            next_params = None
            page = body.get("value", [])
            if isinstance(page, list):
                items.extend(item for item in page if isinstance(item, dict))
            nxt = body.get("@odata.nextLink")
            if isinstance(nxt, str) and nxt:
                next_url = nxt
                continue
            link = body.get("@odata.deltaLink")
            if isinstance(link, str) and link:
                return items, link
            raise ProviderError("Graph delta response had no nextLink or deltaLink")
        raise ProviderError(
            f"Graph delta paging exceeded {_MAX_DELTA_PAGES} pages "
            f"for account {self._account.name!r}"
        )

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if params is not None:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["json"] = json_body
        try:
            response = self._graph.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise ProviderError(str(exc)) from exc
        if response.status_code == 410:
            raise _DeltaExpired()
        if response.status_code >= 400:
            raise ProviderError(f"HTTP {response.status_code} {_error_detail(response)}")
        try:
            body = response.json()
        except (ValueError, TypeError) as exc:
            raise ProviderError("Graph response was not JSON") from exc
        if not isinstance(body, dict):
            raise ProviderError("Graph response was not a JSON object")
        return body

    def _to_message(self, item: dict[str, Any], since: datetime) -> EmailMessage | None:
        if "@removed" in item:
            return None
        message_id = item.get("id")
        if not isinstance(message_id, str) or not message_id:
            return None
        created = _optional_time(item.get("createdDateTime"))
        if created is None or created < since:
            return None
        return EmailMessage(
            account=self._account.name,
            provider=self.name,
            message_id=message_id,
            sender=_sender(item),
            subject=str(item.get("subject") or ""),
            snippet=str(item.get("bodyPreview") or ""),
            received_at=_optional_time(item.get("receivedDateTime")),
        )
