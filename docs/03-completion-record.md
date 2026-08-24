# Qwen-A Commission — Completion Record

**Date:** 2026-08-24
**Scope:** Establish Qwen-A, the first production-quality local inference
service for Newton_Local. Qwen-B, Hermes, Foreman, and all next-phase work
are explicitly out of scope and were not started.

---

## 1. Machine / environment baseline

| Property | Value |
|---|---|
| Machine | Apple Mac Pro tower |
| Chip | Apple M2 Ultra — 24 CPU cores (24 logical), 76 GPU cores |
| Unified memory | 137,438,953,472 bytes (128 GiB) |
| Metal | Metal 3; `MTL0: Apple M2 Ultra (98304 MiB)` addressable |
| macOS | 14.8.5, build 23J423, Darwin 23.6.0 (`RELEASE_ARM64_T6020`) |
| Disk free at start | 222 GiB |
| Homebrew | 5.1.15 (pre-existing, used as-is) |
| Git | 2.39.5 Apple Git-154 (pre-existing, used as-is) |
| Python | 3.12.2 (pre-existing, **not modified**) |
| Apple clang | 16.0.0 (clang-1600.0.26.6) |
| Command Line Tools | 16.2.0.0.1.1733547573 (Xcode not installed; CLT only) |
| Port 8080 | Free at baseline and re-checked defensively before every bind |
| Pre-existing inference services | **None** — no llama/qwen/ollama/mlx processes, no GGUF files, `/Library/LaunchDaemons/` empty |

**Installed by this commission:** CMake 4.4.2 (via Homebrew — hard requirement
to build llama.cpp; none was present).

**Pre-existing defect found:**
`/Library/Developer/CommandLineTools/usr/include/c++/v1/` holds a stale,
incomplete libc++ header set (11 entries dated 2022/2024, missing `array`,
`cstddef`, `mutex`, …). Clang searches it before the SDK's complete 183-header
copy, so **every C++ compile on this machine fails** with
`fatal error: 'array' file not found`. The Command Line Tools were deliberately
**not** reinstalled (that would disturb unrelated development infrastructure on
a working machine). The llama.cpp build instead passes
`-nostdinc++ -isystem "$(xcrun --show-sdk-path)/usr/include/c++/v1"`, which is
contained entirely within the build. **This defect remains and will affect any
other C++ build on this machine.**

**Untouched:** pre-existing user LaunchAgents and network listeners that
belong to unrelated software were not modified, stopped, or interfered with.

---

## 2. Project structure created

```
~/Newton_Local/
├── .gitignore
├── assets/        vision-test.png (proving input)
├── benchmarks/    proving-core.json, proving-launchd.json,
│                  proving-longctx.json, throughput.json, gpu-stats.txt
├── config/        qwen-a.env, com.newton.qwen38server.a.plist
├── docs/          00-machine-baseline, 01-llamacpp, 02-operations,
│                  03-completion-record (this file)
├── logs/          runtime + build logs (gitignored)
├── models/qwen38-27b/
│                  MANIFEST.md + the two GGUF artifacts (weights gitignored)
├── scripts/       build, download, run, install/uninstall, prove, bench, operate
├── state/         runtime state, HF cache redirect, needle prompt (gitignored)
└── tools/         llama.cpp/ and hf-venv/ (gitignored, reproducible)
```

---

## 3. Model repository and artifacts

| Field | Value |
|---|---|
| Model | Qwen 3.8 27B — Q8_0 GGUF + F16 vision projector |
| Download source | Hugging Face; repository and revision in `config/model-sources.local.json` (machine-local, untracked) |
| License | apache-2.0 |

### Downloaded artifacts and provenance hashes

**`Qwen3.8-27B-Q8_0.gguf`**
- Local path: `/Users/ericjellerson/Newton_Local/models/qwen38-27b/Qwen3.8-27B-Q8_0.gguf`
- Size: **29,047,084,416 bytes** (29.05 GB / 27.05 GiB)
- **SHA-256: `31756fca94beca71ea4b8706d6fdc896dab2a3c6376ab0c1863b98512a24f8d6`**

