# 19 — Fish Audio Drama 3 (MLX Q4) and Voice Studio: completion record

**Commission:** NEWTON_LOCAL_FISH_DRAMA3_001, revision 2.0 (Q4-first deployment) ·
**Date:** 2026-10-14 · **Machine:** Mac Pro, M2 Ultra (24 CPU / 76 GPU cores),
128 GiB, macOS 26.6.2 · **Code:** branch `fish-drama3-voice-studio`,
fast-forwarded into `main`; nothing was pushed.

Built with Fish Audio. Plan and audit: [17](17-fish-voice-plan.md) · runbook:
[18](18-fish-voice-studio.md) · future telephone assistant:
[20](20-voice-gateway-architecture.md) · future calendar:
[21](21-ghl-calendar-requirements.md).

## 1. Result

Fish Audio Drama 3 (MLX Q4) runs on this Mac as a local, on-demand speech
service with its own controller, LaunchAgent, network-sandboxed engine and
internal API. A resource policy shared by the Fish and Qwen controllers
refuses Fish beside Flash-Next in both directions and admits Fish beside the
27B only on measured memory; it was proven against real launchd jobs. Voice
Studio is implemented and was used end to end in a browser against the real
model, on a second instance of the control service built from the same code.

The production control service has **not** been restarted, so Voice Studio is
not live at `127.0.0.1:8765/voice` yet: that restart needs the operator's
approval (§15). Cloning the operator's voice awaits the operator's own
recording: **PENDING OPERATOR VOICE REVIEW** (§13).

| Category | Items |
|---|---|
| **IMPLEMENTED and VERIFIED** | Q4 checkpoint installed, pinned and verified file by file; isolated, hash-locked runtime; engine with streamed decoding, progressive audio, cancellation and no network access; `fishctl` lifecycle and LaunchAgent (never runs at load); the shared policy in both controllers and both launch gatekeepers; Voice Studio backend, page and private store (on the second instance); Fish-only benchmarks; 79 new unit tests; docs 17–21 |
| **IMPLEMENTED, VERIFIED IN PART** | Voice Studio in production (same code verified on the second instance; production needs a restart) · browser recording (recorder, level meter, preview, 24-bit encoding, upload and ingestion verified with a synthetic tone in place of the microphone; the automated browser has none) · the page's file picker (the upload route behind it is verified; the automated browser cannot open a file dialog) · off after reboot (by construction and with a fixture boot record; no physical reboot) |
| **OPERATOR-ACCEPTED** | nothing yet |
| **PENDING OPERATOR ACTION** | approve restarting Newton AI Control (§15.1) · approve or decline the Fish + Qwen 27B benchmark (§15.2) · record your own voice and judge the clone (§13) · confirm the licence's revenue condition before any commercial use (§15.4) |
| **BLOCKED** | nothing |
| **DEFERRED** | Q8 (not authorised) · Twilio, GoHighLevel, speech-to-text and the voice gateway (specified only: docs/20, docs/21) · a physical reboot test |

## 2. Deliverables (commission §18)

