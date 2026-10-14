# 18 — Fish speech service and Voice Studio: runbook

Newton_Local runs **Fish Audio Drama 3 (MLX Q4)** on this Mac as a local,
on-demand speech service, and **Voice Studio** — a page of Newton AI Control —
records, clones and compares voices with it. Everything runs on 127.0.0.1;
recordings, transcripts and generated speech never leave the Mac.
**Built with Fish Audio.**

Plan and audit: docs/17. Measurements and the completion record: docs/19.
Future telephone assistant: docs/20. Future calendar integration: docs/21.

## 1. What is where

```
 browser ──▶ Newton AI Control :8765 ──▶ voice_studio.py ──▶ voice_store.py (private folder)
                    │                          │  ├──▶ fish_audio.py (sandboxed ingestion)
                    │                          │  └──▶ engine API :8770 (bearer token)
                    │                          └──▶ fishctl.py ──▶ launchd com.newton.fish
                    └──▶ qwenctl.py (Qwen)          │                  └▶ fishctl exec-engine (gatekeeper)
                                                    │                      └▶ sandbox-exec ▶ fish_engine.py
                       resource_policy.py ◀─────────┴── both controllers decide here, under one lock
```

| Piece | File(s) | Role |
|---|---|---|
| Configuration | `config/fish.json` | the Fish registry: service ports and paths, the pinned runtime, the model (revision, limits, license, memory), the compatibility table, admission margins |
| Runtime | `tools/fish-venv` (built by `scripts/install-fish-runtime.sh`) | isolated Python 3.12.2 venv, hash-locked deps (`config/fish-requirements.lock.txt`), mlx-audio built from pinned commit `e7488935` |
| Model | `models/fish-drama3-mlx-q4/` (24.11 GiB; `MANIFEST.json`, `MANIFEST.md`) | the only Fish variant installed |
| Engine | `scripts/fish_engine.py`, `scripts/fish_tts/` | loads the model once; serves reference encoding, jobs, progressive audio, cancellation; runs under `config/fish-engine.sb` (no network) |
| Lifecycle | `scripts/fishctl.py` (wrapper `scripts/fish`), LaunchAgent `com.newton.fish` | start / stop / restart / status / identity / verify; desired state; off after reboot |
| Shared policy | `scripts/resource_policy.py` + hooks in `qwenctl.py`, `newton_models.py` | Fish vs the Qwen models (§4) |
| Voice Studio | `scripts/voice_studio.py`, `scripts/voice_store.py`, `scripts/voice-studio/` | the page, its API and the private store |
| Ingestion | `scripts/fish_audio.py` → `fish_tts/audio_tool.py` | validates and converts reference audio |
| Proofs | `scripts/prove-fish-adapter.py`, `scripts/prove-fish-policy.py`, `scripts/bench-fish.py`, `tests/test_*` | §12 |

## 2. Install (reproducible)

Prerequisites: Apple Silicon, macOS 26.x, the python.org **Python 3.12.2**
framework build (`/Library/Frameworks/Python.framework/Versions/3.12`), about
25 GiB free for the model plus a 20 GiB margin. `ffmpeg` (Homebrew) is
optional; without it Voice Studio accepts WAV only.

```bash
scripts/install-fish-runtime.sh
```
```bash
scripts/download-fish-model.sh
```
```bash
scripts/fish verify --deep
```
```bash
scripts/install-fish.sh
```

1. `install-fish-runtime.sh` checks the base Python, exports the pinned
   mlx-audio commit (`git archive`, so the checkout stays clean), installs
   every dependency with `--require-hashes --no-deps --only-binary=:all:`,
   runs `pip check`, and writes `tools/fish-venv/newton-fish-install.json`
   (commit, lock hashes, package versions, Metal limits). `--recreate`
   rebuilds from scratch.
2. `download-fish-model.sh` downloads the pinned revision once, straight
   into `models/fish-drama3-mlx-q4/` (no shared cache), anonymously.
