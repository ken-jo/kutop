#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST_DIR="${1:-$ROOT_DIR/dist}"
BUILD_DIR="$ROOT_DIR/build/homebrew"

VERSION="$(
  cd "$ROOT_DIR"
  python - <<'PY'
import re
from pathlib import Path
text = Path("pyproject.toml").read_text(encoding="utf-8")
match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
if not match:
    raise SystemExit("version not found in pyproject.toml")
print(match.group(1))
PY
)"

WHEEL="$DIST_DIR/kutop-${VERSION}-py3-none-any.whl"
if [[ ! -f "$WHEEL" ]]; then
  echo "missing wheel: $WHEEL" >&2
  exit 1
fi

PAYLOAD="$BUILD_DIR/kutop-homebrew-${VERSION}"
rm -rf "$PAYLOAD"
mkdir -p "$PAYLOAD/lib/python" "$PAYLOAD/share/doc/kutop"

python -m pip install \
  --disable-pip-version-check \
  --no-compile \
  --target "$PAYLOAD/lib/python" \
  "$WHEEL"
rm -rf "$PAYLOAD/lib/python/bin"

install -m 0644 "$ROOT_DIR/LICENSE" "$PAYLOAD/share/doc/kutop/LICENSE"
install -m 0644 "$ROOT_DIR/README.md" "$PAYLOAD/share/doc/kutop/README.md"

mkdir -p "$DIST_DIR"
tar -C "$PAYLOAD" -czf "$DIST_DIR/kutop-homebrew-${VERSION}.tar.gz" .