| # | Deliverable | Where | Status |
|---|---|---|---|
| 1 | Working Drama 3 MLX Q4 deployment | LaunchAgent `com.newton.fish`, `scripts/fish` | verified (§6) |
| 2 | Isolated runtime, pinned dependencies | `tools/fish-venv` from `scripts/install-fish-runtime.sh`; `config/fish-requirements.lock.txt` | verified (§3) |
| 3 | Verified artifacts, provenance manifests | `models/fish-drama3-mlx-q4/MANIFEST.json` and `MANIFEST.md`; `fish verify --deep` | verified (§3) |
| 4 | Dedicated lifecycle controller | `scripts/fishctl.py` | verified (§8) |
| 5 | Backend compatibility enforcement | `scripts/resource_policy.py`, hooks in `qwenctl.py`, `newton_models.py`, `fishctl.py` | verified (§8) |
| 6 | Integration with Newton AI Control | Voice Studio card; `/voice`, `/api/voice/*`, `/api/fish/*` | verified on the second instance; production pending restart (§15.1) |
| 7 | Browser-based Voice Studio | `scripts/voice-studio/`, `scripts/voice_studio.py` | as 6 |
| 8 | Private recording and upload | AudioWorklet recorder; ingestion `scripts/fish_audio.py` | verified (§9); a real microphone awaits the operator |
| 9 | Profiles and references | `scripts/voice_store.py` | verified (§9) |
| 10 | Generation and playback | progressive playback in the page | verified (§9) |
| 11 | Take history and download | takes, A/B, download | verified (§9) |
| 12 | Local service interfaces | engine API (docs/18 §8), Voice Studio API | verified (tests, §9) |
| 13 | Unit and integration tests | `tests/test_resource_policy.py` (41), `test_voice_studio.py` (31), `test_fishctl.py` (7); `prove-fish-policy.py`, `prove-fish-adapter.py` | §10 |
| 14 | Regression evidence | §10 | verified |
| 15 | Live M2 Ultra inference evidence | `benchmarks/fish/adapter-proof.json`, `bench-fish-only.json` | §3.1, §6 |
| 16 | Resource and latency benchmarks | §5–§7 | Fish alone verified; with the 27B pending approval |
| 17 | Recording instructions | docs/18 §5.2–5.3, and on the page | done |
| 18 | Installation and operational runbook | docs/18 | done |
| 19 | Rollback and troubleshooting | docs/18 §10–11 | done |
| 20 | Voice-gateway specification | docs/20 | done (specification only) |
| 21 | GoHighLevel requirements | docs/21 | done (requirements only) |
| 22 | Final completion report | this document | done |

## 3. Model, runtime and versions

| | |
|---|---|
| Checkpoint | `cguldogan/drama-3-mlx-q4` at `5331ab509359c0e4a7e353a19e255ddf3e8152a8`: 16 files, 25,888,559,494 bytes, each hashed in `models/fish-drama3-mlx-q4/MANIFEST.json` (manifest SHA-256 `4257e438…0583`, which the running engine also reports) |
| Official model | `fishaudio/drama-3` at `e0a9218aa48888270f06ef1ed9ea67622c0e17d2`; not downloaded |
| Inference implementation | `cguldogan/mlx-audio`, branch `drama3-mlx`, commit `e7488935ec2ebfafd38ef2b58e290d79f3d05dc7`, one commit on `Blaizzy/mlx-audio` v0.5.9 (`0fe0b9cd`); installed unmodified from `git archive` |
| Python | 3.12.2 (python.org framework build), venv `tools/fish-venv` |
| Packages | mlx 0.31.2, mlx-metal 0.31.2, mlx-audio 0.5.9, numpy 2.3.5, scipy 1.17.0, transformers 5.19.0, tokenizers 0.23.3, safetensors 0.8.0, huggingface_hub 2.2.0; 37 in all, each pinned by hash in `config/fish-requirements.lock.txt`; install record `tools/fish-venv/newton-fish-install.json` |
| Metal | Apple M2 Ultra (`applegpu_g14d`), recommended working set 107.5 GiB |
| ffmpeg | 8.0.1 (Homebrew); optional, decodes non-WAV uploads |
| Licence | Fish Audio Community License v1.0, last updated 2026-10-13; `LICENSE` SHA-256 `733a3244…c823e` (docs/17 §1, docs/18 §9) |
| Sampling defaults | temperature 1.0, top-p 0.9, top-k 30: the developer's published settings, and the pinned `generate()` defaults |

No local patch to the port. Newton_Local's adapter
(`scripts/fish_tts/adapter_fish_omni.py`) drives the port's own building
blocks (`split_text`, `History`, `build_chunk_prompt`, `generate_frames`, the
codec) to add cancellation between frames, exact timings and streamed
decoding; §3.1 shows it produces what the port produces.

### 3.1 Adapter proof on the real model (`benchmarks/fish/adapter-proof.json`)

