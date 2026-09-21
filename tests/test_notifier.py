# Copyright (c) 2026 Leo Chen <leo.chen0412@outlook.com>

"""Tests for email_notifier.notifier.SlackNotifier."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from email_notifier.models import EmailMessage
from email_notifier.notifier import SlackError, SlackNotifier

WEBHOOK = "https://hooks.slack.example/services/T000/B000/XXXX"


def _make_message(**overrides) -> EmailMessage:
    fields = {
        "account": "work@example.com",
        "provider": "gmail",
        "message_id": "msg-1",
        "sender": "Alice <alice@example.com>",
        "subject": "Quarterly report",
        "snippet": "Here is the quarterly report you asked for.",
        "received_at": None,
    }
    fields.update(overrides)
    return EmailMessage(**fields)


def _capture_notifier(status_code: int = 200, body: str = "ok"):
    """Return (notifier, captured) where captured collects each request."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status_code, text=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return SlackNotifier(WEBHOOK, client=client), captured


def _sequence_notifier(status_codes: list[int]):
    """Return (notifier, captured); each call consumes the next status code."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status_codes[len(captured) - 1], text="resp")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return SlackNotifier(WEBHOOK, client=client), captured


def _sent_payload(captured: list[httpx.Request]) -> dict:
    assert len(captured) == 1
    return _payload_of(captured[0])


def _payload_of(request: httpx.Request) -> dict:
    return json.loads(request.content.decode("utf-8"))


def _blocks_of_type(payload: dict, block_type: str) -> list[dict]:
    return [block for block in payload["blocks"] if block["type"] == block_type]


def test_notify_happy_path_posts_expected_payload():
    notifier, captured = _capture_notifier()
    notifier.notify(_make_message())

    request = captured[0]
    assert str(request.url) == WEBHOOK
    assert request.method == "POST"

    payload = _sent_payload(captured)
    assert payload["text"] == "New email for work@example.com: Quarterly report"

    headers = _blocks_of_type(payload, "header")
    assert len(headers) == 1
    assert headers[0]["text"]["type"] == "plain_text"
    assert "New email" in headers[0]["text"]["text"]

    subject_block = payload["blocks"][1]
    assert subject_block["type"] == "section"
    assert subject_block["text"] == {"type": "mrkdwn", "text": "*Quarterly report*"}

    fields_block = payload["blocks"][2]
    assert fields_block["type"] == "section"
    field_texts = [field["text"] for field in fields_block["fields"]]
    assert field_texts[0] == "*From:*\nAlice &lt;alice@example.com&gt;"
    assert field_texts[1] == "*Account:*\nwork@example.com (gmail)"


def test_notify_escapes_mrkdwn_characters():
    notifier, captured = _capture_notifier()
    notifier.notify(
        _make_message(
            subject="Q&A <update>",
            sender="Bob & Co <bob@example.com>",
            snippet="1 < 2 > 0 & done",
        )
    )
    payload = _sent_payload(captured)

    assert payload["text"] == "New email for work@example.com: Q&amp;A &lt;update&gt;"
    assert payload["blocks"][1]["text"]["text"] == "*Q&amp;A &lt;update&gt;*"

    from_text = payload["blocks"][2]["fields"][0]["text"]
    assert from_text == "*From:*\nBob &amp; Co &lt;bob@example.com&gt;"

    contexts = _blocks_of_type(payload, "context")
    assert len(contexts) == 1
    assert contexts[0]["elements"][0]["text"] == "1 &lt; 2 &gt; 0 &amp; done"


def test_notify_empty_subject_uses_placeholder():
    notifier, captured = _capture_notifier()
    notifier.notify(_make_message(subject=""))
    payload = _sent_payload(captured)
    assert payload["text"] == "New email for work@example.com: (no subject)"
    assert payload["blocks"][1]["text"]["text"] == "*(no subject)*"


def test_notify_empty_sender_uses_placeholder():
    notifier, captured = _capture_notifier()
    notifier.notify(_make_message(sender=""))
    payload = _sent_payload(captured)
    assert payload["blocks"][2]["fields"][0]["text"] == "*From:*\n(unknown sender)"


def test_notify_empty_snippet_omits_context_block():
    notifier, captured = _capture_notifier()
    notifier.notify(_make_message(snippet=""))
    payload = _sent_payload(captured)
    assert _blocks_of_type(payload, "context") == []


def test_notify_long_snippet_escaped_then_clamped_to_250():
    # The snippet is escaped FIRST and clamped AFTERWARDS: the '&' at position
    # 299 becomes '&amp;' (escaped length 304 > 250), then the clamp cuts the
    # escaped text to exactly 250 characters ending in a single ellipsis.
    snippet = "x" * 299 + "&"
    notifier, captured = _capture_notifier()
    notifier.notify(_make_message(snippet=snippet, received_at=None))
    payload = _sent_payload(captured)

    contexts = _blocks_of_type(payload, "context")
    assert len(contexts) == 1
    text = contexts[0]["elements"][0]["text"]
    assert text == "x" * 249 + "…"
    assert len(text) == 250
    assert text.endswith("…")
    assert "&" not in text


def test_notify_long_subject_clamped_to_2900_in_block_and_text():
    subject = "s" * 3000
    notifier, captured = _capture_notifier()
    notifier.notify(_make_message(subject=subject))
    payload = _sent_payload(captured)

    clamped = "s" * 2899 + "…"
    assert len(clamped) == 2900

    section_text = payload["blocks"][1]["text"]["text"]
    assert section_text == f"*{clamped}*"

    assert payload["text"] == f"New email for work@example.com: {clamped}"


def test_notify_long_sender_clamped_to_1900_in_from_field():
    sender = "a" * 2000
    notifier, captured = _capture_notifier()
    notifier.notify(_make_message(sender=sender))
    payload = _sent_payload(captured)

    clamped = "a" * 1899 + "…"
    assert len(clamped) == 1900
    assert payload["blocks"][2]["fields"][0]["text"] == f"*From:*\n{clamped}"


def test_notify_received_at_adds_context_block():
    received = datetime(2026, 9, 14, 8, 30, tzinfo=UTC)
    notifier, captured = _capture_notifier()
    notifier.notify(_make_message(received_at=received))
    payload = _sent_payload(captured)

    contexts = _blocks_of_type(payload, "context")
    # snippet context + received context
    assert len(contexts) == 2
    received_text = contexts[-1]["elements"][0]["text"]
    assert received_text == "Received 2026-09-14 08:30 UTC"


def test_notify_naive_received_at_strips_empty_timezone():
    received = datetime(2026, 9, 14, 8, 30)
    notifier, captured = _capture_notifier()
    notifier.notify(_make_message(snippet="", received_at=received))
    payload = _sent_payload(captured)

    contexts = _blocks_of_type(payload, "context")
    assert len(contexts) == 1
    assert contexts[0]["elements"][0]["text"] == "Received 2026-09-14 08:30"


def test_notify_received_at_none_omits_received_block():
    notifier, captured = _capture_notifier()
    notifier.notify(_make_message(received_at=None))
    payload = _sent_payload(captured)
    for block in _blocks_of_type(payload, "context"):
        for element in block["elements"]:
            assert "Received" not in element["text"]


def test_non_200_response_raises_slack_error_with_status_and_body():
    # 5xx: no fallback attempt, the error carries the HTTP status and body.
    notifier, captured = _capture_notifier(status_code=500, body="no_service")
    with pytest.raises(SlackError) as excinfo:
        notifier.notify(_make_message())
    assert "500" in str(excinfo.value)
    assert "no_service" in str(excinfo.value)
    assert excinfo.value.status == 500
    assert len(captured) == 1


def test_send_text_non_200_error_carries_status():
    notifier, captured = _capture_notifier(status_code=404, body="no_service")
    with pytest.raises(SlackError) as excinfo:
        notifier.send_text("ping")
    assert excinfo.value.status == 404
    assert len(captured) == 1


def test_connect_error_raises_slack_error_could_not_reach():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("boom", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    notifier = SlackNotifier(WEBHOOK, client=client)
    with pytest.raises(SlackError) as excinfo:
        notifier.notify(_make_message())
    assert "Could not reach Slack" in str(excinfo.value)
    assert excinfo.value.status is None
    # A transport error must not trigger the minimal-payload fallback.
    assert attempts == 1


def test_notify_400_falls_back_to_minimal_text_payload():
    notifier, captured = _sequence_notifier([400, 200])
    notifier.notify(_make_message())  # must not raise

    assert len(captured) == 2
    first = _payload_of(captured[0])
    assert "blocks" in first
    second = _payload_of(captured[1])
    assert second == {"text": first["text"]}
    assert second == {"text": "New email for work@example.com: Quarterly report"}


def test_notify_400_fallback_also_400_propagates_after_two_posts():
    notifier, captured = _sequence_notifier([400, 400])
    with pytest.raises(SlackError) as excinfo:
        notifier.notify(_make_message())
    assert excinfo.value.status == 400
    assert len(captured) == 2
    # The failed second post was the minimal fallback payload.
    assert _payload_of(captured[1]) == {"text": "New email for work@example.com: Quarterly report"}


def test_send_text_posts_plain_text_payload():
    notifier, captured = _capture_notifier()
    notifier.send_text("hello from the test suite")
    payload = _sent_payload(captured)
    assert payload == {"text": "hello from the test suite"}


def test_default_client_created_when_none_given():
    notifier = SlackNotifier(WEBHOOK, timeout=1.5)
    try:
        assert isinstance(notifier._client, httpx.Client)
    finally:
        notifier.close()


def test_close_closes_owned_client():
    notifier = SlackNotifier(WEBHOOK)
    assert not notifier._client.is_closed
    notifier.close()
    assert notifier._client.is_closed


def test_close_does_not_close_injected_client():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    notifier = SlackNotifier(WEBHOOK, client=client)
    notifier.close()
    assert not client.is_closed
    client.close()


def test_context_manager_closes_owned_client_on_exit():
    with SlackNotifier(WEBHOOK) as notifier:
        assert isinstance(notifier, SlackNotifier)
        assert not notifier._client.is_closed
    assert notifier._client.is_closed
