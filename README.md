# Newton_Local

A self-hosted AI assistant on one Apple Silicon workstation (Mac Pro, M2 Ultra,
128 GiB unified memory): local LLM inference, a tool-using agent, and a
one-click control app, with no cloud model anywhere in the loop.

```
you → Open WebUI :3000 → Hermes Agent :8642 → AI Inference :8080
        chat UI            agent + tools        llama.cpp or mlx-serve
```

Everything binds to `127.0.0.1`. No cloud LLM key exists on the machine and the
agent's provider fallback chain is empty, so if the local model is off, a chat
fails clearly instead of quietly going to the cloud. The agent's web tools do
reach the internet, so this is *local intelligence*, not an air-gapped box.

## What's worth looking at

| If you want to see… | Start here |
|---|---|
| A lifecycle controller that trusts observation over its own records | [`scripts/qwenctl.py`](scripts/qwenctl.py) |
| An incident write-up, with tests pinned to the real logs | [`docs/16-start-supervision.md`](docs/16-start-supervision.md) |
| A local control service hardened against browser attacks | [`scripts/newton_control.py`](scripts/newton_control.py) |
| Keeping an agent honest about which model is running and what it finished | [`docs/11`](docs/11-runtime-identity.md), [`docs/12`](docs/12-completion-enforcement.md), [`integrations/newton_runtime/`](integrations/newton_runtime/) |
| Capacity measurements with their confounders disclosed | [`docs/05-commission-2-completion-record.md`](docs/05-commission-2-completion-record.md) |

- **Lifecycle control.** `qwenctl.py` runs each model server as an explicit
  state machine. Before every decision it observes launchd, the process
  table, the listening socket and the HTTP health endpoint; where its record
  and reality disagree, reality wins and the correction is logged. Writers
  serialise on a file lock, unlocked observers persist only by
  compare-and-swap, a lock-free cancel marker lets Stop win over a slow Start,
  and only one large model runs at a time, across two different runtimes
  (a second needs an explicit flag reserved for experiments).
- **Incident response.** On 2026-10-12 a model start hung for ten minutes
  although the engine had refused it within a second. `docs/16` traces the
  hang to four defects in the control path, fixes them, and pins the fixes
  with tests built from the incident's own log lines: a refused start now
  fails in about 1.6 s, with the engine's reason on screen.
- **Security posture.** The control panel accepts only loopback Host headers
  (against DNS rebinding) and requires a same-origin `Origin` plus a custom
  header on every state change (against CSRF). Dangerous agent commands need
  a human approval in the terminal and are denied wherever no human can
  answer.
- **Agent integrity.** When asked which model it is, the agent answers from a
  verified identity record (the registered artifact bound to the serving
  process), not from the model's self-description. A turn is accepted as
  finished only against durable task state and tool receipts.
- **Evidence over claims.** The completion records in `docs/` cite
  transcripts and benchmark JSON in [`benchmarks/`](benchmarks/), and model
  weights are verified file by file against the hashes in their manifests.

## How this was built

