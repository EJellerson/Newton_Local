# Qwen-A — Operations Runbook

The single reference for running and troubleshooting Qwen-A. A future agent
should not need to rediscover anything about this machine to operate it.

## Identity

| Field | Value |
|---|---|
| Service name | Qwen-A |
| launchd label | `com.newton.qwen38server.a` |
| Installed plist | `/Library/LaunchDaemons/com.newton.qwen38server.a.plist` (root:wheel, 0644) |
| Plist source of truth | `~/Newton_Local/config/com.newton.qwen38server.a.plist` |
| Service user | `ericjellerson:staff` — **not root** |
| Bind | `127.0.0.1:8080` (loopback only) |
| Model | Qwen3.8-27B, **Q8_0** |
| Context | **262144** |
| Acceleration | Apple Metal (M2 Ultra, 76 GPU cores) |
| API | OpenAI-compatible (`/v1/...`) |

## Canonical paths

```
Project root   /Users/ericjellerson/Newton_Local
Model          /Users/ericjellerson/Newton_Local/models/qwen38-27b/Qwen3.8-27B-Q8_0.gguf
mmproj         /Users/ericjellerson/Newton_Local/models/qwen38-27b/mmproj-Qwen3.8-27B-f16.gguf
Server binary  /Users/ericjellerson/Newton_Local/tools/llama.cpp/build/bin/llama-server
Runtime config /Users/ericjellerson/Newton_Local/config/qwen-a.env
Daemon stdout  /Users/ericjellerson/Newton_Local/logs/qwen-a/stdout.log   (empty; llama.cpp logs to stderr)
Daemon stderr  /Users/ericjellerson/Newton_Local/logs/qwen-a/stderr.log   (the real log)
Manual log     /Users/ericjellerson/Newton_Local/logs/qwen-a/manual.log
```

## Directory structure

```
~/Newton_Local/
├── assets/       proving inputs (vision test image)
├── benchmarks/   proving evidence + throughput measurements (committed)
├── config/       qwen-a.env, the LaunchDaemon plist source
├── docs/         this documentation
├── logs/         runtime logs (gitignored)
├── models/
│   └── qwen38-27b/
│       ├── MANIFEST.md              provenance: SHA-256, sizes, GGUF metadata
│       ├── Qwen3.8-27B-Q8_0.gguf          (gitignored)
│       └── mmproj-Qwen3.8-27B-f16.gguf    (gitignored)
├── scripts/      build, download, run, install, prove, benchmark, operate
├── state/        runtime state, HF cache redirect, needle prompt (gitignored)
└── tools/
    ├── llama.cpp/   upstream source + build (gitignored, reproducible)
    └── hf-venv/     isolated HF CLI venv (gitignored, reproducible)
```

## Operator commands

### Status
```bash
~/Newton_Local/scripts/qwen-a-status.sh
```
Or directly:
```bash
sudo launchctl print system/com.newton.qwen38server.a
```

### Start
```bash
sudo launchctl kickstart -p system/com.newton.qwen38server.a
```
(If it was booted out entirely, re-bootstrap:
`sudo launchctl bootstrap system /Library/LaunchDaemons/com.newton.qwen38server.a.plist`)

### Stop
```bash
sudo launchctl bootout system/com.newton.qwen38server.a
```
> Note: `KeepAlive=true` means simply killing the process will **not** stop the
> service — launchd restarts it within about a second. Use `bootout` to stop it
> for real.

### Restart
```bash
sudo launchctl kickstart -k system/com.newton.qwen38server.a
```

### Logs
```bash
tail -f ~/Newton_Local/logs/qwen-a/stderr.log
```

### API health / model check
```bash
curl -sS http://127.0.0.1:8080/health
~/Newton_Local/scripts/qwen-a-health.sh
```

### Simple inference test
```bash
~/Newton_Local/scripts/qwen-a-infer.sh "What is 17 * 23? Reply with just the number."
```