3. `fish verify --deep` re-hashes all 16 files against `MANIFEST.json`.
4. `install-fish.sh` renders `config/com.newton.fish.plist.template` into
   `~/Library/LaunchAgents/com.newton.fish.plist` (RunAtLoad and KeepAlive
   false), checks its argument vector against `fishctl shim-argv`, creates
   the private folders (0700), and bootstraps the job **without running it**.
5. Restart Newton AI Control once so it loads Voice Studio (its page and
   status card say so until then). The Fish lifecycle and the shared policy
   work without that restart.

## 3. Operating Fish

From the browser: **Newton AI Control → Voice Studio card**, or the page
`http://127.0.0.1:8765/voice`. From a terminal:

```bash
scripts/fish status --admission
```

| Command | Effect |
|---|---|
| `fish start` | admission (§4), then load; returns when the engine is READY and its identity is verified. Exit 10 = refused by the policy, 8 = the engine exited while loading (reason printed), 4 = timeout, 9 = cancelled by a stop, 6 = model or runtime not intact |
| `fish stop` | records STOPPED first, cancels a start in progress, stops the engine, confirms the process and port are gone, reports memory before/after |
| `fish restart` | stop then start under one lock; the start goes through admission again |
| `fish cancel` | abort a start in progress, return at once |
| `fish identity` / `fish health` / `fish logs` | which model is running (revision, manifest hash, runtime commit, pid = launchd pid = socket owner, sandbox enforced) / liveness / logs |
| `fish verify [--deep]` | model files and runtime intact and pinned |
| `fish admission [--qwen MODEL]` | preview the shared policy decision without starting anything |

**States** (Voice Studio wording): Stopped · Starting · Ready · Busy (a take is
being generated) · Stopping · Failed · Recovering.

**Off after reboot.** The LaunchAgent never runs at load. The desired state
(`state/fish/desired.json`) carries the kernel boot time; a record from an
earlier boot reads as STOPPED.

**Intentional stops are final.** `fishctl` is the only writer of the desired
state: stop and cancel record STOPPED atomically with the cancel marker; a
successful start records RUNNING unless a stop arrived meanwhile; a failed
explicit start records STOPPED. Newton AI Control's watchdog restarts Fish
only while the record says RUNNING for this boot — up to three attempts
(0/60/180 s), each through admission — then records STOPPED and shows
Failed. A stop from the terminal therefore silences it as surely as one from
the page.

## 4. Shared resource policy

| Combination | Rule |
|---|---|
| Fish OFF + Qwen 27B | allowed — no new condition at all |
| Fish OFF + Flash-Next | allowed — no new condition at all |
| Fish ON + no LLM | allowed — memory admission |
| Fish ON + Qwen 27B | conditionally allowed — memory admission for whichever starts second |
| Fish ON + Flash-Next | **prohibited, both directions, whatever memory says** |

Anything not in the table, including a model process that cannot be
identified, is treated as prohibited (fail closed).

**Where it is enforced (backend, not buttons):** `fishctl start/restart`;
`qwenctl start/restart` and `qwenctl acquire` (slot B); both launch
gatekeepers (`fishctl exec-engine`, `newton_models.py exec-slot`) re-check the
prohibited pairs immediately before exec, so a raw `launchctl kickstart`
cannot bypass it; both watchdogs, because they recover through the same
controllers.

**Atomic.** Each controller decides and writes its STARTING reservation while
holding `state/resource-admission.lock`; the other side sees that reservation,
so two simultaneous starts can never both be admitted into a forbidden pair.
Lock order is own controller lock, then the admission lock, with only
non-blocking probes inside, so it cannot deadlock.

**What counts as "present".** A process (engine, runtime or gatekeeper), the
LaunchAgent running, a live STARTING/STOPPING record, or a desired=RUNNING
record for this boot (a recovery the watchdog may perform). A STARTING
record whose controller is gone (its lock is free) or whose deadline passed
does not count; neither does a record from a previous boot.

