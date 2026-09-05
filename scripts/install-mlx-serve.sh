#!/bin/bash
# Newton_Local — install the pinned mlx-serve engine for Flash-Next (Commission #5).
#
# Reproducible: exact release tag, exact asset URL, exact SHA-256. Installs
# into tools/mlx-serve/<version>/ (untracked) and points tools/mlx-serve/current
# at it. Never loads a model: the only execution is `mlx-serve --version`.
#
# The engine is a native Zig/Metal binary. Its LC_BUILD_VERSION declares the
# minimum macOS it links against; on an older host it cannot even be loaded by
# dyld, so this installer reports that condition explicitly instead of leaving
# an ambiguous half-working install. The registry (config/models.json ->
# newton_models.py) independently re-checks executability, so Flash-Next stays
# UNAVAILABLE until the host satisfies the requirement.
#
#   ~/Newton_Local/scripts/install-mlx-serve.sh
set -euo pipefail

VERSION="v26.9.1"
URL="https://github.com/ddalcu/mlx-serve/releases/download/${VERSION}/mlx-serve-bin-macos-arm64.tar.gz"
SHA256="b9bb5178ac2dcfbfa232ffa1e6ce77e87a6408a540cbdaabb66d101d667590b0"
REQUIRES_MACOS="26.2"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="$ROOT/tools/mlx-serve"
DIST="$BASE/dist/mlx-serve-${VERSION}-macos-arm64.tar.gz"
DEST="$BASE/$VERSION"

mkdir -p "$BASE/dist"

echo "== mlx-serve $VERSION"
if [ ! -f "$DIST" ]; then
    echo "== downloading $URL"
    curl -sS -L --fail -o "$DIST.part" "$URL"
    mv "$DIST.part" "$DIST"
fi
ACTUAL="$(shasum -a 256 "$DIST" | awk '{print $1}')"
if [ "$ACTUAL" != "$SHA256" ]; then
    echo "FATAL: sha256 mismatch for $DIST" >&2
    echo "       expected $SHA256" >&2
    echo "       actual   $ACTUAL" >&2
    exit 1
fi
echo "   ok  sha256 verified"

echo "== extracting to $DEST"
rm -rf "$DEST"; mkdir -p "$DEST"
tar xzf "$DIST" -C "$DEST" --strip-components=1
ln -sfn "$VERSION" "$BASE/current"
ls -la "$DEST/mlx-serve"

echo "== host compatibility"
HOST_OS="$(sw_vers -productVersion)"
MINOS="$(otool -l "$DEST/mlx-serve" | awk '/LC_BUILD_VERSION/{f=1} f&&/minos/{print $2; exit}')"
echo "   binary minos = ${MINOS:-?}   host macOS = $HOST_OS   (engine README: needs macOS ${REQUIRES_MACOS}+)"

echo "== non-loading validation: mlx-serve --version"
if OUT="$("$DEST/mlx-serve" --version 2>&1)"; then
    echo "   ok  $OUT"
    echo "INSTALLED: $BASE/current -> $VERSION (runtime executable on this host)"
else
    echo "$OUT" | head -3 | sed 's/^/   /'
    cat <<EOF

!! =====================================================================
!! mlx-serve $VERSION is installed and hash-verified but CANNOT RUN on
!! this host: it links against macOS $MINOS (host is $HOST_OS).
!! The registry will report Flash-Next as UNAVAILABLE (runtime
!! incompatible with host OS) until macOS is upgraded to >= $REQUIRES_MACOS.
!! Nothing else to do here; re-run this installer after the upgrade.
!! =====================================================================
EOF
    exit 3
fi
