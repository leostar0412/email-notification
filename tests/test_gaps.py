# Copyright (c) 2026 Leo Chen <leo.chen0412@outlook.com>

"""Coverage gap tests: the ``__main__`` shim and the provider registry."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

import pytest

from email_notifier import providers
from email_notifier.cli import main
from email_notifier.config import AccountConfig, AppConfig, GmailSettings, SlackConfig
from email_notifier.providers import (
    EmailProvider,
    GmailProvider,
    OutlookProvider,
    ProviderError,
    create_provider,
    get_provider_class,
    register,
)

if TYPE_CHECKING:
    from pathlib import Path

    from email_notifier.models import EmailMessage, WatchInfo


def _app_config(tmp_path: Path, account: AccountConfig) -> AppConfig:
    return AppConfig(
        slack=SlackConfig(webhook_url="https://hooks.slack.example/T/B/x"),
        gmail=GmailSettings(
            credentials_file=tmp_path / "credentials.json",
            topic="projects/proj/topics/gmail-push",
        ),
        accounts=(account,),
        state_file=tmp_path / "state.json",
    )


def test_main_module_exposes_cli_main() -> None:
    module = importlib.import_module("email_notifier.__main__")
    assert module.main is main


def test_get_provider_class_returns_registered_gmail() -> None:
    assert get_provider_class("gmail") is GmailProvider


def test_get_provider_class_returns_registered_outlook() -> None:
    assert get_provider_class("outlook") is OutlookProvider


def test_get_provider_class_unknown_name_raises_with_known_names() -> None:
    with pytest.raises(ProviderError, match=r"Unknown provider 'yahoo' \(registered: .*gmail"):
        get_provider_class("yahoo")


def test_create_provider_builds_gmail_provider(tmp_path: Path) -> None:
    account = AccountConfig(
        name="work",
        email="user@example.com",
        token_file=tmp_path / "token.json",
    )
    provider = create_provider(account, _app_config(tmp_path, account))
    assert isinstance(provider, GmailProvider)


def test_register_decorator_and_create_provider_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Isolate the registry so the fake provider does not leak into other tests.
    monkeypatch.setattr(providers, "_REGISTRY", dict(providers._REGISTRY))

    class FakeProvider(EmailProvider):
        name = "fake"

        def __init__(self, account: AccountConfig, config: AppConfig) -> None:
            self.account = account
            self.config = config

        @classmethod
        def from_config(cls, account: AccountConfig, config: AppConfig) -> FakeProvider:
            return cls(account, config)

        def start_watch(self) -> WatchInfo:
            raise NotImplementedError

        def fetch_messages_since(self, cursor: str) -> tuple[list[EmailMessage], str]:
            raise NotImplementedError

    assert register(FakeProvider) is FakeProvider
    assert get_provider_class("fake") is FakeProvider

    account = AccountConfig(
        name="personal",
        email="other@example.com",
        token_file=tmp_path / "token.json",
        provider="fake",
    )
    config = _app_config(tmp_path, account)
    provider = create_provider(account, config)
    assert isinstance(provider, FakeProvider)
    assert provider.account is account
    assert provider.config is config
