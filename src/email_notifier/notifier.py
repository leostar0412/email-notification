# Copyright (c) 2026 Leo Chen <leo.chen0412@outlook.com>

"""Slack delivery via a single incoming webhook."""

from __future__ import annotations

from typing import Any

import httpx

from .models import EmailMessage

# Slack rejects blocks whose text exceeds hard limits (3000 chars for a
# section, 2000 per field). Email subjects and From headers are unbounded, so
# they are clamped after escaping — otherwise one oversized email would be
# rejected by Slack forever and wedge its account behind endless retries.
_SUBJECT_LIMIT = 2900
_SENDER_LIMIT = 1900
_SNIPPET_LIMIT = 250


class SlackError(Exception):
    """Raised when Slack rejects or fails to receive a notification."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SlackNotifier:
    """Posts new-email notifications to one Slack incoming webhook."""

    def __init__(
        self,
        webhook_url: str,
        *,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._webhook_url = webhook_url
        self._owns_client = client is None
        self._client = httpx.Client(timeout=timeout) if client is None else client

    def close(self) -> None:
        """Release the HTTP connection pool (only if this notifier created it)."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> SlackNotifier:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def notify(self, message: EmailMessage) -> None:
        payload = self._payload(message)
        try:
            self._post(payload)
        except SlackError as exc:
            if exc.status is None or not 400 <= exc.status < 500:
                raise
            # Slack rejected the payload itself; retrying the identical blocks
            # can never succeed. Fall back to a minimal text-only message so a
            # single unrenderable email cannot block the account's queue.
            self._post({"text": payload["text"]})

    def send_text(self, text: str) -> None:
        """Send a plain-text message (used by ``email-notifier test-slack``)."""
        self._post({"text": text})

    def _post(self, payload: dict[str, Any]) -> None:
        try:
            response = self._client.post(self._webhook_url, json=payload)
        except httpx.HTTPError as exc:
            raise SlackError(f"Could not reach Slack: {exc}") from exc
        if response.status_code != 200:
            raise SlackError(
                f"Slack webhook returned {response.status_code}: {response.text[:200]}",
                status=response.status_code,
            )

    @classmethod
    def _payload(cls, message: EmailMessage) -> dict[str, Any]:
        subject = cls._clamp(cls._escape(message.subject), _SUBJECT_LIMIT) or "(no subject)"
        sender = cls._clamp(cls._escape(message.sender), _SENDER_LIMIT) or "(unknown sender)"
        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "📬 New email", "emoji": True},
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{subject}*"}},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*From:*\n{sender}"},
                    {
                        "type": "mrkdwn",
                        "text": f"*Account:*\n{message.account} ({message.provider})",
                    },
                ],
            },
        ]
        if message.snippet:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": cls._clamp(cls._escape(message.snippet), _SNIPPET_LIMIT),
                        }
                    ],
                }
            )
        if message.received_at is not None:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"Received {message.received_at:%Y-%m-%d %H:%M %Z}".rstrip(),
                        }
                    ],
                }
            )
        return {
            "text": f"New email for {message.account}: {subject}",
            "blocks": blocks,
        }

    @staticmethod
    def _escape(text: str) -> str:
        """Escape the characters Slack's mrkdwn treats specially."""
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    @staticmethod
    def _clamp(text: str, limit: int) -> str:
        """Hard-cap ``text`` at ``limit`` characters (may cut an escape entity)."""
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"
