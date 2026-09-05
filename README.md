# Newton_Local

Local AI infrastructure on an Apple M2 Ultra Mac Pro (128 GiB unified memory).

## The assistant

```
you → Open WebUI :3000 → Hermes Agent :8642 → Qwen-A :8080
        presentation        agency              intelligence
```

```bash
./scripts/hermes-localchat status    # every layer at once
open http://127.0.0.1:3000           # the ChatGPT-like front door
./scripts/hermes-localchat chat      # the same agent, in the terminal
```

**Hermes Agent v0.20.5** runs the `localchat` profile: terminal and file tools
on the real macOS host, persistent memory across sessions, sessions and search,
web search and a Playwright browser, vision, skills, and fresh-context
delegation. **Qwen-A supplies all of the intelligence** — 73 of 73 inference
calls during proving went to `http://127.0.0.1:8080/v1` and nowhere else; no
cloud LLM key exists on this machine and the fallback chain is empty.

Both backends run as user LaunchAgents with `RunAtLoad` and `KeepAlive`, so the
stack is already up when you log in and comes back on its own after a crash —
`./scripts/hermes-localchat autostart status`. Only the backends auto-start; the
TUI and the browser stay manual.

Destructive shell commands require an explicit human approval in the CLI.
Web tools do reach the internet when the model calls them, so **this assistant
is not air-gapped** — the split is *internet as a tool*, never *intelligence
from a cloud model*. Runbook: [docs/06-hermes-localchat.md](docs/06-hermes-localchat.md).

## Model selection (Commission #5)

Qwen-A and Qwen-B are **execution slots**; one global selection
(`state/model-selection.json`, default **Qwen 3.8 27B**) decides what both
launch. The registry `config/models.json` describes every supported model
and runtime — Qwen 3.8 27B on llama.cpp, Qwen 3.8 Flash-Next on
`ddalcu/mlx-serve`. The selection can only change while both slots are
stopped, only to a model whose weights and runtime verify, and changing it
never starts anything. Panel selector + `scripts/newton-model
list|status|select|verify|plan`. Both models are provisioned,
hash-verified and live-proven (the Flash-Next engine needs macOS 26.2+; this
Mac runs 26.6.2). Runbook:
[docs/10-dual-model-selection.md](docs/10-dual-model-selection.md).

## Newton AI Control

