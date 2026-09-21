"""Tests for email_notifier.models dataclasses."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from email_notifier.models import EmailMessage, WatchInfo


def _make_message(**overrides):
    kwargs = {
        "account": "a@example.com",
        "provider": "gmail",
        "message_id": "m-1",
        "sender": "Sender <s@example.com>",
        "subject": "Hello",
        "snippet": "Hi there",
    }
    kwargs.update(overrides)
    return EmailMessage(**kwargs)


def test_email_message_construction():
    received = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    msg = _make_message(received_at=received)
    assert msg.account == "a@example.com"
    assert msg.provider == "gmail"
    assert msg.message_id == "m-1"
    assert msg.sender == "Sender <s@example.com>"
    assert msg.subject == "Hello"
    assert msg.snippet == "Hi there"
    assert msg.received_at == received


def test_email_message_received_at_defaults_to_none():
    msg = _make_message()
    assert msg.received_at is None


def test_email_message_is_frozen():
    msg = _make_message()
    with pytest.raises(dataclasses.FrozenInstanceError):
        msg.subject = "changed"


def test_watch_info_construction():
    expires = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
    info = WatchInfo(cursor="12345", expires_at=expires)
    assert info.cursor == "12345"
    assert info.expires_at == expires


def test_watch_info_expires_at_defaults_to_none():
    info = WatchInfo(cursor="12345")
    assert info.expires_at is None


def test_watch_info_is_frozen():
    info = WatchInfo(cursor="12345")
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.cursor = "67890"
