# 09 — Qwen on demand: Newton AI Control (Commission #4)

**Date:** 2026-09-04 · **Machine:** Mac Pro (M2 Ultra, 128 GB) · **Status:** implemented; live model tests deferred (see §8)

Commission #4 reverses the Commission-#1 availability policy. This Mac now
behaves like a normal workstation: the Hermes/chat harness is always ready,
but the 27B model consumes **zero** memory until explicitly started from one
Dock application, and every reboot lands on Qwen = OFF.

```
ALWAYS ON (login)                       ON DEMAND (operator click)
  Hermes gateway     127.0.0.1:8642       Qwen-A  127.0.0.1:8080  (production)
  Open WebUI         127.0.0.1:3000       Qwen-B  127.0.0.1:8081  (maintenance)
  Newton AI Control  127.0.0.1:8765
```

## 1. What changed

| | Before (Comm. #1–3) | After (Comm. #4) |
|---|---|---|
| Qwen-A launchd | root LaunchDaemon, system domain, RunAtLoad+KeepAlive (always on from boot) | user LaunchAgent `gui/<uid>`, RunAtLoad=false, KeepAlive=false (on demand) |
| Qwen-A control | sudo required | no sudo: `scripts/qwen-a` / Newton AI Control |
| Boot/login | model auto-loads | model OFF until explicitly started |
| Crash recovery | launchd KeepAlive | desired-state watchdog in the control service (§4) |
| Operator UX | Terminal | `Newton AI Control.app` in the Dock → local panel |
| Qwen-B | unchanged | unchanged (elastic worker, lease protocol, 180 s TTL reaper) |

The old system-domain rationale — inference availability at boot with nobody
logged in (docs/02 §"Why a LaunchDaemon") — was explicitly retired by
operator policy, which is what makes the no-sudo gui-domain model (already
proven by Qwen-B, docs/04) the correct architecture for A as well.

**Migration:** the legacy root daemon was removed by the operator on
2026-09-04 with `sudo scripts/uninstall-launchdaemon.sh` (the only sudo step
in the whole design; `install-launchagent-a.sh` and the panel nag until it is
gone). Nothing root-owned remains in the stack.

## 2. One controller, two instances

`scripts/qwenctl.py` (the deterministic Commission-#2 lifecycle controller)
is now instance-parametric via `--config`:

* `config/qwen-b.env` — unchanged legacy `QWEN_B_*` keys; Qwen-B behaviour,
  wrapper (`scripts/qwen-b`), lease protocol and reaper are untouched.
* `config/qwen-a-controller.env` — generic `CTL_*` keys; inherits the frozen
  runtime identity from `qwen-a.env`; **`CTL_IDLE_TTL=0` disables the idle
  TTL** (production runs until explicitly stopped; `reap` is a hard no-op).

New production verbs (wrapper: `scripts/qwen-a`):

* `start` — ownerless ensure-running; **refuses (exit 5) while any peer
  llama-server exists**; idempotent when already running.
* `restart` — stop → confirm process+port gone → start, under ONE controller
  lock, so overlapping instances are impossible.
* `terminate` — stop; idempotent when stopped (this is the panel's Stop).

## 3. Singleton enforcement (`active Qwen instances <= 1`)

Layered, all deterministic:

1. **Peer detection** — `classify_llama_processes()` splits every process
   launched from the shared `llama-server` binary into OURS vs PEER by argv
   (`--port`/`--alias`); `start`/`restart` refuse while a peer exists
   (`--allow-peer` exists solely for deliberate dual-instance experiments).
2. **Controller lock** — every mutation serialises on an flock; `restart`
   holds it across stop+start.
3. **Port ownership** — start refuses when 8080 is foreign-held; launchd
   itself cannot double-start a labelled job; llama-server cannot double-bind.
4. **Duplicate detection** — reconciliation flags/refuses on >1 matching
   process (Commission-#2 machinery).
5. **Panel serialisation** — the control service runs one operation at a
   time (409 otherwise) and its buttons are idempotent.
6. `acquire` (Qwen-B dev/bench path) keeps its historical dual capability but
   now logs and warns loudly when a peer is up. It is not reachable from the
   panel or from Hermes (the skill forbids the assistant using it).

## 4. Newton AI Control service

`scripts/newton_control.py` + `scripts/control-panel.html`, installed by
`scripts/install-control.sh` as LaunchAgent `com.newton.control`
(RunAtLoad+KeepAlive, `127.0.0.1:8765` only).

* **API:** `GET /` panel · `GET /api/status` · `GET /chat` (302 → Open WebUI)
  · `POST /api/model/{start,stop,restart}`.
* **Desired state** (`state/qwen-a-desired.json`) records RUNNING/STOPPED
  **plus the kernel boot time**. A record from a previous boot reads as
  STOPPED, so a reboot always lands on Qwen=OFF regardless of shutdown state
  — while a mere control-service restart preserves intent.
* **Crash recovery:** while desired=RUNNING (same boot), a watchdog restarts
  a dead model — up to 3 attempts with 0/60/180 s backoff, then `Unhealthy`.
  Stop writes desired=STOPPED *first*, so recovery can never resurrect an
  intentional stop; with KeepAlive=false, neither can launchd.
* **UI states:** Stopped / Starting / Running / Stopping / Recovering /
  Unhealthy, plus Hermes/Open WebUI/memory health, a Qwen-B-active banner,
  and (until migration was done) a legacy-daemon banner.
* **Hardening:** loopback bind; Host-header allow-list; mutations need the
  `X-Newton-Control` header and same-origin Origin; the Hermes bearer key is
  read only server-side for health probes and never leaves the machine.
* **Fail-clearly:** chat with the model off returns a Hermes error
  (`finish_reason: "error"`, connection refused after bounded retries);
  no cloud key exists anywhere in the stack to fall back to (docs/06).

## 5. The application

`/Applications/Newton AI Control.app` — built by
`scripts/build-control-app.sh` from the operator-supplied artwork
(`Newton-AI-Control.png`, 1254×1254): a minimal ad-hoc-signed launcher
(`LSUIElement`) whose only act is opening `http://127.0.0.1:8765/`. The
artwork is first masked into the Big Sur icon grid by `scripts/round-icon.py`
(1024 canvas, 824 rounded tile, transparent corners/margins — full-bleed
squares render hard-edged in the Dock), then rendered to a full 10-size
`.icns`. Pinned to the Dock idempotently by
`scripts/pin-dock-app.py` (add-only; no other tiles touched; it is the only
Newton tile). There is deliberately **no separate chat app** — the panel's
**Start Chat** opens Open WebUI.

## 6. Boot / login semantics (proven roster)

| launchd job | RunAtLoad | KeepAlive | at login |
|---|---|---|---|
| `ai.hermes.gateway-localchat` | true | true | starts |
| `com.newton.openwebui` | true | true | starts |
| `com.newton.control` | true | true | starts |
| `com.newton.qwen38server.a` | **false** | **false** | registered, OFF |
| `com.newton.qwen38server.b` | false | false | registered, OFF |
| `com.newton.qwen38server.b-reaper` | true (30 s timer) | false | harmless no-op |

## 7. Verification (all with the model OFF)

* `scripts/test-qwenctl-logic.py` — 41 unit checks: peer classification,
  generic+legacy config keys, TTL-disabled reap no-op, **real execution of
  the start/restart refusal branches with kickstart stubbed**, reboot-resets-
  desire, UI state mapping.
* `scripts/prove-control-panel.sh` — 37 live checks: on-demand launchd
  semantics for A and B, Stopped truthfulness, double-Stop idempotence
  spawning nothing, CSRF/Host guards, chat redirect, Hermes/WebUI health and
  independence, loopback-only binds, auto-start roster, and "no llama-server
  before/during/after".
* The double-stop prover check exposed and fixed a real race: concurrent
  lease writers shared one tmp filename (`write_lease` now uses per-process
  tmp names; atomic last-writer-wins).

## 8. Deferred — requires operator authorisation (training workload active)

Commission #4 deliberately **never started Qwen**. Still unproven and left
open, with the minimal follow-up procedure:

1. `scripts/qwen-a start` (or panel **Start**) → expect READY/Running,
   `/health` 200 on 8080, panel shows pid+RSS. *(actual start)*
2. Chat once in Open WebUI → real local inference end-to-end.
3. `scripts/qwen-b acquire --owner smoke` while A runs → observe the loud
   peer warning; then `scripts/qwen-a start` from another shell → **must
   refuse, exit 5** *(live singleton proof)*; `qwen-b release --owner smoke`.
4. `kill -9 <qwen-a pid>` → panel shows Recovering, model returns within
   ~watchdog period + load time *(crash recovery)*.
5. Panel **Stop** → process gone, port closed, RSS reclaimed (compare
   `scripts/qwen-status.sh` memory before/after); watchdog stays silent.
6. Panel **Restart** → exactly one replacement instance, no overlap.
7. Reboot with the model running → after login: Hermes/WebUI/panel up,
   **Qwen OFF** *(the boot-policy invariant, end-to-end)*.

## 9. Operator quick reference

```
Dock: Newton AI Control  →  http://127.0.0.1:8765/
  Start Chat | Start the AI Model | Stop the AI Model | Restart the AI Model

Terminal equivalents:
  scripts/qwen-a status|start|terminate|restart
  scripts/hermes-localchat status
  scripts/prove-control-panel.sh
```