Newton_Local was built by directing AI coding agents rather than by writing
the code by hand: Claude Code did most of the work, and OpenAI Codex made the
fixes of 15–16 September 2026. I wrote each stage as a *commission*: the goal,
hard constraints (for example "never load a model while a training job is
running", "at most one model in memory", "no cloud fallback") and the evidence
that would count as done. I authorised every live test on the real machine and
reviewed the evidence before accepting the work. The agents worked out the
details, wrote the code, tests and proving scripts, ran them, and wrote the
completion records.

That is why the docs speak of "Commission #1" to "#5" and of an "operator", and
why each claim in them cites a transcript or a measurement.

## Architecture

| Component | Role | Port |
|---|---|---|
| [Open WebUI](https://github.com/open-webui/open-webui) | Chat front end (single-user, loopback) | 3000 |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) v0.20.5, `localchat` profile | The agent: terminal and file tools on the host, persistent memory, sessions, web search, a Playwright browser, vision, skills, delegation | 8642 |
| Newton AI Control ([`scripts/newton_control.py`](scripts/newton_control.py)) | Dock app + web panel: Start, Stop, Restart, model selector | 8765 |
| [`scripts/qwenctl.py`](scripts/qwenctl.py) | Deterministic lifecycle controller for the model slots | — |
| [`scripts/newton_models.py`](scripts/newton_models.py) + [`config/models.json`](config/models.json) | Model registry, global selection, launch plans, integrity checks | — |
| Slot A — "AI Inference" | Production model server, alias `local-model` | 8080 |
| Slot B | Elastic worker, leased on demand, stops after a 180 s idle TTL | 8081 |

Hermes, Open WebUI and the control panel run as user LaunchAgents and come up
at login. **The model is off until you click Start, and off again after every
reboot.** While it is intentionally running, a watchdog restarts it after a
crash; an intentional Stop is never undone. Hermes is extended through a small
pinned patch ([`integrations/`](integrations/)) rather than a fork.

## Models

One global selection decides what both slots launch. It can change only
while both slots are stopped, only to a model whose weights and runtime
verify, and changing it never starts anything.

| | Qwen 3.8 27B (default) | Qwen 3.8 Flash-Next |
|---|---|---|
| Runtime | llama.cpp, built from source with Metal | [mlx-serve](https://github.com/ddalcu/mlx-serve) v26.9.1, pinned by SHA-256 |
| Format | Q8_0 GGUF + F16 vision projector | 4-bit MLX pack, 125B total / ~6B active MoE |
| Context | 262,144 tokens | 262,144 tokens |
| Start → ready (panel) | 20 s | 28 s |
| GPU allocation | 47.9 GiB | 71.3 GiB |
| Generation | ~20 tok/s | ~43 tok/s |
| Crash recovery (`kill -9`) | ~21 s | 35 s |

Long-context retrieval was proven with a 150,170-token prompt on the 27B and
32k/100k-token prompts on Flash-Next. Running two 27B instances at once buys
only ~14–18% more aggregate throughput, because the GPU is already saturated;
that is why slot B is elastic rather than permanent
([docs/05](docs/05-commission-2-completion-record.md)).

Weights are not in the repository. The download scripts fetch them from the
Hugging Face sources recorded in the machine-local
`config/model-sources.local.json` (template:
[`config/model-sources.example.json`](config/model-sources.example.json)), and
`scripts/newton-model verify <id> --deep` checks every byte against
`models/*/MANIFEST.json`.

## Running it

This is one machine's deployment, kept as a working record rather than a
packaged tool: paths in `config/` and the plists are specific to this Mac.

```bash
./scripts/hermes-localchat status      # every layer at once
open http://127.0.0.1:3000             # the chat front door
./scripts/hermes-localchat chat        # the same agent, in the terminal

./scripts/qwen-a status                # the production slot (no sudo)
./scripts/newton-model list            # registered models and availability
./scripts/newton-model select <id>     # only while both slots are stopped
./scripts/qwen-status.sh               # both slots + machine memory

./scripts/qwen-b acquire --owner my-job   # lease the elastic worker
./scripts/qwen-b release --owner my-job   # give it back; stops after the idle TTL
```

Day to day, the Newton AI Control app in the Dock does the same: Start Chat,
Start, Stop and Restart the model, and pick which model to run.

## Tests and proving

- `tests/`: 131 hermetic unit tests (`python3 -m unittest discover -s tests`).
- `scripts/test-model-registry.py` and `scripts/test-qwenctl-logic.py`:
  fixture suites for the registry and the controller; no real process starts.
- `scripts/prove-*.py` / `prove-*.sh`: live proving against the real launchd
  jobs and engines. Their outputs are the evidence in `benchmarks/`.

## Documentation

| Document | Contents |
|---|---|
| [00 Machine baseline](docs/00-machine-baseline.md) | The machine before any work, including a pre-existing toolchain defect |
| [01 llama.cpp](docs/01-llamacpp.md) | Build identity and why it is built from source |
| [02 Operations](docs/02-operations.md) | Runbook for the production slot: start, stop, logs, troubleshooting |
| [03 Completion record #1](docs/03-completion-record.md) | First service: proving, performance baseline, provenance hashes |
| [04 Elastic worker](docs/04-qwen-b-elastic-worker.md) | Slot B: lease semantics, idle TTL, orphan policy |
| [05 Completion record #2](docs/05-commission-2-completion-record.md) | Dual-instance benchmarks, memory reclamation |
| [06 Assistant runbook](docs/06-hermes-localchat.md) | Hermes `localchat`: approvals, memory, sessions, web, delegation |
| [07 Completion record #3](docs/07-commission-3-completion-record.md) | Assistant proving and security posture |
| [08 Hermes findings](docs/08-foreman-relevant-hermes-findings.md) | Research note for a planned, not yet built, work-order system ("Foreman"): which Hermes primitives it could reuse |
| [09 On-demand control](docs/09-qwen-on-demand-control.md) | Newton AI Control: boot policy, singleton, watchdog |
| [10 Dual-model selection](docs/10-dual-model-selection.md) | Registry, global selection, execution slots, live proving of both models |
| [11 Runtime identity](docs/11-runtime-identity.md) | How the agent learns which model is actually resident |
| [12 Completion enforcement](docs/12-completion-enforcement.md) | Durable task ledger and tool receipts |
| [13](docs/13-architecture-repair.md), [14](docs/14-tool-recovery.md), [15](docs/15-remediation-verification.md) | Verification of the September fixes |
| [16 Start supervision](docs/16-start-supervision.md) | The 2026-10-12 stuck start: incident, fixes, tests |
| [`models/*/MANIFEST.md`](models/) | Per-model sizes, hashes, metadata, engine requirements |

## Repository layout

```
assets/       proving inputs            models/       MANIFESTs (weights untracked)
benchmarks/   proving evidence          scripts/      build, run, prove, operate
config/       registry, env, plists     skills/       Hermes skill source
docs/         documentation             tests/        unit tests
integrations/ Hermes patch + runtime hooks
```

Not in the repository: model weights, the llama.cpp clone and Python venvs
(rebuilt by the scripts), runtime logs and state, all credentials, and
everything the assistant owns privately (`~/.hermes/`, `~/.open-webui/`).
