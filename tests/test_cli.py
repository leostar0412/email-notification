# Copyright (c) 2026 Leo Chen <leo.chen0412@outlook.com>

"""Tests for email_notifier.cli."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI

import email_notifier.cli as cli
from email_notifier import __version__
from email_notifier.cli import _cmd_auth, _push_urls, build_parser, main
from email_notifier.config import AccountConfig, AppConfig, OutlookSettings, SlackConfig
from email_notifier.models import WatchInfo
from email_notifier.notifier import SlackError
from email_notifier.providers.base import ProviderError

CONFIG_BODY = """
state_file = "state.json"

[slack]
webhook_url = "https://hooks.slack.com/services/T/B/X"

[gmail]
credentials_file = "credentials.json"
topic = "projects/proj/topics/mail"

[[accounts]]
name = "one"
email = "one@example.com"
token_file = "token-one.json"

[[accounts]]
name = "two"
email = "two@example.com"
token_file = "token-two.json"
"""

OTHER_ACCOUNT = """
[[accounts]]
name = "misc"
email = "misc@example.com"
token_file = "token-misc.json"
provider = "other"
"""


def write_config(tmp_path: Path, *, include_other: bool = False) -> Path:
    body = CONFIG_BODY + (OTHER_ACCOUNT if include_other else "")
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("PUBSUB_VERIFICATION_TOKEN", raising=False)
    monkeypatch.delenv("OUTLOOK_CLIENT_STATE", raising=False)


def flatten(text: str) -> str:
    """Collapse rich's line wrapping so multi-word phrases can be asserted."""
    return " ".join(text.split())


# ---------------------------------------------------------------- parser


def test_build_parser_config_default() -> None:
    args = build_parser().parse_args(["test-slack"])
    assert args.config == Path("config.toml")
    assert args.command == "test-slack"


def test_build_parser_serve_defaults() -> None:
    args = build_parser().parse_args(["serve"])
    assert args.host == "0.0.0.0"
    assert args.port == 8000


def test_build_parser_watch_account_default_none() -> None:
    args = build_parser().parse_args(["watch"])
    assert args.account is None


def test_version_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


# ---------------------------------------------------------------- config errors


def test_missing_config_file_rc2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean_env(monkeypatch)
    rc = main(["--config", str(tmp_path / "nope.toml"), "watch"])
    assert rc == 2
    assert "Configuration error" in flatten(capsys.readouterr().err)


# ---------------------------------------------------------------- auth


def test_auth_unknown_account_rc2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean_env(monkeypatch)
    config_path = write_config(tmp_path)
    rc = main(["--config", str(config_path), "auth", "ghost"])
    assert rc == 2
    err = flatten(capsys.readouterr().err)
    assert "No account named 'ghost'" in err
    assert "one, two" in err


def test_auth_non_gmail_provider_rc2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean_env(monkeypatch)
    config_path = write_config(tmp_path, include_other=True)
    rc = main(["--config", str(config_path), "auth", "misc"])
    assert rc == 2
    err = flatten(capsys.readouterr().err)
    assert "uses provider 'other'" in err
    assert "supports only gmail and outlook" in err


def test_auth_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean_env(monkeypatch)
    config_path = write_config(tmp_path)
    calls: list[tuple[Path, Path]] = []

    def fake_flow(credentials_file: Path, token_file: Path) -> str:
        calls.append((credentials_file, token_file))
        return "one@example.com"

    monkeypatch.setattr(cli, "run_oauth_flow", fake_flow)
    rc = main(["--config", str(config_path), "auth", "one"])
    assert rc == 0
    assert calls == [(tmp_path / "credentials.json", tmp_path / "token-one.json")]
    out = flatten(capsys.readouterr().out)
    assert "Token saved" in out
    assert "Warning" not in out


def test_auth_email_match_is_case_insensitive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean_env(monkeypatch)
    config_path = write_config(tmp_path)
    monkeypatch.setattr(cli, "run_oauth_flow", lambda *_: "ONE@Example.COM")
    rc = main(["--config", str(config_path), "auth", "one"])
    assert rc == 0
    assert "Warning" not in flatten(capsys.readouterr().out)


def test_auth_mismatched_email_warns_still_rc0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean_env(monkeypatch)
    config_path = write_config(tmp_path)
    monkeypatch.setattr(cli, "run_oauth_flow", lambda *_: "someone.else@example.com")
    rc = main(["--config", str(config_path), "auth", "one"])
    assert rc == 0
    out = flatten(capsys.readouterr().out)
    assert "Token saved" in out
    assert "Warning" in out
    assert "someone.else@example.com" in out


# ---------------------------------------------------------------- watch


class FakeProvider:
    def __init__(self, info: WatchInfo) -> None:
        self._info = info

    def start_watch(self) -> WatchInfo:
        return self._info


