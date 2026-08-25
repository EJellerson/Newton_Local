# Qwen-B — Elastic Worker Architecture and Runbook

Qwen-B is the **elastic worker**: a second full Qwen3.8-27B Q8_0 / 262144-context
instance that is *normally not running*, is started on demand by deterministic
infrastructure code, and is stopped again after an idle grace period so its
memory returns to the machine.

It is deliberately **not** a second persistent service. Qwen-A on `:8080` remains
the always-available primary; Qwen-B on `:8081` is capacity you rent.

---

## 1. Identity

| Field | Qwen-A (primary) | Qwen-B (elastic worker) |
|---|---|---|
| Role | persistent, always available | on demand, normally stopped |
| Bind | `127.0.0.1:8080` | `127.0.0.1:8081` |
| Alias | `qwen-a` | `qwen-b` |
| launchd label | `com.newton.qwen38server.a` | `com.newton.qwen38server.b` |
| launchd domain | `system` (root LaunchDaemon) | **`gui/501` (user LaunchAgent)** |
| Installed plist | `/Library/LaunchDaemons/com.newton.qwen38server.a.plist` | `~/Library/LaunchAgents/com.newton.qwen38server.b.plist` |
| Plist source of truth | `config/com.newton.qwen38server.a.plist` | `config/com.newton.qwen38server.b.plist` |
| RunAtLoad | `true` | **`false`** |
| KeepAlive | `true` | **`false`** |
| Requires sudo to control | yes | **no** |
| Quant / context | Q8_0 / 262144 | Q8_0 / 262144 (identical) |

Commission #2 did not modify Qwen-A's plist, env file, launchd registration,
port, model, context, or service identity.

### Why a user LaunchAgent and not a system LaunchDaemon

The controller must be able to start and stop Qwen-B **without** granting broad
sudo authority to anything that orchestrates work. A root LaunchDaemon would
force either an interactive password prompt or a passwordless-sudo rule; both
were rejected. A LaunchAgent in `gui/<uid>` is controllable by the owning user
with plain `launchctl` calls and no privilege escalation whatsoever.

The trade-off is recorded honestly: a `gui/<uid>` agent exists only while that
user has a login session. Qwen-B is an interactive-workload worker, so this is
the correct domain. If Qwen-B ever needs to run with no user logged in, it would
have to move to the `system` domain and the no-sudo property would be lost.

### Shared model artifacts — no duplicate weights

Both instances load the **same files**:

```
/Users/ericjellerson/Newton_Local/models/qwen38-27b/Qwen3.8-27B-Q8_0.gguf
/Users/ericjellerson/Newton_Local/models/qwen38-27b/mmproj-Qwen3.8-27B-f16.gguf
```

No second download, no second model tree, no duplicated GGUF, no second HF cache.
`config/qwen-b.env` does not restate those paths — it **sources `config/qwen-a.env`**
so there is exactly one place on disk where the canonical GGUF path is written,
and then overrides only `ALIAS` and `PORT`. `install-launchagent-b.sh` additionally
compares the configured model path against the *running* Qwen-A daemon's argv and
refuses to install if they differ.

A measured side effect: because both processes `mmap` the same file, the OS page
cache holds **one** copy of the weight pages and serves both instances from it.
With A and B both up, `File-backed pages` stayed at ~40–46 GiB rather than doubling.

---

## 2. Runtime configuration

`config/qwen-b.env` (B-specific fields only; the rest is inherited):

```
. /Users/ericjellerson/Newton_Local/config/qwen-a.env   # canonical artifacts

ALIAS=qwen-b
HOST=127.0.0.1
PORT=8081

QWEN_B_LABEL=com.newton.qwen38server.b
QWEN_B_DOMAIN=gui
QWEN_B_LOGDIR=/Users/ericjellerson/Newton_Local/logs/qwen-b
QWEN_B_LEASE=/Users/ericjellerson/Newton_Local/state/qwen-b-lease.json
QWEN_B_LOCK=/Users/ericjellerson/Newton_Local/state/qwen-b.lock
QWEN_B_IDLE_TTL=180            # idle grace period, seconds
QWEN_B_START_TIMEOUT=240       # acquire() readiness ceiling, seconds
QWEN_B_LEASE_MAX_AGE=1800      # BUSY lease considered abandoned after this
```

Resulting argument vector (identical in `scripts/run-qwen-b.sh`,
`scripts/qwen-b-argv.sh`, and the plist — equality is enforced at install time):

