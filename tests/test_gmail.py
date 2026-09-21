# Copyright (c) 2026 Leo Chen <leo.chen0412@outlook.com>

"""Tests for email_notifier.providers.gmail."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httplib2
import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

import email_notifier.providers.gmail as gmail_mod
from email_notifier.config import AccountConfig, AppConfig, GmailSettings, SlackConfig
from email_notifier.models import EmailMessage
from email_notifier.providers.base import ProviderError
from email_notifier.providers.gmail import (
    SCOPES,
    GmailProvider,
    build_service,
    run_oauth_flow,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def http_error(status: int, body: bytes = b"boom") -> HttpError:
    return HttpError(httplib2.Response({"status": str(status)}), body)


class FakeRequest:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return self._result


def _as_request(item):
    if isinstance(item, Exception):
        return FakeRequest(error=item)
    return FakeRequest(result=item)


class FakeHistory:
    """users().history(); .list() pops one canned response per call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return _as_request(self.responses.pop(0))


class FakeMessages:
    """users().messages(); .get() serves canned responses keyed by id."""

    def __init__(self, responses):
        self.responses = dict(responses)
        self.calls = []

    def get(self, **kwargs):
        self.calls.append(kwargs)
        return _as_request(self.responses[kwargs["id"]])


class FakeUsers:
    def __init__(self, *, watch=None, profile=None, history=None, messages=None):
        self._watch = watch
        self._profile = profile
        self._history = history
        self._messages = messages
        self.watch_calls = []
        self.profile_calls = []

    def watch(self, **kwargs):
        self.watch_calls.append(kwargs)
        return _as_request(self._watch)

    def getProfile(self, **kwargs):
        self.profile_calls.append(kwargs)
        return _as_request(self._profile)

    def history(self):
        return self._history

    def messages(self):
        return self._messages


class FakeService:
    def __init__(self, users):
        self._users = users

    def users(self):
        return self._users


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_account(
    name: str = "acct",
    label_ids: tuple[str, ...] = ("INBOX",),
    token_file: Path | None = None,
) -> AccountConfig:
    return AccountConfig(
        name=name,
        email="user@example.com",
        token_file=token_file or Path("/nonexistent/token.json"),
        label_ids=label_ids,
    )


def make_settings() -> GmailSettings:
    return GmailSettings(
        credentials_file=Path("/nonexistent/creds.json"),
        topic="projects/proj/topics/mail",
    )


def make_provider(users, account=None, settings=None):
    service = FakeService(users)
    account = account or make_account()
    settings = settings or make_settings()
    calls = {"count": 0}

    def factory():
        calls["count"] += 1
        return service

    provider = GmailProvider(account, settings, service_factory=factory)
    return provider, calls


# ---------------------------------------------------------------------------
# start_watch
# ---------------------------------------------------------------------------


def test_start_watch_builds_body_and_parses_expiration():
    users = FakeUsers(watch={"historyId": 42, "expiration": "1700000000000"})
    account = make_account(label_ids=("INBOX", "IMPORTANT"))
    provider, _ = make_provider(users, account=account)

    info = provider.start_watch()

    assert users.watch_calls == [
        {
            "userId": "me",
            "body": {
                "topicName": "projects/proj/topics/mail",
                "labelIds": ["INBOX", "IMPORTANT"],
                "labelFilterBehavior": "INCLUDE",
            },
        }
    ]
    assert info.cursor == "42"
    assert info.expires_at == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
    assert info.expires_at.tzinfo is UTC


def test_start_watch_without_expiration_leaves_expires_at_none():
    users = FakeUsers(watch={"historyId": "7"})
    provider, _ = make_provider(users)

    info = provider.start_watch()

    assert info.cursor == "7"
    assert info.expires_at is None


def test_start_watch_http_error_wraps_in_provider_error():
    users = FakeUsers(watch=http_error(403))
    provider, _ = make_provider(users, account=make_account(name="work"))

    with pytest.raises(ProviderError, match="Gmail watch failed for account 'work'"):
        provider.start_watch()


# ---------------------------------------------------------------------------
# fetch_messages_since / _list_history
# ---------------------------------------------------------------------------


