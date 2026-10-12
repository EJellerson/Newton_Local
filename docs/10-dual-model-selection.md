# 10 — Dual-model selection: registry, global selection, execution slots (Commission #5)

Current identity/routing contract: see [11-runtime-identity.md](11-runtime-identity.md).
The original commission record below retains historical aliases and measurements.

**Date:** 2026-09-04 (implementation) · 2026-09-05 (live proving) · **Machine:** Mac Pro (M2 Ultra, 128 GB), macOS 14.8.5 → **26.6.2** · **Status:** implemented, no-load proven, and **live-proven for both models** (§10); Flash-Next required the macOS upgrade (§4).

```
                     GLOBAL MODEL SELECTION  (state/model-selection.json)
                                   │
        ┌──────────────────────────┴──────────────────────────┐
        │ qwen27b   Qwen 3.8 27B        llama.cpp   (default)  │  ← config/models.json
        │ flash-next Qwen 3.8 Flash-Next mlx-serve             │     (the registry)
        └──────────────────────────┬──────────────────────────┘
                                   │ one selection governs BOTH slots
                    ┌──────────────┴──────────────┐
                    ▼                             ▼
             Qwen-A (slot A)               Qwen-B (slot B)
             127.0.0.1:8080                127.0.0.1:8081
             production / Hermes           elastic lease worker
```

**Qwen-A and Qwen-B are execution slots.** The names stay (Hermes, docs,
scripts, launchd labels all use them) but they no longer imply an
implementation: whichever model is globally selected is what a slot launches.

## 1. The registry — `config/models.json`

The single source of truth for supported models. Per model: stable id,
display/long name, runtime, local path, manifest path, context length,
vision config, start timeout, expected memory, the exact launch `args`
(placeholders `{path} {host} {port} {alias} {context_length} {logdir}`),
notes and provenance (HF repo + pinned revision + lineage). Per runtime:
executable, `--version` probe, health semantics, optional `requires_macos`.
Per slot: label, host, port, alias, log dir.

`scripts/newton_models.py` is the only code that interprets it:
`load_registry` (validated), `launch_plan(slot, model)`, `check_model`,
`probe_health`, `runtime_signatures`, `read/write_selection`, `select_model`,
`exec-slot`. The lifecycle controller (`qwenctl.py`) and the control service
consume those functions; no model-specific `if` lives anywhere else.

Byte-equivalence for the proven model: `newton-model plan A --model qwen27b
--argv` equals the frozen Commission-#1 daemon argv token for token, and the
slot-B plan equals `scripts/qwen-b-argv.sh` — both are enforced by the
installers as drift checks and by the proving suite.

## 2. Global selection contract — `state/model-selection.json`

| Rule | Implementation |
|---|---|
| exactly one selection | `{"schema":1,"selected":"qwen27b","set_at","set_by"}` |
| default | registry `default` = `qwen27b`; a missing file reads as the default |
| atomic | write to `.tmp.<pid>` then `os.replace` |
| validated | ids not in the registry are rejected (exit 2 / HTTP 400) |
| fail-safe | corrupt JSON or an unknown id reads as the default **and says so** (`selection_source`); the next valid write repairs the file |
| persists | it is a file under `state/` (untracked): survives panel refresh, Hermes/Open WebUI restarts, logout/login and reboot. Persisting the *selection* never starts anything — boot still lands on model OFF (docs/09) |
| locked while occupied | `select_model` takes **both** slot controller locks, then observes both slots; refused (exit 3 / HTTP 409) if either lease is not STOPPED, either slot is observed up/loading, or any runtime process exists |
| availability-gated | refused (exit 4 / HTTP 422) unless `check_model` says installed + intact + runnable |
| consumed by A and B | `qwenctl start` / `acquire` call `launch_plan(slot)` at start time, record `lease.model` / `lease.runtime`, and refuse (exit 6) if the selection is unavailable. `restart` refuses (exit 7) if the running model differs from the selection — it never switches models |

Operator inspection: `scripts/newton-model status` (selection, source, lock
state and reasons, per-model availability, slot leases) or `GET /api/status`.

## 3. Execution slots and the launch shim

Both LaunchAgents now run `/usr/bin/python3 scripts/newton_models.py exec-slot
<A|B>` (RunAtLoad=false, KeepAlive=false, unchanged on-demand semantics). At
spawn the shim resolves the selection, re-checks availability (exit 78 if
not), logs the plan, and `execv`s the runtime, so the pid launchd tracks *is*
the model server — kill/TERM/ExitTimeOut semantics are unchanged. Everything
in docs/09 (desired state, watchdog recovery, Stop stays stopped, reboot →
OFF) is untouched.

## 4. Runtimes

