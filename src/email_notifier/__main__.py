# Copyright (c) 2026 Leo Chen <leo.chen0412@outlook.com>

"""Allow running as ``python -m email_notifier``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