| Check | Result |
|---|---|
| No reference: adapter vs the port's `generate()`, same seed | identical codes and identical audio (56 frames) |
| With a reference (the model's own no-reference voice, 3.7 s, so no person's voice) | identical codes |
| Two text chunks with history | identical codes (395 + 102 frames) |
| Streamed decoding vs decoding each chunk whole | largest difference 2.7 × 10⁻⁵ of full scale over 1,017,856 samples |
| Progressive vs not progressive delivery (both streamed) | bit-identical audio |
| Cancellation | generation stopped 0.27 s after the request |
| Five repeated generations | all ended by themselves (EOS); peak 25.72 GiB each time |
| One chunk run to the 600-frame cap: whole vs streamed decoding | same codes; peak model memory 34.58 vs 25.74 GiB; 19.8 vs 19.2 s |

The command is in docs/18 §12.

## 4. Disk

| Item | On disk |
|---|---|
| Model pack `models/fish-drama3-mlx-q4` | 24.11 GiB (25,281,908 KiB, of which 68 KiB is download metadata in `.cache`) |
| Runtime `tools/fish-venv` | 520 MiB |
| Runtime source `tools/fish-src` | 25 MiB |
| Hugging Face cache | nothing added: the download went straight into the pack directory |
| **Total** | **24.6 GiB**, one copy of each |
| Free afterwards on the data volume | 438 GiB of 926 GiB |

No Q8 and no other Fish variant was downloaded; `models/` holds the two Qwen
packs and this one.

## 5. Memory (Fish alone; `benchmarks/fish/bench-fish-only.json`)

Before the run: 76.0 GiB free for models (free memory as the model engines
count it), Metal allocation 3.5 GiB, pressure normal, 16 MiB of swap in use.
Other resident workloads: 57 GiB in all, seven processes over 1 GiB, the
largest 5.3 GiB. No Qwen model was running.

| State | MLX (engine) | Process RSS | Metal allocation | Free for models |
|---|---|---|---|---|
| Fish stopped | — | — | 3.51 GiB | 75.97 GiB |
| Loaded, idle | 24.38 GiB active | 24.49 GiB | 30.60 GiB (+27.09) | 49.03 GiB (−26.94) |
| Generating (all 22 progressive runs) | peak 25.73–25.78 GiB | — | peak 32.56–32.86 GiB (at most +29.35) | — |
| One chunk at the 600-frame cap | peak 25.75 GiB | — | peak 32.64 GiB | — |
| After Stop (0.12 s) | — | — | 3.46 GiB | 76.29 GiB |

Pressure stayed normal and swap stayed at 16.12 MiB throughout. Stop released
everything at once: no process, no listening port.

**Admission figure.** `config/fish.json` `memory.required_gib` is **30.0 GiB**,
the measured peak (+29.35 GiB of Metal allocation) rounded up. A start also
needs 4 GiB of headroom on top of it, normal pressure, and room under the GPU
working-set limit (docs/18 §4). It replaces a provisional 32 GiB taken from the
model card.

**Before decoding was streamed** (engine `e2c44e9`,
`bench-fish-worst-case-1cfb20a.json`): decoding a whole chunk at once peaked at
34.48 GiB MLX / 42.59 GiB Metal for a 600-frame chunk, and at 33.11 / 41.06 GiB
for the long text in ordinary 300-byte chunks. See §6.4.

## 6. Performance (Fish alone)

