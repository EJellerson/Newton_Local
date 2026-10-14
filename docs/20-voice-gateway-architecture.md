# 20 — Future real-time voice gateway: architecture specification

**Status: specification only (commission NEWTON_LOCAL_FISH_DRAMA3_001, Phase 8).**
Nothing in this document is implemented. No Twilio, GoHighLevel, OpenAI or
speech-to-text component was installed, configured or contacted, and the
operator's existing telephony project was not read or modified. A later
commission implements this, inside Newton_Local, after operator approval of
each external integration.

The measurements this specification relies on are in docs/19 (§6); the
speech service it builds on is documented in docs/18.

## 1. Goal and non-goals

The operator wants an assistant that can place and take telephone calls,
**identify itself truthfully as an AI assistant acting for the operator**,
speak with the operator's consensually cloned voice, hold a natural
conversation, gather information, complete authorized tasks (including
calendar booking, docs/21), and report back.

Non-goals for the first gateway: autonomous outbound calling at scale, any
call without an AI disclosure, cloning any voice other than the operator's,
and running two calling systems on the shared Twilio number at once.

## 2. Target topology

```
 caller ⇄ PSTN ⇄ Twilio ──(HTTPS webhook + WSS Media Stream)──▶ public ingress
                                                                  │ (TLS, signature check)
                                                                  ▼
                                       Newton Voice Gateway (Newton_Local, new LaunchAgent)
                                       ├── call session manager (one state machine per call)
                                       ├── audio I/O: μ-law 8 kHz ⇄ PCM; jitter buffer; marks
                                       ├── VAD + turn-taking + barge-in
                                       ├── STT client  ──▶ streaming speech-to-text (local, TBD)
                                       ├── dialogue    ──▶ conversational LLM (local Qwen 27B first)
                                       ├── TTS client  ──▶ Fish engine API (127.0.0.1:8770)
                                       └── task tools  ──▶ calendar (docs/21), notes, operator report
```

