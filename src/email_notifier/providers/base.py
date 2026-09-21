# Copyright (c) 2026 Leo Chen <leo.chen0412@outlook.com>

"""Provider interface.

To support a new email service (Outlook, Yahoo, ...), implement
:class:`EmailProvider` and register the class in
:mod:`email_notifier.providers`. See ``docs/extending.md`` for a walkthrough.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from ..config import AccountConfig, AppConfig
    from ..models import EmailMessage, WatchInfo


class ProviderError(Exception):
    """Raised when the upstream email service misbehaves."""


class EmailProvider(ABC):
    """One provider instance handles one configured account."""

    #: Registry key; the value of ``provider = "..."`` in an account's config.
    name: ClassVar[str]

    @classmethod
    @abstractmethod
    def from_config(cls, account: AccountConfig, config: AppConfig) -> EmailProvider:
        """Build a provider instance for ``account`` from the app configuration."""

    @abstractmethod
    def start_watch(self) -> WatchInfo:
        """Register (or renew) push notifications for the account.

        Returns the cursor marking "now"; only mail arriving after this point
        will be reported by :meth:`fetch_messages_since`.
        """

    @abstractmethod
    def fetch_messages_since(self, cursor: str) -> tuple[list[EmailMessage], str]:
        """Return messages that arrived after ``cursor``, plus the new cursor.

        Implementations must recover gracefully when ``cursor`` is too old for
        the upstream service (return no messages and a fresh cursor) so one
        stale entry cannot wedge the account forever.
        """
