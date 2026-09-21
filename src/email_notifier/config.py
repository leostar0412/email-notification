# Copyright (c) 2026 Leo Chen <leo.chen0412@outlook.com>

"""Configuration loading.

The app is configured with a TOML file (see ``config.example.toml``). A few
secrets can instead come from the environment, which takes precedence:

- ``SLACK_WEBHOOK_URL`` overrides ``[slack].webhook_url``
- ``PUBSUB_VERIFICATION_TOKEN`` overrides ``[gmail].pubsub_verification_token``

Relative paths in the file are resolved against the config file's directory,
so the config can be loaded from anywhere.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


@dataclass(frozen=True, slots=True)
class SlackConfig:
    webhook_url: str


@dataclass(frozen=True, slots=True)
class GmailSettings:
    credentials_file: Path
    topic: str
    pubsub_verification_token: str | None = None


@dataclass(frozen=True, slots=True)
class AccountConfig:
    name: str
    email: str
    token_file: Path
    provider: str = "gmail"
    label_ids: tuple[str, ...] = ("INBOX",)


@dataclass(frozen=True, slots=True)
class AppConfig:
    slack: SlackConfig
    gmail: GmailSettings
    accounts: tuple[AccountConfig, ...]
    state_file: Path

    def account_for_email(self, email: str) -> AccountConfig | None:
        needle = email.strip().lower()
        for account in self.accounts:
            if account.email.lower() == needle:
                return account
        return None

    def account_by_name(self, name: str) -> AccountConfig | None:
        for account in self.accounts:
            if account.name == name:
                return account
        return None


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(
            f"Config file not found: {path}. Copy config.example.toml to config.toml "
            "and fill in your values, or pass --config."
        )
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc

    base = path.resolve().parent
    return AppConfig(
        slack=_load_slack(raw),
        gmail=_load_gmail(raw, base),
        accounts=_load_accounts(raw, base),
        state_file=base / str(raw.get("state_file", "state.json")),
    )


def _load_slack(raw: dict[str, Any]) -> SlackConfig:
    section = _section(raw, "slack")
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL") or section.get("webhook_url")
    if not webhook_url:
        raise ConfigError(
            "No Slack webhook configured: set [slack].webhook_url in the config file "
            "or the SLACK_WEBHOOK_URL environment variable (see docs/slack-setup.md)."
        )
    if not str(webhook_url).startswith("https://"):
        raise ConfigError("[slack].webhook_url must be an https:// URL")
    return SlackConfig(webhook_url=str(webhook_url))


def _load_gmail(raw: dict[str, Any], base: Path) -> GmailSettings:
    section = _section(raw, "gmail")
    credentials_file = _require(section, "credentials_file", "[gmail]")
    topic = _require(section, "topic", "[gmail]")
    if not topic.startswith("projects/") or "/topics/" not in topic:
        raise ConfigError(
            f"[gmail].topic must look like projects/<project-id>/topics/<name>, got {topic!r}"
        )
    token = os.environ.get("PUBSUB_VERIFICATION_TOKEN") or section.get("pubsub_verification_token")
    return GmailSettings(
        credentials_file=base / credentials_file,
        topic=topic,
        pubsub_verification_token=str(token) if token else None,
    )


def _load_accounts(raw: dict[str, Any], base: Path) -> tuple[AccountConfig, ...]:
    entries = raw.get("accounts")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(
            "No accounts configured: add at least one [[accounts]] block (see config.example.toml)."
        )
    accounts: list[AccountConfig] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"[[accounts]] entry #{index + 1} must be a table")
        where = f"[[accounts]] entry #{index + 1}"
        label_ids = entry.get("label_ids", ["INBOX"])
        if not isinstance(label_ids, list) or not all(isinstance(x, str) for x in label_ids):
            raise ConfigError(f"{where}: label_ids must be a list of strings")
        accounts.append(
            AccountConfig(
                name=_require(entry, "name", where),
                email=_require(entry, "email", where),
                token_file=base / _require(entry, "token_file", where),
                provider=str(entry.get("provider", "gmail")),
                label_ids=tuple(label_ids),
            )
        )

    names = [account.name for account in accounts]
    if len(set(names)) != len(names):
        raise ConfigError("Account names must be unique")
    emails = [account.email.lower() for account in accounts]
    if len(set(emails)) != len(emails):
        raise ConfigError("Account emails must be unique")
    return tuple(accounts)


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    section = raw.get(name, {})
    if not isinstance(section, dict):
        raise ConfigError(f"[{name}] must be a table")
    return section


def _require(mapping: dict[str, Any], key: str, where: str) -> str:
    value = mapping.get(key)
    if not value or not isinstance(value, str):
        raise ConfigError(f"{where} is missing required key {key!r}")
    return value
