# 17 — Fish Audio Drama 3 (MLX Q4) and Voice Studio: audit and plan

**Commission:** NEWTON_LOCAL_FISH_DRAMA3_001, revision 2.0 · **Date:** 2026-10-14 ·
**Status:** plan, written after the Phase 1 audit and before any installation.
The runbook is [18](18-fish-voice-studio.md); the completion record is
[19](19-fish-completion-record.md).

*As built (2026-10-14):* implemented as planned, with three differences. The
page lives in `scripts/voice-studio/` (`index.html`, `app.js`, `style.css`,
`recorder-worklet.js`); the engine decodes every take in short streamed steps
rather than only progressive ones, because decoding a whole chunk at once
needed about 9 GiB more memory (docs/19 §6); and free memory is not reduced
by the 27B's mapped weights (decision 2), because Metal wires them and the
engine formula already counts them (docs/19 §6.7).

Built with Fish Audio.

## 1. Audit findings

### Machine (read-only, 2026-10-14 12:00)

| | |
|---|---|
| Host | Mac Pro, M2 Ultra (24 CPU / 76 GPU cores), 128 GiB, Metal 4 |
| OS | macOS 26.6.2 (25G83) |
| Disk | 463 GiB free on the internal SSD |
| Memory | 80.3 GiB free as the model engines count it (wired 3.9, compressor 2.5, app 41.6 GiB); pressure normal; swap 15.6 MiB used |
| Other workloads | other applications the operator runs; none may be disturbed. |
| Model slots | A and B STOPPED; selection `qwen27b`; desired STOPPED |
| Python | `/usr/bin/python3` 3.9.6 (runs the control service; no numpy), python.org 3.12.2 framework (not managed by Homebrew), Homebrew 3.11.15 |
| Tools | ffmpeg 8.0.1 (Homebrew), cmake; no sox, uv, rustc, git-lfs |
| Baseline tests | 131 unit, 68 registry, 42 controller: all pass on Python 3.12 and 3.9 |

The C++ header defect from docs/00 is irrelevant here: every dependency
installs from a binary wheel and nothing is compiled.

### Model and runtime sources

| | Pinned to | Notes |
|---|---|---|
| Q4 checkpoint `cguldogan/drama-3-mlx-q4` | `5331ab509359c0e4a7e353a19e255ddf3e8152a8` | 16 files, 25,888,559,494 bytes (24.11 GiB). Modified today, so the revision pin matters. |
| Official model `fishaudio/drama-3` | `e0a9218aa48888270f06ef1ed9ea67622c0e17d2` | 35B parameters, 3B active MoE; FP8 experts; not downloaded |
| MLX implementation `cguldogan/mlx-audio`, branch `drama3-mlx` | `e7488935ec2ebfafd38ef2b58e290d79f3d05dc7` | one commit on upstream mlx-audio v0.5.9 (`0fe0b9cd`) |

What the pinned implementation actually provides (from reading its code):

- Model load from a local directory; `HF_HUB_OFFLINE` keeps it off the network.
- Reference cloning: `ref_audio` + `ref_text`, or pre-encoded `ref_codes`.
  References are resampled to 44.1 kHz with a polyphase FIR (linear
  resampling made generations ramble in upstream testing).
- Several references are accepted syntactically (one `[Reference N]` block
  each); nothing shows that combining recordings of one voice helps.
- Sampling: `generate()` defaults to the official 1.0 / 0.9 / 30; the CLI's
  defaults differ. Repetition-aware resampling is fixed by the recipe. The
  seed is MLX's global seed.
- Bounded length: text is chunked at about 300 bytes; each chunk stops at EOS
  or at 600 frames (27.9 s).
- Output: 44.1 kHz mono, per text chunk. **No streaming within a chunk** (stated
  in its README). The model does expose a frame-level generator, and the pinned
  codec has an incremental decoder (`streaming_step`, documented to match a full
  decode) that the same code base uses for S2 Pro streaming. Composing the two
  for Drama 3 is an engineering option to measure, not an upstream feature.
- Cancellation: none in the API. Between frames is possible when the caller
  drives the frame generator.
- No watermark or provenance signal is applied by the pinned code.

### Licence

Both repositories ship the same `LICENSE` file (SHA-256
`733a32442087acbda12ecafe8e71eb8ee5890c89b50ce7ac4ea3777faa3c823e`): **Fish
Audio Community License v1.0, last updated 2026-10-13**. Conflict found: the
official model card's front matter says `fish-audio-research-license` and links
`LICENSE.md`, which does not exist at the pinned revision (HTTP 404). The card's
own body, the licence file and the Q4 card all say Community License v1.0, and
§IV(h) of the licence says materials are licensed under the version stated "in
the license file accompanying them". The front-matter tag is treated as stale
metadata. The terms that matter here are summarised in the runbook.

### Existing infrastructure: the integration surface

- `qwenctl.py` runs slots A and B; `newton_models.py` owns the registry and the
  `exec-slot` launch shim; `newton_control.py` owns desired state, the watchdog
  and the HTTP panel. All of these are Qwen-specific by design.
