#!/bin/bash
set -e

# Downloads the official, notarized Node.js LTS runtime for macOS (arm64/x86_64)
# and Linux (x86_64/aarch64), verifies it against the published SHASUMS256, and
# prunes it to just `bin/node`. Run from repo root:
#   bash scripts/download_node_standalone.sh
#
# Why bundle node: skill payloads can declare runtime=node, and the preflight
# validator runs `node --check`. A user's Homebrew node gets SIGKILLed by macOS
# code-signing enforcement when launched from the packaged app; the official
# Node.js build is Developer-ID signed + notarized, so it runs cleanly once the
# app's signing pass re-signs it under the hardened runtime (entitlements.plist
# grants allow-jit / allow-unsigned-executable-memory / disable-library-validation).

NODE_VERSION="v24.16.0"   # Node.js LTS ("Krypton"); keep current with nodejs.org LTS
DEST="node-standalone"

OS=$(uname -s)
ARCH=$(uname -m)

if [ "$OS" = "Darwin" ]; then
    if [ "$ARCH" = "arm64" ]; then
        NODE_ARCH="arm64"
    elif [ "$ARCH" = "x86_64" ]; then
        NODE_ARCH="x64"
    else
        echo "Unsupported macOS architecture: $ARCH"
        exit 1
    fi
    NODE_PLATFORM="darwin-${NODE_ARCH}"
elif [ "$OS" = "Linux" ]; then
    if [ "$ARCH" = "x86_64" ]; then
        NODE_ARCH="x64"
    elif [ "$ARCH" = "aarch64" ]; then
        NODE_ARCH="arm64"
    else
        echo "Unsupported Linux architecture: $ARCH"
        exit 1
    fi
    NODE_PLATFORM="linux-${NODE_ARCH}"
else
    echo "Unsupported OS: $OS"
    exit 1
fi

NAME="node-${NODE_VERSION}-${NODE_PLATFORM}"
FILENAME="${NAME}.tar.gz"
BASE_URL="https://nodejs.org/dist/${NODE_VERSION}"
URL="${BASE_URL}/${FILENAME}"
SHASUMS_URL="${BASE_URL}/SHASUMS256.txt"

echo "=== Downloading Node.js ${NODE_VERSION} for ${NODE_PLATFORM} ==="
echo "URL: ${URL}"

rm -rf "$DEST" _node_tmp
mkdir -p _node_tmp

curl -L --fail --progress-bar "$URL" -o "_node_tmp/${FILENAME}"

echo "--- Verifying SHASUMS256 ---"
EXPECTED="$(curl -sL --fail "$SHASUMS_URL" | grep " ${FILENAME}\$" | awk '{print $1}')"
if [ -z "$EXPECTED" ]; then
    echo "ERROR: could not find ${FILENAME} in ${SHASUMS_URL}"
    exit 1
fi
if command -v shasum >/dev/null 2>&1; then
    ACTUAL="$(shasum -a 256 "_node_tmp/${FILENAME}" | awk '{print $1}')"
else
    ACTUAL="$(sha256sum "_node_tmp/${FILENAME}" | awk '{print $1}')"
fi
if [ "$EXPECTED" != "$ACTUAL" ]; then
    echo "ERROR: SHASUMS256 mismatch for ${FILENAME}"
    echo "  expected: $EXPECTED"
    echo "  actual:   $ACTUAL"
    exit 1
fi
echo "Checksum OK: $ACTUAL"

tar xz -C _node_tmp -f "_node_tmp/${FILENAME}"

# Keep only bin/node — drop npm/npx/corepack, headers, shared docs, and the
# ~50MB lib/node_modules tree. The preflight validator and script runner only
# need the `node` binary itself.
mkdir -p "${DEST}/bin"
cp "_node_tmp/${NAME}/bin/node" "${DEST}/bin/node"
chmod +x "${DEST}/bin/node"
if [ -f "_node_tmp/${NAME}/LICENSE" ]; then
    cp "_node_tmp/${NAME}/LICENSE" "${DEST}/LICENSE"
fi
rm -rf _node_tmp

echo ""
echo "=== Done ==="
echo "Node: ${DEST}/bin/node"
"${DEST}/bin/node" --version