def test_fetch_multi_page_accumulates_dedupes_and_skips_bad_records():
    history = FakeHistory(
        [
            {
                "historyId": "100",
                "nextPageToken": "tok2",
                "history": [
                    {
                        "messagesAdded": [
                            {"message": {"id": "m1", "labelIds": ["INBOX"]}},
                            {"message": {"id": "m2", "labelIds": ["INBOX", "UNREAD"]}},
                            {"message": {"id": "m1", "labelIds": ["INBOX"]}},  # dup within page
                        ]
                    },
                    {"labelsRemoved": []},  # no messagesAdded -> skipped
                    {"messagesAdded": [{"message": {"labelIds": ["INBOX"]}}]},  # no id -> skipped
                ],
            },
            {
                "historyId": "101",
                "history": [
                    {
                        "messagesAdded": [
                            {"message": {"id": "m2", "labelIds": ["INBOX"]}},  # dup across pages
                            {"message": {"id": "m3", "labelIds": ["INBOX"]}},
                        ]
                    }
                ],
            },
        ]
    )
    messages = FakeMessages(
        {
            "m1": {
                "payload": {
                    "headers": [
                        {"name": "FROM", "value": "Alice <alice@example.com>"},
                        {"name": "subject", "value": "Hello"},
                    ]
                },
                "snippet": "hi there",
                "internalDate": "1700000000000",
            },
            "m2": {},  # no payload, no snippet, no internalDate
            "m3": {"payload": {"headers": []}},
        }
    )
    users = FakeUsers(history=history, messages=messages)
    provider, _ = make_provider(users)

    result, new_cursor = provider.fetch_messages_since("50")

    assert new_cursor == "101"
    # pagination: startHistoryId stays put, pageToken threads through.
    # Filtering is client-side now, so no labelId kwarg is passed.
    assert history.calls[0] == {
        "userId": "me",
        "startHistoryId": "50",
        "historyTypes": ["messageAdded"],
        "pageToken": None,
    }
    assert history.calls[1]["pageToken"] == "tok2"
    assert history.calls[1]["startHistoryId"] == "50"
    assert len(history.calls) == 2
    # each unique id fetched exactly once, in first-seen order.
    assert [call["id"] for call in messages.calls] == ["m1", "m2", "m3"]
    assert messages.calls[0]["userId"] == "me"
    assert messages.calls[0]["format"] == "metadata"
    assert messages.calls[0]["metadataHeaders"] == ["From", "Subject"]

    assert result == [
        EmailMessage(
            account="acct",
            provider="gmail",
            message_id="m1",
            sender="Alice <alice@example.com>",
            subject="Hello",
            snippet="hi there",
            received_at=datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC),
        ),
        EmailMessage(
            account="acct",
            provider="gmail",
            message_id="m2",
            sender="",
            subject="",
            snippet="",
            received_at=None,
        ),
        EmailMessage(
            account="acct",
            provider="gmail",
            message_id="m3",
            sender="",
            subject="",
            snippet="",
            received_at=None,
        ),
    ]


def test_fetch_empty_history_returns_new_cursor_from_response():
    history = FakeHistory([{"historyId": "200"}])
    users = FakeUsers(history=history)
    provider, _ = make_provider(users)

    assert provider.fetch_messages_since("50") == ([], "200")


def test_fetch_response_without_history_id_keeps_prior_cursor():
    history = FakeHistory([{}])
    users = FakeUsers(history=history)
    provider, _ = make_provider(users)

    assert provider.fetch_messages_since("50") == ([], "50")


def test_fetch_404_resyncs_cursor_from_profile():
    history = FakeHistory([http_error(404, b"not found")])
    users = FakeUsers(history=history, profile={"historyId": 999})
    provider, _ = make_provider(users)

    assert provider.fetch_messages_since("stale") == ([], "999")
    assert users.profile_calls == [{"userId": "me"}]


def test_fetch_non_404_http_error_raises_provider_error():
    history = FakeHistory([http_error(500)])
    users = FakeUsers(history=history)
    provider, _ = make_provider(users, account=make_account(name="broken"))

    with pytest.raises(ProviderError, match="Gmail history read failed for account 'broken'"):
        provider.fetch_messages_since("50")


