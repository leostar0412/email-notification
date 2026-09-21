# Copyright (c) 2026 Leo Chen <leo.chen0412@outlook.com>

"""Tests for email_notifier.state.CursorStore."""

from __future__ import annotations

import json
import logging

import pytest

import email_notifier.state
from email_notifier.state import CursorStore


def test_get_on_nonexistent_file_returns_none(tmp_path):
    store = CursorStore(tmp_path / "cursors.json")
    assert store.get("a@example.com") is None


def test_set_then_get_roundtrip(tmp_path):
    store = CursorStore(tmp_path / "cursors.json")
    store.set("a@example.com", "12345")
    assert store.get("a@example.com") == "12345"


def test_get_unknown_account_returns_none(tmp_path):
    store = CursorStore(tmp_path / "cursors.json")
    store.set("a@example.com", "12345")
    assert store.get("other@example.com") is None


def test_corrupt_non_json_file_treated_as_empty(tmp_path):
    path = tmp_path / "cursors.json"
    path.write_text("not json at all {", encoding="utf-8")
    store = CursorStore(path)
    assert store.get("a@example.com") is None
    # set still works and replaces the corrupt content
    store.set("a@example.com", "1")
    assert store.get("a@example.com") == "1"


def test_corrupt_file_logs_warning(tmp_path, caplog):
    path = tmp_path / "cursors.json"
    path.write_text("not json at all {", encoding="utf-8")
    store = CursorStore(path)
    with caplog.at_level(logging.WARNING, logger="email_notifier.state"):
        assert store.get("a@example.com") is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "corrupt" in warnings[0].getMessage()
    assert str(path) in warnings[0].getMessage()


def test_top_level_list_treated_as_empty(tmp_path):
    path = tmp_path / "cursors.json"
    path.write_text(json.dumps(["a", "b"]), encoding="utf-8")
    store = CursorStore(path)
    assert store.get("a@example.com") is None


def test_int_value_in_file_read_back_as_str(tmp_path):
    path = tmp_path / "cursors.json"
    path.write_text(json.dumps({"a@example.com": 42}), encoding="utf-8")
    store = CursorStore(path)
    assert store.get("a@example.com") == "42"


def test_set_creates_missing_parent_directories(tmp_path):
    path = tmp_path / "deep" / "nested" / "dirs" / "cursors.json"
    store = CursorStore(path)
    store.set("a@example.com", "7")
    assert path.exists()
    assert store.get("a@example.com") == "7"


def test_no_tmp_file_remains_after_set(tmp_path):
    path = tmp_path / "cursors.json"
    store = CursorStore(path)
    store.set("a@example.com", "1")
    assert not (tmp_path / "cursors.json.tmp").exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_set_creates_lock_file_next_to_store(tmp_path):
    path = tmp_path / "cursors.json"
    store = CursorStore(path)
    store.set("a@example.com", "1")
    # The read-modify-write is guarded by an flock on '<name>.lock' beside the store.
    assert (tmp_path / "cursors.json.lock").exists()


def test_failed_replace_cleans_tmp_and_keeps_original(tmp_path, monkeypatch):
    path = tmp_path / "cursors.json"
    store = CursorStore(path)
    store.set("a@example.com", "1")

    def broken_replace(src, dst):
        raise OSError("disk on fire")

    monkeypatch.setattr(email_notifier.state.os, "replace", broken_replace)
    with pytest.raises(OSError, match="disk on fire"):
        store.set("b@example.com", "2")
    monkeypatch.undo()
    # The unique temp file was unlinked on failure…
    assert list(tmp_path.glob("*.tmp")) == []
    # …and the original store content is untouched.
    assert json.loads(path.read_text(encoding="utf-8")) == {"a@example.com": "1"}
    assert store.get("a@example.com") == "1"
    assert store.get("b@example.com") is None


def test_overwrite_existing_account_cursor(tmp_path):
    store = CursorStore(tmp_path / "cursors.json")
    store.set("a@example.com", "1")
    store.set("a@example.com", "2")
    assert store.get("a@example.com") == "2"


def test_multiple_accounts_kept_independently(tmp_path):
    store = CursorStore(tmp_path / "cursors.json")
    store.set("a@example.com", "111")
    store.set("b@example.com", "222")
    assert store.get("a@example.com") == "111"
    assert store.get("b@example.com") == "222"
    store.set("a@example.com", "333")
    assert store.get("a@example.com") == "333"
    assert store.get("b@example.com") == "222"


def test_set_coerces_non_str_cursor_to_str(tmp_path):
    path = tmp_path / "cursors.json"
    store = CursorStore(path)
    store.set("a@example.com", 99)
    assert store.get("a@example.com") == "99"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == {"a@example.com": "99"}


def test_accepts_str_path(tmp_path):
    path = tmp_path / "cursors.json"
    store = CursorStore(str(path))
    store.set("a@example.com", "5")
    assert CursorStore(path).get("a@example.com") == "5"
