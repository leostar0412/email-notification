# Copyright (c) 2026 Leo Chen <leo.chen0412@outlook.com>

"""Data models shared across email providers and notifiers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """A provider-agnostic view of one received email."""

    account: str
    provider: str
    message_id: str
    sender: str
    subject: str
    snippet: str
    received_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class WatchInfo:
    """Result of registering (or renewing) a push-notification watch.

    ``cursor`` is an opaque position marker — for Gmail it is the account's
    ``historyId`` at the moment the watch was registered.
    """

    cursor: str
    expires_at: datetime | None = None
