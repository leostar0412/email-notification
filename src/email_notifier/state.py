"""Persistent per-account cursor storage.

A push notification only says "something changed in this mailbox" — the actual
messages are recovered by asking the provider for everything after a stored
cursor (for Gmail, a ``historyId``). This module persists that cursor per
account between pushes and process restarts, as a small JSON file.

Writes are safe against crashes (unique temp file, fsync, atomic rename) and
against concurrent writers (an exclusive file lock guards the
read-modify-write — the ``watch`` CLI and the server can run at the same
time). The lock is POSIX ``flock``, so this store is Unix-only.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class CursorStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def get(self, account: str) -> str | None:
        value = self._read().get(account)
        return None if value is None else str(value)

    def set(self, account: str, cursor: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_name(self._path.name + ".lock")
        with lock_path.open("w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            data = self._read()
            data[account] = str(cursor)
            self._write(data)

    def _read(self) -> dict[str, str]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            logger.warning(
                "Cursor store %s is corrupt; starting over (accounts resync on their next push)",
                self._path,
            )
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): str(value) for key, value in data.items()}

    def _write(self, data: dict[str, str]) -> None:
        fd, tmp_name = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self._path)
        except BaseException:
            os.unlink(tmp_name)
            raise
