# Copyright (c) 2026 Leo Chen <leo.chen0412@outlook.com>

"""Tests for email_notifier.providers.outlook. No network."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

import email_notifier.providers.outlook as outlook_mod
from email_notifier.config import AccountConfig, AppConfig, OutlookSettings, SlackConfig
from email_notifier.providers.base import ProviderError
from email_notifier.providers.outlook import (
    OutlookProvider,
    access_token_from_cache,
    build_graph_client,
    decode_cursor,
    encode_cursor,
    run_oauth_flow,
)

GRAPH = "https://graph.microsoft.com/v1.0"
DELTA = f"{GRAPH}/me/mailFolders/inbox/messages/delta?$deltatoken=abc"
NOTIFY = "https://notify.example/outlook/push"


def settings() -> OutlookSettings:
    return OutlookSettings(
        client_id="app-id",
        notification_url=NOTIFY,
        client_state="sekret",
        tenant="common",
    )


def make_account(tmp_path: Path, name: str = "work") -> AccountConfig:
    return AccountConfig(
        name=name,
        email="work@example.com",
        token_file=tmp_path / f"{name}.json",
        provider="outlook",
    )


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: object = None,
        *,
        text: str = "",
        bad_json: bool = False,
    ) -> None:
        self.status_code = status_code
        self.payload = payload
        self.text = text
        self.bad_json = bad_json

    def json(self) -> object:
        if self.bad_json:
            raise ValueError("not json")
        return self.payload


class FakeGraph:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        if not self._responses:
            raise AssertionError(f"unexpected {method} {url}")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def provider_for(tmp_path: Path, graph: FakeGraph, name: str = "work") -> OutlookProvider:
    return OutlookProvider(make_account(tmp_path, name), settings(), graph_factory=lambda: graph)


def json_response(status: int, payload: object) -> FakeResponse:
    return FakeResponse(status, payload)


def delta_page(messages: list[dict], *, delta: str = DELTA, nxt: str | None = None) -> FakeResponse:
    body: dict[str, object] = {"value": messages}
    if nxt is not None:
        body["@odata.nextLink"] = nxt
    else:
        body["@odata.deltaLink"] = delta
    return json_response(200, body)


def subscription_page(items: list[dict], nxt: str | None = None) -> FakeResponse:
    body: dict[str, object] = {"value": items}
    if nxt is not None:
        body["@odata.nextLink"] = nxt
    return json_response(200, body)


def created(expires: str = "2026-10-04T00:00:00Z", sub_id: str = "sub-new") -> FakeResponse:
    return json_response(200, {"id": sub_id, "expirationDateTime": expires})


def message(
    message_id: str = "m1",
    created_at: str = "2026-01-02T00:00:00Z",
    **extra: object,
) -> dict:
    item = {
        "id": message_id,
        "createdDateTime": created_at,
        "receivedDateTime": "2026-01-02T03:04:05Z",
        "subject": "Hello",
        "bodyPreview": "preview",
        "from": {"emailAddress": {"name": "Ada", "address": "ada@example.com"}},
    }
    item.update(extra)
    return item


# ---------------------------------------------------------------------------
# Cursor codec
# ---------------------------------------------------------------------------


def test_encode_cursor_normalizes_naive_since() -> None:
    raw = encode_cursor("https://delta.example", datetime(2026, 1, 1, 0, 0, 0))
    decoded = decode_cursor(raw)
    assert decoded is not None
    assert decoded[0] == "https://delta.example"
    assert decoded[1] == datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "cursor",
    [
        "not-json",
        "[]",
        "{}",
        '{"delta": "", "since": "2026-01-01T00:00:00Z"}',
        '{"delta": "x", "since": "nope"}',
    ],
)
def test_decode_cursor_rejects_unusable_values(cursor: str) -> None:
    assert decode_cursor(cursor) is None


def test_decode_cursor_accepts_naive_since() -> None:
    decoded = decode_cursor('{"delta": "https://delta.example", "since": "2026-01-01T00:00:00"}')
    assert decoded is not None
    assert decoded[1] == datetime(2026, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# start_watch
# ---------------------------------------------------------------------------


def test_start_watch_creates_subscription(tmp_path: Path) -> None:
    graph = FakeGraph(
        [
            subscription_page([]),
            created(),
            delta_page([]),
        ]
    )
    info = provider_for(tmp_path, graph).start_watch()
    decoded = decode_cursor(info.cursor)
    assert decoded is not None
    assert decoded[0] == DELTA
    assert info.expires_at == datetime(2026, 10, 4, tzinfo=UTC)
    methods = [call[0] for call in graph.calls]
    assert methods == ["GET", "POST", "GET"]
    post = graph.calls[1]
    assert post[1] == f"{GRAPH}/subscriptions"
    body = post[2]["json"]
    assert body["changeType"] == "created"
    assert body["notificationUrl"] == NOTIFY
    assert body["resource"] == "me/mailFolders/inbox/messages"
    assert body["clientState"] == "sekret:work"
    assert graph.calls[2][2]["params"] == {"$deltatoken": "latest"}


def test_start_watch_renews_matching_inbox_subscription(tmp_path: Path) -> None:
    graph = FakeGraph(
        [
            subscription_page(
                [
                    {
                        "id": "other",
                        "notificationUrl": NOTIFY,
                        "resource": "me/events",
                    },
                    {
                        "id": "inbox-sub",
                        "notificationUrl": NOTIFY,
                        "resource": "me/mailFolders('Inbox')/messages",
                    },
                ]
            ),
            created(expires="2026-10-05T01:02:03Z", sub_id="inbox-sub"),
            delta_page([]),
        ]
    )
    info = provider_for(tmp_path, graph).start_watch()
    assert info.expires_at == datetime(2026, 10, 5, 1, 2, 3, tzinfo=UTC)
    assert [call[0] for call in graph.calls] == ["GET", "PATCH", "GET"]
    assert graph.calls[1][1] == f"{GRAPH}/subscriptions/inbox-sub"


def test_start_watch_pages_subscriptions_and_skips_other_urls(tmp_path: Path) -> None:
    graph = FakeGraph(
        [
            subscription_page(
                [
                    {
                        "id": "elsewhere",
                        "notificationUrl": "https://elsewhere.example",
                        "resource": "me/mailFolders/inbox/messages",
                    },
                    "not-a-dict",
                    {"notificationUrl": NOTIFY, "resource": "me/mailFolders/inbox/messages"},
                ],
                nxt=f"{GRAPH}/subscriptions?$skiptoken=2",
            ),
            subscription_page(
                [
                    {
                        "id": "page-2",
                        "notificationUrl": NOTIFY,
                        "resource": "me/mailFolders/inbox/messages",
                    }
                ]
            ),
            created(),
            delta_page([]),
        ]
    )
    provider_for(tmp_path, graph).start_watch()
    assert graph.calls[1][0] == "GET"
    assert "skiptoken" in graph.calls[1][1]
    assert graph.calls[2][0] == "PATCH"


def test_start_watch_ignores_non_list_subscription_value(tmp_path: Path) -> None:
    graph = FakeGraph(
        [
            json_response(200, {"value": "nope"}),
            created(expires="not-a-date"),
            delta_page([]),
        ]
    )
    info = provider_for(tmp_path, graph).start_watch()
    assert info.expires_at is None
    assert graph.calls[1][0] == "POST"


def test_start_watch_http_error_wraps_provider_error(tmp_path: Path) -> None:
    graph = FakeGraph([FakeResponse(403, {"error": {"message": "denied"}}, text="denied")])
    with pytest.raises(ProviderError, match="HTTP 403 denied"):
        provider_for(tmp_path, graph).start_watch()


def test_start_watch_error_detail_falls_back_to_text(tmp_path: Path) -> None:
    graph = FakeGraph([FakeResponse(500, text="plain failure", bad_json=True)])
    with pytest.raises(ProviderError, match="plain failure"):
        provider_for(tmp_path, graph).start_watch()


def test_start_watch_error_detail_uses_text_when_json_is_not_an_object(tmp_path: Path) -> None:
    graph = FakeGraph([FakeResponse(500, ["nope"], text="list-body")])
    with pytest.raises(ProviderError, match="list-body"):
        provider_for(tmp_path, graph).start_watch()


def test_start_watch_delta_expiry_is_provider_error(tmp_path: Path) -> None:
    graph = FakeGraph([subscription_page([]), created(), FakeResponse(410, {})])
    with pytest.raises(ProviderError, match="delta link expired"):
        provider_for(tmp_path, graph).start_watch()


def test_network_error_is_provider_error(tmp_path: Path) -> None:
    graph = FakeGraph([httpx.ConnectError("down")])
    with pytest.raises(ProviderError, match="Outlook watch failed for account 'work': down"):
        provider_for(tmp_path, graph).start_watch()


# ---------------------------------------------------------------------------
# fetch_messages_since and current_cursor
# ---------------------------------------------------------------------------


def test_fetch_keeps_new_mail_and_skips_updates_and_removals(tmp_path: Path) -> None:
    since = datetime(2026, 1, 1, tzinfo=UTC)
    cursor = encode_cursor(DELTA, since)
    fresh = f"{GRAPH}/delta?$deltatoken=next"
    graph = FakeGraph(
        [
            delta_page(
                [
                    message("new"),
                    message("same-instant", created_at="2026-01-01T00:00:00Z"),
                    message("old", created_at="2025-12-01T00:00:00Z"),
                    {
                        "id": "gone",
                        "@removed": {"reason": "deleted"},
                        "createdDateTime": "2026-02-01T00:00:00Z",
                    },
                    {"createdDateTime": "2026-02-01T00:00:00Z"},
                    {"id": "", "createdDateTime": "2026-02-01T00:00:00Z"},
                    message("bad-time", created_at="not-a-date"),
                    message("empty-time", created_at=""),
                    message("bad-email", **{"from": {"emailAddress": "ada"}}),
                    message("no-from", **{"from": "x"}),
                    message("address-only", **{"from": {"emailAddress": {"address": "a@b.c"}}}),
                    message("name-only", **{"from": {"emailAddress": {"name": "Ada"}}}),
                    message(
                        "blank-headers",
                        **{"subject": None, "bodyPreview": None, "receivedDateTime": "yesterday"},
                    ),
                ],
                delta=fresh,
            )
        ]
    )
    messages, new_cursor = provider_for(tmp_path, graph).fetch_messages_since(cursor)
    ids = [item.message_id for item in messages]
    assert ids == [
        "new",
        "same-instant",
        "bad-email",
        "no-from",
        "address-only",
        "name-only",
        "blank-headers",
    ]
    new_mail = messages[0]
    assert new_mail.sender == "Ada <ada@example.com>"
    assert new_mail.subject == "Hello"
    assert new_mail.snippet == "preview"
    assert new_mail.received_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert new_mail.provider == "outlook"
    assert new_mail.account == "work"
    assert messages[2].sender == ""
    assert messages[3].sender == ""
    assert messages[4].sender == "a@b.c"
    assert messages[5].sender == "Ada"
    assert messages[6].subject == ""
    assert messages[6].snippet == ""
    assert messages[6].received_at is None
    decoded = decode_cursor(new_cursor)
    assert decoded is not None
    assert decoded[0] == fresh
    assert "params" not in graph.calls[0][2]


def test_fetch_pages_until_delta_link(tmp_path: Path) -> None:
    cursor = encode_cursor(DELTA, datetime(2026, 1, 1, tzinfo=UTC))
    page2 = f"{GRAPH}/delta?$skiptoken=2"
    graph = FakeGraph(
        [
            delta_page([message("m1")], nxt=page2),
            delta_page([message("m2")], delta=f"{GRAPH}/delta?$deltatoken=done"),
        ]
    )
    messages, new_cursor = provider_for(tmp_path, graph).fetch_messages_since(cursor)
    assert [item.message_id for item in messages] == ["m1", "m2"]
    assert decode_cursor(new_cursor)[0].endswith("done")
    assert graph.calls[1][1] == page2


def test_fetch_ignores_non_dict_delta_values(tmp_path: Path) -> None:
    cursor = encode_cursor(DELTA, datetime(2026, 1, 1, tzinfo=UTC))
    graph = FakeGraph(
        [
            json_response(
                200,
                {"value": ["skip-me", message("kept")], "@odata.deltaLink": DELTA},
            )
        ]
    )
    messages, _cursor = provider_for(tmp_path, graph).fetch_messages_since(cursor)
    assert [item.message_id for item in messages] == ["kept"]


def test_fetch_ignores_non_list_delta_value(tmp_path: Path) -> None:
    cursor = encode_cursor(DELTA, datetime(2026, 1, 1, tzinfo=UTC))
    graph = FakeGraph([json_response(200, {"value": "nope", "@odata.deltaLink": DELTA})])
    messages, _cursor = provider_for(tmp_path, graph).fetch_messages_since(cursor)
    assert messages == []


def test_fetch_410_resyncs_to_now(tmp_path: Path) -> None:
    cursor = encode_cursor(DELTA, datetime(2026, 1, 1, tzinfo=UTC))
    fresh = f"{GRAPH}/delta?$deltatoken=fresh"
    graph = FakeGraph(
        [
            FakeResponse(410, {}),
            delta_page([], delta=fresh),
        ]
    )
    messages, new_cursor = provider_for(tmp_path, graph).fetch_messages_since(cursor)
    assert messages == []
    assert decode_cursor(new_cursor)[0] == fresh
    assert graph.calls[1][2]["params"] == {"$deltatoken": "latest"}


def test_fetch_malformed_cursor_resyncs(tmp_path: Path) -> None:
    graph = FakeGraph([delta_page([], delta=DELTA)])
    messages, new_cursor = provider_for(tmp_path, graph).fetch_messages_since("not-a-cursor")
    assert messages == []
    assert decode_cursor(new_cursor)[0] == DELTA


def test_fetch_missing_delta_link_raises(tmp_path: Path) -> None:
    cursor = encode_cursor(DELTA, datetime(2026, 1, 1, tzinfo=UTC))
    graph = FakeGraph([json_response(200, {"value": [], "@odata.nextLink": ""})])
    with pytest.raises(ProviderError, match="no nextLink or deltaLink"):
        provider_for(tmp_path, graph).fetch_messages_since(cursor)


def test_fetch_non_json_response_raises(tmp_path: Path) -> None:
    cursor = encode_cursor(DELTA, datetime(2026, 1, 1, tzinfo=UTC))
    graph = FakeGraph([FakeResponse(200, bad_json=True)])
    with pytest.raises(ProviderError, match="not JSON"):
        provider_for(tmp_path, graph).fetch_messages_since(cursor)


def test_fetch_non_object_json_raises(tmp_path: Path) -> None:
    cursor = encode_cursor(DELTA, datetime(2026, 1, 1, tzinfo=UTC))
    graph = FakeGraph([json_response(200, [1, 2])])
    with pytest.raises(ProviderError, match="not a JSON object"):
        provider_for(tmp_path, graph).fetch_messages_since(cursor)


def test_delta_paging_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outlook_mod, "_MAX_DELTA_PAGES", 1)
    cursor = encode_cursor(DELTA, datetime(2026, 1, 1, tzinfo=UTC))
    graph = FakeGraph([delta_page([message()], nxt=f"{GRAPH}/more")])
    with pytest.raises(ProviderError, match="exceeded 1 pages"):
        provider_for(tmp_path, graph).fetch_messages_since(cursor)


def test_current_cursor_wraps_http_error(tmp_path: Path) -> None:
    graph = FakeGraph([FakeResponse(401, {"error": {"code": "InvalidAuthenticationToken"}})])
    with pytest.raises(ProviderError, match="Could not read the Outlook delta link"):
        provider_for(tmp_path, graph).current_cursor()


def test_current_cursor_wraps_expiry(tmp_path: Path) -> None:
    graph = FakeGraph([FakeResponse(410, {})])
    with pytest.raises(ProviderError, match="delta link expired"):
        provider_for(tmp_path, graph).current_cursor()


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_from_config_wires_settings(tmp_path: Path) -> None:
    account = make_account(tmp_path)
    config = AppConfig(
        slack=SlackConfig(webhook_url="https://hooks.example/x"),
        gmail=None,
        accounts=(account,),
        state_file=tmp_path / "state.json",
        outlook=settings(),
    )
    provider = OutlookProvider.from_config(account, config)
    assert provider._settings is config.outlook
    assert provider._account is account


def test_from_config_requires_outlook_settings(tmp_path: Path) -> None:
    account = make_account(tmp_path)
    config = AppConfig(
        slack=SlackConfig(webhook_url="https://hooks.example/x"),
        gmail=None,
        accounts=(account,),
        state_file=tmp_path / "state.json",
    )
    with pytest.raises(ProviderError, match=r"\[outlook\] is not configured"):
        OutlookProvider.from_config(account, config)


def test_default_graph_factory_is_used_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = object()
    calls: list[Path] = []

    def fake_build(token_file: Path, outlook_settings: OutlookSettings) -> object:
        calls.append(token_file)
        assert outlook_settings.client_id == "app-id"
        return sentinel

    monkeypatch.setattr(outlook_mod, "build_graph_client", fake_build)
    provider = OutlookProvider(make_account(tmp_path), settings())
    assert provider._graph is sentinel
    assert provider._graph is sentinel
    assert calls == [tmp_path / "work.json"]


# ---------------------------------------------------------------------------
# OAuth and token cache
# ---------------------------------------------------------------------------


class FakeTokenCache:
    def __init__(self) -> None:
        self.has_state_changed = False
        self.serialized = '{"cache": true}'

    def deserialize(self, raw: str) -> None:
        if raw == "bad":
            raise ValueError("malformed cache")

    def serialize(self) -> str:
        return self.serialized


class FakePublicClient:
    accounts: list[dict[str, str]]
    silent_result: object
    mark_changed: bool

    def __init__(
        self,
        client_id: str,
        authority: str | None = None,
        token_cache: FakeTokenCache | None = None,
    ) -> None:
        self.client_id = client_id
        self.authority = authority
        self.token_cache = token_cache

    def get_accounts(self) -> list[dict[str, str]]:
        return list(self.accounts)

    def acquire_token_silent(self, scopes: list[str], account: dict[str, str]) -> object:
        assert "Mail.Read" in scopes[0]
        assert account["username"] == "work@example.com"
        if self.token_cache is not None:
            self.token_cache.has_state_changed = FakePublicClient.mark_changed
        return self.silent_result

    mark_changed = False


def test_run_oauth_flow_saves_cache_and_returns_email(tmp_path: Path) -> None:
    cache = FakeTokenCache()

    class App:
        token_cache = cache

        def acquire_token_interactive(self, scopes: list[str]) -> dict[str, str]:
            assert "User.Read" in scopes[1]
            return {"access_token": "interactive-token"}

    seen: list[str] = []
    token_path = tmp_path / "nested" / "token.json"
    email = run_oauth_flow(
        settings(),
        token_path,
        app_factory=App,
        profile_reader=lambda token: seen.append(token) or "work@example.com",
    )
    assert email == "work@example.com"
    assert seen == ["interactive-token"]
    assert token_path.read_text(encoding="utf-8") == '{"cache": true}'


def _run(
    tmp_path: Path,
    app: Callable[[], object],
    reader: Callable[[str], str] | None = None,
) -> str:
    return run_oauth_flow(
        settings(),
        tmp_path / "token.json",
        app_factory=app,
        profile_reader=reader or (lambda token: token),
    )


def test_run_oauth_flow_uses_default_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class App:
        def __init__(
            self,
            client_id: str,
            authority: str | None = None,
            token_cache: object = None,
        ) -> None:
            self.token_cache = token_cache

        def acquire_token_interactive(self, scopes: list[str]) -> dict[str, str]:
            return {"access_token": "tok"}

    monkeypatch.setattr(outlook_mod.msal, "PublicClientApplication", App)

    class Response:
        status_code = 200
        text = ""

        def json(self) -> dict[str, str]:
            return {"mail": "real@example.com"}

    monkeypatch.setattr(outlook_mod.httpx, "get", lambda *args, **kwargs: Response())
    email = run_oauth_flow(settings(), tmp_path / "token.json")
    assert email == "real@example.com"
    assert (tmp_path / "token.json").is_file()


def test_run_oauth_flow_error_result(tmp_path: Path) -> None:
    class App:
        token_cache = FakeTokenCache()

        def acquire_token_interactive(self, scopes: list[str]) -> dict[str, str]:
            return {"error": "access_denied", "error_description": "no consent"}

    with pytest.raises(ProviderError, match="no consent"):
        _run(tmp_path, App)


def test_run_oauth_flow_non_dict_result(tmp_path: Path) -> None:
    class App:
        token_cache = FakeTokenCache()

        def acquire_token_interactive(self, scopes: list[str]) -> str:
            return "nope"

    with pytest.raises(ProviderError, match="unknown error"):
        _run(tmp_path, App)


def test_run_oauth_flow_exception_is_wrapped(tmp_path: Path) -> None:
    class App:
        def acquire_token_interactive(self, scopes: list[str]) -> dict[str, str]:
            raise RuntimeError("browser failed")

    with pytest.raises(ProviderError, match="browser failed"):
        _run(tmp_path, App)


def test_run_oauth_flow_reraises_provider_error(tmp_path: Path) -> None:
    class App:
        def acquire_token_interactive(self, scopes: list[str]) -> dict[str, str]:
            raise ProviderError("consent denied")

    with pytest.raises(ProviderError, match=r"^consent denied$"):
        _run(tmp_path, App)


def test_run_oauth_flow_empty_cache(tmp_path: Path) -> None:
    class Cache:
        def serialize(self) -> str:
            return ""

    class App:
        token_cache = Cache()

        def acquire_token_interactive(self, scopes: list[str]) -> dict[str, str]:
            return {"access_token": "tok"}

    with pytest.raises(ProviderError, match="did not produce a token cache"):
        _run(tmp_path, App)


def test_run_oauth_flow_cache_without_serialize(tmp_path: Path) -> None:
    class App:
        token_cache = object()

        def acquire_token_interactive(self, scopes: list[str]) -> dict[str, str]:
            return {"access_token": "tok"}

    with pytest.raises(ProviderError, match="did not produce a token cache"):
        _run(tmp_path, App)


def test_authorized_email_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __init__(self, status: int, payload: object, *, bad_json: bool = False) -> None:
            self.status_code = status
            self.payload = payload
            self.text = "body"
            self.bad_json = bad_json

        def json(self) -> object:
            if self.bad_json:
                raise ValueError("nope")
            return self.payload

    responses: list[Response] = []

    def fake_get(*args: object, **kwargs: object) -> Response:
        return responses.pop(0)

    monkeypatch.setattr(outlook_mod.httpx, "get", fake_get)

    responses.append(
        Response(
            401,
            {"error": {"message": "no"}},
        )
    )
    with pytest.raises(ProviderError, match="HTTP 401"):
        outlook_mod._authorized_email("tok")

    responses.append(Response(200, {}, bad_json=True))
    with pytest.raises(ProviderError, match="not JSON"):
        outlook_mod._authorized_email("tok")

    responses.append(Response(200, ["nope"]))
    with pytest.raises(ProviderError, match="not a JSON object"):
        outlook_mod._authorized_email("tok")

    responses.append(Response(200, {"mail": "", "userPrincipalName": ""}))
    with pytest.raises(ProviderError, match="did not include an email"):
        outlook_mod._authorized_email("tok")

    responses.append(Response(200, {"mail": None, "userPrincipalName": "upn@example.com"}))
    assert outlook_mod._authorized_email("tok") == "upn@example.com"


def _patch_msal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outlook_mod.msal, "SerializableTokenCache", FakeTokenCache)
    monkeypatch.setattr(outlook_mod.msal, "PublicClientApplication", FakePublicClient)
    FakePublicClient.accounts = [{"username": "work@example.com"}]
    FakePublicClient.silent_result = {"access_token": "silent-token"}
    FakePublicClient.mark_changed = False


def test_access_token_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ProviderError, match="No token found"):
        access_token_from_cache(tmp_path / "missing.json", settings())


def test_access_token_malformed_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_msal(monkeypatch)
    path = tmp_path / "token.json"
    path.write_text("bad", encoding="utf-8")
    with pytest.raises(ProviderError, match="malformed"):
        access_token_from_cache(path, settings())


def test_access_token_without_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_msal(monkeypatch)
    FakePublicClient.accounts = []
    path = tmp_path / "token.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ProviderError, match="no Outlook account"):
        access_token_from_cache(path, settings())


def test_access_token_refresh_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_msal(monkeypatch)
    FakePublicClient.silent_result = {"error_description": "revoked"}
    path = tmp_path / "token.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ProviderError, match="revoked"):
        access_token_from_cache(path, settings())


def test_access_token_refresh_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_msal(monkeypatch)
    FakePublicClient.silent_result = None
    path = tmp_path / "token.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ProviderError, match="Could not refresh"):
        access_token_from_cache(path, settings())


def test_access_token_writes_cache_when_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_msal(monkeypatch)
    FakePublicClient.mark_changed = True
    path = tmp_path / "token.json"
    path.write_text("{}", encoding="utf-8")
    assert access_token_from_cache(path, settings()) == "silent-token"
    assert path.read_text(encoding="utf-8") == '{"cache": true}'


def test_access_token_keeps_file_when_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_msal(monkeypatch)
    path = tmp_path / "token.json"
    path.write_text("original", encoding="utf-8")
    assert access_token_from_cache(path, settings()) == "silent-token"
    assert path.read_text(encoding="utf-8") == "original"


def test_build_graph_client_sets_bearer_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(outlook_mod, "access_token_from_cache", lambda *args, **kwargs: "tok")
    client = build_graph_client(tmp_path / "token.json", settings())
    try:
        assert client.headers["Authorization"] == "Bearer tok"
    finally:
        client.close()


def test_error_value_that_is_not_an_object_uses_text(tmp_path: Path) -> None:
    graph = FakeGraph([FakeResponse(400, {"error": "nope"}, text="text-body")])
    with pytest.raises(ProviderError, match="text-body"):
        provider_for(tmp_path, graph).start_watch()


def test_error_dict_without_message_uses_code(tmp_path: Path) -> None:
    graph = FakeGraph([FakeResponse(400, {"error": {}})])
    with pytest.raises(ProviderError, match="HTTP 400"):
        provider_for(tmp_path, graph).start_watch()
