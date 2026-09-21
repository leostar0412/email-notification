# Copyright (c) 2026 Leo Chen <leo.chen0412@outlook.com>

"""Gmail provider: OAuth tokens, watch registration, and history reads.

Gmail's push notifications (``users.watch`` → Cloud Pub/Sub) do not contain
the new mail itself. Each push only carries the account's latest
``historyId``; the provider then calls ``users.history.list`` from the
previously stored id to discover which messages actually arrived, and
``users.messages.get`` for their headers.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from ..config import AccountConfig, AppConfig, GmailSettings
from ..models import EmailMessage, WatchInfo
from .base import EmailProvider, ProviderError

SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)


def build_service(token_file: str | Path, scopes: tuple[str, ...] = SCOPES) -> Any:
    """Build an authenticated Gmail API client from a stored user token."""
    path = Path(token_file)
    if not path.is_file():
        raise ProviderError(
            f"No token found at {path}. Authorize the account first with "
            "`email-notifier auth <account>`."
        )
    try:
        creds = Credentials.from_authorized_user_file(str(path), list(scopes))
    except ValueError as exc:
        raise ProviderError(
            f"Stored token at {path} is malformed: {exc}. Re-run `email-notifier auth <account>`."
        ) from exc
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                raise ProviderError(
                    f"Could not refresh the Gmail token at {path} (revoked or expired): "
                    f"{exc}. Re-run `email-notifier auth <account>`."
                ) from exc
            path.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise ProviderError(
                f"Stored token at {path} is invalid and cannot be refreshed. "
                "Re-run `email-notifier auth <account>`."
            )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def run_oauth_flow(
    credentials_file: str | Path,
    token_file: str | Path,
    *,
    scopes: tuple[str, ...] = SCOPES,
    flow_factory: Callable[[], Any] | None = None,
    service_builder: Callable[..., Any] = build,
) -> str:
    """Run the interactive Google OAuth consent flow and save the token.

    Opens a browser, waits for consent on a local redirect server, writes the
    resulting token to ``token_file``, and returns the email address that was
    actually authorized (so the CLI can warn about account mix-ups).
    """
    credentials_path = Path(credentials_file)
    if not credentials_path.is_file():
        raise ProviderError(
            f"OAuth client file not found at {credentials_path}. Download it from "
            "Google Cloud Console (see docs/gmail-setup.md) and check "
            "[gmail].credentials_file in your config."
        )
    factory = flow_factory or (
        lambda: InstalledAppFlow.from_client_secrets_file(str(credentials_path), list(scopes))
    )
    creds = factory().run_local_server(port=0)
    token_path = Path(token_file)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    service = service_builder("gmail", "v1", credentials=creds, cache_discovery=False)
    profile = service.users().getProfile(userId="me").execute()
    return str(profile["emailAddress"])


class GmailProvider(EmailProvider):
    name = "gmail"

    def __init__(
        self,
        account: AccountConfig,
        settings: GmailSettings,
        *,
        service_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._account = account
        self._settings = settings
        self._service_factory = service_factory or (lambda: build_service(account.token_file))
        self._service: Any = None

    @classmethod
    def from_config(cls, account: AccountConfig, config: AppConfig) -> GmailProvider:
        return cls(account, config.gmail)

    @property
    def _api(self) -> Any:
        if self._service is None:
            self._service = self._service_factory()
        return self._service

    def start_watch(self) -> WatchInfo:
        body = {
            "topicName": self._settings.topic,
            "labelIds": list(self._account.label_ids),
            "labelFilterBehavior": "INCLUDE",
        }
        try:
            response = self._api.users().watch(userId="me", body=body).execute()
        except HttpError as exc:
            raise ProviderError(
                f"Gmail watch failed for account {self._account.name!r}: {exc}"
            ) from exc
        expires_at = None
        if expiration := response.get("expiration"):
            expires_at = datetime.fromtimestamp(int(expiration) / 1000, tz=UTC)
        return WatchInfo(cursor=str(response["historyId"]), expires_at=expires_at)

    def fetch_messages_since(self, cursor: str) -> tuple[list[EmailMessage], str]:
        message_ids, new_cursor = self._list_history(cursor)
        messages = [
            message
            for message_id in message_ids
            if (message := self._get_message(message_id)) is not None
        ]
        return messages, new_cursor

    def current_cursor(self) -> str:
        """The account's present history position, from the Gmail profile."""
        try:
            profile = self._api.users().getProfile(userId="me").execute()
        except HttpError as exc:
            raise ProviderError(
                f"Could not read Gmail profile for account {self._account.name!r}: {exc}"
            ) from exc
        return str(profile["historyId"])

    def _list_history(self, cursor: str) -> tuple[list[str], str]:
        message_ids: list[str] = []
        new_cursor = cursor
        page_token = None
        # The watch's labelIds decide which changes are PUSHED, but
        # history.list can only filter on a single label — so filter here on
        # each added message's labels instead, which supports any number of
        # configured labels. Messages that only later GAIN a watched label
        # (e.g. rescued from Spam into INBOX) produce labelAdded records, not
        # messageAdded, and are deliberately not notified.
        watched = set(self._account.label_ids)
        while True:
            request = (
                self._api.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=cursor,
                    historyTypes=["messageAdded"],
                    pageToken=page_token,
                )
            )
            try:
                response = request.execute()
            except HttpError as exc:
                if exc.resp.status == 404:
                    # The stored cursor is older than Gmail's history window
                    # (about a week). Resynchronize to "now" instead of
                    # failing on every future push; mail from the gap is not
                    # notified.
                    return [], self.current_cursor()
                raise ProviderError(
                    f"Gmail history read failed for account {self._account.name!r}: {exc}"
                ) from exc
            new_cursor = str(response.get("historyId", new_cursor))
            for record in response.get("history", []):
                for added in record.get("messagesAdded", []):
                    message = added.get("message", {})
                    message_id = message.get("id")
                    if not message_id or message_id in message_ids:
                        continue
                    if watched and not watched.intersection(message.get("labelIds", [])):
                        continue
                    message_ids.append(message_id)
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return message_ids, new_cursor

    def _get_message(self, message_id: str) -> EmailMessage | None:
        try:
            response = (
                self._api.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject"],
                )
                .execute()
            )
        except HttpError as exc:
            if exc.resp.status == 404:
                return None  # deleted between the push and our fetch
            raise ProviderError(
                f"Gmail message read failed for account {self._account.name!r}: {exc}"
            ) from exc
        headers = {
            header["name"].lower(): header["value"]
            for header in response.get("payload", {}).get("headers", [])
        }
        received_at = None
        if internal_date := response.get("internalDate"):
            received_at = datetime.fromtimestamp(int(internal_date) / 1000, tz=UTC)
        return EmailMessage(
            account=self._account.name,
            provider=self.name,
            message_id=message_id,
            sender=headers.get("from", ""),
            subject=headers.get("subject", ""),
            snippet=response.get("snippet", ""),
            received_at=received_at,
        )