**Memory admission (measured, not nominal).** Free memory is computed as the
model engines compute it — physical memory minus wired, compressed and app
memory — minus the weights of any running llama.cpp model (memory-mapped, so
otherwise counted as reclaimable cache). A start is admitted only if free ≥
requirement + 4 GiB headroom, memory pressure is *normal*, and Metal's current
allocation plus the requirement stays 4 GiB below the GPU working-set limit.
Fish's requirement (`config/fish.json` `memory.required_gib`) is the measured
peak from docs/19, not the model card's figure. Nothing is ever forced into
swap and nothing else is stopped to make room.

**When a start is refused** the message says why in plain words, for example
*"Fish Audio Drama 3 (MLX Q4) can't start while Qwen 3.8 Flash-Next is running
(slot A). They are never allowed to run at the same time. Stop the AI model
first."* Exit code 10 on the command line; the same sentence in the panel.

## 5. Voice Studio

### 5.1 Profiles and consent
A voice profile is a named set of recordings of one voice. Creating one asks
whose voice it is: your own, or someone else's **with their documented
permission** (then: who gave it and where the record is kept). Only those two
are accepted; the Fish Audio Community License forbids imitating a person's
voice without consent.

### 5.2 Recording guidance (for the operator)
* **Quiet room.** Fans, music and TV off. Soft furnishings help; bare rooms echo.
* **Same microphone position each time**, about a hand-span (20–30 cm) from
  your mouth, slightly to the side so breaths don't pop.
* **Speak naturally** at your normal pace and volume, as if talking to someone.
  The clone copies how you sound in the recording, including tiredness or a
  performed voice.
* **8–15 seconds** of continuous speech per recording works best (at least
  3 s and at most 30 s after silence is trimmed).
* **Watch the level meter.** Peaks should stay out of the red; red means
  clipping — move back or lower the input gain.
* **Record several** (different sentences and moods), then generate the same
  text from each and keep the one that sounds most like you.
* **Type exactly what you said** as the transcript (§5.3).
* No studio is needed; a decent USB or headset microphone in a quiet room is
  enough. Recording in the browser turns off its echo cancellation, noise
  suppression and automatic gain so the reference is your real voice.

### 5.3 Transcript guidance
The model learns the voice by lining up your recording with its transcript;
a wrong transcript is the most common cause of poor cloning.
* Write every word you actually said, in order — including repeated words
  and fillers ("um") if you said them.
