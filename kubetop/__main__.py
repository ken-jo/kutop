"""Run kutop through the legacy ``python -m kubetop`` alias."""

from __future__ import annotations

from kutop.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