Measured through the production service (fishctl and the engine's API, as
Voice Studio uses them), engine commit `b2ef8c5`. Voice reference: the model's
own no-reference voice, 12.5 s, made with a fixed seed (nobody's voice).
Sampling 1.0 / 0.9 / 30; seeds 1000–1004; progressive delivery in 11-frame
segments (0.51 s). Texts and every run: `bench-fish-only.json`.

### 6.1 Lifecycle

| | |
|---|---|
| Cold start, 24.11 GiB of model files evicted from the page cache first | **6.9 s** from `fish start` to ready with identity verified (engine load 5.0 s, warm-up 0.4 s) |
| Warm start, files in the page cache | **3.7–4.1 s** (load 2.2 s, warm-up 0.4 s) |
| Stop | **0.12 s**; memory released at once (§5) |

### 6.2 Speech

Median, with [min–max], over 5 runs (2 for the long text). Every run used the
same 12.5 s reference.

| Text | Audio | First frame | First audio, engine | First audio, client | Total | Real-time factor | Stalls |
|---|---|---|---|---|---|---|---|
| short (6 words) | 2.1–2.6 s | 0.65 s | 0.92 s [0.92–0.94] | 0.93 s [0.93–0.95] | 2.0 s | 1.19× [1.08–1.23] | 0 |
| question (13 words) | 3.4–3.8 s | 0.65 s | 0.93 s [0.92–0.96] | 0.95 s [0.93–0.98] | 2.8 s | 1.30× [1.26–1.35] | 0 |
| medium (27 words) | 9.2–11.0 s | 0.67 s | 0.95 s [0.94–1.03] | 0.96 s [0.95–1.05] | 6.7 s | 1.56× [1.56–1.58] | 0 |
| numbers (22 words: an order number, a date, times, a price) | 13.3–14.3 s | 0.76 s | 1.04 s [1.03–1.06] | 1.05 s [1.05–1.07] | 8.7 s | 1.58× [1.55–1.61] | 0 |
| long (112 words, 3 chunks) | 46.1–46.7 s | 0.68 s | 0.96 s [0.95–0.97] | 0.97 s [0.96–0.97] | 31.8 s | 1.46× [1.45–1.47] | 0 |

* **First frame**: the first acoustic token (one frame is 2,048 samples,
  46 ms). **First audio, engine**: the first decoded 11-frame segment exists.
  **First audio, client**: a loopback HTTP client polling `/pcm` every 20 ms
  has it, which is the time to first playable audio before any player
  buffering. In the browser, the first audio was heard 1.09 s after pressing
  Generate, including the first encoding of the reference (§9).
* **Stalls**: whether, once playback starts at the first arrival, any later
  audio arrives too late to play without a gap. None in 22 runs.
* Generation runs at 34–38 frames per second against 21.5 frames per second
  of audio. Short texts have the lowest real-time factor because the fixed
  ~0.65 s before the first frame weighs most.
* The engine used about 0.9 CPU-seconds per second of synthesis, one core;
  the GPU does the work.
* No failures and no quality flags (run-on, early end, frame cap): every
  chunk ended by itself.

### 6.3 Takes not delivered progressively, and reproducibility

The medium text without progressive delivery (3 runs): 6.1–7.0 s, the same
peak memory, and the client has audio only when the take is complete
(median 6.9 s). The same seed gave the same audio twice, for this take and for
the reference voice.

### 6.4 Why every take is decoded in streamed steps

The engine first decoded a whole text chunk at once unless the client asked
for progressive audio. That peaked 8.8–9.0 GiB higher (§5; at the frame cap
the adapter proof measured 34.58 vs 25.74 GiB). Streamed decoding is now the
only path (commit `c4dae52`): the same codes, audio within 2.7 × 10⁻⁵ of a
whole decode, no slower (17.3 s for the 600-frame chunk before and after), and
a take's audio no longer depends on how it is delivered (the frame-cap take
had the same SHA-256 both ways).

### 6.5 References

Encoding the 12.5 s reference the first time: 0.24 s. Later jobs only name its
hash (1 ms). The engine keeps up to 32 encoded references until it stops.

### 6.6 The frame cap

One chunk run to the 600-frame cap (27.9 s of audio): 17.3 s, real-time factor
1.6, first audio 1.17 s with progressive delivery (1.20 s at the client), no
stalls, flagged `no eos` as it should be (the chunk was cut, not finished).
With the default 300-byte chunks the same text stayed well under the cap
(288, 498 and 207 frames).

### 6.7 Fish + Qwen 27B: not measured

Running both is a GPU-heavy concurrency test that needs the operator's
approval (commission §11.2, §19); it is PENDING OPERATOR ACTION (§15.2).
`scripts/bench-fish.py --qwen-probe` is ready for it: with the 27B already
running (the script never starts or stops Qwen) it measures the 27B's time to
first token and decode rate alone and while Fish generates, Fish's latency
meanwhile, and memory.