* Write numbers the way you said them ("twenty twenty-six", "a hundred and
  ten").
* Use ordinary punctuation where you paused or your voice rose or fell.
* No cues or brackets in transcripts.
* If you can't tell what you said, record again rather than guess.
* After **Replace…**, the old transcript is copied over and marked "check the
  transcript" — read it against the new recording.

### 5.4 Recordings: formats and the derived model input
Accepted uploads, recognised by content (never by file name): WAV (PCM 8–32
bit or float), FLAC, MP3, Ogg (Vorbis/Opus), M4A/AAC, WebM/Opus, CAF, AIFF;
at most 32 MiB and 120 s. The original is kept byte for byte. A separate
**model input** is derived (the original is never modified):

| Step | Method |
|---|---|
| Decode | WAV parsed directly; other formats decoded by ffmpeg at their native rate |
| Channels | averaged to mono |
| DC offset | mean removed |
| Silence | leading/trailing silence trimmed, keeping 0.25 s each side; nothing inside is cut |
| Resampling | to 44,100 Hz with `mlx_audio.resample.resample_audio_array` (scipy polyphase FIR) — the resampler the pinned model uses itself; its authors found linear upsampling made generations ramble |
| Level | one linear gain to a −1 dBFS peak, at most +20 dB, never upward if the original clipped; no compression or limiting |
| Output | `model-input.wav`, 24-bit PCM, 44.1 kHz, mono |

Each recording shows its duration, peak, noise floor, speech-to-noise ratio
and source format, with warnings for clipping, noise, low sample rate, lossy
sources and lengths outside 8–15 s. Silent and corrupt files are refused with
a plain message.

### 5.5 Generating speech
Choose a profile and recording, type text (up to 2,000 characters), then
**Generate**. With **Play while it generates** the audio starts after about a
second and continues as it is decoded.

| Control | Meaning | Default |
|---|---|---|
| Temperature / Top-p / Top-k | sampling randomness | **1.0 / 0.9 / 30** — the model developer's published settings |
| Seed | blank = random; a number makes a take reproducible | random |
| Takes | 1–4 takes in one go (seeds n, n+1, … when a seed is given) | 1 |
| Cues | `[laughs]`, `[sighs]` — the cues the pinned implementation documents. Cue-heavy lines miss on roughly 15–20 % of seeds and run on; generate again with another seed | — |
| Advanced: max frames per chunk | upper bound on one chunk's audio (600 frames ≈ 28 s) | 600 |
| Advanced: chunk size | long text is read in pieces of about this many bytes, each conditioned on the reference and the previous piece | 300 |
| Advanced: single chunk | read the whole text as one piece (short texts only) | off |

**Several recordings together (experimental).** The model's prompt accepts up
to three examples of the same voice (each tagged speaker 0). Whether that
sounds more like you than your best single recording is for you to judge; the
page makes no such assumption.

### 5.6 Takes
Every take is a new, immutable file — nothing is overwritten. Each keeps its
text, voice, recording(s) (with the model-input hash and transcript), every
setting, the seed actually used, the model revision, runtime commit and
timings. Play, Download (WAV, 16-bit, 44.1 kHz, mono), A/B compare (switch at
the same moment), star, notes, **Same seed again** (bit-identical replay),
**Edit as new**, Delete. Flags on a take:

| Flag | Meaning |
|---|---|
| `no eos` | a chunk hit the frame limit instead of ending by itself: may run on or be cut |
| `slow` | far fewer words per second than the text implies: may have run on past the script |
| `fast` | far more words per second: words may be missing |
| `empty chunk` | the model ended a chunk before producing audio |

Generation is bounded: one take at a time per request, a per-job time limit,
and no automatic retries.

## 6. Private data

| What | Where | Protection |
|---|---|---|
| Profiles, recordings (original + model input), transcripts, takes | `~/Library/Application Support/Newton_Local/voice-studio/` (`config/fish.json` `service.store`) | folders 0700, files 0600; Spotlight indexing off; outside every repository; `.gitignore` also blocks audio files and copied stores |
| Upload staging | `<store>/tmp/<id>/` | created per upload, removed after; emptied at every start of the control service; the audio tool's temporary files go there too |
| Reference encodings | engine memory only (up to 32, least recently used dropped) | gone when Fish stops |
| Finished jobs (audio) | engine memory, 15 min / last 16 | gone when Fish stops; takes are saved by Voice Studio |
| In the browser | page memory and blob URLs only; the session token is never stored | gone when the page closes |
| Logs (`logs/control/voice.log`, `logs/fish/`) | ids, sizes, durations, timings, error kinds | **never text, transcripts, names or audio** |

**Backups.** Time Machine backs up `~/Library/Application Support` by default,
so the store is included in backups (as encrypted as the backup disk is). To
exclude it:

```bash
tmutil addexclusion "$HOME/Library/Application Support/Newton_Local/voice-studio"
```

**Deleting.** Delete in Voice Studio removes the files. On an SSD with APFS
deleted data is not overwritten in place; FileVault keeps it encrypted at
rest. Deleting a profile deletes its recordings; takes are kept until deleted
(each take keeps a copy of the transcript it used).

## 7. Security model

* Loopback only. The engine binds 127.0.0.1:8770 and runs under a Seatbelt
  profile that denies all outbound network and DNS (verified at start:
  `network_sandbox: enforced` in its identity).
* **Engine API**: a fresh 256-bit bearer token per start (0600 file, read only
  by the controller and Voice Studio), loopback Host header required, and any
  request with an `Origin` header refused — a web page can never drive it.
* **Voice Studio API**: Host allow-list (DNS rebinding); every route needs
  the `X-Newton-Control` header, a same-origin `Origin` and `Sec-Fetch-Site`
  (other web pages cannot send or read; the CORS preflight that header would
  need is never answered). Private routes also need a **session**, issued
  only after `lsof` shows the program on the other end of the TCP connection
  runs as this Mac's operator account — other accounts and system services
  are refused even on 127.0.0.1. The token lives in page memory and travels
  in a header, never a cookie (cookies are shared by every 127.0.0.1 port).
  What this does not do: processes running as the operator account are
  trusted, as they can read the store's files directly anyway.
* The page runs under a strict Content-Security-Policy (no inline code, no
  framing); user text is inserted only as text, never as HTML.
* Bodies need `Content-Length` and are bounded (JSON 64 KiB, audio 32 MiB);
  ids are server-generated and validated; no request names a path; the audio
  tool runs sandboxed; error replies never include audio, transcripts or text.

## 8. Internal service interface (for future Newton components)

Engine (`127.0.0.1:8770`, bearer token from `state/fish/engine.token`, no
`Origin` header):

| Request | Purpose |
|---|---|
| `GET /health` | `{state: loading/ready/failed/stopping, busy}` (no auth) |
| `GET /v1/identity` | model id, revision, manifest hash, runtime commit, pid, sandbox |
| `GET /v1/status` | memory (MLX active/peak/cache), queue, load timings |
| `PUT /v1/references/<sha256>` | 44.1 kHz mono WAV, 3–30 s → encoded once, cached |
| `POST /v1/jobs` | `{text, references:[{sha256, transcript}], settings, progressive:{segment_frames}}` → 202 job; 409 if a reference is not cached; 429 if 4 are queued |
| `GET /v1/jobs/<id>` · `/audio` · `/pcm?from=N` | progress and result · WAV · progressive PCM16 after sample N |
| `POST /v1/jobs/<id>/cancel` | stops within one frame |

Voice Studio's own routes are listed at the top of `scripts/voice_studio.py`.
A separately authenticated application API for other Newton apps can be added
in front of the engine later; it must keep the engine loopback-only and must
not become a hosted service for third parties (§9).

## 9. Licensing and attribution

**Model: Fish Audio Community License, Version 1.0** (the `LICENSE` file
distributed with the pinned checkpoint, "Last Updated: October 13, 2026",
SHA-256 recorded in `config/fish.json`; the model card agrees). In short —
the license text governs:

* **Research and non-commercial use** (including personal/hobby use,
  evaluation and testing) is free.
* **Commercial use** — which includes a business's *internal operations* — is
  free only while the licensee's total annual revenue (with affiliates, from
  all sources) is below **USD 500,000**; reaching it requires notifying Fish
  Audio within 30 days and a separate licence after a 90-day transition.