| | Qwen 3.8 27B | Qwen 3.8 Flash-Next |
|---|---|---|
| runtime | llama.cpp `llama-server` (`tools/llama.cpp`, build `f280b26`) | `ddalcu/mlx-serve` **v26.9.1** (`tools/mlx-serve/current`) |
| argv | the frozen 22-token llama argv | `--model <dir> --serve --host 127.0.0.1 --port N --ctx-size 262144 --max-concurrent 1 --log-level info --log-file <logdir>/mlx-serve.log` |
| health | `GET /health` → `{"status":"ok"}`; identity `/props` | `GET /health` 200 **and** `GET /v1/models` lists the model |
| vision | F16 mmproj | bf16 vision tower inside the pack |
| memory | ~47 GiB GPU alloc (measured) | ~68 GB resident (card), 96 GB unified minimum; n-gram table mmapped from SSD |

**Flash-Next runtime status:** every Flash-capable mlx-serve release links
against macOS 26.2 (`LC_BUILD_VERSION minos 26.2`; README "Needs macOS
26.2+"). On the commission day (macOS 14.8.5) the binary failed under dyld
(`_OBJC_CLASS_$_MTLResidencySetDescriptor` missing) and the registry
correctly reported **`flash-next: UNAVAILABLE — runtime requires macOS 26.2+
but host is 14.8.5`** (not selectable, not startable). The host was upgraded
to **macOS 26.6.2 on 2026-09-05**; `install-mlx-serve.sh` then validated
(`mlx-serve 26.9.1`), `newton-model verify` reported both models AVAILABLE
without any re-download, and the live proving in §10 was executed.

The PyPI package named `mlx-serve` (raspoli, 0.1.0) is an unrelated Python
project — do not install it for this model.

## 5. Provenance

Download sources (Hugging Face repository and pinned revision) live in the
machine-local `config/model-sources.local.json`; the tracked records carry
sizes and hashes.

* **Qwen 27B** — record: `models/qwen38-27b/MANIFEST.md` + `MANIFEST.json`.
* **Flash-Next** — 113 files, 107,326,642,700 bytes, all SHA-256/blob-verified;
  record: `models/qwen38-flash-next-mlx-4bit/MANIFEST.md` + `MANIFEST.json`.

## 6. Availability / integrity checks (no inference involved)

`newton-model verify [id] [--deep]`: every manifest file present with the
exact byte size (fast path, run on every status), `--deep` re-hashes (SHA-256
for LFS files, git blob SHA-1 for small files); runtime executable present,
executable, host-OS requirement met, `--version` answers. A name in a config
file never makes a model startable.

## 7. UI and CLI

Newton AI Control (unchanged app, unchanged Start/Stop/Restart) gained a
**Model** selector in the model card: both models with human names and
runtime, the default marked, unavailable models shown greyed with the reason,
"Selected model: …", and an explanation line. While either slot is occupied
the selector is disabled and says why. Changing it POSTs
`/api/model/select` and only records the choice — nothing starts. Errors are
shown inline, never swallowed. `GET /api/models` exposes the registry view.

Since 2026-10-12 each model also carries an advisory **memory fit** while
both slots are stopped — *fits in free memory*, *memory is tight*, or
*probably won't fit right now* — computed with mlx-serve's own admission
formula (`models.<id>.memory.admission` in the registry; Flash-Next needs
77.13 GiB free). A warning appears above Start when the selected model will
probably not fit. It never blocks Start; the engine's own check decides,
and a refusal is reported within seconds with its reason (docs/16).

```
scripts/newton-model list                      # models, availability, selection
scripts/newton-model status [--json]           # + lock state, slot leases
scripts/newton-model select qwen27b            # both slots must be stopped
scripts/newton-model select flash-next         # refused while unavailable
scripts/newton-model verify [id] [--deep]
scripts/newton-model plan A|B [--model id] [--argv]
scripts/qwen-a start | terminate | restart | status | cancel   (slot A)
scripts/qwen-b acquire --owner X | release | terminate    (slot B)
```
CLI and panel read and write the same `state/model-selection.json`.

## 8. Singleton across heterogeneous runtimes

`classify_model_processes(cfg, ps, signatures)` treats any process whose
argv starts with **any registered runtime executable** (llama-server,
mlx-serve — symlink targets included) as a model process; the one carrying
this slot's `--port` is OURS, every other one is a PEER. `start`/`restart`
refuse while a peer exists (exit 5); `acquire` warns; the panel's selector
locks. Port ownership, controller locks and lease reconciliation are as in
docs/09. Proven with fixtures for every combination (llama on A + mlx on B,
mlx via symlink on a foreign port, unrelated python on a slot port).

## 9. Adding another model later

1. Provision the pack under `models/<name>/` and write `MANIFEST.json`
   (`scripts/make-model-manifest.py --model <id>` after adding the entry).
2. Add a `models.<id>` entry (and a `runtimes.<x>` entry if the runtime is
   new, with its health semantics and `requires_macos`).
3. `newton-model verify <id>`; `newton-model plan A --model <id> --argv`.
4. Extend `scripts/test-model-registry.py` fixtures; run the suite.
No lifecycle code changes are needed.

## 10. Live proving — EXECUTED 2026-09-05 (macOS 26.6.2, training finished)

All measured on this Mac through the production path (panel / `qwen-a` /
`qwen-b`); at most one model resident at any moment.

**Physical reboot (the OS upgrade)** — after login: Hermes, Open WebUI and
the control panel running (runs=1 each), both slots registered and not
running (runs=0), 8080/8081 closed, no runtime process, and
`state/model-selection.json` intact from the previous day. Control-plane
prover 48/48.

**Engine on 26.6.2** — `install-mlx-serve.sh`: `mlx-serve 26.9.1` answers
`--version`; `newton-model verify`: both models AVAILABLE (no re-download).

| | Qwen 3.8 27B (llama.cpp, slot shim) | Qwen 3.8 Flash-Next (mlx-serve) |
|---|---|---|
| panel Start → Running | **20 s** (launchd pid *is* llama-server; lease `qwen27b/llama.cpp`) | **28 s** (`load_to_ready 23.8 s`; lease `flash-next/mlx-serve`) |
| health / identity | `/health` ok; alias `qwen-a`, ctx 262144, vision | `/health` ok; `/v1/models` id `qwen38-flash-next-mlx-4bit`, ctx **262144** advertised, caps chat/tool_use/streaming/vision/reasoning/json_schema |
| memory while running | GPU alloc 47.9 GiB | GPU alloc **71.3 GiB** (+69.4), 36.6 GiB still available, pressure normal, swap 0; 77.9 GiB after a 100k-token prompt |
| basic inference | `17*23 → 391`, 0.64 s | `391`, 2.8 s (low reasoning effort, 62 tokens) |
| reasoning / coding | — | correct efficient `is_prime`, 527 tokens in 12.3 s (~43 tok/s) |
| tool calling | `get_weather {"city":"Boston"}`, finish `tool_calls` | same, finish `tool_calls` |
| vision | correct description of `assets/vision-test.png`, 3.6 s | correct, 4.4 s |
| long context | (proven in Commissions #1–2) | needle recovered exactly at **32k** (48.9 s) and **100k** tokens (134.8 s, ~743 tok/s prefill) |
| Hermes end-to-end (Open WebUI path) | `pong`, 57 s | `pong`, 27.8 s — mlx-serve accepts Hermes' `model: qwen-a` id, so Hermes needs no change |
| crash recovery (`kill -9`) | proven 2026-09-04 (21 s) | Recovering at t=1 s, watchdog attempt ~12 s, **Running at 35 s**, new pid |
| panel Restart | 8 s, new pid, overlap sampler MAX=1 (30 samples) | 27 s, new pid, MAX=1 (86 samples), lease model unchanged |
| panel Stop / reclamation | 2 s; GPU alloc 47.9 → 1.8 | 5 s; GPU alloc **71.0 → 1.7**; port closed; watchdog silent |
| Qwen-B lease on :8081 | 4.7 s, answer 144, release + terminate 0.75 s | 20.7 s, `/v1/models` lists the model, answer 144, release + terminate 1.9 s |
| singleton, other direction | A running → `qwen-b start` exit 5; B leased → `qwen-a start` exit 5 | A running → `qwen-b start` exit 5 (peer `exe=mlx-serve`); B leased → `qwen-a start` exit 5 |
| selection lock while occupied | CLI exit 3, panel 409 (A running and B leased) | CLI exit 3, panel 409 (A running and B leased) |
| idempotent start | `already running (was READY)` | same |

**Swaps (both slots stopped):** `qwen27b → flash-next → qwen27b` via the CLI
and `flash-next → qwen27b` via the panel, each accepted (200 / exit 0), the
slot-A plan flipping between `llama-server` and `mlx-serve`; no process ever
started; final selection `qwen27b` (recorded). Switching while A or B is
occupied was refused every time (see the table).

**Robustness fix found during proving:** `--version` probes spawned by the
availability check are processes whose argv starts with a runtime executable;
they briefly counted as runtime occupancy and, with a failed probe cached
for the panel's lifetime, could pin a model unavailable. Both fixed
(`classify_model_processes` ignores `--version`/`--help`; failed probes are
retried after 30 s), with tests. The panel's start ceiling was raised to
700 s to cover the registry's 600 s Flash-Next start timeout.

**Open / tuning (not blockers):** `--mtp` and `--reasoning-budget` were left
at engine defaults; Flash-Next was measured with `reasoning_effort: low`
for short answers. Both remain operator choices.