def fake_factory(watch_infos: dict[str, WatchInfo], calls: list[str]):
    def factory(account, config):
        calls.append(account.name)
        return FakeProvider(watch_infos[account.name])

    return factory


def test_watch_all_accounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean_env(monkeypatch)
    config_path = write_config(tmp_path)
    infos = {
        "one": WatchInfo(cursor="123", expires_at=datetime(2031, 1, 2, 3, 4, tzinfo=UTC)),
        "two": WatchInfo(cursor="456", expires_at=None),
    }
    calls: list[str] = []
    monkeypatch.setattr(cli, "create_provider", fake_factory(infos, calls))
    rc = main(["--config", str(config_path), "watch"])
    assert rc == 0
    assert calls == ["one", "two"]
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state == {"one": "123", "two": "456"}
    out = flatten(capsys.readouterr().out)
    assert "123" in out
    assert "456" in out
    assert "2031-01-02 03:04" in out
    assert "—" in out  # expires_at=None renders as an em dash
    assert "at least daily" in out


def test_watch_single_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean_env(monkeypatch)
    config_path = write_config(tmp_path)
    infos = {
        "one": WatchInfo(cursor="123", expires_at=None),
        "two": WatchInfo(cursor="456", expires_at=None),
    }
    calls: list[str] = []
    monkeypatch.setattr(cli, "create_provider", fake_factory(infos, calls))
    rc = main(["--config", str(config_path), "watch", "--account", "two"])
    assert rc == 0
    assert calls == ["two"]
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state == {"two": "456"}
    out = flatten(capsys.readouterr().out)
    assert "456" in out
    assert "one@example.com" not in out


def test_watch_unknown_account_rc2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean_env(monkeypatch)
    config_path = write_config(tmp_path)
    rc = main(["--config", str(config_path), "watch", "--account", "ghost"])
    assert rc == 2
    assert "No account named 'ghost'" in flatten(capsys.readouterr().err)
    assert not (tmp_path / "state.json").exists()


def test_watch_renewal_keeps_stored_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A renewal must not overwrite an existing cursor with the fresh one.

    Overwriting with "now" would skip any mail that arrived while the server
    was down. First-time accounts in the same run still get seeded.
    """
    clean_env(monkeypatch)
    config_path = write_config(tmp_path)
    # Account "one" already has a stored cursor from a previous registration.
    (tmp_path / "state.json").write_text(json.dumps({"one": "old-100"}), encoding="utf-8")
    infos = {
        "one": WatchInfo(cursor="new-999", expires_at=None),
        "two": WatchInfo(cursor="456", expires_at=None),
    }
    calls: list[str] = []
    monkeypatch.setattr(cli, "create_provider", fake_factory(infos, calls))
    rc = main(["--config", str(config_path), "watch"])
    assert rc == 0
    assert calls == ["one", "two"]
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    # "one" keeps its stored position; "two" is seeded for the first time.
    assert state == {"one": "old-100", "two": "456"}


def test_watch_provider_error_rc1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean_env(monkeypatch)
    config_path = write_config(tmp_path)

    class BrokenProvider:
        def start_watch(self) -> WatchInfo:
            raise ProviderError("watch registration exploded")

    monkeypatch.setattr(cli, "create_provider", lambda account, config: BrokenProvider())
    rc = main(["--config", str(config_path), "watch"])
    assert rc == 1
    assert "watch registration exploded" in flatten(capsys.readouterr().err)


# ---------------------------------------------------------------- serve


def test_serve_custom_host_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean_env(monkeypatch)
    config_path = write_config(tmp_path)
    captured: dict[str, object] = {}

    def fake_run(app, *, host, port):
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    rc = main(["--config", str(config_path), "serve", "--host", "127.0.0.1", "--port", "9001"])
    assert rc == 0
    assert isinstance(captured["app"], FastAPI)
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9001
    out = flatten(capsys.readouterr().out)
    assert "http://127.0.0.1:9001/gmail/push" in out
    assert "2 account(s)" in out


# ---------------------------------------------------------------- test-slack


def test_test_slack_happy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean_env(monkeypatch)
    config_path = write_config(tmp_path)
    sent: list[tuple[str, str]] = []

    exits: list[bool] = []

    class FakeNotifier:
        def __init__(self, webhook_url: str) -> None:
            self.webhook_url = webhook_url

        def __enter__(self) -> FakeNotifier:
            return self

        def __exit__(self, *exc_info: object) -> None:
            exits.append(True)

        def send_text(self, text: str) -> None:
            sent.append((self.webhook_url, text))

    monkeypatch.setattr(cli, "SlackNotifier", FakeNotifier)
    rc = main(["--config", str(config_path), "test-slack"])
    assert rc == 0
    assert len(sent) == 1
    assert sent[0][0] == "https://hooks.slack.com/services/T/B/X"
    assert "email-notifier is connected" in sent[0][1]
    # The CLI uses the notifier as a context manager, so it is closed.
    assert exits == [True]
    assert "Test message sent" in flatten(capsys.readouterr().out)


def test_test_slack_error_rc1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean_env(monkeypatch)
    config_path = write_config(tmp_path)

    exits: list[bool] = []

    class FailingNotifier:
        def __init__(self, webhook_url: str) -> None:
            pass

        def __enter__(self) -> FailingNotifier:
            return self

        def __exit__(self, *exc_info: object) -> None:
            exits.append(True)

        def send_text(self, text: str) -> None:
            raise SlackError("webhook said no")

    monkeypatch.setattr(cli, "SlackNotifier", FailingNotifier)
    rc = main(["--config", str(config_path), "test-slack"])
    assert rc == 1
    # __exit__ still runs when send_text raises inside the with block.
    assert exits == [True]
    assert "webhook said no" in flatten(capsys.readouterr().err)


# ---------------------------------------------------------------- outlook auth and routes


OUTLOOK_BODY = """
state_file = "state.json"