* **Hosted services** (letting third parties generate speech or clone voices
  from text or audio they supply) always need a separate licence. Generating
  speech for content you control — explicitly including *an assistant's
  spoken replies* — is not a hosted service.
* **Restrictions**: no imitation of an identifiable person's voice without
  their consent; no impersonation to deceive, defraud or harass, and no calls
  that violate telemarketing or robocall laws; no removal of watermarks or
  provenance signals; outputs may not be used to train other speech or
  generative models.
* **Attribution**: "Built with Fish Audio" must be displayed prominently where
  a product that uses the model is made available — shown in Voice Studio,
  the Newton AI Control card, the README and these docs. Copies of the
  weights are not distributed by this repository (only manifests); anyone
  who does distribute them must include the licence and the `NOTICE` text.
* **Trademarks**: factual references to "Fish Audio Drama 3" only; no logos;
  no implied endorsement. The MLX conversion is a community project that Fish
  Audio does not endorse.

**Inference code:** the `cguldogan/mlx-audio` port is published under MIT
(base mlx-audio v0.5.9, MIT). **Reported ambiguity:** its Drama 3 prompt
builder and chunker say they port Fish Audio's own `fish_omni` code, and the
Community License covers Fish Audio's software ("Fish Speech source code") as
well as the weights. If those parts count as derivative works of Fish Audio
software, the Community License applies to them too. Newton_Local does not
redistribute the port (it is fetched at install time into the git-ignored
`tools/fish-src`), and its private use here is permitted under either reading,
so nothing is blocked; a future commercial or distributed use should have the
question answered first.

