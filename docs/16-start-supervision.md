# Start supervision — the 2026-10-12 stuck start

## 1. What happened

On 2026-10-12 the operator selected **Qwen 3.8 Flash-Next** in Newton AI
Control and clicked Start. The panel showed **Starting** for ten minutes, the
model never loaded, and **Stop could not be used** until the start gave up.

| time | event |
|---|---|
| 15:35:15 | start generation 42, Flash-Next via mlx-serve v26.9.1 |
| 15:35:15 | mlx-serve refuses at once: `[preflight] weights ~70.13 GB, available 68.98 GB`, `Insufficient memory … needs ~77.1 GB free … only 69.0 GB is available` |
| 15:35–15:45 | panel says Starting; the controller keeps polling `/health` |
| 15:45:15 | `start.timeout` after 600 s; worker stopped |
| 15:50:46 | second attempt (after freeing some memory): 72.20 GB available, same refusal |
| 16:00:46 | second `start.timeout` after 600 s |

**Root cause.** The engine's own memory guard refused the load. Other
workloads on the machine (27 days of uptime) held the memory; on 2026-09-16
the same model loaded with 94.9–106.8 GiB available. Nothing was broken in
the model, the weights, or the runtime.

**Why it looked stuck.** Four defects in our own control path turned a
one-second refusal into a ten-minute hang:

1. `wait_ready` only polled `/health`. A launchd job that had already exited
   looked exactly like a slow load, so the start waited for its whole 600 s
   timeout.
2. The engine's reason went to `logs/qwen-a/stderr.log` and nowhere else. The
   panel said only "did not become healthy within 600s".
3. Stop queued behind the start: the panel runs one operation at a time, and
   the controller lock was held by the start for its whole wait.
4. `status` (polled by the panel every 4 s) reconciled and **wrote** the
   lease without the lock. A poll that had read the lease just before the
   start wrote `STARTING/42` wrote back its stale `STOPPED/41` copy; the
   controller log shows generation 42 starting while the lease said 41.

## 2. The fixes

### 2.1 Fail fast when the engine exits (`scripts/qwenctl.py`)

`wait_ready` now also watches the slot's launchd job. Before the kickstart it
records launchd's run counter; a start fails as **exited** when launchd
reports the job `not running`, no slot process exists, and the job has
demonstrably run (the counter rose, or the job or a process was seen alive),
on two consecutive polls. The window before launchd registers the spawn
(state `not running`, counter unchanged) and a one-poll blip can therefore
never be mistaken for an exit. Measured: a refused start now ends in
**about 1.6 s** instead of 600 s. The start timeout still ends a load that
hangs.

### 2.2 The engine's reason, in plain words

The controller notes the size of the slot's launchd stderr log before the
kickstart and, on failure, reads only what was written since. A ranked
parser picks the decisive line: mlx-serve's memory refusal first (parsed into
required, weights and available figures, plus the precise `[preflight]`
figures), then the exec-slot shim's "refusing to launch", load failures, bind
failures, `error:`/`fatal` lines, and Python exceptions, with the last line as
a fallback. mlx-serve prints "GB" but divides by 1024³ (`src/scheduler.zig`),
so the figures are GiB and are shown as GiB.

With `--json`, a failed start prints a record:

```json
{"ok": false, "error": "exited", "exit_code": 8, "display_name": "Qwen 3.8 Flash-Next",
 "reason": {"kind": "insufficient_memory", "required_gib": 77.1, "available_gib": 69.0, ...},
 "log": ".../logs/qwen-a/stderr.log", "worker_stopped": true}
```

The panel turns it into: *"Not enough free memory to load Qwen 3.8
Flash-Next: it needs 77.1 GiB free and only 69.0 GiB was available. Close
other memory-heavy apps and try again, or choose a smaller model."* and shows
the engine log path underneath.

### 2.3 Stop cancels a start in progress

A lock-free **cancel marker** (`state/qwen-a.cancel`, next to the lock) lets
a stop reach a start that holds the lock:

* `qwen-a cancel` writes the marker and returns at once.
* `qwen-a terminate` writes the marker **before** waiting for the lock, so it
  no longer queues behind a load.
* A start checks the marker on every poll (and once more just before it
  spawns anything). A marker newer than the start's own beginning aborts it:
  the start stops its worker, records STOPPED, and exits **9**.
* The marker is deliberately left in place. A start that was requested
  before the stop but was still waiting for the lock sees it too and is
  refused without spawning anything. Any later start treats it as stale and
  clears it. An unreadable marker counts as stale, so it can never wedge the
  slot.
* An owned lease (a Qwen-B job) is only cancelled with `--force`, exactly as
  `terminate` already required for a BUSY lease.

In the panel, **Stop stays enabled while a start or restart is in flight**.
Pressing it writes desired=STOPPED (so the watchdog can never restart it),
drops the marker (`POST /api/model/stop` answers `202 {"operation":
"cancel"}`), shows *Stopping — Cancelling the start and unloading the
model…*, and the operation ends as *"Start cancelled; the model is
stopped."* — reported as what the operator asked for, not as a failure. The
service confirms with an idempotent `terminate`. Measured: under a second
from Stop to Stopped. While an operation runs the page now polls every
second instead of every four, so outcomes appear promptly.