def test_fetch_empty_label_ids_disables_label_filtering():
    # With no watched labels, messages are kept even when their labels do
    # not match anything (or are absent entirely).
    history = FakeHistory(
        [
            {
                "historyId": "5",
                "history": [
                    {
                        "messagesAdded": [
                            {"message": {"id": "m1", "labelIds": ["SPAM"]}},
                            {"message": {"id": "m2"}},  # no labelIds at all
                        ]
                    }
                ],
            }
        ]
    )
    messages = FakeMessages({"m1": {}, "m2": {}})
    users = FakeUsers(history=history, messages=messages)
    provider, _ = make_provider(users, account=make_account(label_ids=()))

    result, new_cursor = provider.fetch_messages_since("1")

    assert new_cursor == "5"
    assert [message.message_id for message in result] == ["m1", "m2"]
    assert "labelId" not in history.calls[0]


def test_fetch_skips_message_without_watched_label_and_advances_cursor():
    history = FakeHistory(
        [
            {
                "historyId": "60",
                "history": [
                    {
                        "messagesAdded": [
                            {"message": {"id": "spammy", "labelIds": ["SPAM", "UNREAD"]}}
                        ]
                    }
                ],
            }
        ]
    )
    messages = FakeMessages({})
    users = FakeUsers(history=history, messages=messages)
    provider, _ = make_provider(users)  # watches ("INBOX",)

    assert provider.fetch_messages_since("1") == ([], "60")
    assert messages.calls == []  # never fetched


def test_fetch_skips_message_with_no_label_ids_when_labels_watched():
    history = FakeHistory(
        [
            {
                "historyId": "61",
                "history": [{"messagesAdded": [{"message": {"id": "bare"}}]}],
            }
        ]
    )
    messages = FakeMessages({})
    users = FakeUsers(history=history, messages=messages)
    provider, _ = make_provider(users)  # watches ("INBOX",)

    assert provider.fetch_messages_since("1") == ([], "61")
    assert messages.calls == []


def test_fetch_multi_label_account_keeps_message_matching_any_watched_label():
    account = make_account(label_ids=("INBOX", "Label_7"))
    history = FakeHistory(
        [
            {
                "historyId": "70",
                "history": [
                    {"messagesAdded": [{"message": {"id": "m7", "labelIds": ["Label_7"]}}]}
                ],
            }
        ]
    )
    messages = FakeMessages({"m7": {"snippet": "labelled"}})
    users = FakeUsers(history=history, messages=messages)
    provider, _ = make_provider(users, account=account)

    result, new_cursor = provider.fetch_messages_since("1")

    assert new_cursor == "70"
    assert [message.message_id for message in result] == ["m7"]
    assert result[0].snippet == "labelled"


def test_message_404_is_skipped_but_others_returned():
    history = FakeHistory(
        [
            {
                "historyId": "10",
                "history": [
                    {
                        "messagesAdded": [
                            {"message": {"id": "gone", "labelIds": ["INBOX"]}},
                            {"message": {"id": "kept", "labelIds": ["INBOX"]}},
                        ]
                    }
                ],
            }
        ]
    )
    messages = FakeMessages(
        {
            "gone": http_error(404),
            "kept": {"snippet": "still here"},
        }
    )
    users = FakeUsers(history=history, messages=messages)
    provider, _ = make_provider(users)

    result, new_cursor = provider.fetch_messages_since("1")

    assert new_cursor == "10"
    assert [message.message_id for message in result] == ["kept"]
    assert result[0].snippet == "still here"


def test_message_non_404_http_error_raises_provider_error():
    history = FakeHistory(
        [
            {
                "historyId": "10",
                "history": [{"messagesAdded": [{"message": {"id": "m1", "labelIds": ["INBOX"]}}]}],
            }
        ]
    )
    messages = FakeMessages({"m1": http_error(503)})
    users = FakeUsers(history=history, messages=messages)
    provider, _ = make_provider(users, account=make_account(name="acct"))

    with pytest.raises(ProviderError, match="Gmail message read failed for account 'acct'"):
        provider.fetch_messages_since("1")


# ---------------------------------------------------------------------------
# current_cursor
# ---------------------------------------------------------------------------


def test_current_cursor_reads_profile_history_id():
    users = FakeUsers(profile={"historyId": 77})
    provider, _ = make_provider(users)

    assert provider.current_cursor() == "77"
    assert users.profile_calls == [{"userId": "me"}]


