#!/usr/bin/env python3
"""Live proof of the Drama 3 adapter against the pinned library (docs/19 §3).

Run under the Fish runtime and its network sandbox, with nothing else loaded:

    HF_HUB_OFFLINE=1 /usr/bin/sandbox-exec -f config/fish-engine.sb \
        tools/fish-venv/bin/python3 -I scripts/prove-fish-adapter.py \
        --model models/fish-drama3-mlx-q4 --report benchmarks/fish/adapter-proof.json

Checks, each with the same seed on both sides:
  1. no reference: adapter codes == Model.generate() codes;
  2. synthetic reference: the model's own no-reference voice is the reference
     (no real person's voice is involved), adapter == generate();
  3. multi-chunk text (history): adapter == generate();
  4. streamed decoding (codec streaming_step, what the service always uses)
     == whole-chunk decode, and a take's audio is bit-identical whether or
     not it is delivered progressively; streaming's peak memory vs whole;
  5. cancellation stops within a frame or two;
  6. repeated generation is stable (EOS reached, no growth in peak memory);
  7. at the frame cap (one chunk run to 600 frames, the most the limits
     allow), streamed decoding gives the same codes and needs less peak
     memory than decoding the chunk whole.
Writes a JSON report with no audio. --keep-audio DIR also saves the WAVs for
listening (never under the repository).
"""
import argparse
import json
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import mlx.core as mx  # noqa: E402
import numpy as np  # noqa: E402

from fish_tts.adapter_fish_omni import FishOmniSynthesizer  # noqa: E402
from fish_tts.jobs import JobCancelled  # noqa: E402
from fish_tts.protocol import validate_job  # noqa: E402
from fish_tts.wavio import write_wav  # noqa: E402

LIMITS = {"max_text_chars": 2000, "max_references": 3, "max_frames_per_chunk": 600}
REF_TEXT = "The weather was calm this morning, so we walked down to the harbor."
SHORT = "Thanks for calling. I can help you with that right away."
LONG = ("Good afternoon. I am calling on behalf of the office to confirm a few details "
        "about next week. The meeting is on Tuesday the twenty-first at ten thirty in "
        "the morning, and it should take about forty-five minutes. If that time no "
        "longer works, we can move it to Wednesday or Thursday afternoon. Please let "
        "me know which option suits you best, and I will update the calendar today.")


def native(model, text, seed, ref_codes=None, ref_texts=None):
    mx.random.seed(seed)
    res = list(model.generate(text, temperature=1.0, top_p=0.9, top_k=30,
                              ref_codes=ref_codes, ref_texts=ref_texts))
    return [np.array(r.codes, dtype=np.int32) for r in res], \
        np.concatenate([np.array(r.audio.astype(mx.float32)) for r in res])


def adapter(syn, text, seed, refs=(), ref_codes=(), progressive=None, cancel_after=None,
            whole=False, settings=None):
    req = validate_job({"text": text, "references": list(refs),
                        "settings": dict(settings or {}, seed=seed),
                        "progressive": progressive}, LIMITS)
    cap, prog = [], {"frames": 0}

    def on_progress(**kw):
        prog.update(kw)

    def should_cancel():
        return cancel_after is not None and prog["frames"] >= cancel_after
    meta, audio = syn.synthesize(req, list(ref_codes), should_cancel, on_progress,
                                 on_segment=lambda a: None, capture=cap, whole_decode=whole)
    return cap, audio, meta


