# Copyright (c) 2026 Leo Chen <leo.chen0412@outlook.com>

"""Every Python file in the project starts with the copyright header."""

from __future__ import annotations

from pathlib import Path

HEADER = "# Copyright (c) 2026 Leo Chen <leo.chen0412@outlook.com>"


def test_python_files_start_with_copyright() -> None:
    root = Path(__file__).resolve().parents[1]
    missing: list[str] = []
    for folder in ("src", "tests"):
        for path in sorted((root / folder).rglob("*.py")):
            if path.name.startswith("._"):
                continue
            first = path.read_text(encoding="utf-8").splitlines()[0]
            if first != HEADER:
                missing.append(str(path.relative_to(root)))
    assert missing == []
