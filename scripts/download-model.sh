#!/bin/bash
# Newton_Local — download the canonical Qwen3.8-27B Q8_0 weights
# and matching F16 vision projector.
#
# SINGLE-COPY STRATEGY:
# `hf download --local-dir` on huggingface_hub >= 1.x writes files DIRECTLY
# into the target directory. It does NOT populate ~/.cache/huggingface/hub
# with a second full copy (that was the pre-0.23 symlink behaviour). This
# keeps exactly one canonical copy of the 27 GiB Q8_0 weights on disk.
# HF_HUB_CACHE is additionally redirected to a project-local scratch dir so
# that even metadata cannot land in the user's home cache.
#
# SOURCE: the Hugging Face repository and the upstream file names come from
# config/model-sources.local.json (machine-local, untracked; template:
# config/model-sources.example.json). Files are stored under the local names.
set -euo pipefail

ROOT="$HOME/Newton_Local"
SOURCES="$ROOT/config/model-sources.local.json"
DEST="$ROOT/models/qwen38-27b"
HF="$ROOT/tools/hf-venv/bin/hf"

[ -f "$SOURCES" ] || { echo "FATAL: $SOURCES missing (see config/model-sources.example.json)" >&2; exit 1; }

# source_of repo | source_of file <local-name>  — this model's download source.
source_of() {
    /usr/bin/python3 - "$SOURCES" "$@" <<'PY'
import json, sys
entry = json.load(open(sys.argv[1]))["qwen27b"]
print(entry["hf_repo"] if sys.argv[2] == "repo"
      else entry.get("files", {}).get(sys.argv[3], sys.argv[3]))
PY
}
REPO="$(source_of repo)"

export HF_HUB_CACHE="$ROOT/state/hf-cache"
mkdir -p "$HF_HUB_CACHE" "$DEST"

echo "== repo:  $REPO"
echo "== dest:  $DEST"
echo

for FILE in "Qwen3.8-27B-Q8_0.gguf" "mmproj-Qwen3.8-27B-f16.gguf"; do
    UPSTREAM="$(source_of file "$FILE")"
    echo "== downloading $FILE"
    "$HF" download "$REPO" "$UPSTREAM" --local-dir "$DEST"
    [ "$UPSTREAM" = "$FILE" ] || mv "$DEST/$UPSTREAM" "$DEST/$FILE"
    echo
done

echo "== result"
ls -la "$DEST"
