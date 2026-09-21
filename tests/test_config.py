# Copyright (c) 2026 Leo Chen <leo.chen0412@outlook.com>

"""Tests for email_notifier.config."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from email_notifier.config import (
    AccountConfig,
    AppConfig,
    ConfigError,
    GmailSettings,
    SlackConfig,
    load_config,
)

VALID_TOML = """
    [slack]
    webhook_url = "https://hooks.slack.com/services/T0/B0/XYZ"

    [gmail]
    credentials_file = "credentials.json"
    topic = "projects/my-proj/topics/gmail-push"

    [[accounts]]
    name = "personal"
    email = "Alice@Example.com"
    token_file = "tokens/alice.json"

    [[accounts]]
    name = "work"
    email = "bob@example.com"
    token_file = "tokens/bob.json"
    provider = "outlook"
    label_ids = ["INBOX", "IMPORTANT"]
"""


def clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("PUBSUB_VERIFICATION_TOKEN", raising=False)


def write_config(tmp_path: Path, text: str = VALID_TOML) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# load_config: happy path and file-level errors
# --------------------------------------------------------------------------- #


def test_load_config_happy_path_two_accounts(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    path = write_config(tmp_path)
    config = load_config(path)

    base = path.resolve().parent
    assert isinstance(config, AppConfig)
    assert config.slack == SlackConfig(webhook_url="https://hooks.slack.com/services/T0/B0/XYZ")
    assert config.gmail == GmailSettings(
        credentials_file=base / "credentials.json",
        topic="projects/my-proj/topics/gmail-push",
        pubsub_verification_token=None,
    )
    assert len(config.accounts) == 2

    first, second = config.accounts
    assert first == AccountConfig(
        name="personal",
        email="Alice@Example.com",
        token_file=base / "tokens/alice.json",
        provider="gmail",
        label_ids=("INBOX",),
    )
    assert second.name == "work"
    assert second.email == "bob@example.com"
    assert second.token_file == base / "tokens/bob.json"
    assert second.provider == "outlook"
    assert second.label_ids == ("INBOX", "IMPORTANT")

    # Relative paths resolve against the config file's directory.
    assert config.gmail.credentials_file.is_absolute()
    assert first.token_file.is_absolute()
    assert config.state_file == base / "state.json"


def test_load_config_accepts_str_path(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    path = write_config(tmp_path)
    config = load_config(str(path))
    assert config.slack.webhook_url.startswith("https://")


def test_load_config_missing_file(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    missing = tmp_path / "nope.toml"
    with pytest.raises(ConfigError, match="Config file not found"):
        load_config(missing)


def test_load_config_invalid_toml(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    path = tmp_path / "config.toml"
    path.write_text("this is [not valid toml", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid TOML"):
        load_config(path)


def test_state_file_custom(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    path = write_config(tmp_path, 'state_file = "data/cursors.json"\n' + VALID_TOML)
    config = load_config(path)
    assert config.state_file == path.resolve().parent / "data/cursors.json"


# --------------------------------------------------------------------------- #
# [slack]
# --------------------------------------------------------------------------- #


def test_slack_env_override_wins_over_file(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/ENV/OVERRIDE")
    path = write_config(tmp_path)
    config = load_config(path)
    assert config.slack.webhook_url == "https://hooks.slack.com/services/ENV/OVERRIDE"


def test_slack_env_only_no_file_value(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/ENV/ONLY")
    text = VALID_TOML.replace('webhook_url = "https://hooks.slack.com/services/T0/B0/XYZ"', "")
    config = load_config(write_config(tmp_path, text))
    assert config.slack.webhook_url == "https://hooks.slack.com/services/ENV/ONLY"


def test_slack_section_missing(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    text = VALID_TOML.replace("[slack]", "").replace(
        'webhook_url = "https://hooks.slack.com/services/T0/B0/XYZ"', ""
    )
    with pytest.raises(ConfigError, match="No Slack webhook configured"):
        load_config(write_config(tmp_path, text))


def test_slack_webhook_url_missing(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    text = VALID_TOML.replace('webhook_url = "https://hooks.slack.com/services/T0/B0/XYZ"', "")
    with pytest.raises(ConfigError, match="No Slack webhook configured"):
        load_config(write_config(tmp_path, text))


def test_slack_webhook_url_not_https(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    text = VALID_TOML.replace(
        'webhook_url = "https://hooks.slack.com/services/T0/B0/XYZ"',
        'webhook_url = "http://hooks.slack.com/services/T0/B0/XYZ"',
    )
    with pytest.raises(ConfigError, match=r"must be an https:// URL"):
        load_config(write_config(tmp_path, text))


def test_slack_not_a_table(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    text = VALID_TOML.replace(
        '[slack]\n    webhook_url = "https://hooks.slack.com/services/T0/B0/XYZ"',
        'slack = "oops"',
    )
    with pytest.raises(ConfigError, match=r"\[slack\] must be a table"):
        load_config(write_config(tmp_path, text))


# --------------------------------------------------------------------------- #
# [gmail]
# --------------------------------------------------------------------------- #


def test_gmail_not_a_table(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    text = """
        gmail = ["oops"]

        [slack]
        webhook_url = "https://hooks.slack.com/services/T0/B0/XYZ"
    """
    with pytest.raises(ConfigError, match=r"\[gmail\] must be a table"):
        load_config(write_config(tmp_path, text))


def test_gmail_missing_credentials_file(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    text = VALID_TOML.replace('credentials_file = "credentials.json"', "")
    with pytest.raises(ConfigError, match=r"\[gmail\] is missing required key 'credentials_file'"):
        load_config(write_config(tmp_path, text))


def test_gmail_credentials_file_not_a_string(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    text = VALID_TOML.replace('credentials_file = "credentials.json"', "credentials_file = 5")
    with pytest.raises(ConfigError, match="missing required key 'credentials_file'"):
        load_config(write_config(tmp_path, text))


def test_gmail_missing_topic(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    text = VALID_TOML.replace('topic = "projects/my-proj/topics/gmail-push"', "")
    with pytest.raises(ConfigError, match=r"\[gmail\] is missing required key 'topic'"):
        load_config(write_config(tmp_path, text))


def test_gmail_topic_not_projects_prefix(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    text = VALID_TOML.replace(
        'topic = "projects/my-proj/topics/gmail-push"',
        'topic = "my-proj/topics/gmail-push"',
    )
    with pytest.raises(ConfigError, match=r"must look like projects/<project-id>/topics/<name>"):
        load_config(write_config(tmp_path, text))


def test_gmail_topic_missing_topics_segment(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    text = VALID_TOML.replace(
        'topic = "projects/my-proj/topics/gmail-push"',
        'topic = "projects/my-proj/gmail-push"',
    )
    with pytest.raises(ConfigError, match=r"must look like projects/<project-id>/topics/<name>"):
        load_config(write_config(tmp_path, text))


def test_pubsub_token_from_file(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    text = VALID_TOML.replace(
        'topic = "projects/my-proj/topics/gmail-push"',
        'topic = "projects/my-proj/topics/gmail-push"\n'
        '    pubsub_verification_token = "file-token"',
    )
    config = load_config(write_config(tmp_path, text))
    assert config.gmail.pubsub_verification_token == "file-token"


def test_pubsub_token_env_override(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("PUBSUB_VERIFICATION_TOKEN", "env-token")
    text = VALID_TOML.replace(
        'topic = "projects/my-proj/topics/gmail-push"',
        'topic = "projects/my-proj/topics/gmail-push"\n'
        '    pubsub_verification_token = "file-token"',
    )
    config = load_config(write_config(tmp_path, text))
    assert config.gmail.pubsub_verification_token == "env-token"


def test_pubsub_token_absent_is_none(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    config = load_config(write_config(tmp_path))
    assert config.gmail.pubsub_verification_token is None


def test_pubsub_token_empty_string_is_none(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    text = VALID_TOML.replace(
        'topic = "projects/my-proj/topics/gmail-push"',
        'topic = "projects/my-proj/topics/gmail-push"\n    pubsub_verification_token = ""',
    )
    config = load_config(write_config(tmp_path, text))
    assert config.gmail.pubsub_verification_token is None


# --------------------------------------------------------------------------- #
# [[accounts]]
# --------------------------------------------------------------------------- #

MINIMAL_HEAD = """
    [slack]
    webhook_url = "https://hooks.slack.com/services/T0/B0/XYZ"

    [gmail]
    credentials_file = "credentials.json"
    topic = "projects/my-proj/topics/gmail-push"