**`mmproj-Qwen3.8-27B-f16.gguf`**
- Local path: `/Users/ericjellerson/Newton_Local/models/qwen38-27b/mmproj-Qwen3.8-27B-f16.gguf`
- Size: **931,145,984 bytes** (0.93 GB)
- **SHA-256: `add205b7bfdb3f71f6da36b0a82aa20928dd829a920878c602628cdfbebc5288`**

Both hashes were computed locally with `shasum -a 256` and **match the
`lfs.oid` values published by the Hugging Face API exactly**. Byte lengths
also match the HF tree API exactly. These hashes are the provenance evidence
for the exact artifacts installed on this machine.

**Key GGUF metadata (main model):** `general.architecture = qwen35`,
`general.file_type = 7` (**Q8_0**), `qwen35.context_length = 262144`,
`block_count = 65`, `embedding_length = 5120`, `head_count/head_count_kv = 24/4`,
`nextn_predict_layers = 1` (MTP), `full_attention_interval = 4`,
`ssm.state_size = 128` (hybrid gated delta net).

**Projector pairing proof:** mmproj `clip.vision.projection_dim = 5120`
equals the model's `qwen35.embedding_length = 5120`; both share
`general.basename = Qwen3.8` and the same model lineage.
`clip.projector_type = qwen3vl_merger`, `general.file_type = 32` (F16).

**Single-copy guarantee:** downloaded with `hf download --local-dir`
(huggingface_hub 1.8.0 writes straight to the destination, no cache copy), with
`HF_HUB_CACHE` additionally redirected to `state/hf-cache`. That directory
finished at **0 B**; `~/.cache/huggingface/hub` measured **4.0 KB**; a
filesystem-wide search found no other `.gguf` beyond llama.cpp's own
kilobyte-sized vocabulary fixtures. Exactly one copy of the Q8_0 weights exists.

---

## 4. llama.cpp identity

| Field | Value |
|---|---|
| Source | https://github.com/ggml-org/llama.cpp |
| Location | `/Users/ericjellerson/Newton_Local/tools/llama.cpp` |
| **Commit** | **`f280b26983ad0fdb705a0d9ebf0503e76f2899b0`** |
| **Tag** | **`b10615`** |
| Commit date | 2026-08-24T19:22:27+03:00 |
| Reported version | `0.2.0-dev (build 1, commit f280b26)`; server `build_info = b1-f280b26` |
| ggml version | 0.21.0 |
| Compiler | AppleClang 16.0.0.16000026, Darwin arm64, Release |
| Backends | **Metal**, BLAS (Accelerate), CPU (`+dotprod +i8mm`) |

Built from source rather than Homebrew: Homebrew ships bottle `llama.cpp 0.2.0`,
which cannot be assumed to carry `qwen35`. Compatibility was **verified in the
source before building**, not assumed — `LLM_ARCH_QWEN35` / `"qwen35"` in
`src/llama-arch.{h,cpp}` and the full `LLM_TENSOR_NEXTN_*` / `nextn_predict_layers`
tensor set. `GGML_METAL_EMBED_LIBRARY=ON` embeds the Metal shader library in the
binary so the daemon does not depend on a working directory to find
`default.metallib`.

---

## 5. Final runtime configuration

```
llama-server
  --model        /Users/ericjellerson/Newton_Local/models/qwen38-27b/Qwen3.8-27B-Q8_0.gguf
  --mmproj       /Users/ericjellerson/Newton_Local/models/qwen38-27b/mmproj-Qwen3.8-27B-f16.gguf
  --alias        qwen-a
  --host         127.0.0.1
  --port         8080
  --ctx-size     262144
  --n-gpu-layers 999
  --flash-attn   on
  --jinja
  --parallel     1
  --cont-batching
  --metrics
```

Server-confirmed at runtime: `model_ftype = Q8_0`, `n_ctx = 262144`,
`total_slots = 1`, `modalities = {vision: true, video: true, audio: false}`,
`chat_template_caps.supports_tool_calls = true`.

