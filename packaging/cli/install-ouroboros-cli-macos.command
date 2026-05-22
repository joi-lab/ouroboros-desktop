#!/bin/sh
# Ouroboros packaged CLI installer launcher for the macOS DMG.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
INSTALLED_CLI="/Applications/Ouroboros.app/Contents/Resources/bin/install-ouroboros-cli"

case "$SCRIPT_DIR" in
    /Volumes/*|*AppTranslocation*)
        if [ -x "$INSTALLED_CLI" ]; then
            exec "$INSTALLED_CLI" "$@"
        fi
        echo "Install Ouroboros.app to /Applications before installing the CLI command."
        echo "Then rerun: $INSTALLED_CLI"
        exit 2
        ;;
esac

if [ -x "$SCRIPT_DIR/Ouroboros.app/Contents/Resources/bin/install-ouroboros-cli" ]; then
    exec "$SCRIPT_DIR/Ouroboros.app/Contents/Resources/bin/install-ouroboros-cli" "$@"
fi

if [ -x "$INSTALLED_CLI" ]; then
    exec "$INSTALLED_CLI" "$@"
fi

echo "Could not find Ouroboros.app. Install it to /Applications, then rerun this command."
exit 2