What the policy would decide today, from the measurements above: with Fish
loaded, 49.0 GiB is free for models, and the 27B needs 56.0 GiB (its
registered 52 GiB plus 4 GiB of headroom), so **the 27B would be refused next
to a running Fish by about 7 GiB** while the other workloads hold their
present 57 GiB. The other order could not be computed without loading the
27B. The policy subtracts the 27B's 27.9 GiB of memory-mapped weights from free
memory on the assumption that the engine formula counts them as file cache;
if macOS instead counts them as wired while the GPU holds them, they are
subtracted twice and the policy is stricter than it needs to be (never
looser). The approved benchmark would settle both points.

## 7. Streaming and conversational latency

What the pinned port offers, and what Newton_Local built on it:

| Capability | Port | Newton_Local |
|---|---|---|
| Incremental generation | a frame generator (46 ms per frame); `generate()` itself returns audio per text chunk | drives the frame generator directly |
| Audio chunk delivery | none for Drama 3 (README: no streaming within a chunk) | the codec's `streaming_step` decodes every 11 frames; `/v1/jobs/<id>/pcm?from=N` serves them as they exist |
| Cancellation, interrupting generation | none | between frames; measured 0.26–0.27 s |
| Reusing an encoded reference | `ref_codes` accepted | content-addressed cache per engine run: 0.24 s once, then 1 ms |
| Short utterances | — | 0.92 s to first audio for a 6-word line |

Limits that matter for a telephone conversation (docs/20):

* **About 0.65 s before the first frame** with a 12.5 s reference. The
  reference is part of every chunk's prompt, so its length costs time on every
  job: 0.09 s with no reference and 0.32 s with a 3.7 s reference (adapter
  proof, an 11-word line), 0.65 s with 12.5 s (benchmark). Ten more frames and the first decode bring the first
  audio to about 0.92 s.
* **Thin margin for short lines.** At 1.1–1.2× real time a short line is
  generated only a little faster than it plays; progressive playback had no
  gaps here, but a slower machine state (for example the 27B generating at the
  same time, §6.7) could create them.
* **Polling transport.** Clients poll `/pcm`; the 20 ms polls here added about
  15 ms. A push stream would remove the polling delay (docs/20 §5).
* **Fixed text per job.** A job's text cannot grow while it runs, so a gateway
  must cut LLM output into sentences, one job each.
* **One job at a time.** One MLX worker; up to four jobs queue.
* **No carry-over between jobs.** Separate jobs do not share the prosody
  context that chunks of one job share (docs/20 §5.6).

Native audio is 44.1 kHz; telephone audio is 8 kHz μ-law. Nothing here
predicts how the clone sounds over a phone line (docs/20 §4).

## 8. Resource policy and lifecycle

**Policy and lifecycle proof** — `scripts/prove-fish-policy.py`, on `main` at
`b22915b`: real controllers, gatekeepers, launchd jobs and Newton AI Control
in a throwaway root, with fake engines (no model loaded). Evidence:
`benchmarks/fish/policy-proof.json`. **65 of 65 checks passed.**

| Scenario | What was shown |
|---|---|
| Matrix | Fish alone, 27B alone, Flash-Next alone start; Fish + 27B admitted on memory; Flash-Next refused beside Fish (slot A and slot B, exit 10, nothing spawned); Fish refused beside Flash-Next (exit 10, nothing spawned); a refused start leaves Fish desired STOPPED |
| Memory guard | a requirement larger than free memory is refused in plain words; nothing spawned |
| Races (0.1 s process sampler) | simultaneous Fish and Flash-Next starts (6 rounds), Fish vs a slot-B acquire (3), a Fish restart vs a Flash-Next start (3): one side started and the other was refused every time; Fish vs 27B (3): no deadlock, every start ended with a defined outcome |
| Recovery | a pending Fish recovery blocks Flash-Next; a pending Flash-Next recovery blocks Fish; a pending 27B recovery does not; a desired=RUNNING record from an earlier boot is ignored |
| Stale reservations | STARTING records left by dead controllers block nothing and are reconciled |
| Backstops | a direct `launchctl kickstart` cannot bypass the policy: both gatekeepers refuse (exit 78) before anything loads |
| Lifecycle | Stop during a slow start: 0.8 s, start exits 9, desired STOPPED; an engine that dies while loading: exit 8 within 2.3 s with its own reason; a load error surfaced; a second start while READY does nothing; duplicate engines detected and refused (exit 5) |
| Panel watchdogs | the Qwen watchdog's recovery of Flash-Next is refused beside Fish; the Fish watchdog's recovery is refused beside Flash-Next and shown in plain words; an intentional stop makes the watchdog stand down |
| Invariant | in all 1,071 samples, a Fish engine and Flash-Next were never alive together (once, only the gatekeeper was alive while it refused) |

