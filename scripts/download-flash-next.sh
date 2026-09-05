#!/bin/bash
# Newton_Local — download the accepted Flash-Next MLX-Serve pack (Commission #5).
#
#   repository + pinned revision: config/model-sources.local.json
#   (machine-local, untracked; template: config/model-sources.example.json)
#
# SINGLE-COPY, PINNED, RESUMABLE:
#   * `hf download --local-dir` (huggingface_hub >= 1.x) writes files directly
#     into the destination; incomplete files are staged inside the destination
#     (same volume) and resume on re-run. No second copy lands in ~/.cache.
#   * --revision pins the exact upstream commit so the artifact is reproducible.
#   * The internal SSD is mandatory: the engine gathers the 32 GB n-gram table
#     from disk on the hot path (model card, "Requirements").
#
# DISK GATE: refuses to start unless free space covers the full pack plus a
# safety margin. Never deletes anything to make room.
#
# This script only moves bytes. It never loads or serves the model.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCES="$ROOT/config/model-sources.local.json"
DEST="$ROOT/models/qwen38-flash-next-mlx-4bit"
HF="$ROOT/tools/hf-venv/bin/hf"
EXPECTED_BYTES=107326642700          # sum of the 112 files at the pinned revision
MARGIN_GIB=40

[ -x "$HF" ] || { echo "FATAL: $HF missing (tools/hf-venv)" >&2; exit 1; }
[ -f "$SOURCES" ] || { echo "FATAL: $SOURCES missing (see config/model-sources.example.json)" >&2; exit 1; }
read -r REPO REVISION < <(/usr/bin/python3 -c '
import json, sys
e = json.load(open(sys.argv[1]))["flash-next"]
print(e["hf_repo"], e["hf_revision"])' "$SOURCES")
[ -n "${REVISION:-}" ] || { echo "FATAL: no flash-next hf_repo/hf_revision in $SOURCES" >&2; exit 1; }

export HF_HUB_CACHE="$ROOT/state/hf-cache"
mkdir -p "$HF_HUB_CACHE" "$DEST"

echo "== repo:      $REPO"
echo "== revision:  $REVISION"
echo "== dest:      $DEST"

# Bytes still needed = pack size minus what is already complete on disk.
HAVE=$(find "$DEST" -maxdepth 1 -type f ! -name 'MANIFEST.md' -exec stat -f %z {} + 2>/dev/null | awk '{s+=$1} END{print s+0}')
NEED_GIB=$(( (EXPECTED_BYTES - HAVE) / 1073741824 + MARGIN_GIB ))
FREE_GIB=$(df -g "$DEST" | awk 'NR==2 {print $4}')
echo "== disk:      free ${FREE_GIB} GiB, need $(( (EXPECTED_BYTES - HAVE) / 1073741824 )) GiB + ${MARGIN_GIB} GiB margin = ${NEED_GIB} GiB"
if [ "$FREE_GIB" -lt "$NEED_GIB" ]; then
    echo "BLOCKED: insufficient free space on the internal SSD. Not deleting anything." >&2
    exit 2
fi

echo "== downloading (resumable; re-run to resume)"
"$HF" download "$REPO" --revision "$REVISION" --local-dir "$DEST"

echo
echo "== result"
du -sh "$DEST"
ls "$DEST" | wc -l | xargs echo "   files:"
echo "verify completeness with: $ROOT/scripts/newton-model verify flash-next"
