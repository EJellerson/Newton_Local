# Commission #2 — Completion Record

**Date:** 2026-08-24
**Scope:** Add **Qwen-B**, an on-demand elastic worker on `127.0.0.1:8081`,
reusing Qwen-A's canonical model artifacts and llama.cpp build, plus a
deterministic elastic-worker controller.
**Explicitly out of scope and not started:** Hermes, Foreman, Open WebUI, MCP
servers, coder/reviewer agents, worktree orchestration, commission schemas,
cloud fallback, agent memory, multi-agent routing, a third Qwen instance.

Commission #1 provenance in `docs/00-machine-baseline.md`,
`docs/01-llamacpp.md`, `docs/02-operations.md` and
`docs/03-completion-record.md` is unchanged. This is a separate record.

---

## 1. State before any change

Verified without disturbing Qwen-A.

| Check | Observed |
|---|---|
| Qwen-A launchd | `system/com.newton.qwen38server.a`, `state = running`, LaunchDaemon, user `ericjellerson:staff` |
| Qwen-A PID | 97191 |
| Qwen-A health | `{"status":"ok"}` |
| Qwen-A socket | `TCP 127.0.0.1:8080 (LISTEN)` — loopback only |
| Qwen-A runtime | `alias=qwen-a ftype=Q8_0 n_ctx=262144 slots=1 vision=True build=b1-f280b26` |
| Qwen-A RSS | 23.2 GiB |
| Machine memory | 128 GiB total; app 52.2 GiB, cached files 40.3 GiB, wired 2.6 GiB |
| Swap | `total 0.00M used 0.00M`, swapouts 0 |
| Compression | 0.058 GiB occupied |
| Memory pressure | level 1 (normal), free-percentage 97% |
| GPU / Metal allocation | 46.14 GiB (`AGXAccelerator` `Alloc system memory`) |
| Port 8081 | **free** — nothing listening |
| Existing Qwen-B / 8081 service | **none** |
| Git | branch `main`, clean, in sync with `origin/main` at `45b4000` |

**Unrelated heavy workload: PRESENT.** An ML training sweep
(`ML_Model/SP6/tools/run_e002.py` → `run_e003.py` → `run_e004.py` →
`run_e005.py`) ran back-to-back throughout the commission at **1,000–2,200% CPU
and 13–23 GiB RSS**, with only ~60–90 s gaps between experiments. This is
recorded, not hidden; see §5.

`sudo` was **not** available non-interactively in this session. That did not
block anything — it reinforced the user-domain LaunchAgent design, which needs
no elevation — but it does mean Qwen-A's root LaunchDaemon was inspected
read-only and never modified.

---

## 2. What was built

| Path | Purpose |
|---|---|
| `config/qwen-b.env` | Qwen-B runtime config; **sources `qwen-a.env`** so canonical artifacts have one definition |
| `config/com.newton.qwen38server.b.plist` | On-demand user LaunchAgent (`RunAtLoad=false`, `KeepAlive=false`) |
| `config/com.newton.qwen38server.b-reaper.plist` | 30 s idle-TTL reaper agent |
| `scripts/qwenctl.py` | The deterministic elastic-worker controller |
| `scripts/qwen-b` | Operator entry point |
| `scripts/qwen-b-argv.sh` | Canonical argv generator (drift-checked against the plist) |
| `scripts/run-qwen-b.sh` | Foreground runner for debugging |
| `scripts/install-launchagent-b.sh` / `uninstall-launchagent-b.sh` | Install/remove the worker agent (no sudo) |
| `scripts/install-reaper-b.sh` / `uninstall-reaper-b.sh` | Install/remove the reaper |
| `scripts/qwen-status.sh` | Side-by-side A/B + machine status |
| `scripts/qwen-props.sh` | Runtime config as the server reports it |
| `scripts/snapshot-machine.py` | Common memory/GPU measurement primitive |
| `scripts/prove-qwen-b.py` | Runtime-parity proving suite |
| `scripts/prove-concurrency.py` | A+B overlapping-correctness proof |
| `scripts/prove-lifecycle-b.sh` | Release / reuse / TTL / duplicate-prevention |
| `scripts/prove-reconcile-b.sh` | Orphan and stale-lease reconciliation |
| `scripts/prove-cycles-b.sh` | Repeated cycles / leak check |
| `scripts/prove-reclamation-b.sh` | Stop + memory reclamation evidence |
| `scripts/prove-acceptance-b.sh` | 34 end-to-end acceptance checks |
| `scripts/bench-dual.py` | Overlap-aware benchmark primitive |
| `scripts/bench-capacity.sh` | Three-phase capacity comparison driver |
| `docs/04-qwen-b-elastic-worker.md` | Architecture + runbook |