---

## 6. launchd service identity

| Field | Value |
|---|---|
| Label | `com.newton.qwen38server.a` |
| Plist | `/Library/LaunchDaemons/com.newton.qwen38server.a.plist` |
| Ownership / mode | `root:wheel`, `0644` |
| Domain | system (starts at boot, no interactive login required) |
| Service user | `ericjellerson:staff` — **inference does not run as root** |
| RunAtLoad | `true` |
| KeepAlive | `true` |
| ProcessType | `Interactive` (avoids background CPU/IO throttling) |
| Logs | `logs/qwen-a/stdout.log` (empty), `logs/qwen-a/stderr.log` (the real log) |
| Plist source | `config/com.newton.qwen38server.a.plist` |

`plutil -lint` passes. All executable, model, and log paths are absolute. No
shell, no login profile, no PATH lookup; the environment is declared explicitly
(`PATH`, `HOME`, `TMPDIR`).

---

## 7. Proving results

Every test ran directly against the OpenAI-compatible HTTP API. No agent
harness, framework, or SDK was involved. Harness: `scripts/prove-qwen-a.py`.
Full transcripts in `logs/proving-*.log`; machine-readable in `benchmarks/`.

The suite was run **twice**: once against the manually launched server and
again against the launchd-managed daemon.

| Test | Manual | launchd | Evidence |
|---|---|---|---|
| API health | PASS | PASS | `/health` = ok; `/v1/models` returns 1 model; `/props` `n_ctx=262144`, `model_ftype=Q8_0` |
| Conversation | PASS | PASS | 3-turn exchange; turn 2 recalled both the name and machine stated in turn 1 |
| Reasoning | PASS | PASS | Three-switch puzzle solved via the heat/timing insight; bat-and-ball returned `BALL=0.05` (the intuitive-but-wrong answer is 0.10) |
| Coding | PASS | PASS | Generated `merge_intervals` was **executed** against 6 cases and passed all, including the touching-interval case `(1,2)+(2,3) -> (1,3)` and the empty list |
| Structured JSON | PASS | PASS | `response_format: json_schema` strict; `json.loads()` succeeded; all 5 required keys present with conforming types |
| Tool calling | PASS | PASS | `finish_reason = tool_calls`; real `tool_calls` array with id, `name=get_weather`, `arguments={"location":"Reykjavik","unit":"celsius"}` — a structured call, not prose. Round-trip verified: tool result fed back and consumed correctly |
| Vision | PASS | PASS | 5/5 ground-truth elements recovered from a 640x480 PNG — red circle, blue square, green triangle, **read the number "47"**, and read the caption "NEWTON LOCAL VISION TEST" |
| Stability | PASS | PASS | 12/12 sequential requests completed, 12/12 content-correct, `/health` still ok afterwards |
| **Long context >128K** | **PASS** | (see note) | Server-reported **`prompt_tokens = 150,170`** (threshold 131,072). Needle `PELICAN-93417-VERDIGRIS` planted at 55% depth (line 2,870 of 5,220) and recovered **verbatim**. 819.2 s end-to-end |

Long-context note: the 150K needle test was run once, against the manually
launched server. The launchd daemon runs an identical argument vector and
reports the same `n_ctx = 262144`, so the capability is configuration-identical.
It was not repeated under launchd purely to avoid a second 14-minute run.

Haystack construction: 5,220 distinct non-degenerate log lines, token count
**calibrated against the model's own tokenizer** via the server `/tokenize`
endpoint (28.740 tokens/line measured), not estimated.

**Totals: 8/8 manual + 8/8 launchd + 1/1 long-context = 17/17 passed.**

---

## 8. Performance baseline

Measured, not tuned. No optimization was attempted; the goal was a truthful
baseline.

### Load / recovery
| Metric | Value |
|---|---|
| launchd respawn after `kill -9` | **0.56 s** |
| Model load → `/health` ok (warm page cache) | **4.07 s** |
| Total recovery, crash → serving | **4.64 s** |

