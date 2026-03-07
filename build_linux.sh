#!/bin/bash
set -e

VERSION=$(cat VERSION | tr -d '[:space:]')
ARCHIVE_NAME="Ouroboros-${VERSION}-linux-$(uname -m).tar.gz"

echo "=== Building Ouroboros for Linux (v${VERSION}) ==="

if [ ! -f "python-standalone/bin/python3" ]; then
    echo "ERROR: python-standalone/ not found."
    echo "Run first:  bash scripts/download_python_standalone.sh"
    exit 1
fi

echo "--- Installing launcher dependencies ---"
pip install -q -r requirements-launcher.txt

echo "--- Installing agent dependencies into python-standalone ---"
python-standalone/bin/pip3 install -q -r requirements.txt

rm -rf build dist

echo "--- Running PyInstaller ---"
python -m PyInstaller Ouroboros.spec --clean --noconfirm

# ── Package ──────────────────────────────────────────────────────

echo ""
echo "=== Creating archive ==="
cd dist
tar -czf "$ARCHIVE_NAME" Ouroboros/
cd ..
mv "dist/$ARCHIVE_NAME" "dist/$ARCHIVE_NAME"

echo ""
echo "=== Done ==="
echo "Archive: dist/$ARCHIVE_NAME"
echo ""
echo "To run: extract and execute ./Ouroboros/Ouroboros"