```
llama-server
  --model      .../Qwen3.8-27B-Q8_0.gguf
  --mmproj     .../mmproj-Qwen3.8-27B-f16.gguf
  --alias      qwen-b
  --host       127.0.0.1
  --port       8081
  --ctx-size   262144
  --n-gpu-layers 999
  --flash-attn on
  --jinja
  --parallel   1
  --cont-batching
  --metrics
```

This matches Qwen-A token-for-token except `--alias` and `--port`. No speculative
decoding, no MTP, no extra slots, no tuning.

---

## 3. Lifecycle

```
                    acquire (cold)
   STOPPED ─────────────────────────────► STARTING ──► READY ──► BUSY
      ▲                                       │                   │
      │                                       │ start failed      │ release
      │                                       ▼                   ▼
      │                                    STOPPED               IDLE
      │                                                           │
      │           terminate / idle-TTL expiry                     │
      └────────────── STOPPING ◄────────────────────────────────┘
                                                                  │
                                       acquire inside the TTL ────┘
                                       (reuses the loaded worker,
                                        no model reload)
```

| State | Means |
|---|---|
| `STOPPED` | No worker process, port 8081 closed, no model RAM. The normal resting state. |
| `STARTING` | `launchctl kickstart` issued; process may exist; `/health` not green yet. |
| `READY` | Worker healthy and unowned. |
| `BUSY` | Worker healthy and held by a named owner. |
| `IDLE` | Worker healthy, unowned, inside its idle grace period. |
| `STOPPING` | Stop issued; waiting for process exit and port closure. |

### Ground truth, not remembered state

The controller **never trusts the recorded lease on its own**. Before every
decision it observes four independent sources:

| Source | Command |
|---|---|
| launchd job | `launchctl print gui/<uid>/com.newton.qwen38server.b` |
| process | `ps`, matched on the exact llama-server argv (binary path + `--port 8081` + `--alias qwen-b`) |
| socket | `lsof -nP -iTCP:8081 -sTCP:LISTEN` |
| health | `GET /health`, then `GET /props` |

Those collapse to an observed physical state of `DOWN` / `LOADING` / `UP`, which
is then reconciled against the recorded lifecycle state. **Where they disagree,
reality wins** and the correction is written to `logs/qwen-b/controller.log`.

---

## 4. Lease and ownership

Lease file: `state/qwen-b-lease.json` (git-ignored — it is runtime state).

Tracked fields:

| Field | Purpose |
|---|---|
| `state` | current lifecycle state |
| `owner` | job identifier holding the worker when `BUSY` |
| `pid` | worker process id |
| `label`, `domain`, `endpoint`, `alias` | service identity |
| `acquired_at` | when the current owner took the lease |
| `last_activity` | heartbeat; drives abandoned-lease detection |
| `idle_since` | when the idle TTL clock started |
| `start_deadline` / `stop_deadline` | bounds on transitional states |
| `generation` | increments on every **cold start** — a stable generation across an acquire is proof no reload happened |
| `notes` | human-readable record of the last correction |

**Mutual exclusion.** Every mutating operation takes an exclusive `flock` on
`state/qwen-b.lock` for its whole duration, including the model load. Two
simultaneous `acquire` calls therefore cannot both start a worker: one wins and
the other, on acquiring the lock, finds the lease already `BUSY` and exits with
code **3**.

**Conflict handling.** An `acquire` by a different owner while `BUSY` is refused
(exit 3) with the current holder, its pid, and its timestamps. A deliberate
takeover requires the explicit `--steal` flag, which is logged. A re-`acquire`
by the *same* owner is idempotent and just refreshes the heartbeat.

---

## 5. Idle TTL

**Default: `QWEN_B_IDLE_TTL=180` seconds (3 minutes).**

Chosen from the commission's 2–5 minute range. The rationale: a cold start costs
~5–9 s of model load, while holding the worker idle costs ~46 GiB of GPU
allocation. Three minutes is long enough to absorb the realistic gap between a
result being returned and the follow-up request arriving, and short enough that a
forgotten worker returns its memory quickly.

`release` moves `BUSY → IDLE` and starts the clock. It deliberately does **not**
kill the worker, so back-to-back work does not pay repeated load/unload churn.

Expiry is enforced by a second, tiny LaunchAgent:

| | |
|---|---|
| Label | `com.newton.qwen38server.b-reaper` |
| Plist | `~/Library/LaunchAgents/com.newton.qwen38server.b-reaper.plist` |
| Action | `python3 scripts/qwenctl.py reap` |
| Interval | `StartInterval` 30 s |
| Priority | `ProcessType Background`, `LowPriorityIO` |

