"""Entry point for ``python -m microphone_cli``."""

from __future__ import annotations

import sys

from microphone_cli.cli import main

if __name__ == "__main__":
    sys.exit(main())