"""


def test_accounts_key_missing(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    with pytest.raises(ConfigError, match="No accounts configured"):
        load_config(write_config(tmp_path, MINIMAL_HEAD))


def test_accounts_empty_list(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    with pytest.raises(ConfigError, match="No accounts configured"):
        load_config(write_config(tmp_path, "accounts = []\n" + MINIMAL_HEAD))


def test_accounts_not_a_list(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    with pytest.raises(ConfigError, match="No accounts configured"):
        load_config(write_config(tmp_path, 'accounts = "oops"\n' + MINIMAL_HEAD))


def test_accounts_entry_not_a_table(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    with pytest.raises(ConfigError, match=r"\[\[accounts\]\] entry #1 must be a table"):
        load_config(write_config(tmp_path, 'accounts = ["oops"]\n' + MINIMAL_HEAD))


def test_accounts_label_ids_not_a_list(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    text = VALID_TOML.replace('label_ids = ["INBOX", "IMPORTANT"]', 'label_ids = "INBOX"')
    with pytest.raises(ConfigError, match="entry #2: label_ids must be a list of strings"):
        load_config(write_config(tmp_path, text))


def test_accounts_label_ids_not_all_strings(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    text = VALID_TOML.replace('label_ids = ["INBOX", "IMPORTANT"]', 'label_ids = ["INBOX", 3]')
    with pytest.raises(ConfigError, match="entry #2: label_ids must be a list of strings"):
        load_config(write_config(tmp_path, text))


def test_accounts_missing_name(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    text = VALID_TOML.replace('name = "personal"', "")
    with pytest.raises(ConfigError, match=r"entry #1 is missing required key 'name'"):
        load_config(write_config(tmp_path, text))


def test_accounts_missing_email(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    text = VALID_TOML.replace('email = "Alice@Example.com"', "")
    with pytest.raises(ConfigError, match=r"entry #1 is missing required key 'email'"):
        load_config(write_config(tmp_path, text))


def test_accounts_missing_token_file(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    text = VALID_TOML.replace('token_file = "tokens/alice.json"', "")
    with pytest.raises(ConfigError, match=r"entry #1 is missing required key 'token_file'"):
        load_config(write_config(tmp_path, text))


def test_accounts_duplicate_names(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    text = VALID_TOML.replace('name = "work"', 'name = "personal"')
    with pytest.raises(ConfigError, match="Account names must be unique"):
        load_config(write_config(tmp_path, text))


def test_accounts_duplicate_emails_case_insensitive(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    text = VALID_TOML.replace('email = "bob@example.com"', 'email = "ALICE@example.COM"')
    with pytest.raises(ConfigError, match="Account emails must be unique"):
        load_config(write_config(tmp_path, text))


def test_account_defaults(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    config = load_config(write_config(tmp_path))
    first = config.accounts[0]
    assert first.provider == "gmail"
    assert first.label_ids == ("INBOX",)


# --------------------------------------------------------------------------- #
# AppConfig lookup helpers
# --------------------------------------------------------------------------- #


def test_account_for_email_exact(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    config = load_config(write_config(tmp_path))
    account = config.account_for_email("bob@example.com")
    assert account is not None
    assert account.name == "work"


def test_account_for_email_case_insensitive(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    config = load_config(write_config(tmp_path))
    account = config.account_for_email("alice@EXAMPLE.com")
    assert account is not None
    assert account.name == "personal"


def test_account_for_email_strips_whitespace(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    config = load_config(write_config(tmp_path))
    account = config.account_for_email("  Bob@Example.com \n")
    assert account is not None
    assert account.name == "work"


def test_account_for_email_miss(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    config = load_config(write_config(tmp_path))
    assert config.account_for_email("nobody@example.com") is None


def test_account_by_name_hit(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    config = load_config(write_config(tmp_path))
    account = config.account_by_name("work")
    assert account is not None
    assert account.email == "bob@example.com"


def test_account_by_name_miss(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    config = load_config(write_config(tmp_path))
    assert config.account_by_name("does-not-exist") is None