Raw equivalent:
```bash
curl -sS http://127.0.0.1:8080/v1/chat/completions -H "Content-Type: application/json" -d '{"model":"qwen-a","messages":[{"role":"user","content":"Say hello."}],"max_tokens":256}'
```

### Full proving suite
```bash
python3 ~/Newton_Local/scripts/prove-qwen-a.py --image ~/Newton_Local/assets/vision-test.png --out /tmp/prove.json
```

### Rebuild llama.cpp
```bash
~/Newton_Local/scripts/build-llamacpp.sh
```

## Runtime configuration

The LaunchDaemon and `scripts/run-qwen-a.sh` use an identical argument vector:

```
llama-server
  --model     .../Qwen3.8-27B-Q8_0.gguf
  --mmproj    .../mmproj-Qwen3.8-27B-f16.gguf
  --alias     qwen-a
  --host      127.0.0.1
  --port      8080
  --ctx-size  262144
  --n-gpu-layers 999
  --flash-attn on
  --jinja
  --parallel  1
  --cont-batching
  --metrics
```

Rationale for each non-obvious flag:

| Flag | Why |
|---|---|
| `--n-gpu-layers 999` | Offload every layer to Metal. |
| `--flash-attn on` | Required to keep the KV cache tractable at 262144 context. |
| `--jinja` | Loads the model's own Jinja chat template. Without it, tool calling and the reasoning/`<think>` split do not work. |
| `--parallel 1` | One inference slot, per commission. The full 262144 context belongs to a single slot. More slots would divide it. |
| `--metrics` | Exposes `/metrics` for future observability. |

Deliberately **not** used: `--mlock` (macOS unified memory + Metal residency
already keeps weights resident; mlock adds limit-tuning risk under launchd for
no measured benefit), speculative decoding, and draft models. The commission
called for the boring, reliable baseline.

## Why a LaunchDaemon and not a LaunchAgent

A LaunchAgent only runs after a GUI login. The requirement is start-at-boot
with no interactive login, which requires a system-domain LaunchDaemon. To
avoid running inference as root, the plist sets `UserName`/`GroupName`, so
launchd starts the service at boot in the system domain but drops the process
to `ericjellerson:staff`. This is the least-privileged configuration that
still satisfies the boot requirement.

## Known warnings (all benign)

| Warning | Meaning |
|---|---|
| `model has unused tensor blk.64.* -- ignoring` (15 lines) | Block 64 is the NextN/MTP layer (`nextn_predict_layers = 1`). llama.cpp loads the model without MTP speculative decoding, so those tensors go unused. Harmless. |
| `Qwen-VL models require at minimum 1024 image tokens ... try --image-min-tokens 1024` | A hint for *grounding* tasks (bounding boxes / spatial coordinates). Our vision proving passed 5/5 without it. Add the flag if precise grounding is ever needed. |
| `CORS is set to allow all origins ('*') and no API key is set` | Inert here: the server binds only to `127.0.0.1`, so no cross-origin caller can reach it from another host. If the bind is ever widened, add `--api-key`. |
| `server default port will be changed to :9931 in a future release` | Upstream notice. The port is set explicitly to 8080, so this cannot silently change us. |
| `chat template supports preserving reasoning, consider --reasoning-preserve` | Optional; would keep reasoning traces across turns. Not enabled — default keeps prompts smaller. |

## Troubleshooting

**Port 8080 busy on start** — `scripts/run-qwen-a.sh` and the installer both
check first. Find the holder with `lsof -nP -iTCP:8080 -sTCP:LISTEN`.

**Service will not stay stopped** — that is `KeepAlive=true` doing its job.
Use `sudo launchctl bootout system/com.newton.qwen38server.a`.

**C++ build failures on this machine** — the Command Line Tools have a stale
libc++ header directory. See `docs/00-machine-baseline.md`; the build script
already works around it.

**Slow first request after boot** — the model is mmap'd, so pages fault in on
first use. Steady state is reached after one full-length generation.
