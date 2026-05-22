#!/bin/bash
set -e

# Signing identity: explicit SIGN_IDENTITY, then Developer ID auto-detect, else fail later.
if [ -z "${SIGN_IDENTITY:-}" ]; then
    SIGN_IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null \
        | grep -E '\"Developer ID Application' \
        | head -1 \
        | sed -E 's/^.*\"([^\"]+)\".*$/\1/')"
    if [ -n "${SIGN_IDENTITY:-}" ]; then
        echo "Auto-detected SIGN_IDENTITY from keychain: $SIGN_IDENTITY"
    fi
fi
ENTITLEMENTS="entitlements.plist"
SIGN_MODE="${OUROBOROS_SIGN:-1}"
MANAGED_SOURCE_BRANCH="${OUROBOROS_MANAGED_SOURCE_BRANCH:-ouroboros}"

APP_PATH="dist/Ouroboros.app"
DMG_NAME="Ouroboros-$(cat VERSION | tr -d '[:space:]').dmg"
DMG_PATH="dist/$DMG_NAME"

echo "=== Building Ouroboros.app ==="

if [ ! -f "python-standalone/bin/python3" ]; then
    echo "ERROR: python-standalone/ not found."
    echo "Run first: bash scripts/download_python_standalone.sh"
    exit 1
fi

echo "--- Installing launcher dependencies ---"
pip install -q -r requirements-launcher.txt

echo "--- Installing agent dependencies into python-standalone ---"
python-standalone/bin/pip3 install -q -r requirements.txt

echo "--- Installing Chromium for browser tools (bundled into python-standalone) ---"
# Full Chromium app bundle breaks nested-bundle codesign on arm64 runners.
PLAYWRIGHT_BROWSERS_PATH=0 python-standalone/bin/python3 -m playwright install --only-shell chromium

echo "--- Normalizing python-standalone symlinks for PyInstaller ---"
python3 - <<'PY'
import pathlib
import shutil

root = pathlib.Path("python-standalone")
replaced = 0
skipped = 0


def _should_skip_symlink(path: pathlib.Path) -> bool:
    # Preserve Playwright app/framework symlinks; flattening breaks codesign.
    parts = path.parts
    return (
        ".local-browsers" in parts
        or any(part.endswith(".app") or part.endswith(".framework") for part in parts)
    )

for path in sorted(root.rglob("*")):
    if not path.is_symlink():
        continue
    if _should_skip_symlink(path):
        skipped += 1
        continue
    target = path.resolve()
    path.unlink()
    if target.is_dir():
        shutil.copytree(target, path)
    else:
        shutil.copy2(target, path)
    replaced += 1

print(
    f"Replaced {replaced} symlinks in python-standalone "
    f"(skipped {skipped} inside bundled browser bundles)"
)
PY

echo "--- Building embedded managed repo bundle ---"
python3 scripts/build_repo_bundle.py --source-branch "$MANAGED_SOURCE_BRANCH"

rm -rf build dist

echo "--- Running PyInstaller ---"
python3 -m PyInstaller Ouroboros.spec --clean --noconfirm

echo "--- Installing packaged CLI wrappers ---"
CLI_BIN_DIR="$APP_PATH/Contents/Resources/bin"
mkdir -p "$CLI_BIN_DIR"
cp packaging/cli/ouroboros "$CLI_BIN_DIR/ouroboros"
cp packaging/cli/install-ouroboros-cli "$CLI_BIN_DIR/install-ouroboros-cli"
chmod +x "$CLI_BIN_DIR/ouroboros" "$CLI_BIN_DIR/install-ouroboros-cli"

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
rm -f "$DMG_PATH"
DMG_STAGE_DIR="dist/dmg-stage"
rm -rf "$DMG_STAGE_DIR"
mkdir -p "$DMG_STAGE_DIR"
cp -R "$APP_PATH" "$DMG_STAGE_DIR/Ouroboros.app"
cp packaging/cli/install-ouroboros-cli-macos.command "$DMG_STAGE_DIR/Install CLI.command"
chmod +x "$DMG_STAGE_DIR/Install CLI.command"
for attempt in 1 2 3; do
    if hdiutil create -volname Ouroboros -srcfolder "$DMG_STAGE_DIR" -ov -format UDZO "$DMG_PATH"; then
        break
    fi
    if [ "$attempt" -eq 3 ]; then
        echo "ERROR: hdiutil create failed after $attempt attempts."
        exit 1
    fi
    echo "WARNING: hdiutil create failed (attempt $attempt/3); waiting for disk image helpers to settle."
    hdiutil detach "/Volumes/Ouroboros" -force >/dev/null 2>&1 || true
    pkill -f diskimages-helper >/dev/null 2>&1 || true
    rm -f "$DMG_PATH"
    sleep 5
done

if [ "$SIGN_MODE" != "0" ]; then
    codesign -s "$SIGN_IDENTITY" --timestamp "$DMG_PATH"
fi

# Optional notarization only after signing and complete Apple credentials.
# Outcome enum keeps final summary honest: success/staple_failed/submit_failed/unconfigured.
NOTARIZE_OUTCOME="unconfigured"
if [ "$SIGN_MODE" != "0" ] \
        && [ -n "${APPLE_ID:-}" ] \
        && [ -n "${APPLE_TEAM_ID:-}" ] \
        && [ -n "${APPLE_APP_SPECIFIC_PASSWORD:-}" ]; then
    echo ""
    echo "=== Notarizing DMG (Apple ID: $APPLE_ID) ==="
    # Submit failures warn, not abort; signed DMG still ships with clear logs.
    if xcrun notarytool submit "$DMG_PATH" \
            --apple-id "$APPLE_ID" \
            --team-id "$APPLE_TEAM_ID" \
            --password "$APPLE_APP_SPECIFIC_PASSWORD" \
            --wait; then
        echo "--- Stapling notarization ticket ---"
        # Stapler can fail after successful notarization; Gatekeeper can fetch online.
        if xcrun stapler staple "$DMG_PATH"; then
            NOTARIZE_OUTCOME="success"
        else
            NOTARIZE_OUTCOME="staple_failed"
            echo "WARNING: stapler staple failed — DMG is notarized but ticket not embedded; receivers may briefly need right-click → Open until Apple's ticket propagates."
        fi
    else
        NOTARIZE_OUTCOME="submit_failed"
        echo "WARNING: notarytool submit failed — DMG is signed but not notarized; verify APPLE_ID / APPLE_TEAM_ID / APPLE_APP_SPECIFIC_PASSWORD are correct or check the notarytool log above."
    fi
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
case "$NOTARIZE_OUTCOME" in
    success)
        echo "(Notarized + stapled — no right-click → Open required on first launch)"
        ;;
    staple_failed)
        echo "(Notarized but ticket not stapled — Gatekeeper will fetch the ticket online; receivers need internet on first launch)"
        ;;
    submit_failed)
        echo "(Signed but notarytool submit failed — DMG was not accepted by Apple; check the WARNING above for details)"
        ;;
    unconfigured)
        if [ "$SIGN_MODE" != "0" ]; then
            echo "(Signed but not notarized — set APPLE_ID / APPLE_TEAM_ID / APPLE_APP_SPECIFIC_PASSWORD to enable notarization)"
        else
            echo "(Not notarized — users need right-click → Open on first launch)"
        fi
        ;;
    *)
        # Surface future enum drift instead of omitting the summary.
        echo "(Unknown notarization outcome: '$NOTARIZE_OUTCOME' — please report; likely a missing case arm in build.sh)"
        ;;
esac