Everything except the public ingress binds to 127.0.0.1. The ingress is the
first component in Newton_Local that is reachable from outside the Mac; it
needs its own operator approval (commission §19: "exposing services beyond
localhost"), TLS, Twilio request-signature validation on every webhook, and
a token in the Media Stream URL that the gateway checks before accepting the
WebSocket.

## 3. Telephony exclusivity (shared Twilio number)

The operator's existing telephony project and Newton_Local will share one
Twilio account and number and must never handle calls at the same time. The
design must not rely on the operator remembering which one is active.

* **Single source of truth:** the number's *Voice URL* in Twilio decides
  which implementation receives inbound calls. Switching owner is one
  explicit, logged operation ("make Newton_Local the phone owner") that
  updates the Voice URL and records the change locally.
* **Self-check before acting:** at start-up and before every outbound call,
  the gateway reads the number's configuration and refuses to run (inbound)
  or dial (outbound) unless the Voice URL points at itself. A stale or
  foreign owner is shown in Newton AI Control as "phone owned by the
  other telephony project".
* **Local lock:** an flock (`state/telephony-owner.lock`) prevents two local
  gateway processes; the panel's Start for the gateway is refused while the
  other implementation owns the number.
* That project is not changed by any of this; its own behaviour is only
  *read* (later, when commissioned) to reuse proven patterns from its voice
  runtime.

## 4. Audio path

| Leg | Format | Notes |
|---|---|---|
| Twilio Media Stream, both directions | G.711 μ-law, 8 kHz, mono, 20 ms frames (160 bytes), base64 in JSON messages | inbound `media` events; outbound `media` events; `mark` to learn when queued audio has played; `clear` to drop queued audio (barge-in) |
| Caller → STT | μ-law decode → 16-bit PCM → resample 8 → 16 kHz | the STT model decides the rate; telephone band limits it to ~3.4 kHz |
| Fish → caller | 44.1 kHz PCM16 (engine `/pcm`) → band-limit and resample 44.1 → 8 kHz (polyphase) → μ-law encode → 20 ms frames | resample once, at the edge; never round-trip through files |

Fish's native output is far wider band than a telephone line. **Voice
similarity judged on native 44.1 kHz takes in Voice Studio will not carry
over unchanged to the phone**; the operator should judge cloned-voice
quality again on an 8 kHz μ-law rendering before any calling use. A Voice
Studio "phone preview" (render a take through the exact telephone chain)
is a small, recommended addition for the implementing commission.

## 5. How the Fish service participates

The gateway is a client of the existing internal engine API (docs/18 §8);
no new inference code is needed:

1. **Readiness.** Before taking calls, the gateway requires Fish READY and,
   for a local LLM, Qwen 27B READY — the only pairing the resource policy
   allows (Flash-Next can never run beside Fish). It starts nothing itself;
   it asks fishctl/qwenctl, so admission applies.
2. **Reference once per engine start.** The operator's chosen recording is
   PUT once (`/v1/references/<sha256>`; 0.24 s to encode a 12.5 s
   recording); each turn then only names its hash. The reference is still
   part of every job's prompt, so a longer reference adds to every turn's
   first audio (docs/19 §7): pick the shortest recording that still sounds
   right.
3. **Sentence-sized jobs.** The dialogue layer streams LLM text and cuts it
   at sentence (or strong clause) boundaries; each piece becomes a
   progressive job (`segment_frames` ≈ 11, about 0.5 s of audio). The first
   sentence is submitted as soon as it is complete, so speech starts while
   the LLM is still writing.
4. **Streaming out.** Audio is pulled from `/v1/jobs/<id>/pcm?from=N` and
   forwarded frame by frame. docs/19 measures the engine's first decoded
   audio, the client's first received audio and whether progressive delivery
   keeps ahead of real-time playback. A push endpoint (chunked HTTP or a
   WebSocket) would remove the ~20–200 ms polling granularity and is the one
   engine change recommended for telephony.
5. **Barge-in.** When VAD detects the caller speaking over the assistant:
   send Twilio `clear`, `POST /v1/jobs/<id>/cancel` for the running job
   (measured: generation stops within one frame, ~0.26 s), drop queued
   sentences, and hand the turn back to STT.
6. **Continuity.** A long answer split into several jobs loses the
   prompt-level conditioning that the engine applies between chunks of one
   job. The implementing commission should measure whether per-sentence
   jobs sound disjointed and, if so, add an engine option to carry the
   previous turn's codes as history (the pinned port supports history
   turns; docs/18 §6).

## 6. Real-time requirements

| Requirement | Specification |
|---|---|
| Streaming STT | Partial and final hypotheses with word timing; endpointing; ≤ 300 ms finalisation after end of speech as a design target (to be measured, not assumed). Local candidates to evaluate: Whisper-family streaming on MLX, NVIDIA Parakeet-class models via MLX ports. No cloud STT without operator approval. Installing an STT model needs its own approval (commission §13). |
| Incremental LLM output | Token streaming from the LLM; the dialogue layer emits complete sentences to TTS, never single tokens. |
| Chunked synthesis | One progressive Fish job per sentence; next sentence queued while the current one plays. |
| Time to first playable audio | Budget from end of caller speech: endpointing + STT final + LLM time to first sentence + Fish first audio + transport. Every term is logged per turn so the real budget is known (docs/19 gives the Fish term). |
| Turn-taking | Explicit states per call: LISTENING → THINKING → SPEAKING → LISTENING, with timeouts; silence prompts after N seconds. |
| Voice activity detection | Frame-level VAD on caller audio (e.g. WebRTC-class or Silero-class, local); tuned for telephone noise. |
| Interruption / barge-in | §5.5; also cancel the in-flight LLM request. |
| Cancelling TTS | Engine cancel endpoint; Twilio `clear`; no audio from a cancelled job may be sent after the cancel. |
| Audio buffering | Outbound jitter buffer of ~100–200 ms; Twilio `mark` events to know what the caller has actually heard (needed to truncate the transcript on barge-in). |
| Audio resampling | Polyphase, at the edges only (§4). |
| Telephone codecs | G.711 μ-law 8 kHz for Twilio Media Streams. |
| Caller / call-session identity | Twilio CallSid and stream SID as the session key; caller number used only as needed for the task; per-call state isolated. |
| Recoverable provider failures | STT/LLM/TTS failure → short spoken fallback ("Sorry, I'm having trouble; I'll have Eric call you back"), retry once where safe, then a graceful hang-up and an operator report. Never fall back to a cloud provider silently. |
| Session cleanup | On hang-up, WebSocket close or timeout: cancel jobs, close streams, release references in memory, write the call record. |
| Call logging and evidence | Per call: times, durations, latency terms per turn, tool actions, outcome. Recording and transcripts only with the required consent (§7), stored privately like Voice Studio data, never in Git or diagnostic logs. |