def test_current_cursor_http_error_raises_provider_error():
    users = FakeUsers(profile=http_error(401))
    provider, _ = make_provider(users, account=make_account(name="locked"))

    with pytest.raises(ProviderError, match="Could not read Gmail profile for account 'locked'"):
        provider.current_cursor()


# ---------------------------------------------------------------------------
# construction wiring
# ---------------------------------------------------------------------------


def test_from_config_wires_account_and_gmail_settings():
    account = make_account()
    config = AppConfig(
        slack=SlackConfig(webhook_url="https://hooks.example.com/x"),
        gmail=make_settings(),
        accounts=(account,),
        state_file=Path("/nonexistent/state.json"),
    )

    provider = GmailProvider.from_config(account, config)

    assert isinstance(provider, GmailProvider)
    assert provider.name == "gmail"
    assert provider._account is account
    assert provider._settings is config.gmail


def test_lazy_api_calls_service_factory_exactly_once():
    history = FakeHistory([{"historyId": "10"}, {"historyId": "11"}])
    users = FakeUsers(history=history)
    provider, calls = make_provider(users)

    assert provider.fetch_messages_since("1") == ([], "10")
    assert provider.fetch_messages_since("10") == ([], "11")
    assert calls["count"] == 1


def test_default_service_factory_uses_build_service_on_token_file(tmp_path):
    account = make_account(token_file=tmp_path / "missing-token.json")
    provider = GmailProvider(account, make_settings())

    with pytest.raises(ProviderError, match="No token found"):
        provider.current_cursor()


# ---------------------------------------------------------------------------
# build_service
# ---------------------------------------------------------------------------


class FakeCreds:
    def __init__(self, *, valid, expired=False, refresh_token=None):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.refresh_calls = []

    def refresh(self, request):
        self.refresh_calls.append(request)

    def to_json(self):
        return '{"token": "refreshed"}'


def _patch_credentials(monkeypatch, creds):
    calls = []

    class FakeCredentials:
        @staticmethod
        def from_authorized_user_file(filename, scopes):
            calls.append((filename, scopes))
            return creds

    monkeypatch.setattr(gmail_mod, "Credentials", FakeCredentials)
    return calls


def _patch_build(monkeypatch):
    calls = []
    sentinel = object()

    def fake_build(api, version, *, credentials, cache_discovery):
        calls.append((api, version, credentials, cache_discovery))
        return sentinel

    monkeypatch.setattr(gmail_mod, "build", fake_build)
    return calls, sentinel


def test_build_service_missing_token_file(tmp_path):
    with pytest.raises(ProviderError, match="email-notifier auth"):
        build_service(tmp_path / "no-token.json")


def test_build_service_valid_creds(tmp_path, monkeypatch):
    token = tmp_path / "token.json"
    token.write_text('{"token": "old"}', encoding="utf-8")
    creds = FakeCreds(valid=True)
    creds_calls = _patch_credentials(monkeypatch, creds)
    build_calls, sentinel = _patch_build(monkeypatch)

    service = build_service(token)

    assert service is sentinel
    assert creds_calls == [(str(token), list(SCOPES))]
    assert build_calls == [("gmail", "v1", creds, False)]
    assert creds.refresh_calls == []
    assert token.read_text(encoding="utf-8") == '{"token": "old"}'


def test_build_service_expired_creds_refreshes_and_rewrites_token(tmp_path, monkeypatch):
    token = tmp_path / "token.json"
    token.write_text('{"token": "old"}', encoding="utf-8")
    creds = FakeCreds(valid=False, expired=True, refresh_token="rt")
    _patch_credentials(monkeypatch, creds)
    build_calls, sentinel = _patch_build(monkeypatch)
    monkeypatch.setattr(gmail_mod, "Request", lambda: "fake-request")

    service = build_service(token)

    assert service is sentinel
    assert creds.refresh_calls == ["fake-request"]
    assert token.read_text(encoding="utf-8") == '{"token": "refreshed"}'
    assert build_calls == [("gmail", "v1", creds, False)]


def test_build_service_malformed_token_raises_provider_error(tmp_path):
    # The real Credentials.from_authorized_user_file raises ValueError on
    # unparseable JSON; build_service must wrap it.
    token = tmp_path / "token.json"
    token.write_text("this is not json{", encoding="utf-8")

    with pytest.raises(ProviderError, match="malformed") as excinfo:
        build_service(token)

    assert "email-notifier auth" in str(excinfo.value)


