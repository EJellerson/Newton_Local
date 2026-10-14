#!/bin/bash
# Newton_Local — download a Fish speech model pack at its pinned revision (docs/18).
#
#   scripts/download-fish-model.sh [model-id]       (default: config default_model)
#
# repository, revision and destination: config/fish.json (tracked; the licence
# requires attribution, so the source is public record).
#
# SINGLE COPY, PINNED, RESUMABLE (the docs/03 convention):
#   * `hf download --local-dir` writes files straight into the destination;
#     HF_HUB_CACHE points at the project-local state/hf-cache, so no second
#     copy lands in ~/.cache/huggingface.
#   * --revision pins the exact commit; a moving branch is never downloaded.
#   * HF_HUB_DISABLE_IMPLICIT_TOKEN: the repositories are public, so the
#     operator's stored Hugging Face token is never sent.
#
# DISK GATE: refuses unless free space covers the pack plus a margin. Never
# deletes anything. Moves bytes only; never loads the model.
set -euo pipefail

ROOT="${NEWTON_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CONFIG="${NEWTON_FISH_CONFIG:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/config/fish.json}"
HF="$ROOT/tools/hf-venv/bin/hf"
MARGIN_GIB=20

[ -x "$HF" ] || { echo "FATAL: $HF missing (tools/hf-venv)" >&2; exit 1; }
read -r MODEL REPO REVISION REL BYTES < <(/usr/bin/python3 - "$CONFIG" "${1:-}" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
mid = sys.argv[2] or c["default_model"]
m = c["models"][mid]
s = m["source"]
print(mid, s["hf_repo"], s["hf_revision"], m["path"], s["bytes"])
PY
)
DEST="$ROOT/$REL"

export HF_HUB_CACHE="$ROOT/state/hf-cache"
export HF_HUB_DISABLE_IMPLICIT_TOKEN=1
export HF_HUB_DISABLE_TELEMETRY=1
mkdir -p "$HF_HUB_CACHE" "$DEST"

echo "== model:     $MODEL"
echo "== repo:      $REPO"
echo "== revision:  $REVISION"
echo "== dest:      $DEST"

HAVE=$(find "$DEST" -maxdepth 1 -type f ! -name 'MANIFEST.*' -exec stat -f %z {} + 2>/dev/null | awk '{s+=$1} END{print s+0}')
NEED_GIB=$(( (BYTES - HAVE) / 1073741824 + MARGIN_GIB ))
FREE_GIB=$(df -g "$DEST" | awk 'NR==2 {print $4}')
echo "== disk:      free ${FREE_GIB} GiB, need $(( (BYTES - HAVE) / 1073741824 )) GiB + ${MARGIN_GIB} GiB margin"
if [ "$FREE_GIB" -lt "$NEED_GIB" ]; then
    echo "BLOCKED: insufficient free space. Not deleting anything." >&2
    exit 2
fi

echo "== downloading (resumable; re-run to resume)"
"$HF" download "$REPO" --revision "$REVISION" --local-dir "$DEST"

echo
echo "== result"
du -sh "$DEST"
echo "verify with: scripts/fish verify $MODEL --deep"