Installed outside the repo (user domain, no sudo):

```
~/Library/LaunchAgents/com.newton.qwen38server.b.plist
~/Library/LaunchAgents/com.newton.qwen38server.b-reaper.plist
```

Qwen-A's `config/qwen-a.env`, `config/com.newton.qwen38server.a.plist`,
`/Library/LaunchDaemons/com.newton.qwen38server.a.plist`, and its running
process were **not** modified.

---

## 3. Qwen-B proving

### 3.1 Runtime parity — `benchmarks/proving-qwen-b.json` (5/5 PASS)

| Test | Result |
|---|---|
| `identity_parity` | `alias=qwen-b ftype=Q8_0 n_ctx=262144 vision=True`, model path identical to Qwen-A's canonical GGUF |
| `basic_inference` | "Tungsten has the chemical symbol W and atomic number 74." — 21.43 tok/s |
| `tool_calling` | `finish_reason=tool_calls`; emitted `get_air_quality({"city":"Reykjavik","country_code":"IS"})` |
| `tool_roundtrip` | Consumed the tool result and reported AQI 17 / Good |
| `vision` | Read `assets/vision-test.png`: red circle, blue square, green triangle, text "47", "NEWTON LOCAL VISION TEST" |

### 3.2 Loopback-only

```
lsof:    llama-ser  PID  TCP 127.0.0.1:8081 (LISTEN)
netstat: tcp4  127.0.0.1.8081  LISTEN
127.0.0.1:8081        -> 200
10.0.0.204:8081       -> connection refused (curl exit 7)
10.0.0.53:8081        -> connection refused (curl exit 7)
10.10.10.1:8081       -> connection refused (curl exit 7)
169.254.80.186:8081   -> connection refused (curl exit 7)
```

(`Erics-Mac-Pro.local` resolves to `127.0.0.1` first, so it answers via loopback;
every non-loopback address refuses.)

### 3.3 Start behaviour

From `STOPPED`, through `scripts/qwen-b acquire`:

| Metric | Value |
|---|---|
| Mechanism | `launchctl kickstart gui/501/com.newton.qwen38server.b` |
| Spawn latency | **0.03–0.06 s** (median 0.04 s) |
| Load-to-ready | **4.61–9.65 s** across 13 cold starts (median ≈ 6.2 s) |
| Total `acquire` wall | 5.0–9.9 s cold |
| Verified at acquire | `alias=qwen-b ftype=Q8_0 n_ctx=262144 vision=True` |
| Qwen-A during start | healthy; answered at 20.13 tok/s mid-sequence |

13/13 cold starts succeeded. Load-to-ready is fast because the 29 GB GGUF is warm
in the page cache (Qwen-A holds it mapped); a genuinely cold disk read is slower.

---

## 4. Dual-instance overlap

### 4.1 Correctness under genuine overlap — `benchmarks/proving-concurrency.json` (PASS)

A and B were given **different** verifiable questions at the same instant.