Load is fast because the GGUF is mmap'd and the file was warm in the page
cache; pages fault in lazily on first inference. A genuinely cold first load
pays that cost during the first generation instead.

### Throughput
| Measurement | Value |
|---|---|
| Generation, short context | **21.17 tok/s** median (21.15 / 21.17 / 21.21 across 3 x 400-token runs) |
| Generation, ~150K context | **13.1 tok/s** |
| Prompt processing, 4,386 fresh tokens | **300–342 tok/s** |
| Prompt processing, peak observed | **434.9 tok/s** (4,096-token batch) |

Prompt-processing decay with context depth (single 150K run):

| Tokens ingested | tok/s |
|---|---|
| 2,048 | 371.7 |
| 20,480 | 344.0 |
| 40,960 | 305.1 |
| 61,440 | 272.6 |
| 81,920 | 246.1 |
| 102,400 | 224.4 |
| 122,880 | 206.2 |
| 143,360 | 190.8 |
| 150,124 | 186.1 |

The decay is expected: `full_attention_interval = 4` means one layer in four is
full attention and scales quadratically with depth, while the other three are
constant-state gated-delta-net layers.

### Memory
| Metric | Value |
|---|---|
| Process RSS, idle daemon | **22.02 GiB** |
| Process RSS, under load | 22.6 – 24.99 GiB |
| GPU (Metal) allocated | **46.17 GiB** |
| GPU in-use during active inference | 45.19 GiB |
| GPU after clean shutdown | **0.16 GiB** (fully released) |
| **Swap used** | **0.00 M — throughout, including the 150K run** |
| **Compressed memory** | **0 B — compressor never engaged** |

### GPU utilization
`ioreg AGXAccelerator` during active inference: **Device Utilization 99%**,
Renderer Utilization 99%, `recoveryCount = 0`. Process CPU stayed at ~171–179%
(≈1.8 of 24 cores). Combined with 21 tok/s on a 27 GiB Q8 model — several times
what CPU-only inference would achieve — Metal is confirmed doing the work,
including under launchd.

### Benchmark confounder (recorded at the user's instruction)

**An unrelated model-training workload was running concurrently on this machine
during the proving and benchmarking run.** Consequently the *system-wide*
free-memory readings taken during that window (e.g. `PhysMem: 124G used …
2825M unused`, `48G wired`) must **not** be read as Qwen-A's standalone
requirement or as normal idle headroom. Directly attributable measurements —
process RSS, Qwen's own GPU allocation (independently confirmed by the drop to
0.16 GiB on shutdown), swap/compression, and llama.cpp's own throughput
timings — are unaffected and stand as reported.

A later clean reading, after the training workload finished, with the daemon
idle: `PhysMem: 93G used (2662M wired, 0B compressor), 34G unused`, Qwen RSS
22.02 GiB, GPU 46.17 GiB, swap 0.00 M.

---

## 9. Lifecycle and persistence verification

| Check | Result |
|---|---|
| Plist installed in system domain | `/Library/LaunchDaemons/…` `root:wheel 0644` |
| `plutil -lint` | OK |
| Process parent | **PPID = 1** — owned by launchd, not a shell |
| Process user | `ericjellerson`, not root |
| Listening socket | `127.0.0.1:8080` only |
| Non-loopback exposure | LAN probe to `10.0.0.204:8080` — **connection refused** |
| `RunAtLoad` / `KeepAlive` | `true` / `true`, confirmed by `plutil -extract` |
| **Unexpected-failure restart** | `kill -9` → launchd respawned in **0.56 s**, new PID, PPID 1, serving in 4.64 s |
| Config survived restart | `n_ctx = 262144`, `Q8_0`, vision true, alias `qwen-a` |
| Inference after restart | `17 * 23` → **391**, 21.44 tok/s |
| Full suite after restart | **8/8 PASS** |
| Daemon log cleanliness | 0 errors; only the known benign warnings |

