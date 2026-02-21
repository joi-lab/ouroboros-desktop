#!/bin/bash
set -e

echo "=== Building Ouroboros.app ==="

# Clean previous build
rm -rf build dist

# Build (use python -m to ensure correct environment)
python -m PyInstaller Ouroboros.spec --clean --noconfirm

# Ad-hoc codesign so macOS doesn't quarantine-block on first open
codesign -s - --force --deep "dist/Ouroboros.app"

echo ""
echo "=== Build complete ==="
echo "Output: dist/Ouroboros.app"
echo ""
echo "To transfer: zip it with"
echo "  cd dist && zip -r Ouroboros.zip Ouroboros.app"
echo ""
echo "On target Mac: unzip, then right-click > Open to bypass Gatekeeper."
