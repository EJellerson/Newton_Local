# 10 — Dual-model selection: registry, global selection, execution slots (Commission #5)

**Date:** 2026-09-04 · **Machine:** Mac Pro (M2 Ultra, 128 GB, macOS 14.8.5) · **Status:** implemented and proven without loading any model; Flash-Next weights provisioned and verified; **Flash-Next runtime not executable on this macOS** (see §4) — live proving deferred (§10).

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

**Flash-Next runtime status on this host:** every Flash-capable mlx-serve
release links against macOS 26.2 (`LC_BUILD_VERSION minos 26.2`; README
"Needs macOS 26.2+") and fails under dyld on macOS 14.8.5
(`_OBJC_CLASS_$_MTLResidencySetDescriptor` missing). Building from source
needs Xcode 26.2 + Metal Toolchain, equally unavailable on macOS 14. The
pinned tarball is installed and hash-verified (`scripts/install-mlx-serve.sh`
reports the incompatibility explicitly, exit 3). The registry therefore
reports **`flash-next: UNAVAILABLE — runtime requires macOS 26.2+ but host is
14.8.5`**: not selectable, not startable, no ambiguous state. After a macOS
upgrade, re-run `install-mlx-serve.sh`; the weights need no re-download.

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

```
scripts/newton-model list                      # models, availability, selection
scripts/newton-model status [--json]           # + lock state, slot leases
scripts/newton-model select qwen27b            # both slots must be stopped
scripts/newton-model select flash-next         # refused while unavailable
scripts/newton-model verify [id] [--deep]
scripts/newton-model plan A|B [--model id] [--argv]
scripts/qwen-a start | terminate | restart | status      (slot A)
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

## 10. Deferred live proving (after the training workload finishes)

None of the following was executed in Commission #5.

**Physical reboot** — reboot → Hermes, Open WebUI, control panel return;
no model starts; `state/model-selection.json` survives.

**Qwen 27B** — `newton-model select qwen27b` → A start → health →
inference → vision + tool behaviour → restart → stop → memory reclamation →
B lease → inference → release.

**Flash-Next** (requires macOS ≥ 26.2 first) — `install-mlx-serve.sh` passes
→ `newton-model verify flash-next` AVAILABLE → select → A start (watch the
engine's admission guard and the real context it pins; ~68 GB resident) →
health (`/health` + `/v1/models`) → basic inference → reasoning/coding →
tool calling → vision/multimodal → long-context characterisation → Hermes
end-to-end (Open WebUI → :8642 → :8080) → restart → stop → memory
reclamation → B lease → release. Decide `--mtp` and `--reasoning-budget`
from measurements.

**Swaps** — both stopped: 27B → Flash → 27B; attempted switching while A or
B is occupied is refused (409/exit 3).

**Singleton** — both directions under both selections (llama-vs-mlx peers).

Also still deferred from docs/09: crash recovery, restart and stop/memory
reclamation under Flash-Next.