### Physical reboot test — WAIVED BY THE USER

The commission originally required an actual machine reboot. **The user
explicitly waived the physical reboot and post-reboot inference test**,
directing that successful system-domain installation, correct
`RunAtLoad`/`KeepAlive` configuration, a successful launchd-managed lifecycle
restart, and healthy inference after that restart be treated as sufficient
persistence evidence.

**This test was waived, not failed and not forgotten.** All four substitute
conditions are satisfied and evidenced above. The residual gap is honest and
worth stating: `RunAtLoad=true` in an installed system-domain LaunchDaemon is
the mechanism that produces boot-time start, and `KeepAlive` respawn has been
demonstrated empirically — but boot-time start itself has not been observed on
this machine. Running `sudo reboot` and then
`curl -sS http://127.0.0.1:8080/health` would close it in under two minutes.

---

## 10. Warnings, deviations, and unresolved concerns

**Deviations from the commission**
1. **Physical reboot test waived by the user** (see §9). The only acceptance
   criteria not directly observed are boot-time auto-start and post-reboot
   inference.
2. **CMake installed** via Homebrew — not present at baseline, hard requirement
   to build llama.cpp.
3. **llama.cpp built from source** rather than using Homebrew's bottle, for
   `qwen35` support. Documented in `docs/01-llamacpp.md`.
4. **Isolated Python venv** at `tools/hf-venv` instead of upgrading the system
   `huggingface_hub` 0.21.4, to avoid modifying existing Python infrastructure.

**Unresolved machine concern**
- The stale libc++ header directory in the Command Line Tools is **still
  broken**. Newton_Local's build works around it, but any other C++ build on
  this machine will fail until the Command Line Tools are reinstalled.

**Benign runtime warnings** (detail in `docs/02-operations.md`)
- `model has unused tensor blk.64.* -- ignoring` (15 lines) — the NextN/MTP
  layer; llama.cpp does not use MTP speculative decoding for this architecture.
- `Qwen-VL models require at minimum 1024 image tokens` — a hint for *grounding*
  tasks. Vision proving passed 5/5 without `--image-min-tokens 1024`.
- `CORS is set to allow all origins ('*') and no API key is set` — inert while
  the bind is loopback-only. Add `--api-key` if the bind is ever widened.
- `server default port will be changed to :9931 in a future release` — the port
  is set explicitly, so this cannot silently change the service.

**Measurement caveats**
- The "medium prompt" rows in `benchmarks/throughput.json` report
  `prompt_n = 4` because llama.cpp's prefix cache absorbed the repeated filler.
  Those rows do **not** measure fresh prompt throughput; the 4,386-token and
  150K runs are the valid prompt-processing figures. Reported as measured
  rather than silently dropped.
- Concurrent training workload confounded system-wide memory readings (§8).

**Scope confirmation** — none of the following were installed, configured, or
started: Qwen-B, any port 8081 service, elastic worker controller, Hermes,
Foreman, Open WebUI, MCP servers, autonomous coding agents, Git worktree
orchestration, cloud API fallback, or multi-agent routing.

---

## 11. Git closeout

Project artifacts, documentation, and proving evidence are committed to
`main` and pushed to `origin` (`git@github.com:EJellerson/Newton_Local_Foreman.git`).

Deliberately **not** committed: model weights (`*.gguf`, 30 GB), the
`tools/llama.cpp` upstream clone (338 MB, reproducible from the pinned commit
in `docs/01-llamacpp.md`), the `tools/hf-venv` virtualenv (32 MB, recreated on
demand), runtime logs, runtime state, and any credential material. Hugging Face
tokens live in `~/.cache/huggingface`, outside this tree; `.gitignore` also
carries defence-in-depth patterns so one cannot land here by accident.

`models/` itself is tracked so that `models/qwen38-27b/MANIFEST.md`
— which carries the SHA-256 provenance for both artifacts — remains versioned
alongside the weights it describes.

The resulting commit SHA is reported in the closing engineering report.