**Operator's responsibility:** before any commercial use (for example business
calls), confirm the revenue condition above.

## 10. Rollback

1. Stop and remove the LaunchAgent (keeps model, runtime, state and voice store):
   ```bash
   scripts/uninstall-fish.sh
   ```
2. Remove the shared policy hooks, if ever needed, by reverting the policy
   commit (`feat(policy): shared resource admission …`); `qwenctl.py` and
   `newton_models.py` return exactly to their previous behaviour.
3. Voice Studio: reverting the Voice Studio commits and restarting Newton AI
   Control removes the page and card; the panel is unaffected otherwise.
4. By hand, only if wanted: `models/fish-drama3-mlx-q4` (24 GiB),
   `tools/fish-venv`, `tools/fish-src`, `state/fish`, `logs/fish`, and the
   voice store folder (§6) — the last contains the operator's private data.

## 11. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Start refused: "never allowed to run at the same time" | Flash-Next (or an unidentified model) is running or set to recover. Stop the AI model in Newton AI Control, or choose Qwen 3.8 27B. |
| Start refused: not enough free memory / pressure / GPU working set | Measured headroom is too small. Close memory-heavy apps or stop the other model. `fish admission` shows the numbers. |
| Start refused: "set to run (it is recovering)" | A desired=RUNNING record for this boot exists for the other service. Stop that service explicitly. |
| "the engine exited — …" | The engine's own last error is shown; full text in `logs/fish/stderr.log`. |
| "not available — runtime built from …" / "lock changed" | The runtime no longer matches the pin: `scripts/install-fish-runtime.sh --recreate`. |
| "weights missing / size mismatch" | `scripts/fish verify --deep`; re-run `scripts/download-fish-model.sh`. |
| "identity verification failed" | Something other than this start answered on :8770, or the engine is a different model/revision. `fish stop`, check `lsof -iTCP:8770`. |
| "several engine processes exist" | `fish stop` (it stops and verifies all of them). |
| Voice Studio says the control service must be restarted | Restart `com.newton.control` once after installing. |
| "Voice Studio refused this connection" | The browser is running under another macOS account, or `lsof` could not identify it. Use the operator's own session. |
| Microphone blocked | Allow it for 127.0.0.1 in the browser's site settings and for the browser in System Settings › Privacy & Security › Microphone. |
| Upload refused: "ffmpeg is needed" | Install ffmpeg (`brew install ffmpeg`) or upload WAV. |
| A take runs on, repeats or stops early | Check its flags; generate again with another seed; keep cue-heavy lines short. |
| Restart is slow | First loads after a reboot read 24 GiB from the SSD; later loads come from the file cache (docs/19). |

## 12. Tests and proofs

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```
```bash
scripts/prove-fish-adapter.py
```
```bash
scripts/prove-fish-policy.py
```
```bash
scripts/bench-fish.py --cold
```

* Unit and integration tests (standard library; the end-to-end Voice Studio
  class drives the real sandboxed audio tool and the real engine in fixture
  mode).
* `prove-fish-adapter.py` — the adapter reproduces the port's own generation
  bit for bit, progressive decoding matches whole decoding, cancellation is
  prompt (real model).
* `prove-fish-policy.py` — the whole policy and lifecycle against real
  launchd jobs with fake engines (no model is loaded): matrix, refusals in
  both directions, concurrent-start races with a process sampler, recovery,
  stale reservations, launchd bypass attempts, stop during start, engine
  failures, duplicates, both watchdogs. Evidence:
  `benchmarks/fish/policy-proof.json`.
* `bench-fish.py` — measurements on the real model (docs/19).
