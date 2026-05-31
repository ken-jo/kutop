#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST_DIR="${1:-$ROOT_DIR/dist}"
BUILD_DIR="$ROOT_DIR/build/deb"

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

PKGROOT="$BUILD_DIR/kutop_${VERSION}_all"
rm -rf "$PKGROOT"
mkdir -p \
  "$PKGROOT/DEBIAN" \
  "$PKGROOT/opt/kutop/lib/python" \
  "$PKGROOT/usr/bin" \
  "$PKGROOT/usr/share/doc/kutop"

python -m pip install \
  --disable-pip-version-check \
  --no-compile \
  --target "$PKGROOT/opt/kutop/lib/python" \
  "$WHEEL"
rm -rf "$PKGROOT/opt/kutop/lib/python/bin"

cat > "$PKGROOT/usr/bin/kutop" <<'EOF'
#!/bin/sh
export PYTHONPATH="/opt/kutop/lib/python${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m kutop "$@"
EOF
chmod 0755 "$PKGROOT/usr/bin/kutop"

cat > "$PKGROOT/usr/bin/kubetop" <<'EOF'
#!/bin/sh
export PYTHONPATH="/opt/kutop/lib/python${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m kutop "$@"
EOF
chmod 0755 "$PKGROOT/usr/bin/kubetop"

install -m 0644 "$ROOT_DIR/LICENSE" "$PKGROOT/usr/share/doc/kutop/copyright"

cat > "$PKGROOT/DEBIAN/control" <<EOF
Package: kutop
Version: $VERSION
Section: admin
Priority: optional
Architecture: all
Depends: python3 (>= 3.9), ca-certificates
Maintainer: kutop contributors
Homepage: https://github.com/ken-jo/kutop
Description: btop-style Kubernetes resource dashboard for the terminal
 kutop is a Textual-based terminal dashboard for Kubernetes resource usage.
 It installs both kutop and kubetop command aliases.
EOF

mkdir -p "$DIST_DIR"
dpkg-deb --root-owner-group --build "$PKGROOT" "$DIST_DIR/kutop_${VERSION}_all.deb"