`reap` takes the same lock as everything else, so it can never race an acquire,
and is a no-op unless the state is `IDLE` **and** the TTL has elapsed.

**Worst-case shutdown latency is `TTL + 30 s` (210 s)** because the reaper polls.
That was observed exactly: a worker released at 19:46:34 was terminated at
19:50:05 after 210 s idle.

---

## 6. Orphan and inconsistency policy

| Situation | Detection | Default action |
|---|---|---|
| Controller says `STOPPED`, worker is running | observed `UP`/`LOADING` with recorded `STOPPED` | **Adopt.** Enters `IDLE` (or `STARTING` if still loading) with **no owner**, and falls under the normal idle TTL. Never silently killed. |
| Controller says `READY`/`BUSY`/`IDLE`, worker is gone | observed `DOWN` | Reset to `STOPPED`, clear the stale owner, record who held it. |
| Controller says `STARTING`, process gone and launchd reports "not running" | recorded pid absent + launchd not running | Treat as a definitively failed start; reset to `STOPPED` immediately rather than waiting out the deadline. |
| `BUSY` lease with no heartbeat for > `QWEN_B_LEASE_MAX_AGE` (1800 s) | `last_activity` age | Demote to `IDLE` (worker kept alive, ownership released) so it is acquirable again and the TTL can reclaim it. |
| Controller restarts with the lease file lost/corrupt | lease unreadable | Rebuild state entirely from launchd/process/socket/health. There is nothing remembered to trust. |
| More than one Qwen-B process | argv match returns >1 | Refuse to `acquire` (exit **5**) and report both pids. `reconcile --orphan-policy terminate` kills every copy launchd does not own. |

Policy is selectable per invocation: `--orphan-policy adopt` (default, never
destroys work), `terminate` (explicit operator choice), or `report` (observe only).

The adopt-by-default choice is deliberate: an unexpected running worker may be
doing someone's real work, so the safe move is to take ownership of its lifecycle
rather than destroy it — and because an adopted orphan carries no owner and is
subject to the idle TTL, it still cannot leak memory indefinitely.

---

## 7. Controller

| | |
|---|---|
| Implementation | `scripts/qwenctl.py` (plain Python 3, no dependencies) |
| Operator entry point | `scripts/qwen-b` |
| Lease | `state/qwen-b-lease.json` |
| Lock | `state/qwen-b.lock` |
| Transition log | `logs/qwen-b/controller.log` (JSON lines) |
| Worker stderr | `logs/qwen-b/stderr.log` |
| Worker stdout | `logs/qwen-b/stdout.log` |
| Reaper log | `logs/qwen-b/reaper.log` |

**The controller owns lifecycle. The model does not.** Nothing in this design
consults Qwen about when to start, when to stop, which launchctl verbs to run, or
whether another worker should exist. Those are all plain state transitions over
observed facts.

### Commands

| Command | Effect |
|---|---|
| `status` | Reconcile, then report state, owner, pid, launchd, socket, health, runtime config, idle countdown |
| `health` | Probe `/health` + `/props`; exit 1 if unhealthy |
| `acquire --owner ID` | Ensure a `READY` worker and take the lease. Starts B if stopped; reuses it if loaded |
| `release --owner ID` | `BUSY → IDLE`, start the idle TTL. Does not kill |
| `terminate` | Stop now → `STOPPED`. Refuses while `BUSY` unless `--force` |
| `reconcile` | Force reconciliation; handle orphans and stale leases |
| `reap` | Terminate only if the idle TTL has expired (used by the reaper agent) |
| `heartbeat --owner ID` | Refresh a held lease's activity timestamp |
| `logs [-n N]` | Tail controller, stderr, and stdout logs |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | error |
| 3 | lease conflict (held by another owner) |
| 4 | timeout (readiness or lock) |
| 5 | inconsistent state (duplicate workers, foreign process on 8081) |

### Acquire contract

`acquire` returns everything a caller needs and nothing it must guess:

```json
{
  "ok": true, "state": "BUSY", "owner": "my-job", "pid": 9053,
  "endpoint": "http://127.0.0.1:8081",
  "openai_base_url": "http://127.0.0.1:8081/v1",
  "model": "qwen-b",
  "reused_loaded_worker": false, "model_reloaded": true,
  "acquire_wall_s": 7.14,
  "last_start": {"spawn_latency_s": 0.03, "load_to_ready_s": 6.71, "generation": 1},
  "runtime": {"alias":"qwen-b","ftype":"Q8_0","n_ctx":262144,
              "modalities":{"vision":true,"video":true,"audio":false}},
  "idle_ttl_seconds": 180, "generation": 1
}
```

