#!/bin/bash
# Newton_Local — build the isolated Fish runtime: tools/fish-venv (docs/18).
#
#   scripts/install-fish-runtime.sh            create or repair, idempotent
#   scripts/install-fish-runtime.sh --recreate remove tools/fish-venv first
#
# Reproducible and isolated:
#   * base interpreter: the python.org 3.12 framework build (Homebrew never
#     upgrades it); system Python and every other venv are untouched;
#   * mlx-audio from tools/fish-src/mlx-audio, refused unless its HEAD is the
#     commit pinned in config/fish.json (the commit id covers the whole tree);
#   * every dependency from config/fish-requirements.lock.txt: one wheel per
#     package, hash-checked, binary only, no dependency resolution, no pip
#     cache, no user pip configuration;
#   * writes tools/fish-venv/newton-fish-install.json, the install record that
#     fishctl and the engine report as the runtime identity.
# Never loads the model.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="${NEWTON_ROOT:-$REPO}"
CONFIG="$REPO/config/fish.json"
# cfg key.key... — a value from the runtime entry of config/fish.json.
cfg() {
    /usr/bin/python3 -c 'import json,sys
v=json.load(open(sys.argv[1]))["runtimes"]["mlx-audio-fish-omni"]
for k in sys.argv[2].split("."): v=v[k]
print(v)' "$CONFIG" "$1"
}

BASE_PY="$(cfg base_python)"
BASE_VER="$(cfg base_python_version)"
COMMIT="$(cfg source.commit)"
REMOTE="$(cfg source.repository)"
SRC="$ROOT/$(cfg source_checkout)"
VENV="$ROOT/tools/fish-venv"
PY="$VENV/bin/python3"
LOCK="$REPO/$(cfg requirements_lock)"
BUILD_LOCK="$REPO/$(cfg build_requirements_lock)"
RECORD="$ROOT/$(cfg install_record)"

die() { printf 'install-fish-runtime: %s\n' "$*" >&2; exit 1; }

echo "== base interpreter $BASE_PY"
[ -x "$BASE_PY" ] || die "missing $BASE_PY (python.org $BASE_VER framework build)"
GOT="$("$BASE_PY" -c 'import sys;print(sys.version.split()[0])')"
[ "$GOT" = "$BASE_VER" ] || die "base interpreter is $GOT, expected $BASE_VER"
echo "   ok  Python $GOT"

echo "== pinned source $SRC"
if [ ! -d "$SRC/.git" ]; then
    mkdir -p "$SRC"
    git -C "$SRC" init -q
    git -C "$SRC" remote add origin "$REMOTE"
    git -C "$SRC" fetch -q --depth 1 origin "$COMMIT"
    git -C "$SRC" checkout -q --detach FETCH_HEAD
fi
HEAD="$(git -C "$SRC" rev-parse HEAD)"
[ "$HEAD" = "$COMMIT" ] || die "checkout is at $HEAD, pinned $COMMIT"
[ -z "$(git -C "$SRC" status --porcelain --ignored)" ] || die "checkout has local modifications or build leftovers"
TREE="$(git -C "$SRC" rev-parse 'HEAD^{tree}')"
echo "   ok  HEAD $HEAD (tree $TREE), clean"

if [ "${1:-}" = "--recreate" ] && [ -d "$VENV" ]; then
    echo "== removing $VENV (--recreate)"
    rm -rf "$VENV"
fi
if [ ! -x "$PY" ]; then
    echo "== creating $VENV"
    "$BASE_PY" -m venv "$VENV"
fi

PIP=("$PY" -m pip --isolated install --no-cache-dir --disable-pip-version-check)
echo "== locked dependencies ($(grep -c '==' "$LOCK") packages, hash-checked)"
"${PIP[@]}" --require-hashes --no-deps --only-binary=:all: -r "$LOCK" | tail -1
echo "== build backend"
"${PIP[@]}" --require-hashes --no-deps --only-binary=:all: -r "$BUILD_LOCK" | tail -1
echo "== mlx-audio from the pinned checkout"
# Build from a pristine export: an in-tree build would leave build/ and
# *.egg-info in the checkout, and the checkout must stay byte-for-byte clean.
EXPORT="$(mktemp -d "${TMPDIR:-/tmp}/fish-mlx-audio.XXXXXX")"
trap 'rm -rf "$EXPORT"' EXIT
git -C "$SRC" archive --format=tar HEAD | tar -x -C "$EXPORT"
"${PIP[@]}" --no-deps --no-build-isolation --force-reinstall "$EXPORT" | tail -1
[ -z "$(git -C "$SRC" status --porcelain --ignored)" ] || die "the build modified the checkout"
"$PY" -m pip --isolated --disable-pip-version-check check

echo "== non-loading validation"
"$PY" -I - "$RECORD" "$HEAD" "$TREE" "$LOCK" "$BUILD_LOCK" "$CONFIG" <<'PY'
import hashlib, json, os, subprocess, sys, time
record, head, tree, lock, build_lock, config = sys.argv[1:7]
import mlx.core as mx
from mlx_audio.tts.models.fish_omni import Model  # noqa: F401  (import check)
from mlx_audio.codec.models.fish_s1_dac import DAC  # noqa: F401
from importlib import metadata
info = mx.device_info() if hasattr(mx, "device_info") else mx.metal.device_info()
sha = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()
ffmpeg = "/opt/homebrew/bin/ffmpeg"
ff = None
if os.path.exists(ffmpeg):
    out = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True).stdout
    ff = {"path": ffmpeg, "version": out.splitlines()[0] if out else None,
          "realpath": os.path.realpath(ffmpeg)}
rec = {
    "schema": 1,
    "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "python": sys.version.split()[0],
    "python_executable": sys.executable,
    "venv": sys.prefix,
    "mlx_audio_commit": head,
    "mlx_audio_tree": tree,
    "requirements_lock_sha256": sha(lock),
    "build_requirements_lock_sha256": sha(build_lock),
    "config_runtime": json.load(open(config))["runtimes"]["mlx-audio-fish-omni"]["source"],
    "packages": {d.metadata["Name"].lower(): d.version for d in metadata.distributions()},
    "metal": {"device_name": info.get("device_name"),
              "architecture": info.get("architecture"),
              "memory_size": info.get("memory_size"),
              "max_recommended_working_set_size": info.get("max_recommended_working_set_size"),
              "max_buffer_length": info.get("max_buffer_length")},
    "ffmpeg": ff,
}
tmp = record + ".tmp"
with open(tmp, "w") as f:
    json.dump(rec, f, indent=2, sort_keys=True)
    f.write("\n")
os.replace(tmp, record)
p = rec["packages"]
print(f"   ok  mlx {p.get('mlx')}  mlx-audio {p.get('mlx-audio')}  numpy {p.get('numpy')}  "
      f"working set {info.get('max_recommended_working_set_size', 0) / 2**30:.2f} GiB")
print(f"   ok  install record {record}")
PY
echo "INSTALLED: $VENV"
