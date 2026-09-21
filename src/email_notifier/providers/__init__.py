# Copyright (c) 2026 Leo Chen <leo.chen0412@outlook.com>

"""Email provider registry.

``register`` maps a provider key (the ``provider = "..."`` value in an
account's config) to its implementation class. Gmail is built in; new
providers register here — see ``docs/extending.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import EmailProvider, ProviderError
from .gmail import GmailProvider

if TYPE_CHECKING:
    from ..config import AccountConfig, AppConfig

_REGISTRY: dict[str, type[EmailProvider]] = {}


def register(provider_class: type[EmailProvider]) -> type[EmailProvider]:
    """Register a provider class under its ``name``; usable as a decorator."""
    _REGISTRY[provider_class.name] = provider_class
    return provider_class


def get_provider_class(name: str) -> type[EmailProvider]:
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        raise ProviderError(f"Unknown provider {name!r} (registered: {known})") from None


def create_provider(account: AccountConfig, config: AppConfig) -> EmailProvider:
    """Build the provider instance for one configured account."""
    return get_provider_class(account.provider).from_config(account, config)


register(GmailProvider)

__all__ = [
    "EmailProvider",
    "GmailProvider",
    "ProviderError",
    "create_provider",
    "get_provider_class",
    "register",
]
