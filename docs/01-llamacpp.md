# Newton_Local — llama.cpp Runtime Identity

## Why built from source

The target model (Qwen 3.8 27B, Q8_0 GGUF) declares GGUF
architecture **`qwen35`** and ships **NextN/MTP** tensors. The publisher
explicitly requires a recent llama.cpp.

Options evaluated:

| Option | Verdict |
|---|---|
| Pre-existing installation | **None existed.** No `llama-server`/`llama-cli` on PATH, no Homebrew formula installed, no source tree under `$HOME`. |
| `brew install llama.cpp` | **Rejected.** Homebrew ships versioned bottle `llama.cpp 0.2.0`, which lags upstream master. `qwen35` + NextN support landed upstream recently; a bottled release is not a safe assumption for a model repo last modified 2026-08-20. |
| **Build current master from source** | **Chosen.** Guarantees `qwen35` architecture support, NextN/MTP tensors, Metal, and current mtmd vision stack. |

## Compatibility verified BEFORE building

Confirmed directly in the cloned source tree, not assumed:

```
src/llama-arch.h:46:    LLM_ARCH_QWEN35,
src/llama-arch.h:47:    LLM_ARCH_QWEN35MOE,
src/llama-arch.cpp:41:    { LLM_ARCH_QWEN35,    "qwen35"    },
src/llama-arch.cpp:42:    { LLM_ARCH_QWEN35MOE, "qwen35moe" },
```

NextN / MTP tensor support:

```
src/llama-arch.cpp:218:  { LLM_KV_NEXTN_PREDICT_LAYERS,       "%s.nextn_predict_layers" }
src/llama-arch.cpp:534:  { LLM_TENSOR_NEXTN_PROJ_PRE,         "nextn.pre_projection"    }
src/llama-arch.cpp:536:  { LLM_TENSOR_NEXTN_EH_PROJ,          "blk.%d.nextn.eh_proj"    }
src/llama-arch.cpp:537:  { LLM_TENSOR_NEXTN_EMBED_TOKENS,     "blk.%d.nextn.embed_tokens" }
...
```

## Build identity

| Property | Value |
|---|---|
| Source | `https://github.com/ggml-org/llama.cpp` |
| Location | `/Users/ericjellerson/Newton_Local/tools/llama.cpp` |
| Commit | **`f280b26983ad0fdb705a0d9ebf0503e76f2899b0`** |
| Tag | **`b10615`** |
| Commit date | 2026-08-24T19:22:27+03:00 |
| Commit subject | `metal : per-device tuned (Q, NE) for flash-attn vec (#26570)` |
| `--version` string | `version: 0.2.0-dev (build 1, commit f280b26)` |
| ggml version | 0.21.0 |
| Compiler | AppleClang 16.0.0.16000026, Darwin arm64 |
| Build type | Release |
| Build script | `scripts/build-llamacpp.sh` |
| Build log | `logs/llamacpp-build.log` |

> The `build 1` in the `--version` string is an artefact of a `--depth 1`
> shallow clone (no tag history for the build-number script). The
> authoritative identity is the commit SHA and tag above.

## CMake configuration

```
-DCMAKE_BUILD_TYPE=Release
-DCMAKE_CXX_FLAGS="-nostdinc++ -isystem <SDK>/usr/include/c++/v1"
-DCMAKE_OBJCXX_FLAGS="-nostdinc++ -isystem <SDK>/usr/include/c++/v1"
-DGGML_METAL=ON
-DGGML_METAL_EMBED_LIBRARY=ON
-DLLAMA_CURL=ON
-DLLAMA_BUILD_TESTS=OFF
-DLLAMA_BUILD_EXAMPLES=ON
-DLLAMA_BUILD_SERVER=ON
```

`GGML_METAL_EMBED_LIBRARY=ON` is deliberate and important: it compiles the
Metal shader library **into** the binary. Without it the runtime must locate
`default.metallib` relative to the working directory, which is fragile under
launchd where the working directory is not the build tree.

The `-nostdinc++` flags work around a pre-existing broken libc++ header
directory in this machine's Command Line Tools — see
`docs/00-machine-baseline.md`.

## Build result

- Exit code **0**, **0 compile errors**.
- Backends compiled: **Metal**, **BLAS (Accelerate)**, CPU (`+dotprod +i8mm`).

Device enumeration from the built binary:

```
$ llama-server --list-devices
Available devices:
  MTL0: Apple M2 Ultra (98304 MiB, 98303 MiB free)
  BLAS: Accelerate (0 MiB, 0 MiB free)
```

96 GiB of GPU-addressable unified memory is available to Metal — ample for
27 GiB of weights plus the KV cache for a 262,144-token context.

## Binaries used by Newton_Local

| Binary | Purpose |
|---|---|
| `build/bin/llama-server` | The Qwen-A service (OpenAI-compatible HTTP API) |
| `build/bin/llama-cli` | Ad-hoc CLI inference |
| `build/bin/llama-mtmd-cli` | Multimodal CLI (vision checks) |
| `build/bin/llama-bench` | Throughput benchmarking |
| `build/bin/llama-gguf` | GGUF metadata inspection |

## Rebuilding

```bash
~/Newton_Local/scripts/build-llamacpp.sh
```

This performs a clean reconfigure and rebuild. It is safe to re-run.
