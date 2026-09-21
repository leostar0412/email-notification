"""Tests for email_notifier.server.create_app."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi.testclient import TestClient

from email_notifier.config import AccountConfig, AppConfig, GmailSettings, SlackConfig
from email_notifier.models import EmailMessage
from email_notifier.notifier import SlackError
from email_notifier.providers.base import ProviderError
from email_notifier.server import create_app
from email_notifier.state import CursorStore

# ---------------------------------------------------------------------------
# Builders and fakes
# ---------------------------------------------------------------------------


def make_account(tmp_path: Path, name: str = "work", email: str = "work@example.com"):
    return AccountConfig(name=name, email=email, token_file=tmp_path / f"{name}-token.json")


def make_config(tmp_path: Path, accounts, token: str | None = None) -> AppConfig:
    return AppConfig(
        slack=SlackConfig(webhook_url="https://hooks.slack.com/services/T0/B0/XYZ"),
        gmail=GmailSettings(
            credentials_file=tmp_path / "credentials.json",
            topic="projects/proj/topics/mail",
            pubsub_verification_token=token,
        ),
        accounts=tuple(accounts),
        state_file=tmp_path / "state.json",
    )


def make_message(mid: str = "m1", account: str = "work") -> EmailMessage:
    return EmailMessage(
        account=account,
        provider="gmail",
        message_id=mid,
        sender="alice@example.com",
        subject="Hello",
        snippet="hi there",
    )


def envelope(email: str = "work@example.com", history_id: object = 100) -> dict:
    payload = {"emailAddress": email, "historyId": history_id}
    return raw_envelope(json.dumps(payload).encode())


def raw_envelope(data_bytes: bytes) -> dict:
    return {
        "message": {
            "data": base64.b64encode(data_bytes).decode(),
            "messageId": "1",
        },
        "subscription": "s",
    }


class FakeNotifier:
    """Records notified messages; can raise SlackError on the Nth call."""

    def __init__(self, fail_on: int | None = None) -> None:
        self.sent: list[EmailMessage] = []
        self.fail_on = fail_on
        self.calls = 0

    def notify(self, message: EmailMessage) -> None:
        self.calls += 1
        if self.fail_on is not None and self.calls == self.fail_on:
            raise SlackError("slack said no")
        self.sent.append(message)


class FakeProvider:
    """Records fetch cursors; returns a preset (messages, new_cursor) or raises."""

    def __init__(self, messages=(), new_cursor: str = "999", error: Exception | None = None):
        self.cursors: list[str] = []
        self.messages = list(messages)
        self.new_cursor = new_cursor
        self.error = error

    def fetch_messages_since(self, cursor: str):
        self.cursors.append(cursor)
        if self.error is not None:
            raise self.error
        return self.messages, self.new_cursor


class FakeFactory:
    """Provider factory that hands out one FakeProvider per account name."""

    def __init__(self, providers: dict[str, FakeProvider]) -> None:
        self.providers = providers
        self.calls: list[str] = []

    def __call__(self, account: AccountConfig, config: AppConfig) -> FakeProvider:
        self.calls.append(account.name)
        return self.providers[account.name]


def build(tmp_path: Path, *, token=None, accounts=None, providers=None, notifier=None):
    """Wire up an app with fakes; return (client, store, notifier, factory)."""
    accounts = accounts if accounts is not None else [make_account(tmp_path)]
    config = make_config(tmp_path, accounts, token=token)
    store = CursorStore(config.state_file)
    notifier = notifier or FakeNotifier()
    factory = FakeFactory(providers or {a.name: FakeProvider() for a in accounts})
    app = create_app(config, notifier=notifier, store=store, provider_factory=factory)
    return TestClient(app), store, notifier, factory


# ---------------------------------------------------------------------------
# /healthz and default collaborators
# ---------------------------------------------------------------------------


def test_healthz_ok(tmp_path):
    client, _, _, _ = build(tmp_path)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_app_with_default_collaborators(tmp_path):
    config = make_config(tmp_path, [make_account(tmp_path)])
    app = create_app(config)
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_app_without_token_logs_open_endpoint_warning(tmp_path, caplog):
    config = make_config(tmp_path, [make_account(tmp_path)], token=None)
    with caplog.at_level("WARNING", logger="email_notifier.server"):
        create_app(config)
    assert any("will accept pushes from anyone" in record.message for record in caplog.records)


def test_create_app_with_token_does_not_log_open_endpoint_warning(tmp_path, caplog):
    config = make_config(tmp_path, [make_account(tmp_path)], token="sekret")
    with caplog.at_level("WARNING", logger="email_notifier.server"):
        create_app(config)
    assert not any("will accept pushes from anyone" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Verification token
# ---------------------------------------------------------------------------


def test_missing_token_rejected_when_configured(tmp_path):
    client, store, _, factory = build(tmp_path, token="sekret")
    response = client.post("/gmail/push", json=envelope())
    assert response.status_code == 403
    assert factory.calls == []
    assert store.get("work") is None


def test_wrong_token_rejected(tmp_path):
    client, store, _, factory = build(tmp_path, token="sekret")
    response = client.post("/gmail/push?token=nope", json=envelope())
    assert response.status_code == 403
    assert factory.calls == []
    assert store.get("work") is None


def test_non_ascii_token_rejected_not_500(tmp_path):
    client, store, _, factory = build(tmp_path, token="sekret")
    response = client.post("/gmail/push?token=sekr%C3%A9t", json=envelope())
    assert response.status_code == 403
    assert factory.calls == []
    assert store.get("work") is None


def test_correct_token_processed(tmp_path):
    provider = FakeProvider(messages=[make_message()], new_cursor="150")
    client, store, notifier, _ = build(tmp_path, token="sekret", providers={"work": provider})
    store.set("work", "50")
    response = client.post("/gmail/push?token=sekret", json=envelope(history_id=120))
    assert response.status_code == 204
    assert provider.cursors == ["50"]
    assert [m.message_id for m in notifier.sent] == ["m1"]
    assert store.get("work") == "150"


def test_no_token_configured_processed_without_token(tmp_path):
    provider = FakeProvider(messages=[], new_cursor="150")
    client, store, _, _ = build(tmp_path, providers={"work": provider})
    store.set("work", "50")
    response = client.post("/gmail/push", json=envelope())
    assert response.status_code == 204
    assert provider.cursors == ["50"]
    assert store.get("work") == "150"


# ---------------------------------------------------------------------------
# Malformed envelopes are acknowledged with 204
# ---------------------------------------------------------------------------


def test_non_json_body_acknowledged(tmp_path):
    client, _, _, factory = build(tmp_path)
    response = client.post(
        "/gmail/push", content=b"this is not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 204
    assert factory.calls == []


def test_invalid_utf8_body_acknowledged(tmp_path):
    client, _, _, factory = build(tmp_path)
    response = client.post(
        "/gmail/push", content=b"\xff\xfe{", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 204
    assert factory.calls == []


def test_envelope_json_list_acknowledged(tmp_path):
    client, _, _, factory = build(tmp_path)
    response = client.post("/gmail/push", json=[1, 2, 3])
    assert response.status_code == 204
    assert factory.calls == []


def test_message_not_a_dict_acknowledged(tmp_path):
    client, _, _, factory = build(tmp_path)
    response = client.post("/gmail/push", json={"message": "hello", "subscription": "s"})
    assert response.status_code == 204
    assert factory.calls == []


def test_message_without_data_acknowledged(tmp_path):
    client, _, _, factory = build(tmp_path)
    response = client.post("/gmail/push", json={"message": {"messageId": "1"}})
    assert response.status_code == 204
    assert factory.calls == []


def test_data_invalid_base64_acknowledged(tmp_path):
    client, _, _, factory = build(tmp_path)
    response = client.post("/gmail/push", json={"message": {"data": "!!!", "messageId": "1"}})
    assert response.status_code == 204
    assert factory.calls == []


def test_data_bad_base64_padding_acknowledged(tmp_path):
    client, _, _, factory = build(tmp_path)
    response = client.post("/gmail/push", json={"message": {"data": "A", "messageId": "1"}})
    assert response.status_code == 204
    assert factory.calls == []


def test_data_decodes_but_not_json_acknowledged(tmp_path):
    client, _, _, factory = build(tmp_path)
    response = client.post("/gmail/push", json=raw_envelope(b"plain text, not json"))
    assert response.status_code == 204
    assert factory.calls == []


def test_data_json_not_an_object_acknowledged(tmp_path):
    client, _, _, factory = build(tmp_path)
    response = client.post("/gmail/push", json=raw_envelope(b"[1]"))
    assert response.status_code == 204
    assert factory.calls == []


def test_missing_email_address_acknowledged(tmp_path):
    client, _, _, factory = build(tmp_path)
    response = client.post("/gmail/push", json=raw_envelope(json.dumps({"historyId": 5}).encode()))
    assert response.status_code == 204
    assert factory.calls == []


def test_missing_history_id_acknowledged(tmp_path):
    client, _, _, factory = build(tmp_path)
    body = raw_envelope(json.dumps({"emailAddress": "work@example.com"}).encode())
    response = client.post("/gmail/push", json=body)
    assert response.status_code == 204
    assert factory.calls == []


def test_unconfigured_email_acknowledged_and_nothing_stored(tmp_path):
    client, store, _, factory = build(tmp_path)
    response = client.post("/gmail/push", json=envelope(email="stranger@example.com"))
    assert response.status_code == 204
    assert factory.calls == []
    assert store.get("work") is None
    assert not (tmp_path / "state.json").exists()


# ---------------------------------------------------------------------------
# Cursor handling and delivery
# ---------------------------------------------------------------------------


def test_first_push_adopts_pushed_history_id_without_fetching(tmp_path):
    provider = FakeProvider(messages=[make_message()])
    client, store, notifier, factory = build(tmp_path, providers={"work": provider})
    response = client.post("/gmail/push", json=envelope(history_id=12345))
    assert response.status_code == 204
    assert store.get("work") == "12345"
    assert provider.cursors == []
    assert factory.calls == []
    assert notifier.sent == []


def test_subsequent_push_fetches_from_stored_cursor_and_advances(tmp_path):
    messages = [make_message("m1"), make_message("m2")]
    provider = FakeProvider(messages=messages, new_cursor="300")
    client, store, notifier, _ = build(tmp_path, providers={"work": provider})
    store.set("work", "200")
    response = client.post("/gmail/push", json=envelope(history_id=250))
    assert response.status_code == 204
    assert provider.cursors == ["200"]  # stored cursor, not the pushed 250
    assert [m.message_id for m in notifier.sent] == ["m1", "m2"]
    assert store.get("work") == "300"


def test_email_address_matched_case_insensitively(tmp_path):
    provider = FakeProvider(messages=[make_message()], new_cursor="400")
    client, store, notifier, _ = build(tmp_path, providers={"work": provider})
    store.set("work", "350")
    response = client.post("/gmail/push", json=envelope(email="WORK@Example.COM"))
    assert response.status_code == 204
    assert provider.cursors == ["350"]
    assert len(notifier.sent) == 1
    assert store.get("work") == "400"


def test_provider_error_returns_500_and_keeps_cursor(tmp_path):
    provider = FakeProvider(error=ProviderError("gmail history unavailable"))
    client, store, notifier, _ = build(tmp_path, providers={"work": provider})
    store.set("work", "50")
    response = client.post("/gmail/push", json=envelope())
    assert response.status_code == 500
    assert response.json() == {"detail": "processing failed"}  # static, no internals leaked
    assert provider.cursors == ["50"]
    assert notifier.sent == []
    assert store.get("work") == "50"


def test_slack_error_on_second_message_returns_500_and_keeps_cursor(tmp_path):
    messages = [make_message("m1"), make_message("m2")]
    provider = FakeProvider(messages=messages, new_cursor="300")
    notifier = FakeNotifier(fail_on=2)
    client, store, _, _ = build(tmp_path, providers={"work": provider}, notifier=notifier)
    store.set("work", "50")
    response = client.post("/gmail/push", json=envelope())
    assert response.status_code == 500
    assert response.json() == {"detail": "processing failed"}  # static, no internals leaked
    assert [m.message_id for m in notifier.sent] == ["m1"]  # first delivery succeeded
    assert store.get("work") == "50"


def test_provider_instance_cached_across_pushes(tmp_path):
    provider = FakeProvider(messages=[], new_cursor="60")
    client, store, _, factory = build(tmp_path, providers={"work": provider})
    store.set("work", "50")
    assert client.post("/gmail/push", json=envelope()).status_code == 204
    provider.new_cursor = "70"
    assert client.post("/gmail/push", json=envelope()).status_code == 204
    assert factory.calls == ["work"]  # created once, reused on the second push
    assert provider.cursors == ["50", "60"]
    assert store.get("work") == "70"


def test_two_accounts_have_separate_providers_and_cursors(tmp_path):
    work = make_account(tmp_path, name="work", email="work@example.com")
    home = make_account(tmp_path, name="home", email="home@example.com")
    providers = {
        "work": FakeProvider(messages=[make_message("w1", account="work")], new_cursor="11"),
        "home": FakeProvider(messages=[make_message("h1", account="home")], new_cursor="22"),
    }
    client, store, notifier, factory = build(tmp_path, accounts=[work, home], providers=providers)
    store.set("work", "10")
    store.set("home", "20")
    assert client.post("/gmail/push", json=envelope(email="work@example.com")).status_code == 204
    assert client.post("/gmail/push", json=envelope(email="home@example.com")).status_code == 204
    assert factory.calls == ["work", "home"]
    assert providers["work"].cursors == ["10"]
    assert providers["home"].cursors == ["20"]
    assert [m.message_id for m in notifier.sent] == ["w1", "h1"]
    assert store.get("work") == "11"
    assert store.get("home") == "22"
