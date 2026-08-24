#!/bin/bash
# Newton_Local — build llama.cpp with Apple Metal acceleration.
#
# NOTE ON THE -nostdinc++ FLAGS:
# This machine's Command Line Tools carry a stale, incomplete C++ header
# directory at /Library/Developer/CommandLineTools/usr/include/c++/v1
# (11 entries, dated 2022/2024). Clang searches that path BEFORE the SDK,
# finds it present-but-incomplete, and never falls through to the real
# libc++ headers in the SDK (183 entries). Every C++ translation unit then
# fails with "'array' file not found", "'cstddef' file not found", etc.
#
# Rather than reinstalling the Command Line Tools system-wide (which would
# touch unrelated development infrastructure on a working machine), we
# bypass the stale directory for THIS BUILD ONLY: -nostdinc++ disables the
# default C++ search path and -isystem points at the SDK's real libc++.
# Nothing outside this build tree is modified.
set -euo pipefail

LLAMA_DIR="$HOME/Newton_Local/tools/llama.cpp"
SDKROOT_DIR="$(xcrun --show-sdk-path)"
SDKCXX="${SDKROOT_DIR}/usr/include/c++/v1"

if [ ! -f "${SDKCXX}/array" ]; then
    echo "FATAL: SDK libc++ headers not found at ${SDKCXX}" >&2
    exit 1
fi

CXXFIX="-nostdinc++ -isystem ${SDKCXX}"

cd "$LLAMA_DIR"
rm -rf build

cmake -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CXX_FLAGS="${CXXFIX}" \
  -DCMAKE_OBJCXX_FLAGS="${CXXFIX}" \
  -DGGML_METAL=ON \
  -DGGML_METAL_EMBED_LIBRARY=ON \
  -DLLAMA_CURL=ON \
  -DLLAMA_BUILD_TESTS=OFF \
  -DLLAMA_BUILD_EXAMPLES=ON \
  -DLLAMA_BUILD_SERVER=ON

cmake --build build --config Release -j "$(sysctl -n hw.logicalcpu)"
echo "BUILD OK"
