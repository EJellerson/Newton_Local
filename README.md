# Newton_Local

Local AI infrastructure on an Apple M2 Ultra Mac Pro (128 GiB unified memory).

## Current state

**Qwen-A** is the one service established so far: a persistent, Metal-accelerated,
OpenAI-compatible inference server on `127.0.0.1:8080`, managed by system launchd.

| | |
|---|---|
| Model | Qwen3.8-27B **Q8_0** (+ matching F16 vision projector) |
| Context | 262144 |
| Runtime | llama.cpp `b10615` / `f280b26`, Apple Metal |
| Bind | `127.0.0.1:8080` — loopback only, never LAN or public |
| launchd label | `com.newton.qwen38server.a` |
| Service user | `ericjellerson` (not root) |

Not yet built, and deliberately out of scope so far: Qwen-B (`:8081`), the
elastic worker controller, Hermes, and Foreman.

## Quick start

```bash
# Is it up?
curl -sS http://127.0.0.1:8080/health

# Full status
./scripts/qwen-a-status.sh

# Ask it something
./scripts/qwen-a-infer.sh "What is 17 * 23? Reply with just the number."

# Watch the log
tail -f logs/qwen-a/stderr.log
```

## Documentation

| Document | Contents |
|---|---|
| [docs/00-machine-baseline.md](docs/00-machine-baseline.md) | Machine state before any work; the pre-existing Command Line Tools defect |
| [docs/01-llamacpp.md](docs/01-llamacpp.md) | llama.cpp build identity, why source over Homebrew, `qwen35` verification |
| [docs/02-operations.md](docs/02-operations.md) | **Operations runbook** — start/stop/restart/status/logs, flag rationale, troubleshooting |
| [docs/03-completion-record.md](docs/03-completion-record.md) | Full commission record: proving results, performance baseline, provenance hashes, deviations |
| [models/qwen38-27b/MANIFEST.md](models/qwen38-27b/MANIFEST.md) | Model provenance: SHA-256, sizes, GGUF metadata, single-copy guarantee |

## What is not in this repository

Model weights (~30 GB), the `tools/llama.cpp` upstream clone, the
`tools/hf-venv` virtualenv, runtime logs, runtime state, and all credential
material. The weights are reproducible from the manifest; the toolchain is
reproducible from the pinned commit via `scripts/build-llamacpp.sh`.

## Layout

```
assets/      proving inputs          logs/      runtime logs (untracked)
benchmarks/  proving evidence        models/    weights (untracked) + MANIFEST
config/      runtime env + plist     scripts/   build, run, prove, operate
docs/        documentation           state/     runtime state (untracked)
                                     tools/     llama.cpp, hf-venv (untracked)
```