- Reusable as they are: `qwenctl.Lock`, `TryLock`, `obs_launchd`, `obs_socket`,
  the cancel-marker functions, `read_log_since`, `diagnose_start_failure`,
  `newton_models.check_weights`, `vm_counts` and `engine_available_bytes`.
- Two small hooks are needed in production code: a shared admission check at
  the moment `qwenctl` writes STARTING (start, restart, acquire, which also
  covers the watchdog), and a launch-time backstop in `exec-slot`.

## 2. Decisions

1. **Fish is its own service**, not a Qwen slot: its own LaunchAgent
   (`com.newton.fish`, RunAtLoad and KeepAlive false), controller (`fishctl.py`),
   configuration (`config/fish.json`), state (`state/fish/`), logs (`logs/fish/`),
   environment (`tools/fish-venv`, Python 3.12.2) and port (`127.0.0.1:8770`).
2. **Shared resource policy** (`resource_policy.py`, standard library only,
   Python 3.9-compatible), called by both controllers:
   - Fish + Flash-Next is refused in both directions, whatever memory says.
     Unknown model processes are refused too (fail closed).
   - Fish + Qwen 27B: admitted only if measured free memory, minus the 27B
     weights it maps from disk, covers the newcomer's measured requirement plus
     a reserve, memory pressure is normal, and the GPU working-set limit holds.
   - Only one large model loads at a time: a start is refused while the other
     service is starting.
   - Atomicity: a short-held admission lock (`state/resource-admission.lock`)
     covers *check + write STARTING*. The STARTING record is the reservation the
     other side sees. Lock order: own controller lock, then admission lock;
     inside it only non-blocking attempts on other locks, so no deadlock.
   - Presence = observed process/socket/launchd job, OR a live STARTING/READY
     record (a STARTING record whose controller lock is free and with nothing
     observed is stale), OR desired=RUNNING this boot (recovery pending).
     Observers never write.
3. **Engine** (`fish_engine.py` + `fish_tts/`): standard-library HTTP on
   127.0.0.1:8770 with a per-start bearer token (0600 file), a Host allow-list,
   no browser origins, and one MLX worker thread. It drives the pinned model's
   frame generator, so cancellation works between frames and timings are
   exact. It runs under a Seatbelt profile that denies all non-loopback network,
   with `HF_HUB_OFFLINE=1`. References are content-addressed (SHA-256), uploaded
   on a cache miss and encoded once per engine run. The engine never takes a
   filesystem path from a request.
4. **Voice Studio** lives inside Newton AI Control, at `/voice`. The page is
   separate (`voice-studio.html` + `.js`); its routes live in `voice_studio.py`,
   so `newton_control.py` only delegates to them. The private store sits
   outside the repository: `~/Library/Application Support/Newton_Local/voice-studio/`
   (directories 0700, files 0600, Spotlight disabled). Uploads keep the
   original; the model input is derived from it (mono, polyphase resample to
   44.1 kHz, conservative silence trim and gain). Recording uses raw PCM through
   an AudioWorklet, with echo cancellation, noise suppression and automatic gain
   control turned off. Private GET requests need the custom header, so a
   cross-site page cannot fetch them.
5. **Configuration-driven variants**: `config/fish.json` lists models by id.
   A future Q8 is a new entry (path, manifest, source, memory figure) on the
   same `fish_omni` adapter. Nothing else changes.

## 3. Testing and rollback

- Hermetic unit tests: policy decisions (every combination), stale
  reservations, admission arithmetic, `fishctl` transitions with fakes,
  cancellation, the store (IDs, paths, permissions), upload validation, API
  authorisation, parameter validation, take metadata, the engine job queue with
  a fake synthesiser.
- Fixture proofs with real launchd and fake engines (the docs/16 pattern):
  both exclusion directions, concurrent start races, recovery paths, Stop
  during start. Production slots are never touched.
- Live, Fish only (authorised): install verification, load, generation,
  cancellation, identity, reclamation, benchmarks.
- Regression: every existing suite, under Python 3.12 and 3.9.
- Needs approval: restarting `com.newton.control` to load the new panel code,
  and the Fish + Qwen 27B concurrency benchmark.
- Rollback: `scripts/uninstall-fish.sh` (boot out the job, keep data unless
  asked), `git revert` of the hook commits; `qwenctl` behaves exactly as before
  whenever Fish is absent.

## 4. Risks

- The community port is one day old. Upstream reports that cue-heavy lines miss
  on 15–20% of seeds.
- Peak memory (~30 GB on an M4 Pro) next to the 27B (~48 GiB GPU) and about
  42 GiB of other workloads: co-residency may simply not fit, and the policy
  must say so.
- Seed reproducibility on Metal must be measured, not assumed.
- `sandbox-exec` is deprecated, though still used by current tools; if Metal or
  binding fails inside the sandbox, fall back to offline flags and record it.
