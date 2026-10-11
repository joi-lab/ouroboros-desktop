#!/usr/bin/env bash
set -euo pipefail

VERSION="${RIPGREP_VERSION:-14.1.1}"
ROOT="ripgrep-standalone"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ARCH="$(uname -m)"
OS="$(uname -s)"
case "$OS:$ARCH" in
  Darwin:arm64) target="aarch64-apple-darwin" ;;
  Darwin:x86_64) target="x86_64-apple-darwin" ;;
  Linux:x86_64) target="x86_64-unknown-linux-musl" ;;
  Linux:aarch64|Linux:arm64) target="aarch64-unknown-linux-gnu" ;;
  *) echo "Unsupported platform for bundled ripgrep: $OS/$ARCH" >&2; exit 1 ;;
esac

asset="ripgrep-${VERSION}-${target}.tar.gz"
url="https://github.com/BurntSushi/ripgrep/releases/download/${VERSION}/${asset}"
echo "Downloading $url"
curl -fsSL "$url" -o "$TMP/$asset"
curl -fsSL "${url}.sha256" -o "$TMP/$asset.sha256"
python3 - "$TMP/$asset" "$TMP/$asset.sha256" <<'PY'
import hashlib
import pathlib
import sys

asset = pathlib.Path(sys.argv[1])
sidecar = pathlib.Path(sys.argv[2])
expected = sidecar.read_text(encoding="utf-8").split()[0].strip().lower()
actual = hashlib.sha256(asset.read_bytes()).hexdigest()
if actual != expected:
    raise SystemExit(f"SHA256 mismatch for {asset.name}: expected {expected}, got {actual}")
print(f"Verified SHA256 for {asset.name}: {actual}")
PY
tar -xzf "$TMP/$asset" -C "$TMP"
rm -rf "$ROOT"
mkdir -p "$ROOT/bin"
cp "$TMP/ripgrep-${VERSION}-${target}/rg" "$ROOT/bin/rg"
chmod +x "$ROOT/bin/rg"
"$ROOT/bin/rg" --version