| | Qwen-A (:8080) | Qwen-B (:8081) |
|---|---|---|
| Question | train 14:20→21:05 duration; capital of Portugal | 17 × 23; symbol for potassium |
| Answer | "The journey is 6 hours 45 minutes, and the capital of Portugal is Lisbon." | "17 × 23 is 391, and the chemical symbol for potassium is K." |
| Verified substrings | `6`, `45`, `lisbon` — all present | `391`, `k` — all present |
| finish_reason | `stop` | `stop` |
| gen tok/s | 15.21 | 12.33 |
| `n_ctx` during flight | **262144** | **262144** |
| quant during flight | **Q8_0** | **Q8_0** |

Overlap 9.236 s = **100% of the shorter request**; 5/9 probe samples had both
servers reporting `is_processing: true`.

### 4.2 Throughput under overlap — `benchmarks/bench-ab-asrun.json`

| | Qwen-A | Qwen-B |
|---|---|---|
| gen tok/s | 11.657 | 11.013 |
| prompt tok/s | 192.95 | 202.44 |
| tokens generated | 500 | 500 |
| wall | 67.84 s | 69.19 s |
| errors / throttling | none | none |

Overlap 67.836 s = **100%** of the shorter request. **29 of 30** in-flight samples
showed *both* servers with `is_processing: true` — independent server-side
corroboration, not just client clocks. `n_ctx` queried live on both servers during
the phase: **262144 / 262144**.

Overlap was forced with a `threading.Barrier`, so both requests are released in
the same instant rather than merely being issued close together.

---

## 5. Memory / capacity comparison

**Measurement conditions — stated plainly.** The commission asks for this
comparison to be free of unrelated heavy workload. **It was not.** The ML sweep
(`ML_Model/SP6/tools/run_e00N.py`) was present in **100% of samples in every
phase of both runs**, at 1,175–1,814% CPU and ~20.8 GiB RSS.

A clean window never opened. The sweep runs experiments back-to-back with only
~60–90 s gaps, and the three-phase comparison needs ~2.5 minutes. An automated
quiet-gate did fire during one gap at 19:56:28, but `run_e004.py` started seconds
later and contaminated that run too. On the user's instruction, the contaminated
numbers are reported rather than waiting indefinitely.

Two mitigations make the result usable anyway:

1. **Contamination was uniform across all three phases**, so the A-alone versus
   A+B *delta* is a valid like-for-like comparison.
2. **The comparison was run twice, independently** (run 1 at 19:56–19:59, run 2
   at 20:12–20:15). Every headline metric replicated within **3%**.

The *absolute* rates are ~5% below Commission #1's idle-machine baseline
(19.7–19.9 vs ~21 tok/s for A alone) and should be read as a floor, not a ceiling.

### Replication check

| Metric | Run 1 | Run 2 | Spread |
|---|---:|---:|---:|
| A alone, generation tok/s | 19.692 | 19.874 | 0.9% |
| A+B, A generation tok/s | 12.004 | 11.657 | 2.9% |
| A+B, B generation tok/s | 11.177 | 11.013 | 1.5% |
| A after B stopped, tok/s | 19.657 | 19.931 | 1.4% |
| A alone, prompt tok/s | 332.41 | 329.11 | 1.0% |
| A+B, A prompt tok/s | 189.45 | 192.95 | 1.8% |
| A+B, B prompt tok/s | 192.14 | 202.44 | 5.1% |
| A+B, GPU allocation GiB | 93.14 | 93.17 | 0.0% |
| A+B, overlap seconds | 66.73 | 67.84 | 1.6% |
| A+B, both-processing samples | 29/30 | 29/30 | — |

### Comparison table (run 2; run 1 in parentheses where it differs materially)

`benchmarks/bench-{a-alone,ab,a-after-b}-run{1,2}.json`. 500 generated tokens per
request, ~4,800-token unique prompt per phase (nonce-salted so llama.cpp's prompt
cache cannot turn a prompt-processing measurement into a cache hit), identical
parameters in every phase, **no tuning between phases**.