**Unit tests**: `tests/test_resource_policy.py` (41) covers the matrix,
presence rules, stale reservations, the memory arithmetic and the
gatekeepers; `tests/test_fishctl.py` (7) the desired state, including a
100-iteration race between a stop and a finishing start that always ends
STOPPED.

**Production lifecycle** (the benchmark, §6.1): starts cold and warm, stops in
0.12 s with memory back to baseline; the engine identity check (model, pack
revision, manifest hash, runtime commit, process = launchd process = socket
owner, network sandbox enforced) passed on every start.

**Off after reboot**: the LaunchAgent has RunAtLoad and KeepAlive false, and
the desired state carries the kernel boot time (a record from an earlier boot
reads STOPPED; shown by the prover). A physical reboot was not performed.

## 9. Voice Studio, used in a browser

Set-up: a second Newton AI Control from the branch on `127.0.0.1:18765`, with
its own Fish LaunchAgent (`com.newton.test.fishdev`, port 18770), state and
voice store inside the worktree, driving the real model; the in-app browser
of the development environment. The test voice was the model's own
no-reference voice.

| Step | Result |
|---|---|
| Panel card and `/voice` | status, memory, Start and Stop work; state names as in docs/18 §3 |
| Start / Stop Fish from the page | Start Fish to Ready; Stop during a start cancelled it ("Start cancelled; Fish is stopped.", desired STOPPED); Stop during a generation cancelled the take and freed the model (free for models 43 → 66 GiB) |
| Profile | created with a consent statement; renamed |
| Upload | the test voice posted through the same route the page's file picker uses (the automated browser cannot open a file dialog): accepted and analysed (3.7 s, speech-to-noise 42 dB, the expected "short reference" warning), original kept, model input derived |
| Transcript | edited and saved |
| Recording | the page's recorder with a synthetic tone in place of the microphone (no real audio captured): AudioWorklet capture at 48 kHz under the strict CSP, level meter live, browser speech processing off, preview, save; the server received 24-bit / 48 kHz and produced a 44.1 kHz model input (+4.2 dB, with sensible warnings); deleted through the page |
| Generate with "Play while it generates" | first audio heard 1.09 s after pressing Generate (including the reference's first encoding); a 7.9 s take finished in 5.3 s |
| Takes | details show text, settings, seed, recording and timings; "Same seed again" gave a byte-identical take (same SHA-256); A/B compare; star and note. Download and deleting a take were exercised through the same routes by the end-to-end tests, not clicked in the browser |
| Errors | not provoked on purpose in the browser; refusals, a stopped engine and bad uploads are reported in plain words by the API (end-to-end tests) and in the panel (policy prover, §8) |

Defects found in that run were fixed in `17d408c` (§11). A real microphone
needs the operator's permission in the browser and in macOS.

## 10. Regression

All on `main` at `b22915b` unless stated.

| Suite | Result |
|---|---|
| `tests/` unit and integration (Python 3.9.6 and 3.12.2) | **210 passed** on each (131 before this work, 79 new) |
| `scripts/test-model-registry.py` | 68 passed |
| `scripts/test-qwenctl-logic.py` | 42 passed |
| `scripts/prove-start-supervision.py` (real launchd, fake engine) | 40 passed |
| `scripts/prove-fish-policy.py` (§8) | 65 passed |
| `scripts/prove-fish-adapter.py` (real model, §3.1), at `c4dae52` | 8 passed |
| `scripts/prove-control-panel.sh` (live production panel, model off), run once at `e2c44e9` | 48 passed; see §17 |
| `scripts/fish verify --deep`, at the end | 16/16 files, 25,888,559,494 bytes re-hashed; runtime at the pinned commit |
| `install-hermes-integration.py --check` (read-only) | Hermes integration verified (upstream `41447a6d`) |
| Hermes, Open WebUI, Newton AI Control health | 200 on each |

Two regressions in the existing suites were found and fixed, both in the
provers rather than in production code: `prove-start-supervision.py` built
its fixture from a script list that predates the policy, so `qwenctl`, which
refuses when the policy cannot load, refused every fixture start (17 of 40
failed; `41774ec`); and one of its checks read a status snapshot that could
lag the operation by a moment (failed once in three runs; `e2c44e9`).

Not run: the live identity and agent proofs (`prove-runtime-identity.py`,
`prove-agent-completion.py`, `prove-action-recovery.py`), because they start
and switch the Qwen models, which needs the operator's approval. This work
changed nothing they exercise except the admission hook, which imposes no
condition while Fish is absent (unit tests and the matrix above).

## 11. Defects found and fixed during the work

| Defect | Fix |
|---|---|
| Whole-chunk decoding peaked about 9 GiB higher than needed | every take decoded in streamed steps (`c4dae52`) |
| A launch soon after the previous exit waited out launchd's 10 s throttle, so a failed load was reported after 10.7 s | `ThrottleInterval` 1 in the Fish plist (KeepAlive is false, so there is no respawn loop): 2.3 s (`19ff220`) |
| Take details showed "[object HTMLElement]" | the page's DOM helper flattens nested children (`17d408c`) |
| "Same seed again" could differ by rounding from the original | replays with the original take's decoding (`17d408c`); now moot, as all takes decode the same way |
| The status poll spent ~0.7 s of CPU on the policy view every time | refreshed every 15 s and after operations (`17d408c`) |
| The benchmark reported free pages as 0 and swap without a unit | fixed (`b2ef8c5`) |
| Prover issues (slot B not bootstrapped, the gatekeeper counted as an engine, command lines truncated before matching) | `19ff220` |

## 12. Known model defects and limitations

* **Run-on and early ends.** The port's authors report that cue-heavy lines
  miss on roughly 15–20 % of seeds. In these benchmarks (no cues) no chunk ran
  to the cap and none ended early. Takes are flagged `no eos`, `slow`, `fast`
  or `empty chunk` when it happens (docs/18 §5.6); there are no automatic
  retries.
* **Seeds.** A seed reproduces a take on this runtime and machine; across
  MLX versions or machines it may not.
* **Cues.** Only `[laughs]` and `[sighs]` are documented by the pinned port;
  the page offers no others.
* **Several references.** Accepted (up to three), but nothing shows that
  combining recordings improves a clone; the page says so.
* **No watermark.** The pinned code applies none.
* **Community conversion.** The MLX port is a community project, one day old
  when pinned; Fish Audio does not endorse it.

## 13. Voice cloning: PENDING OPERATOR VOICE REVIEW

The cloning path works end to end with a permitted test voice (the model's own
voice, §3.1 and §9): references are recorded or uploaded, stored privately with
their transcripts, encoded locally, and every take records the exact recording
(model-input hash and transcript) it used. Whether the clone sounds
acceptably like the operator can only be judged by the operator, with the
operator's own recordings; no public sample was used to stand in for that.
This is not a technical failure and not a completed validation.

## 14. Acceptance criteria (commission §17)

| | Criterion | Status |
|---|---|---|
| A | Q4 installed locally · revision pinned · runtime isolated and reproducible (hash-locked; rebuilt from scratch with `install-fish-runtime.sh --recreate` during the work) · integrity verified · generation works on the M2 Ultra · no Q8 downloaded · no cloud fallback | verified |
| B | starts on demand · stops cleanly · duplicates prevented · failed starts handled · Stop cancels startup · memory reclaimed · existing LLM lifecycle preserved | verified |
| B | remains off after reboot | verified by construction and fixture; no physical reboot |
| C | Fish alone · 27B alone · Flash-Next alone permitted · Fish + Flash-Next rejected in both directions · races cannot bypass · watchdog recovery respects the policy · memory guards not bypassed | verified (§8) |
| C | Fish + 27B admitted only when safe | the admission rule is verified with fixtures; with real models it is part of the pending benchmark (§6.7) |
| D | accessible in Newton AI Control | on the second instance; production pending restart (§15.1) |
| D | status visible · start/stop in the browser · recording · upload · preview · transcripts · profiles · references · generation · playback · download · comparison · settings · clear errors | verified on the second instance (§9), with the recording and file-picker caveats there |
| E | recordings and audio local · excluded from Git · temporary files controlled · file access restricted · upload validation · mutation protections · no credentials · no network exposure | verified (docs/18 §6–7; tests; only loopback listeners) |
| F | Fish-only memory · cold start · warm latency · first playable audio · real-time factor · reclamation · streaming assessed · Flash-Next exclusion verified safely · limitations documented | verified (§5–§8) |
| F | Fish + 27B benchmarked if authorised | pending authorisation (§15.2) |
| G | cloning infrastructure · operator samples supported · references and transcripts preserved · takes keep their reference · the operator can evaluate | verified with a test voice; available to the operator once Voice Studio is live |
| G | personal voice similarity | **PENDING OPERATOR VOICE REVIEW** |
| H | regression · documentation · reproducible install · rollback · revisions · licences and attribution · gateway interfaces · limitations | done (§3, §10, §12; docs 17–21) |

## 15. Pending operator actions

1. **Restart Newton AI Control** so Voice Studio goes live in production
   (`launchctl kickstart -k gui/$(id -u)/com.newton.control`). The panel is
   unreachable for a second or two. The running process dates from
   2026-10-12; the restart loads Voice Studio's routes, the Fish status behind
   the panel card and the Fish watchdog. (The panel page and the Qwen
   controller are already read fresh from disk: today the card says Voice
   Studio is not active yet, and Qwen starts already go through the policy.)
   Qwen and Fish are stopped, so nothing else changes. Needs approval
   (commission §19).
2. **Fish + Qwen 27B benchmark** (§6.7). Needs approval. With today's other
   workloads the policy would refuse the pair by about 7 GiB; freeing that much
   (for example by closing large applications) first would let it run.
3. **Record your voice** in Voice Studio (docs/18 §5.2–5.3), generate the same
   text from each recording, and decide whether the clone is acceptable.
4. **Licence.** Commercial use, including a business's internal operations,
   is free only below USD 500,000 total annual revenue (docs/18 §9). Confirm
   before any commercial use.
5. Optional: exclude the voice store from Time Machine (docs/18 §6); a
   physical reboot test at a convenient time.

## 16. Future engineering dependencies

* Speech-to-text: none is installed; a local streaming model must be chosen,
  approved and measured next to Fish and the 27B (docs/20 §6, §10).
* The engine: a push stream for job audio and optional carry-over of the
  previous turn (docs/20 §5).
* Measurement of the clone over the telephone chain (8 kHz μ-law, docs/20 §4).
* Fish + 27B co-residency under real workloads (§6.7), which decides whether a
  local LLM can serve calls while Fish speaks.
* Telephony ownership, consent and calling-law review, and the GoHighLevel
  credential procedure (docs/20 §3, §7; docs/21).
* Q8: a new entry in `config/fish.json` with its own manifest and measured
  memory figure, on the same adapter, if the operator ever authorises it.

## 17. Disclosures

* `prove-control-panel.sh`, an existing regression prover, was run once
  against the production panel during this work (at 13:53). Its selection test
  changes the global model selection from Qwen 3.8 27B to Flash-Next and back
  while both slots are stopped (1.2 s; nothing was started) and restores it.
  Changing the selection needs the operator's approval under commission §19,
  and that approval had not been asked for. The selection is `qwen27b`, as
  before. The prover was not run again.
* No Qwen model was started, stopped or restarted; the production control
  service was not restarted; no Q8 or other model was downloaded; no
  credential store was read; Twilio, GoHighLevel and the existing telephony
  project were not accessed; nothing was pushed.

## 18. Git

Commits `da53304`…`b22915b` on `fish-drama3-voice-studio`, fast-forwarded into
`main`; this record is committed on top. Model weights, the runtime, logs,
state and the voice store are outside Git.
