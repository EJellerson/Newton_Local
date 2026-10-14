"""Fish Audio Drama 3 (fish_omni) adapter over the pinned mlx-audio (docs/18).

The only model-specific code in the speech service. It mirrors
mlx_audio.tts.models.fish_omni.Model.generate step for step, using the model's
own building blocks (split_text, History, build_chunk_prompt, generate_frames,
decode_codes), but drives the frame generator itself. That buys three things
generate() does not offer:

  * cancellation between frames (generate() only yields per text chunk);
  * exact timings: prompt build, first frame, frame loop, codec decode;
  * whether each chunk ended on EOS or ran into the frame cap.

Same inputs and seed give bit-identical codes to generate(); the live proof
(scripts/prove-fish-adapter.py) checks that before the service relies on it.

Decoding is always streamed: the codec's streaming_step turns every
DECODE_SEGMENT_FRAMES frames into audio as they are generated. It "matches
decode() on all frames at once" (fish_s1_dac.py) — measured: within 2.7e-5 —
and the same code base already uses it to stream S2 Pro. Decoding a whole
600-frame chunk at once instead peaks about 9 GiB higher (docs/19 §6), so the
service never does; whole_decode=True exists only for the equivalence proof.
Progressive delivery to a client is then just publishing those segments, so
at the default segment size a take's audio is bit-identical whether or not it
was delivered progressively.
"""
import secrets
import time
from pathlib import Path

import mlx.core as mx
import numpy as np

from mlx_audio.tts.models.fish_omni.prompt import History, split_text
from mlx_audio.tts.utils import load_model

from .jobs import JobCancelled

GIB = 1 << 30
DECODE_SEGMENT_FRAMES = 11          # about 0.51 s of audio per decode step


