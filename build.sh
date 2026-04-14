#!/bin/bash
set -e

SIGN_IDENTITY="Developer ID Application: Ian Mironov (WHY6PAKA5V)"
NOTARYTOOL_PROFILE="ouroboros-notarize"
ENTITLEMENTS="entitlements.plist"
SIGN_MODE="${OUROBOROS_SIGN:-1}"

APP_PATH="dist/Ouroboros.app"
DMG_NAME="Ouroboros-$(cat VERSION | tr -d '[:space:]').dmg"
DMG_PATH="dist/$DMG_NAME"

echo "=== Building Ouroboros.app ==="

if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

if [ ! -f "python-standalone/bin/python3" ]; then
    echo "ERROR: python-standalone/ not found."
    echo "Run first: bash scripts/download_python_standalone.sh"
    exit 1
fi

echo "--- Installing launcher dependencies ---"
uv pip install --system --python python3 -q ".[desktop,build]"

echo "--- Installing agent dependencies into python-standalone ---"
uv pip install --system --python python-standalone/bin/python3 -q -r pyproject.toml

echo "--- Normalizing python-standalone symlinks for PyInstaller ---"
python3 - <<'PY'
import pathlib
import shutil

root = pathlib.Path("python-standalone")
replaced = 0

for path in sorted(root.rglob("*")):
    if not path.is_symlink():
        continue
    target = path.resolve()
    path.unlink()
    if target.is_dir():
        shutil.copytree(target, path)
    else:
        shutil.copy2(target, path)
    replaced += 1

print(f"Replaced {replaced} symlinks in python-standalone")
PY

rm -rf build dist

echo "--- Running PyInstaller ---"
python3 -m PyInstaller Ouroboros.spec --clean --noconfirm

if [ "$SIGN_MODE" != "0" ]; then
    echo ""
    echo "=== Signing Ouroboros.app ==="

    echo "--- Finding and signing all Mach-O binaries ---"
    find "$APP_PATH" -type f | while read -r f; do
        if file "$f" | grep -q "Mach-O"; then
            codesign -s "$SIGN_IDENTITY" --timestamp --force --options runtime \
                --entitlements "$ENTITLEMENTS" "$f" 2>&1 || true
        fi
    done
    echo "Signed embedded binaries"

    echo "--- Signing the app bundle ---"
    codesign -s "$SIGN_IDENTITY" --timestamp --force --options runtime \
        --entitlements "$ENTITLEMENTS" "$APP_PATH"

    echo "--- Verifying signature ---"
    codesign -dvv "$APP_PATH"
    codesign --verify --strict "$APP_PATH"
    echo "Signature OK"
else
    echo ""
    echo "=== Skipping signing (OUROBOROS_SIGN=0) ==="
fi

echo ""
echo "=== Creating DMG ==="
hdiutil create -volname Ouroboros -srcfolder "$APP_PATH" -ov -format UDZO "$DMG_PATH"

if [ "$SIGN_MODE" != "0" ]; then
    codesign -s "$SIGN_IDENTITY" --timestamp "$DMG_PATH"
fi

echo ""
echo "=== Done ==="
if [ "$SIGN_MODE" != "0" ]; then
    echo "Signed app: $APP_PATH"
    echo "Signed DMG: $DMG_PATH"
else
    echo "Unsigned app: $APP_PATH"
    echo "Unsigned DMG: $DMG_PATH"
fi
echo "(Not notarized — users need right-click → Open on first launch)"