`reused_loaded_worker` / `model_reloaded` / `generation` let a caller verify for
itself whether a reload occurred.

---

## 8. Operator commands

```bash
# status of the elastic worker
~/Newton_Local/scripts/qwen-b status

# start / take the worker (starts it if stopped, reuses it if loaded)
~/Newton_Local/scripts/qwen-b acquire --owner my-job

# health probe
~/Newton_Local/scripts/qwen-b health

# give it back — goes IDLE, terminates itself after the 180 s TTL
~/Newton_Local/scripts/qwen-b release --owner my-job

# stop it now
~/Newton_Local/scripts/qwen-b terminate

# fix inconsistent state / adopt or kill an orphan
~/Newton_Local/scripts/qwen-b reconcile
~/Newton_Local/scripts/qwen-b reconcile --orphan-policy terminate

# logs
~/Newton_Local/scripts/qwen-b logs -n 60

# both services side by side, plus machine memory
~/Newton_Local/scripts/qwen-status.sh

# runtime configuration as the server itself reports it
~/Newton_Local/scripts/qwen-props.sh 8080
~/Newton_Local/scripts/qwen-props.sh 8081
```

Install / remove (no sudo):

```bash
~/Newton_Local/scripts/install-launchagent-b.sh
~/Newton_Local/scripts/install-reaper-b.sh
~/Newton_Local/scripts/uninstall-launchagent-b.sh
~/Newton_Local/scripts/uninstall-reaper-b.sh
```

Proving suites:

```bash
python3 ~/Newton_Local/scripts/prove-qwen-b.py        # runtime parity, tool call, vision
python3 ~/Newton_Local/scripts/prove-concurrency.py   # A+B overlapping correctness
~/Newton_Local/scripts/prove-lifecycle-b.sh           # release/reuse/TTL/duplicate prevention
~/Newton_Local/scripts/prove-reconcile-b.sh           # orphan + stale-lease reconciliation
~/Newton_Local/scripts/prove-cycles-b.sh 6            # repeated cycles, leak check
~/Newton_Local/scripts/prove-reclamation-b.sh         # stop + memory reclamation
~/Newton_Local/scripts/prove-acceptance-b.sh          # 34 end-to-end acceptance checks
TAG=run ~/Newton_Local/scripts/bench-capacity.sh      # A-alone vs A+B vs A-after-B
```

---

## 9. Using the worker from code

```python
import json, subprocess, urllib.request

OWNER = "my-job-1"
Q = "/Users/ericjellerson/Newton_Local/scripts/qwen-b"

lease = json.loads(subprocess.run([Q, "acquire", "--owner", OWNER, "--json"],
                                  capture_output=True, text=True, check=True).stdout)
try:
    req = urllib.request.Request(
        lease["openai_base_url"] + "/chat/completions",
        data=json.dumps({"model": lease["model"],
                         "messages": [{"role": "user", "content": "hello"}],
                         "max_tokens": 2048}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        print(json.load(r)["choices"][0]["message"]["content"])
finally:
    subprocess.run([Q, "release", "--owner", OWNER], check=False)
```

`release` in a `finally` is the right pattern: even if the caller crashes without
it, the abandoned-lease rule (30 min) and the idle TTL will recover the worker.

---

## 10. Known limitations

- **Reaper granularity.** Idle shutdown happens within `TTL + 30 s`, not exactly at
  the TTL, because the reaper polls on a 30 s interval.
- **Session-scoped domain.** `gui/<uid>` agents require a login session. This is
  the price of the no-sudo control model and is accepted deliberately.
- **Single worker by design.** There is one elastic worker. This is not a
  scheduler and does not manage a pool.
- **`--steal` is unguarded.** It exists so an operator can deliberately take a
  worker from a wedged owner; it does not ask the previous owner's permission. It
  is logged every time.
- **Cold-start timing depends on the page cache.** Measured load-to-ready of
  ~5–9 s reflects the 29 GB GGUF being warm in the file cache. A genuinely cold
  read from disk will be materially slower.
- **Capacity.** See `docs/05-commission-2-completion-record.md` §5 — two full
  Q8/262K instances fit, but the honest headroom picture depends on what else is
  resident.