## 7. Conduct, disclosure and compliance (must be designed in, not added later)

* **Truthful identity.** Every call opens with a clear statement that the
  caller is speaking with an AI assistant acting for Eric. A cloned voice
  never implies that the operator himself is speaking; if asked, the
  assistant confirms it is an AI.
* **Voice consent.** Only the operator's own voice (or a voice with
  documented permission, as Voice Studio already records) may be used; the
  Fish Audio Community License also forbids impersonation and cloning
  without consent.
* **Outbound calling law.** Automated or prerecorded/artificial-voice calls
  are regulated (for example the U.S. Telephone Consumer Protection Act and
  FCC rules treating AI-generated voices as "artificial", the Telemarketing
  Sales Rule, do-not-call lists, calling-hour limits and state laws). The
  implementing commission must obtain a legal review of the intended call
  types before any outbound calling, and the gateway must enforce: consent
  records, calling windows by the callee's time zone, do-not-call checks and
  an immediate opt-out path.
* **Recording consent.** Some jurisdictions require all-party consent to
  record; the gateway must not record or store call audio unless the
  disclosure at the start covers it.
* **Licensing.** Commercial use of Fish outputs is subject to the Fish Audio
  Community License revenue threshold, and a hosted voice service needs a
  separate licence (docs/18 §9). A business telephone assistant must be
  checked against those terms before deployment.

## 8. Resource and lifecycle integration

* The gateway is another on-demand LaunchAgent (off after reboot), started
  from Newton AI Control, with a desired state and watchdog like the others.
* It never starts or stops Fish or Qwen itself; it requests them through
  fishctl/qwenctl, so the shared admission policy (docs/18 §4) applies.
* Calling is "ready" only when Fish and the chosen LLM are both READY and
  telephony ownership is confirmed; the panel shows each condition.
* Fish + Qwen 27B is admitted only when measured memory allows; docs/19
  records the measurement (or that it is pending the operator's approval).

## 9. Interfaces to implement later

| Module (proposed) | Responsibility |
|---|---|
| `scripts/voice_gateway.py` | the service: webhook + WebSocket server, session manager |
| `scripts/telephony_owner.py` | Voice-URL ownership check and switch (§3) |
| `scripts/gateway_audio.py` | μ-law codec, resampling, framing, jitter buffer |
| `scripts/gateway_dialogue.py` | turn-taking state machine, sentence segmentation, barge-in |
| `scripts/stt_client.py` | the chosen local STT engine's client |
| Fish engine (existing) | add a push stream for job audio (§5.4); optional history carry-over (§5.6) |
| Newton AI Control | gateway card: owner, readiness, current call, latency per turn |

## 10. Open questions for the implementing commission

1. Local STT model choice and its memory next to Fish + Qwen 27B.
2. Local Qwen 27B vs a hosted LLM: measured time to first sentence and
   conversation quality (docs/19 measures 27B's time to first token alone
   and next to Fish only if the operator authorizes that run).
3. Whether Q4's voice similarity survives the telephone chain (§4).
4. Public ingress choice and how it is secured and monitored.
5. Legal review of the call types (§7).