[slack]
webhook_url = "https://hooks.slack.com/services/T/B/X"

[outlook]
client_id = "app-id"
notification_url = "https://example.com/outlook/push"
client_state = "sekret"

[[accounts]]
name = "ol"
email = "ol@example.com"
token_file = "token-ol.json"
provider = "outlook"
"""


def test_auth_outlook_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean_env(monkeypatch)
    path = tmp_path / "config.toml"
    path.write_text(OUTLOOK_BODY, encoding="utf-8")
    calls: list[tuple[object, Path]] = []

    def fake_flow(settings: object, token_file: Path) -> str:
        calls.append((settings, token_file))
        return "ol@example.com"

    monkeypatch.setattr(cli, "run_outlook_oauth_flow", fake_flow)
    rc = main(["--config", str(path), "auth", "ol"])
    assert rc == 0
    assert calls[0][1] == tmp_path / "token-ol.json"
    assert "Token saved" in flatten(capsys.readouterr().out)
    assert "Warning" not in flatten(capsys.readouterr().out)


def test_auth_outlook_mismatched_email_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean_env(monkeypatch)
    path = tmp_path / "config.toml"
    path.write_text(OUTLOOK_BODY, encoding="utf-8")
    monkeypatch.setattr(cli, "run_outlook_oauth_flow", lambda *_: "other@example.com")
    rc = main(["--config", str(path), "auth", "ol"])
    assert rc == 0
    out = flatten(capsys.readouterr().out)
    assert "Warning" in out
    assert "other@example.com" in out


def test_auth_refuses_gmail_account_without_settings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    account = AccountConfig(
        name="one",
        email="one@example.com",
        token_file=tmp_path / "token.json",
        provider="gmail",
    )
    config = AppConfig(
        slack=SlackConfig(webhook_url="https://hooks.slack.com/services/T/B/X"),
        gmail=None,
        accounts=(account,),
        state_file=tmp_path / "state.json",
    )
    assert _cmd_auth(config, "one") == 2
    assert "[gmail] is not configured" in flatten(capsys.readouterr().err)


def test_auth_refuses_outlook_account_without_settings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    account = AccountConfig(
        name="ol",
        email="ol@example.com",
        token_file=tmp_path / "token.json",
        provider="outlook",
    )
    config = AppConfig(
        slack=SlackConfig(webhook_url="https://hooks.slack.com/services/T/B/X"),
        gmail=None,
        accounts=(account,),
        state_file=tmp_path / "state.json",
    )
    assert _cmd_auth(config, "ol") == 2
    assert "[outlook] is not configured" in flatten(capsys.readouterr().err)


def test_serve_prints_outlook_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean_env(monkeypatch)
    path = tmp_path / "config.toml"
    path.write_text(OUTLOOK_BODY, encoding="utf-8")
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)
    rc = main(["--config", str(path), "serve", "--host", "127.0.0.1", "--port", "9001"])
    assert rc == 0
    out = flatten(capsys.readouterr().out)
    assert "http://127.0.0.1:9001/outlook/push" in out
    assert "/gmail/push" not in out


def test_push_urls_cover_each_combination(tmp_path: Path) -> None:
    slack = SlackConfig(webhook_url="https://hooks.slack.com/services/T/B/X")
    outlook = OutlookSettings(
        client_id="app-id",
        notification_url="https://example.com/outlook/push",
        client_state="sekret",
    )
    base = {"slack": slack, "accounts": (), "state_file": tmp_path / "state.json"}
    assert _push_urls(AppConfig(gmail=None, outlook=None, **base), "h", 1) == "http://h:1"
    both = _push_urls(AppConfig(gmail=None, outlook=outlook, **base), "h", 1)
    assert both == "http://h:1/outlook/push"
