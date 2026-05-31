#!/usr/bin/env python3
"""Render the Homebrew tap formula for a tagged kutop release."""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--template", default="packaging/homebrew/kutop.rb.template")
    parser.add_argument("--out", default="Formula/kutop.rb")
    args = parser.parse_args()

    template = Path(args.template).read_text(encoding="utf-8")
    rendered = (
        template
        .replace("{{VERSION}}", args.version)
        .replace("{{TAG}}", args.tag)
        .replace("{{SHA256}}", args.sha256)
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