| Metric | A alone | A + B concurrent | A after B stopped |
|---|---:|---:|---:|
| **A generation tok/s** | 19.874 (19.692) | **11.657** (12.004) | 19.931 (19.657) |
| **B generation tok/s** | — | **11.013** (11.177) | — |
| **Aggregate generation tok/s** | 19.874 | **22.670** (23.181) | 19.931 |
| A prompt-processing tok/s | 329.11 | 192.95 | 331.23 |
| B prompt-processing tok/s | — | 202.44 | — |
| Aggregate prompt tok/s | 329.11 | 395.39 | 331.23 |
| A RSS (GiB, max) | 26.29 | 26.76 | 27.27 |
| B RSS (GiB, max) | — | 17.92 | — |
| **GPU / Metal allocation (GiB, max)** | **47.03** | **93.17** | **47.03** |
| GPU utilisation (%, max) | 100 | 100 | 100 |
| Free memory (GiB, min) | 6.12 | **0.03** | 20.80 |
| App memory (GiB, max) | 59.32 | 59.98 | 62.50 |
| Cached files (GiB, min) | 28.77 | 13.59 | 13.60 |
| Reclaimable headroom (GiB, min) | 35.07 | **14.32** | 34.44 |
| Wired (GiB, max) | 47.76 | 66.20 | 47.74 |
| Compressed memory (GiB, max) | 0.451 | 0.476 | 0.476 |
| **Swapouts** | **0** | **0** | **0** |
| Swap used (MiB) | 0.0 | 0.0 | 0.0 |
| **Memory pressure level** | **1 (normal)** | **1 (normal)** | **1 (normal)** |
| Pressure free-% (min) | 62 | 47 | 62 |
| Errors / throttling reported | none | none | none |
| Unrelated heavy load present | **yes** | **yes** | **yes** |

### What this says

- **Two full Q8/262K instances fit and run.** Metal allocation goes 47 GiB →
  93 GiB and both servers stay healthy, at full 262144 context, with no errors
  and no throttling, in both runs.
- **Concurrency is GPU-bound, and the cost is real.** Each instance runs at
  **59–61%** of its solo generation rate. Aggregate generation throughput rises
  only **+14% to +18%** (19.9 → 22.7 tok/s). Running B does not buy two
  full-speed models; it buys a second *independent* context plus roughly a sixth
  more total token throughput. GPU utilisation was already 100% with A alone —
  that is the whole explanation.
- **A recovers completely.** 19.874 → 19.931 tok/s after B stops (run 1:
  19.692 → 19.657). No residual degradation either way.
- **The weights are shared, not duplicated.** Both processes `mmap` the same
  GGUF, so the page cache holds one copy of the weight pages. Combined physical
  footprint measured 22.0 GiB (A) + 17.2 GiB (B) = 39.2 GiB, far below 2 × 29 GB.
- **Wired memory is not the constraint.** A 47–66 GiB `wired` reading appears
  transiently while Metal sets up a model's buffers; steady state with both
  instances up was **2.64 GiB wired**, sampled repeatedly.

### Is this operationally comfortable? Honestly: it works, with thin margin.

Not sugar-coated:

- Free memory fell to **0.03 GiB** during concurrent operation, and per-phase
  reclaimable headroom bottomed at **14.3 GiB**. The machine was leaning on
  reclaimable cache — which is exactly what cache is for — but the cushion was
  thin.
- Memory pressure **never left level 1 (normal)**, compression stayed under
  0.5 GiB during both benchmark runs, and swapouts were **0** across all six
  phases.
- Later, during the concurrent correctness test (A + B **plus** the ~20.8 GiB ML
  sweep), the machine crossed a threshold for the first time in the project's
  history: macOS created a 1024 MiB swap file and wrote **56 pages / 0.88 MiB**.
  Swapins remained **0** — nothing had to be read back — and pressure stayed at
  level 1. Compression rose to 3.19 GiB.

So: two 27B Q8 instances at 262144 context are **comfortable on this 128 GiB
M2 Ultra when they are the main tenants**, and **workable but tight when a
~20 GiB third-party workload is also resident**. The first swap byte of the whole
project appeared only in that three-way case. The binding constraint is not
memory capacity — it is **GPU throughput**: at 100% GPU utilisation with A alone,
adding B mostly redistributes existing throughput rather than adding much.

