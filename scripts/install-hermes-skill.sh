#!/usr/bin/env bash
# Install this repository's Hermes skills into the localchat profile.
# Skills are copied (not symlinked) because Hermes rewrites its own skill
# index; a symlink into the repo would let the agent edit tracked files.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$HOME/.hermes/profiles/localchat/skills/newton"
mkdir -p "$DEST"
cat > "$DEST/DESCRIPTION.md" <<'EOF'
---
description: Newton_Local — operating the local Qwen inference services and the Hermes assistant stack on this Mac
---
EOF
for d in "$REPO"/skills/*/; do
  name="$(basename "$d")"
  rm -rf "${DEST:?}/$name"
  cp -R "$d" "$DEST/$name"
  printf 'installed skill: %s -> %s/%s\n' "$name" "$DEST" "$name"
done