### 2.4 Status polls never overwrite the record

`status` is now an **unlocked observer**: it reconciles in memory and
persists a correction only by compare-and-swap — when something actually
changed, no mutation holds the controller lock (non-blocking try), and the
lease file is still byte-identical to what it read. Otherwise it skips the
write (and logs `reconcile.persist_skipped` when the state would have moved).
An unlocked observer also never applies the destructive `terminate` orphan
policy; `qwen-a reconcile`, which may, now takes the lock like every other
mutation. Proven: 19 status polls raced a healthy start without a single
regression of its record.

### 2.5 Memory warning next to the model choice (advisory)

`scripts/newton_models.py` mirrors mlx-serve v26.9.1's load admission
exactly, verified against the engine's own unit-test vectors:

* free memory (`src/status.zig` `computeAvailableBytes`):
  `hw.memsize − (wired + compressor + max(0, anonymous − purgeable)) × page`,
  from the same host statistics `vm_stat` prints;
* requirement (`src/scheduler.zig` `loadRequirementBytes`):
  `weights + min(weights / 8, 6 GiB) + 1 GiB`;
* weights (`modelDiskBytes`): the pack's top-level `*.safetensors` files that
  `model.safetensors.index.json` names. For the Flash-Next pack that is
  75,303,252,216 bytes = 70.13 GiB, so it needs **77.13 GiB** free — the
  figures the engine printed during the incident.

The registry states the rule per model (`models.<id>.memory.admission`):
Flash-Next uses `mlx-serve-preflight`; Qwen 27B uses a `fixed` estimate of
52 GiB (llama.cpp has no preflight; ~47 GiB measured resident at 262144
context plus headroom). While both slots are stopped the panel labels each
model *fits in free memory*, *memory is tight* (under 2 GiB of margin) or
*probably won't fit right now*, and shows a warning above Start when the
selected model will probably not fit. **It never blocks Start**: the engine's
own check still decides, and if it refuses, the reason appears within
seconds. `newton-model status` prints the same verdicts.

The panel's Memory row now reads **"N GiB free for models"**: free memory as
the engine counts it. It previously showed free + inactive + speculative
pages, a number that does not predict whether a load is admitted.

## 3. Exit codes added to `qwenctl`

| code | meaning |
|---:|---|
| 8 | the engine exited while loading; the reason is in the JSON record and the controller log |
| 9 | the start was cancelled by a stop (before or during the load); the worker is stopped |

Existing codes are unchanged; a hung load still ends with **4** and the same
wording as before.

## 4. Verification

| suite | result |
|---|---|
| `tests/test_start_supervision.py` (new; fakes only) | 55 passed |
| `tests/` (all unit tests) | 131 passed |
| `scripts/test-qwenctl-logic.py` | 42 passed |
| `scripts/test-model-registry.py` | 68 passed |
| `scripts/prove-start-supervision.py` (new; real launchd, fake engine) | 40 passed |
| `scripts/prove-control-panel.sh` (live panel, model off) | 48 passed |

`prove-start-supervision.py` builds a throwaway root (a copy of the scripts,
a fixture registry, leases and logs), compiles `tests/fixtures/fake_engine.c`
— a binary with no model code that can only print the real refusal, hang, or
answer `/health` — and bootstraps one fixture LaunchAgent
(`com.newton.test.supervision.a`, port 18080; fixture panel on 18765). It
proves the fast fail, terminate and cancel during a hung load, the queued
start being refused without a spawn, a healthy start racing status polls,
and the panel flows. A guard samples the process table throughout and fails
the run if any real llama-server or mlx-serve process appears. The
operator's selection, desired state and the production slots are never
touched. `--keep-panel` leaves the fixture panel up for a manual look;
`--cleanup ROOT` removes it.

`prove-control-panel.sh` also had a defect fixed: its selection test forced
Qwen 27B at the end instead of restoring the operator's selection. It now
restores whatever was selected before it ran.

**Not run:** a real Flash-Next start. It was planned only if memory was
still tight afterwards, because then the engine refuses before loading
anything. During this work free memory moved between about 74 and 78 GiB
against the 77.13 GiB requirement, so a real start could have loaded the
whole 70 GiB model next to the other workloads. The fixture engine prints the
engine's exact refusal lines instead.

## 5. Operator notes

* If Flash-Next will not fit, the panel says so before Start and gives the
  figures. Free memory (the Memory row) must exceed 77.13 GiB. Quitting
  memory-heavy applications, or choosing Qwen 3.8 27B (needs about 52 GiB),
  are the remedies.
* `mlx-serve --skip-mem-preflight` would bypass the engine's guard. It is not
  configured, and should not be: the guard exists to prevent swapping the
  machine into the ground.
* Command line: `qwen-a cancel` aborts a start in progress; `qwen-a
  terminate` now does the same before stopping.