**Recommendation:** the elastic model — start on demand, release after a short
TTL — is the right design. Keeping B resident permanently would spend ~46 GiB of
GPU allocation to buy ~14–18% aggregate throughput, and would remove the headroom
that absorbed the ML sweep. Rent B; do not own it.

## 6. Stop and reclamation — `benchmarks/snap-reclaim-{before,after}.json`

Measured from the dual-running state, stopping through the controller.

| Signal | Before B stop | After B stop | Delta |
|---|---:|---:|---:|
| Qwen-B process | `pid=10240` | **none** | gone |
| Port 8081 | **OPEN** | **CLOSED** | closed |
| Qwen-B health | ok | unreachable | down |
| **GPU / Metal allocation** | 93.14 GiB | **47.03 GiB** | **−46.11 GiB** |
| App memory | 80.45 GiB | 60.49 GiB | −19.96 GiB |
| Free memory | 0.96 GiB | 20.94 GiB | +19.98 GiB |
| Reclaimable headroom | 40.89 GiB | 60.87 GiB | +19.98 GiB |
| Cached files | 39.89 GiB | 39.89 GiB | 0.00 |
| Wired | 2.69 GiB | 2.66 GiB | −0.03 GiB |
| Compressed | 3.187 GiB | 3.187 GiB | 0.00 |
| Swapouts | 56 | 56 | no new swap |
| Memory pressure | level 1 | level 1 | unchanged |
| **Qwen-A** | healthy | **healthy**, `Q8_0 / n_ctx 262144`, 20.12 tok/s | unaffected |

Stop took **0.84 s**. Across 6 scripted cycles, GPU allocation returned to
46.11–46.12 GiB **every** time.

---

## 7. Controller proving

### 7.1 Release / reuse / TTL — `benchmarks/transcripts/lifecycle-b.txt`

| Step | Evidence |
|---|---|
| `BUSY → IDLE` on release | worker stayed loaded, `pid=1587`, 179.8 s of 180 s TTL remaining |
| **Re-acquire inside the TTL** | pid **1587 → 1587**, generation **1 → 1**, `reused_loaded_worker=true`, `model_reloaded=false`, wall **0.47 s** vs 7.14 s cold |
| Duplicate acquire refused | second owner got `lease_conflict`, **exit 3**; process count stayed **1** |
| Concurrent cold-acquire race | racer-1 exit **0**, racer-2 exit **3**; exactly **1** process, exactly **1** launchd job |
| Inference on the leased worker | `B-LEASED-OK` at 21.28 tok/s |
| **Idle-TTL expiry** | released at 19:46:34 → reaper terminated at 19:50:05 after **210 s** idle (TTL 180 s + 30 s poll) |
| Reaper log | `reap: idle TTL expired (210s >= 180s) — terminating qwen-b pid 3008` / `reap: state=STOPPED listening=False processes=0` |
| Memory after TTL stop | GPU 92.25 → **46.11 GiB**; free 21.15 → 38.66 GiB; headroom 38.45 → 85.05 GiB |
| Qwen-A | healthy at every checkpoint |

### 7.2 Reconciliation / orphan handling — `benchmarks/transcripts/reconcile-b.txt`

Each case was created by **bypassing the controller**, then reconciled.

| Case | Setup | Result |
|---|---|---|
| **A/C — orphan, no lease** | started B directly via `launchctl kickstart`, forced the lease back to `STOPPED` | Adopted: `STOPPED → STARTING`, `owner=null`, note recorded, subject to the idle TTL. Not killed. |
| **B — BUSY but worker died** | took a real lease, then `kill -9` the worker | `status` auto-reconciled `BUSY → STOPPED`, correction `worker_vanished`, `cleared_owner: case-b-owner` |
| **D — controller restart with no state** | acquired, then deleted `state/qwen-b-lease.json` entirely | State rebuilt from launchd/process/socket/health: `observed UP → IDLE`, pid 4224 recovered |
| **E — abandoned BUSY lease** | back-dated `last_activity` by 2 h | `BUSY → IDLE`, `busy_lease_abandoned`, `age_s 7200.2 > max_age_s 1800`; worker kept alive, ownership released |
| **F — orphan policy `terminate`** | orphan recreated, `reconcile --orphan-policy terminate` | Worker stopped, port closed, `STOPPED` |
| Qwen-A throughout | — | `{"status":"ok"}`, answered `A-UNAFFECTED` |