class FishOmniSynthesizer:
    adapter = "fish_omni"

    def __init__(self, model_path, cache_limit_gib=2.0):
        self.model_path = Path(model_path)
        self.cache_limit = int(cache_limit_gib * GIB)
        self.model = None
        self.sample_rate = None
        self.frame_length = None
        self.load_info = {}

    # ---------------------------------------------------------------- load
    def load(self):
        """Load weights, tokenizer and codec from the local pack (never the Hub)."""
        t0 = time.perf_counter()
        # MLX keeps freed buffers in a cache that otherwise grows towards the
        # GPU working-set limit; bound it so the service holds what it uses.
        mx.set_cache_limit(self.cache_limit)
        model = load_model(self.model_path)          # a Path: local only
        mx.eval(model.parameters())
        if model.codec is None or model.tokenizer is None:
            raise RuntimeError("the pack's codec or tokenizer did not load")
        self.model = model
        self.sample_rate = int(model.sample_rate)
        self.frame_length = int(model.codec.frame_length)
        self.load_info = {"load_s": round(time.perf_counter() - t0, 2),
                          "active_gib_after_load": round(mx.get_active_memory() / GIB, 2),
                          "sample_rate": self.sample_rate,
                          "frame_length": self.frame_length}
        return self.load_info

    def warmup(self):
        """One tiny generation, so Metal kernels are compiled before the first
        request instead of during it. Its audio is discarded."""
        t0 = time.perf_counter()
        prompt = self.model.build_chunk_prompt("Ready.", [], [], History())
        mx.random.seed(0)
        n = 0
        for _ in self.model.generate_frames(prompt, max_new_tokens=6, temperature=1.0,
                                            top_k=30, top_p=0.9):
            n += 1
        if n:
            mx.eval(self.model.decode_codes(mx.zeros((10, max(n, 2)), dtype=mx.int32)))
        mx.clear_cache()
        self.load_info["warmup_s"] = round(time.perf_counter() - t0, 2)
        return self.load_info["warmup_s"]

    # ------------------------------------------------------------ references
    def encode_reference(self, samples):
        """Mono float32 audio at the model rate -> codes [num_codebooks, frames]."""
        audio = mx.array(np.asarray(samples, dtype=np.float32).reshape(-1))
        codes = self.model.encode_reference(audio)
        mx.eval(codes)
        out = np.array(codes, dtype=np.int32)
        mx.clear_cache()
        return out

    # ------------------------------------------------------------- memory
    def memory(self):
        return {"active_gib": round(mx.get_active_memory() / GIB, 2),
                "peak_gib": round(mx.get_peak_memory() / GIB, 2),
                "cache_gib": round(mx.get_cache_memory() / GIB, 2),
                "cache_limit_gib": round(self.cache_limit / GIB, 2)}

    # ----------------------------------------------------------- generate
    def synthesize(self, request, reference_codes, should_cancel, on_progress,
                   on_segment=None, capture=None, whole_decode=False):
        """Generate one take. Returns (metadata, float32 audio). Segments go to
        on_segment as they are decoded when the request is progressive. If
        capture is a list, each chunk's codes are appended to it, and
        whole_decode decodes each chunk at once (both for the proof only)."""
        m = self.model
        s = request["settings"]
        seed = s["seed"] if s["seed"] is not None else secrets.randbelow(2**31 - 1)
        progressive = request.get("progressive")
        stream = not whole_decode
        seg_frames = progressive["segment_frames"] if progressive else DECODE_SEGMENT_FRAMES
        publish = on_segment if progressive else None
        max_frames = s["max_frames_per_chunk"]

        prompt_texts = [r["transcript"] for r in request["references"]]
        prompt_tokens = [mx.array(c) for c in reference_codes]
        text = request["text"]
        chunks = [text] if s["single_chunk"] else (split_text(text, s["chunk_bytes"]) or [text])
        history = History(recent_turns=1)        # generate() with no previous takes

        mx.random.seed(seed)
        mx.reset_peak_memory()
        t_job = time.perf_counter()
        first_frame_s = first_decoded_s = None
        parts, chunk_meta, total_frames = [], [], 0
        on_progress(chunk=0, chunks=len(chunks), frames=0, audio_seconds=0.0)

        for idx, chunk in enumerate(chunks):
            t0 = time.perf_counter()
            prompt = m.build_chunk_prompt(chunk, prompt_texts, prompt_tokens, history)
            t_prompt = time.perf_counter()
            frames, t_first, decoded, seg_audio = [], None, 0, []
            if stream:
                m.codec.reset_streaming_state()
            gen = m.generate_frames(prompt, max_new_tokens=max_frames,
                                    temperature=s["temperature"], top_k=s["top_k"],
                                    top_p=s["top_p"])
            try:
                for frame in gen:
                    if t_first is None:
                        t_first = time.perf_counter()
                        if first_frame_s is None:
                            first_frame_s = t_first - t_job
                    frames.append(frame)
                    if stream and len(frames) - decoded >= seg_frames:
                        a = self._stream_decode(frames[decoded:])
                        decoded = len(frames)
                        seg_audio.append(a)
                        if first_decoded_s is None:
                            first_decoded_s = time.perf_counter() - t_job
                        if publish:
                            publish(a)
                    on_progress(chunk=idx + 1, frames=total_frames + len(frames),
                                audio_seconds=round((total_frames + len(frames))
                                                    * self.frame_length / self.sample_rate, 2))
                    if should_cancel():
                        raise JobCancelled()
            finally:
                gen.close()
            t_loop = time.perf_counter()
            eos = len(frames) < max_frames
            meta = {"index": idx, "text_bytes": len(chunk.encode("utf-8")),
                    "prompt_rows": int(len(prompt)), "frames": len(frames), "eos": eos,
                    "prompt_build_s": round(t_prompt - t0, 4),
                    "first_frame_s": round(t_first - t0, 4) if t_first else None,
                    "frame_loop_s": round(t_loop - (t_first or t_loop), 4),
                    "frames_per_s": round((len(frames) - 1) / (t_loop - t_first), 2)
                    if t_first and len(frames) > 1 and t_loop > t_first else None}
            if not frames:                       # EOS before any audio: generate() skips
                meta.update(empty=True, decode_s=0.0, audio_seconds=0.0)
                chunk_meta.append(meta)
                continue
            codes = mx.concatenate(frames, axis=1)[0, :, 1:].T
            history.push(chunk, codes)
            if capture is not None:
                capture.append(np.array(codes, dtype=np.int32))
            if stream:
                if len(frames) > decoded:
                    a = self._stream_decode(frames[decoded:])
                    seg_audio.append(a)
                    if first_decoded_s is None:
                        first_decoded_s = time.perf_counter() - t_job
                    if publish:
                        publish(a)
                audio = np.concatenate(seg_audio)
            else:
                audio = np.array(m.decode_codes(codes).astype(mx.float32))
                if first_decoded_s is None:
                    first_decoded_s = time.perf_counter() - t_job
            t_dec = time.perf_counter()
            meta.update(decode_s=round(t_dec - t_loop, 4),
                        audio_seconds=round(len(audio) / self.sample_rate, 3))
            chunk_meta.append(meta)
            parts.append(audio)
            total_frames += len(frames)

        audio = np.concatenate(parts) if parts else np.zeros(0, np.float32)
        elapsed = time.perf_counter() - t_job
        # First audio the CLIENT can have: a published segment, or the whole take.
        first_audio_s = first_decoded_s if publish else elapsed
        dur = len(audio) / self.sample_rate
        meta = {
            "seed": seed, "chunks": chunk_meta, "chunk_count": len(chunks),
            "frames": total_frames, "audio_seconds": round(dur, 3),
            "sample_rate": self.sample_rate, "synthesis_s": round(elapsed, 3),
            "first_frame_s": round(first_frame_s, 4) if first_frame_s is not None else None,
            "first_audio_s": round(first_audio_s, 4) if first_audio_s is not None else None,
            "realtime_factor": round(dur / elapsed, 3) if elapsed > 0 else None,
            "progressive": bool(progressive),
            "segment_frames": seg_frames if progressive else None,
            "decode": f"streaming, {seg_frames}-frame segments" if stream else "whole chunk",
            "first_decoded_s": round(first_decoded_s, 4) if first_decoded_s is not None else None,
            "peak_gib": round(mx.get_peak_memory() / GIB, 2),
        }
        mx.clear_cache()
        return meta, audio

    def _stream_decode(self, frames):
        codes = mx.concatenate(frames, axis=1)[0, :, 1:].T
        audio = self.model.codec.streaming_step(codes[None])[0, 0]
        mx.eval(audio)
        return np.array(audio.astype(mx.float32))