One Dock application operates the whole stack (Commission #4):

```
Dock: Newton AI Control  →  http://127.0.0.1:8765/
  Start Chat · Start the AI Model · Stop the AI Model · Restart the AI Model
```

Hermes, Open WebUI and the control panel are always on from login; **the 27B
model is OFF until you click Start, and OFF again after every reboot**. Stop
unloads it and keeps it unloaded (`KeepAlive=false` plus a desired-state
watchdog that never resurrects an intentional stop); while it is
*intentionally* running, that same watchdog recovers it after a crash. The
production path enforces **at most one** active Qwen instance. A chat sent
while the model is off fails clearly — there is no cloud fallback to leak to.
Runbook: [docs/09-qwen-on-demand-control.md](docs/09-qwen-on-demand-control.md).


## Current state

Two Qwen3.8-27B **Q8_0 / 262144-context** instances, both Metal-accelerated,
both OpenAI-compatible, both loopback-only, sharing **one** set of model weights.

| | **Qwen-A** — production primary | **Qwen-B** — elastic worker |
|---|---|---|
| Bind | `127.0.0.1:8080` | `127.0.0.1:8081` |
| Alias | `qwen-a` | `qwen-b` |
| Availability | **on demand** — operator Start/Stop, OFF at boot | **normally stopped**, leased on demand |
| launchd | `com.newton.qwen38server.a` (user LaunchAgent, `gui/501`) | `com.newton.qwen38server.b` (user LaunchAgent, `gui/501`) |
| Autostart | **no** (`RunAtLoad=false`, `KeepAlive=false`) | **no** (`RunAtLoad=false`, `KeepAlive=false`) |
| Control | `scripts/qwen-a` / Newton AI Control — **no sudo** | `scripts/qwen-b` — **no sudo** |
| Model | Qwen3.8-27B Q8_0 + F16 vision projector | **the same files** — no duplicate copy |
| Runtime | llama.cpp `b10615` / `f280b26`, Apple Metal | identical |

### Using the elastic worker

```bash
~/Newton_Local/scripts/qwen-b status                   # state, pid, health, idle countdown
~/Newton_Local/scripts/qwen-b acquire --owner my-job   # start it (or reuse a loaded one)
~/Newton_Local/scripts/qwen-b release --owner my-job   # give it back; stops after a 180 s idle TTL
~/Newton_Local/scripts/qwen-b terminate                # stop it now
~/Newton_Local/scripts/qwen-status.sh                  # both services + machine memory
```

Cold start is ~6 s; re-acquiring inside the idle TTL is ~0.5 s and does not
reload the model. Stopping returns ~46 GiB of Metal allocation to the machine.

Lifecycle is owned by `scripts/qwenctl.py`, ordinary deterministic code. The
model decides nothing about when workers start or stop.

### Measured capacity (Apple M2 Ultra, 128 GiB)

| | A alone | A + B concurrent | A after B stopped |
|---|---:|---:|---:|
| A generation | 19.9 tok/s | 11.7 tok/s | 19.9 tok/s |
| B generation | — | 11.0 tok/s | — |
| Aggregate | 19.9 tok/s | 22.7 tok/s | 19.9 tok/s |
| Metal allocation | 47 GiB | 93 GiB | 47 GiB |
| Swap / pressure | 0 / normal | 0 / normal | 0 / normal |

Both instances fit and run correctly at full 262144 context. The constraint is
**GPU throughput, not memory** — the GPU is already saturated by one instance, so
a second buys a separate context and ~14–18% more aggregate throughput, not a
second full-speed model. That is precisely why B is elastic rather than
persistent. Full numbers, caveats and measurement contamination:
`docs/05-commission-2-completion-record.md`.

Not yet built, and deliberately out of scope so far: **Foreman** (governed
commissioned-implementation workflow), MCP servers, automatic Qwen-B routing
from the assistant, and any third Qwen instance. Hermes and Open WebUI arrived
in Commission #3; what Hermes already gives a future Foreman — and what it does
not — is written up in
[docs/08-foreman-relevant-hermes-findings.md](docs/08-foreman-relevant-hermes-findings.md).

## Quick start

```bash
# The primary (no sudo; the same verbs sit behind the Newton AI Control panel)
./scripts/qwen-a status
curl -sS http://127.0.0.1:8080/health

# Both services + machine memory
./scripts/qwen-status.sh

# Ask the primary something
./scripts/qwen-a-infer.sh "What is 17 * 23? Reply with just the number."

# Rent the elastic worker for a job, then give it back
./scripts/qwen-b acquire --owner my-job
curl -sS http://127.0.0.1:8081/v1/chat/completions \
     -H 'Content-Type: application/json' \
     -d '{"model":"qwen-b","messages":[{"role":"user","content":"hello"}],"max_tokens":512}'
./scripts/qwen-b release --owner my-job

# Watch the logs
tail -f logs/qwen-a/stderr.log
./scripts/qwen-b logs -n 40
```

## Documentation

| Document | Contents |
|---|---|
| [docs/00-machine-baseline.md](docs/00-machine-baseline.md) | Machine state before any work; the pre-existing Command Line Tools defect |
| [docs/01-llamacpp.md](docs/01-llamacpp.md) | llama.cpp build identity, why source over Homebrew, `qwen35` verification |
| [docs/02-operations.md](docs/02-operations.md) | **Operations runbook** — start/stop/restart/status/logs, flag rationale, troubleshooting |
| [docs/03-completion-record.md](docs/03-completion-record.md) | Commission #1 record: proving results, performance baseline, provenance hashes, deviations |
| [docs/04-qwen-b-elastic-worker.md](docs/04-qwen-b-elastic-worker.md) | **Qwen-B runbook** — launchd design, lifecycle states, lease semantics, idle TTL, orphan policy, operator commands |
| [docs/05-commission-2-completion-record.md](docs/05-commission-2-completion-record.md) | Commission #2 record: dual-instance benchmarks, memory comparison, reclamation, controller proving, deviations |
| [models/qwen38-27b/MANIFEST.md](models/qwen38-27b/MANIFEST.md) | Model provenance: SHA-256, sizes, GGUF metadata, single-copy guarantee |
| [docs/06-hermes-localchat.md](docs/06-hermes-localchat.md) | **Assistant runbook** — architecture, Qwen wiring, approvals, memory, sessions, web, delegation, skills, the two front doors, operator commands |
| [docs/07-commission-3-completion-record.md](docs/07-commission-3-completion-record.md) | Commission #3 record: Hermes identity, proving results, security posture, deviations |
| [docs/08-foreman-relevant-hermes-findings.md](docs/08-foreman-relevant-hermes-findings.md) | Research note: which Hermes primitives a future Foreman can reuse, and what it must still build |
| [docs/09-qwen-on-demand-control.md](docs/09-qwen-on-demand-control.md) | **Newton AI Control runbook** — on-demand Qwen-A, boot policy, singleton enforcement, watchdog semantics, the control panel and app, deferred live tests |
| [docs/10-dual-model-selection.md](docs/10-dual-model-selection.md) | **Dual-model runbook** — model registry, global selection contract, execution slots + launch shim, runtimes, Flash-Next provenance and host requirement, UI/CLI, heterogeneous singleton, deferred live proving |
| [models/qwen38-flash-next-mlx-4bit/MANIFEST.md](models/qwen38-flash-next-mlx-4bit/MANIFEST.md) | Flash-Next record: architecture, file inventory, hash verification, engine requirement |

## What is not in this repository

Model weights (~30 GB), the `tools/llama.cpp` upstream clone, the
`tools/hf-venv` virtualenv, runtime logs, runtime state, all credential
material, and everything the assistant privately owns — `~/.hermes/` (its
install, config, memory, sessions and delegation transcripts) and
`~/.open-webui/` (conversation data) both live outside this tree. The weights are reproducible from the manifest; the toolchain is
reproducible from the pinned commit via `scripts/build-llamacpp.sh`.

## Layout

```
assets/      proving inputs          logs/      runtime logs (untracked)
benchmarks/  proving evidence        models/    weights (untracked) + MANIFEST
config/      runtime env + plists    scripts/   build, run, prove, operate
docs/        documentation           skills/    Hermes skill source
state/       lease + lock (untracked)  tools/   llama.cpp, hf-venv (untracked)

Key scripts
  qwenctl.py             instance-parametric lifecycle controller (A and B)
  qwen-a / qwen-b        operator entry points for each instance
  newton_control.py      Newton AI Control service (127.0.0.1:8765)
  install-launchagent-a.sh / install-control.sh / build-control-app.sh /
  pin-dock-app.py        on-demand Qwen-A + control panel + Dock app setup
  qwen-status.sh         A and B side by side, plus machine memory
  hermes-localchat       assistant lifecycle: chat, status, api, ui, memory,
                         sessions, logs, recover
  install-hermes-localchat.sh / install-openwebui.sh / install-hermes-skill.sh
                         reproducible setup of the assistant stack
  snapshot-machine.py    the common memory/GPU measurement primitive
  prove-*.py / prove-*.sh   proving suites (parity, concurrency, lifecycle,
                            reconciliation, cycles, reclamation)
  bench-dual.py / bench-capacity.sh   overlap-aware capacity benchmarks
```