def test_build_service_refresh_error_raises_and_leaves_token_untouched(tmp_path, monkeypatch):
    token = tmp_path / "token.json"
    token.write_text('{"token": "old"}', encoding="utf-8")

    class RevokedCreds(FakeCreds):
        def refresh(self, request):
            raise RefreshError("token has been revoked")

    creds = RevokedCreds(valid=False, expired=True, refresh_token="rt")
    _patch_credentials(monkeypatch, creds)
    monkeypatch.setattr(gmail_mod, "Request", lambda: "fake-request")

    with pytest.raises(ProviderError, match="revoked or expired") as excinfo:
        build_service(token)

    assert "Re-run `email-notifier auth" in str(excinfo.value)
    # The stale token file must NOT be rewritten.
    assert token.read_text(encoding="utf-8") == '{"token": "old"}'


def test_build_service_invalid_not_expired_raises(tmp_path, monkeypatch):
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    creds = FakeCreds(valid=False, expired=False, refresh_token="rt")
    _patch_credentials(monkeypatch, creds)

    with pytest.raises(ProviderError, match="cannot be refreshed"):
        build_service(token)


def test_build_service_expired_without_refresh_token_raises(tmp_path, monkeypatch):
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    creds = FakeCreds(valid=False, expired=True, refresh_token=None)
    _patch_credentials(monkeypatch, creds)

    with pytest.raises(ProviderError, match="cannot be refreshed"):
        build_service(token)


# ---------------------------------------------------------------------------
# run_oauth_flow
# ---------------------------------------------------------------------------


class FakeOAuthCreds:
    def to_json(self):
        return '{"tok": 1}'


class FakeProfileUsers:
    def getProfile(self, **kwargs):
        assert kwargs == {"userId": "me"}
        return FakeRequest(result={"emailAddress": "someone@example.com"})


class FakeProfileService:
    def users(self):
        return FakeProfileUsers()


def test_run_oauth_flow_missing_credentials_file(tmp_path):
    with pytest.raises(ProviderError, match="OAuth client file not found"):
        run_oauth_flow(tmp_path / "absent.json", tmp_path / "token.json")


def test_run_oauth_flow_happy_path_writes_token_and_returns_email(tmp_path):
    credentials_file = tmp_path / "client.json"
    credentials_file.write_text("{}", encoding="utf-8")
    token_file = tmp_path / "nested" / "deep" / "token.json"
    fake_creds = FakeOAuthCreds()

    server_calls = []

    class FakeFlow:
        def run_local_server(self, *, port):
            server_calls.append(port)
            return fake_creds

    builder_calls = []

    def fake_builder(api, version, *, credentials, cache_discovery):
        builder_calls.append((api, version, credentials, cache_discovery))
        return FakeProfileService()

    email = run_oauth_flow(
        credentials_file,
        token_file,
        flow_factory=FakeFlow,
        service_builder=fake_builder,
    )

    assert email == "someone@example.com"
    assert token_file.read_text(encoding="utf-8") == '{"tok": 1}'
    assert server_calls == [0]
    assert builder_calls == [("gmail", "v1", fake_creds, False)]


def test_run_oauth_flow_default_factory_uses_installed_app_flow(tmp_path, monkeypatch):
    credentials_file = tmp_path / "client.json"
    credentials_file.write_text("{}", encoding="utf-8")
    token_file = tmp_path / "token.json"
    fake_creds = FakeOAuthCreds()

    secrets_calls = []

    class FakeFlow:
        def run_local_server(self, *, port):
            return fake_creds

    class FakeInstalledAppFlow:
        @staticmethod
        def from_client_secrets_file(filename, scopes):
            secrets_calls.append((filename, scopes))
            return FakeFlow()

    monkeypatch.setattr(gmail_mod, "InstalledAppFlow", FakeInstalledAppFlow)

    email = run_oauth_flow(
        credentials_file,
        token_file,
        service_builder=lambda *args, **kwargs: FakeProfileService(),
    )

    assert email == "someone@example.com"
    assert secrets_calls == [(str(credentials_file), list(SCOPES))]
    assert token_file.read_text(encoding="utf-8") == '{"tok": 1}'