def same(a, b):
    return len(a) == len(b) and all(x.shape == y.shape and np.array_equal(x, y)
                                    for x, y in zip(a, b))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--keep-audio")
    a = ap.parse_args()
    out = {"started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "checks": {}}
    ok_all = True

    def check(name, ok, **detail):
        nonlocal ok_all
        ok_all &= bool(ok)
        out["checks"][name] = {"ok": bool(ok), **detail}
        print(f"  {'ok  ' if ok else 'FAIL'} {name} {json.dumps(detail)}", flush=True)

    syn = FishOmniSynthesizer(a.model)
    out["load"] = syn.load()
    out["warmup_s"] = syn.warmup()
    print(f"loaded: {out['load']}  warmup {out['warmup_s']} s", flush=True)
    model = syn.model

    # 1. no reference
    c_nat, a_nat = native(model, SHORT, 1234)
    c_ad, a_ad, m1 = adapter(syn, SHORT, 1234, whole=True)
    check("no_reference_codes_identical", same(c_nat, c_ad), frames=int(c_ad[0].shape[1]),
          audio_identical=bool(np.array_equal(a_nat, a_ad)), first_frame_s=m1["first_frame_s"])

    # 2. synthetic reference
    _, ref_audio, mref = adapter(syn, REF_TEXT, 77, whole=True)
    ref_codes = syn.encode_reference(ref_audio)
    out["synthetic_reference"] = {"seconds": round(len(ref_audio) / syn.sample_rate, 2),
                                  "frames": int(ref_codes.shape[1]), "text": REF_TEXT}
    c_nat, _ = native(model, SHORT, 4321, ref_codes=[mx.array(ref_codes)], ref_texts=[REF_TEXT])
    c_ad, a_ref, m2 = adapter(syn, SHORT, 4321,
                              refs=[{"sha256": "0" * 64, "transcript": REF_TEXT}],
                              ref_codes=[ref_codes], whole=True)
    check("reference_codes_identical", same(c_nat, c_ad), frames=int(c_ad[0].shape[1]),
          first_frame_s=m2["first_frame_s"], prompt_rows=m2["chunks"][0]["prompt_rows"])

    # 3. multi-chunk with history
    c_nat, _ = native(model, LONG, 99, ref_codes=[mx.array(ref_codes)], ref_texts=[REF_TEXT])
    c_ad, a_long, m3 = adapter(syn, LONG, 99, refs=[{"sha256": "0" * 64, "transcript": REF_TEXT}],
                               ref_codes=[ref_codes], whole=True)
    check("multi_chunk_codes_identical", same(c_nat, c_ad) and len(c_ad) > 1,
          chunks=len(c_ad), frames=[int(c.shape[1]) for c in c_ad])

    # 4. progressive == whole
    c_p, a_p, m4 = adapter(syn, LONG, 99, refs=[{"sha256": "0" * 64, "transcript": REF_TEXT}],
                           ref_codes=[ref_codes], progressive={"segment_frames": 11})
    diff = float(np.max(np.abs(a_p - a_long))) if a_p.shape == a_long.shape else None
    check("progressive_equals_whole", same(c_p, c_ad) and diff is not None and diff < 1e-3,
          max_abs_diff=diff, samples=int(a_p.shape[0]),
          first_audio_s_progressive=m4["first_audio_s"], first_audio_s_whole=m3["first_audio_s"],
          peak_gib_streamed=m4["peak_gib"], peak_gib_whole=m3["peak_gib"])
    c_s, a_s, m4b = adapter(syn, LONG, 99, refs=[{"sha256": "0" * 64, "transcript": REF_TEXT}],
                            ref_codes=[ref_codes])
    check("delivery_mode_does_not_change_audio",
          same(c_s, c_p) and a_s.shape == a_p.shape and bool(np.array_equal(a_s, a_p)),
          decode=m4b["decode"], first_audio_s_not_progressive=m4b["first_audio_s"],
          first_decoded_s=m4b["first_decoded_s"])

    # 5. cancellation
    t = time.perf_counter()
    try:
        adapter(syn, LONG, 5, cancel_after=5)
        check("cancellation", False, detail="did not cancel")
    except JobCancelled:
        check("cancellation", True, seconds_to_stop=round(time.perf_counter() - t, 3))

    # 6. stability
    runs = []
    for seed in (11, 12, 13, 14, 15):
        _, au, mm = adapter(syn, SHORT, seed, refs=[{"sha256": "0" * 64, "transcript": REF_TEXT}],
                            ref_codes=[ref_codes])
        runs.append({"seed": seed, "frames": mm["frames"], "eos": all(c["eos"] for c in mm["chunks"]),
                     "audio_s": mm["audio_seconds"], "synthesis_s": mm["synthesis_s"],
                     "peak_gib": mm["peak_gib"], "first_frame_s": mm["first_frame_s"]})
    peaks = [r["peak_gib"] for r in runs]
    check("repeated_generation_stable", all(r["eos"] for r in runs) and max(peaks) - min(peaks) < 1.0,
          runs=runs)

    # 7. decoding memory at the frame cap
    capped = {"single_chunk": True, "max_frames_per_chunk": LIMITS["max_frames_per_chunk"]}
    at_cap = {}
    for label, whole in (("whole", True), ("streamed", False)):
        c, _, mm = adapter(syn, LONG, 7, refs=[{"sha256": "0" * 64, "transcript": REF_TEXT}],
                           ref_codes=[ref_codes], whole=whole, settings=capped)
        at_cap[label] = (c, mm)
    (c_w, m_w), (c_s7, m_s7) = at_cap["whole"], at_cap["streamed"]
    check("streamed_decode_memory_at_frame_cap",
          same(c_w, c_s7) and m_s7["peak_gib"] < m_w["peak_gib"],
          frames=m_s7["frames"], eos=m_s7["chunks"][0]["eos"],
          peak_gib_whole=m_w["peak_gib"], peak_gib_streamed=m_s7["peak_gib"],
          saved_gib=round(m_w["peak_gib"] - m_s7["peak_gib"], 2),
          synthesis_s_whole=m_w["synthesis_s"], synthesis_s_streamed=m_s7["synthesis_s"])
    out["memory_after"] = syn.memory()

    if a.keep_audio:
        os.makedirs(a.keep_audio, exist_ok=True)
        for name, au in (("no-reference", a_ad), ("synthetic-reference", ref_audio),
                         ("cloned-short", a_ref), ("cloned-long", a_long),
                         ("cloned-long-progressive", a_p)):
            with open(os.path.join(a.keep_audio, f"{name}.wav"), "wb") as f:
                f.write(write_wav(au, syn.sample_rate))
    out["ok"] = ok_all
    out["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    os.makedirs(os.path.dirname(os.path.abspath(a.report)), exist_ok=True)
    with open(a.report, "w") as f:
        json.dump(out, f, indent=2)
        f.write("\n")
    print("ALL OK" if ok_all else "FAILURES", flush=True)
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
