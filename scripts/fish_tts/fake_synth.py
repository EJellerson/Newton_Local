"""A synthesiser with no model, for fixture proofs only (docs/19 §5).

fish_engine.py uses it only with --fake-model AND a configuration marked
"fixture": true, so a production engine can never run it. It reproduces the
engine-visible behaviour of the real adapter — a load that takes time, frames
at a steady rate, EOS, progressive segments, cancellation between frames —
and produces a quiet tone instead of speech. The fixture config's "fake"
block can make the load slow, fail, or hang, to exercise start supervision.
"""
import os
import sys
import time

import numpy as np

from .jobs import JobCancelled


class FakeSynthesizer:
    adapter = "fake"

    def __init__(self, cfg, model_id):
        self.opts = dict(cfg.get("fake") or {})
        self.sample_rate = 44100
        self.frame_length = 2048
        self.load_info = {}

    def load(self):
        t0 = time.perf_counter()
        if self.opts.get("load_error"):
            raise RuntimeError(self.opts["load_error"])
        if self.opts.get("exit_on_load"):
            sys.stderr.write(self.opts["exit_on_load"] + "\n")
            sys.stderr.flush()
            os._exit(1)
        time.sleep(float(self.opts.get("load_s", 0.5)))
        self.load_info = {"load_s": round(time.perf_counter() - t0, 2), "fake": True,
                          "sample_rate": self.sample_rate, "frame_length": self.frame_length}
        return self.load_info

    def warmup(self):
        self.load_info["warmup_s"] = 0.0
        return 0.0

    def encode_reference(self, samples):
        frames = max(1, int(len(samples) / self.frame_length))
        return np.zeros((10, frames), dtype=np.int32)

    def memory(self):
        return {"active_gib": 0.0, "peak_gib": 0.0, "cache_gib": 0.0, "fake": True}

    def synthesize(self, request, reference_codes, should_cancel, on_progress,
                   on_segment=None, capture=None):
        s = request["settings"]
        fps = float(self.opts.get("frames_per_s", 200.0))
        frames = min(s["max_frames_per_chunk"], max(8, len(request["text"]) // 2))
        seg = (request.get("progressive") or {}).get("segment_frames")
        t0 = time.perf_counter()
        on_progress(chunk=1, chunks=1, frames=0, audio_seconds=0.0)
        out, pending = [], 0
        for i in range(frames):
            time.sleep(1.0 / fps)
            pending += 1
            if seg and pending >= seg:
                out.append(self._tone(pending, len(out)))
                pending = 0
                if on_segment:
                    on_segment(out[-1])
            on_progress(frames=i + 1)
            if should_cancel():
                raise JobCancelled()
        if pending or not out:
            out.append(self._tone(max(pending, 1), len(out)))
            if seg and on_segment:
                on_segment(out[-1])
        audio = np.concatenate(out)
        dur = len(audio) / self.sample_rate
        el = time.perf_counter() - t0
        meta = {"seed": s["seed"] if s["seed"] is not None else 0, "chunk_count": 1,
                "chunks": [{"index": 0, "frames": frames, "eos": True, "text_bytes":
                            len(request["text"].encode()), "audio_seconds": round(dur, 3)}],
                "frames": frames, "audio_seconds": round(dur, 3), "sample_rate": self.sample_rate,
                "synthesis_s": round(el, 3), "first_frame_s": round(1.0 / fps, 4),
                "first_audio_s": round(el, 4), "realtime_factor": round(dur / el, 3),
                "progressive": bool(seg), "segment_frames": seg,
                "decode": "fake tone", "first_decoded_s": round(1.0 / fps, 4),
                "peak_gib": 0.0, "fake": True}
        return meta, audio

    def _tone(self, nframes, k):
        n = nframes * self.frame_length
        t = (np.arange(n) + k * n) / self.sample_rate
        return (0.05 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)