Two real controller defects were found by these tests and fixed before the run
was accepted: `acquire` had no handler for the `STARTING` state (it aborted), and
a `STARTING` worker that died was not recognised as failed until its 240 s
deadline elapsed. Both are corrected in `scripts/qwenctl.py`; the transcript above
is from the post-fix run.

### 7.3 Repeated cycles — `benchmarks/cycles-b.json`

Six full `STOPPED → acquire → BUSY → infer → release → IDLE → terminate → STOPPED`
cycles:

| # | pid | spawn s | ready s | procs after | zombies | launchd jobs | port 8081 | GPU alloc after | A |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 4511 | 0.03 | 5.27 | 0 | 0 | 1 | CLOSED | 46.12 | ok |
| 2 | 4688 | 0.04 | 6.16 | 0 | 0 | 1 | CLOSED | 46.12 | ok |
| 3 | 4865 | 0.04 | 6.15 | 0 | 0 | 1 | CLOSED | 46.11 | ok |
| 4 | 5040 | 0.06 | 6.68 | 0 | 0 | 1 | CLOSED | 46.11 | ok |
| 5 | 5229 | 0.04 | 6.18 | 0 | 0 | 1 | CLOSED | 46.12 | ok |
| 6 | 5405 | 0.04 | 6.15 | 0 | 0 | 1 | CLOSED | 46.12 | ok |

Every cycle returned `CYCLE-N-OK` at `n_ctx=262144`. Final invariants: **0**
Qwen-B processes, **0** zombies, **1** launchd job (`state = not running`,
`runs = 12`), port **CLOSED**, controller `STOPPED`, Qwen-A `ok`.

Controller event tally over the whole commission
(`logs/qwen-b/controller.log`): 13 `acquire.start`, 13 `acquire.ready`,
3 `acquire.reuse`, 2 `acquire.conflict`, 12 `release`, 10 `terminate`,
10 `reconcile`, 1 `reap.expired`, 1 `reap.terminated`. No failed starts.

---

## 8. Deviations and warnings

1. **The capacity benchmark is contaminated.** Documented in §5 with the exact
   contaminating process, its CPU/RSS, and its presence in every sample of both
   runs. Reported on the user's explicit instruction after a clean window failed
   to open; mitigated by uniform contamination across phases and by running the
   whole comparison twice with <3% spread.
2. **Absolute throughput is ~5% below the Commission #1 idle baseline**
   (19.7–19.9 vs ~21 tok/s for A alone), attributable to the same contamination.
   Read the absolute rates as a floor.
3. **First swap activity in the project's history** — 56 pages / 0.88 MiB, with
   swapins 0 and pressure level 1 — observed only with A + B + the ~20.8 GiB ML
   sweep resident simultaneously. See §5.
4. **Idle shutdown latency is `TTL + 30 s`,** not exactly the TTL, because the
   reaper polls on a 30 s interval.
5. **`gui/<uid>` domain requires a login session.** Accepted deliberately as the
   price of sudo-free lifecycle control.
6. **`sudo` was unavailable non-interactively.** Qwen-A's root LaunchDaemon was
   therefore only ever read, never modified — which is what the commission wanted
   anyway. `scripts/qwen-a-status.sh` (Commission #1) still invokes `sudo
   launchctl print`; the new `scripts/qwen-status.sh` reads the same information
   without it.
7. **Two controller defects found and fixed** during reconciliation testing
   (§7.2). Disclosed rather than quietly patched.
8. **`--steal` is unguarded by design** — an operator escape hatch for a wedged
   owner. Every use is logged.
9. **Vision on B was proven, not re-validated.** Commission #1 already validated
   the model; §3.1 proves the *second instance* exposes the same vision surface.
10. **The 5.07 s spawn-latency outlier** in the controller log is from the
    reconciliation test, where the worker was started outside the controller and
    the first observation happened after it was already running. All
    controller-initiated spawns were 0.03–0.06 s.

---

## 9. Scope compliance

| Constraint | Status |
|---|---|
| Qwen-A unchanged on `127.0.0.1:8080` | Held — plist, env, model, context, port, domain and process untouched |
| No duplicate model download or tree | Held — `qwen-b.env` sources `qwen-a.env`; installer verifies against the running daemon's argv |
| llama.cpp reused, not rebuilt | Held — `b1-f280b26`, unchanged |
| Loopback-only on 8081 | Held and tested against four non-loopback addresses |
| Normally stopped, no autostart | Held — `RunAtLoad=false`, `KeepAlive=false`, verified after install |
| Least privilege, no sudo weakening | Held — user LaunchAgent; no sudoers change; no passwordless root |
| Deterministic control, not model-driven | Held — `qwenctl.py` is plain code; the model decides nothing |
| No Hermes | Not installed, not configured |
| No Foreman | Not implemented |
| No Qwen-C / third worker | Does not exist |
| No weights, caches, logs, state or secrets in git | Held — see §10 |

---

## 9b. Final acceptance run

`scripts/prove-acceptance-b.sh` — **34 passed, 0 failed**
(`benchmarks/transcripts/acceptance-check.txt`). It re-verifies, from a cold
`STOPPED` state: both services' identity and loopback binding; B registered but
not running with `RunAtLoad`/`KeepAlive` false; GPU allocation back to A-only;
**exactly one** copy of the GGUF and mmproj on disk; deterministic start to
`BUSY`; B's live `Q8_0` / `262144` / vision / identical model path; A usable
while B runs; duplicate acquire refused with exit 3 and still one process;
`release → IDLE`; re-acquire reusing the same pid with no reload; terminate
reclaiming 93.25 → 47.02 GiB; port closed; A healthy; and the scope exclusions
(no Hermes, no Foreman, no third instance, exactly one `llama-server` at rest).

End state: **Qwen-A running, Qwen-B stopped.**

## 10. Git hygiene

Verified before staging:

- `.gitignore` from Commission #1 is unchanged and still covers `*.gguf`,
  `tools/llama.cpp/`, `logs/`, `state/`, `.cache/`, tokens and keys.
- `git check-ignore` confirms the new runtime paths are excluded:
  `state/qwen-b-lease.json`, `state/qwen-b.lock`,
  `logs/qwen-b/controller.log`, `logs/qwen-b/stderr.log`,
  `logs/qwen-b/stdout.log`, `logs/qwen-b/reaper.log`.
- No model weights, HF cache, virtualenv, credential, token, PID file or
  lease/state file is staged.
- Committed evidence is proving output only: JSON results and text transcripts
  under `benchmarks/`, totalling well under 1 MB.
- The installed LaunchAgent plists live in `~/Library/LaunchAgents/` (outside the
  repo); their **sources** are committed under `config/`.

## 11. Operator quick reference

```bash
~/Newton_Local/scripts/qwen-b status                   # elastic worker state
~/Newton_Local/scripts/qwen-b acquire --owner my-job   # start / take
~/Newton_Local/scripts/qwen-b health                   # health probe
~/Newton_Local/scripts/qwen-b release --owner my-job   # give back -> IDLE (180 s TTL)
~/Newton_Local/scripts/qwen-b terminate                # stop now
~/Newton_Local/scripts/qwen-b reconcile                # fix inconsistent state
~/Newton_Local/scripts/qwen-b logs -n 60               # worker + controller logs
~/Newton_Local/scripts/qwen-status.sh                  # A and B side by side
```

Full runbook: `docs/04-qwen-b-elastic-worker.md`.
