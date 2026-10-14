#!/usr/bin/env python3
"""Newton_Local — benchmark the Fish speech service on this Mac (docs/19 §6).

Measures what the commission asks for, on the real model, through the same
service boundary Voice Studio uses (fishctl for the lifecycle, the engine's
token-protected loopback API for speech):

  lifecycle    cold start (model files evicted from the page cache first,
               with --cold), warm starts, idle memory after load, stop time
               and memory reclamation
  speech       per text case and seed: time to first frame, first decoded
               audio (engine) and first audio received by a client, total
               synthesis time, audio length, real-time factor, steady frame
               rate, peak model memory, engine CPU time, defect flags; for
               progressive delivery, whether audio arrives fast enough to
               play without gaps once playback starts
  worst case   with --worst-case: the longest chunk the limits allow (one
               chunk run to the 600-frame cap), delivered whole and
               progressively: peak model memory and Metal allocation
  contention   with --qwen-probe and a Qwen model ALREADY running on :8080
               (this script never starts or stops Qwen): Qwen time to first
               token and decode rate alone and while Fish generates

The voice is a permitted test voice: the model's own no-reference voice,
generated here with a fixed seed and used as the reference, so the run is
reproducible and contains nobody's voice. Results go to
benchmarks/fish/bench-<time>.json: test text, settings, seeds, revisions,
machine state and timings; never audio.

USAGE
    scripts/bench-fish.py [--config PATH] [--reps N] [--cold] [--worst-case]
                          [--qwen-probe] [--leave-running] [--out FILE]
"""
import argparse
import ctypes
import hashlib
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

import fish_config as fc  # noqa: E402
import resource_policy as rp  # noqa: E402
from voice_studio import EngineClient, EngineDown  # noqa: E402

GIB = 1 << 30
REF_TEXT = ("The weather was calm this morning, so we walked down to the harbor and watched "
            "the fishing boats come in, one after another, until the sun was high.")
REF_SEED = 20261014
CASES = [
    ("short", "Sure, I can help with that."),
    ("question", "Did you get a chance to look at the proposal before the meeting?"),
    ("medium", "Thanks for calling back. I checked the schedule, and Thursday at two works for "
               "both of us. I'll send a confirmation with the address and a map."),
    ("numbers", "Your order number is 48213. It ships on October 21st and should arrive between "
                "9 and 11 a.m.; the total is $112.50."),
    ("long", "The river runs north for about forty miles before it turns toward the coast. In "
             "spring the water rises quickly, and the old bridge near the mill is often closed "
             "for several days. People who live on the far side plan ahead: they stock up on "
             "groceries, park their cars on the high road, and walk the last half mile with "
             "flashlights. Most years nobody minds. It is quieter then, and the town feels "
             "smaller, as if everyone has agreed to slow down for a while. When the water "
             "finally drops, the bridge reopens with no ceremony at all, and by the end of the "
             "week it is as if nothing happened."),
]
SEGMENT_FRAMES = 11


def now():
    return time.time()


def sh(cmd, timeout=600):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


# ---------------------------------------------------------------- machine
def machine():
    _, ver, _ = sh(["sw_vers", "-productVersion"])
    _, model, _ = sh(["sysctl", "-n", "hw.model"])
    _, cpu, _ = sh(["sysctl", "-n", "machdep.cpu.brand_string"])
    _, ncpu, _ = sh(["sysctl", "-n", "hw.ncpu"])
    _, head, _ = sh(["git", "-C", REPO, "rev-parse", "HEAD"])
    return {"macos": ver.strip(), "hw_model": model.strip(), "cpu": cpu.strip(),
            "cpu_cores": int(ncpu.strip() or 0), "memory_gib": round(rp.nm.host_memory_bytes() / GIB),
            "python": platform.python_version(), "repo_commit": head.strip()}


def memory_state():
    import newton_models as nm
    c, t = nm.vm_counts(), nm.host_memory_bytes()
    g = rp.gpu_allocation_bytes()
    return {"free_for_models_gib": round(nm.engine_available_bytes(t, c) / GIB, 2) if c else None,
            "pages_free_gib": round(c.get("free", 0) * 16384 / GIB, 2) if c else None,
            "gpu_alloc_gib": round(g / GIB, 2) if g else None, "pressure": rp.pressure_level(),
            "swap_used": swap_used()}


def swap_used():
    _, out, _ = sh(["sysctl", "-n", "vm.swapusage"])
    m = re.search(r"used = ([\d.]+)M", out)
    return float(m.group(1)) if m else None


def other_workloads(exclude_pids=()):
    """Resident workloads by executable name only (no arguments, no paths)."""
    _, out, _ = sh(["ps", "-Ao", "pid=,rss=,comm="])
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[1].isdigit() and int(parts[0]) not in exclude_pids:
            rows.append((int(parts[1]) * 1024, os.path.basename(parts[2].strip())))
    rows.sort(reverse=True)
    return {"processes_over_1_gib": sum(1 for r in rows if r[0] > GIB),
            "top_rss": [{"name": n, "rss_gib": round(b / GIB, 2)} for b, n in rows[:6]],
            "total_rss_gib": round(sum(r[0] for r in rows) / GIB, 1)}


def cpu_seconds(pid):
    _, out, _ = sh(["ps", "-o", "time=", "-p", str(pid)])
    m = re.match(r"\s*(?:(\d+)-)?(?:(\d+):)?(\d+):([\d.]+)", out)
    if not m:
        return None
    d, h, mi, s = (float(x) if x else 0.0 for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def rss_gib(pid):
    _, out, _ = sh(["ps", "-o", "rss=", "-p", str(pid)])
    return round(int(out.strip()) / 2**20, 2) if out.strip().isdigit() else None


# ------------------------------------------------------------- page cache
def evict_page_cache(paths):
    """Drop these files' pages from the unified buffer cache, without root:
    msync(MS_INVALIDATE) over a read-only shared mapping. Nothing else is
    touched; the files are not modified."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mmap.restype = ctypes.c_void_p
    libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_longlong]
    libc.msync.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    done = 0
    for p in paths:
        fd = os.open(p, os.O_RDONLY)
        try:
            size = os.fstat(fd).st_size
            if size == 0:
                continue
            addr = libc.mmap(None, size, 1, 1, fd, 0)            # PROT_READ, MAP_SHARED
            if addr in (None, ctypes.c_void_p(-1).value):
                continue
            if libc.msync(ctypes.c_void_p(addr), size, 2) == 0:  # MS_INVALIDATE
                done += size
            libc.munmap(ctypes.c_void_p(addr), size)
        finally:
            os.close(fd)
    return done


# ------------------------------------------------------------------ fish
class Fish:
    def __init__(self, config):
        self.config = config
        self.cfg = fc.load_config(config)
        self.engine = EngineClient(self.cfg)

    def ctl(self, *args, timeout=600):
        rc, out, err = sh([sys.executable, os.path.join(SCRIPT_DIR, "fishctl.py"), "--config",
                           self.cfg["_config_path"], *args, "--json"], timeout=timeout)
        try:
            return rc, json.loads(out)
        except ValueError:
            return rc, {"error": (err or out).strip()[-300:]}

    def status(self):
        return self.ctl("status")[1]

    def start(self):
        t = now()
        rc, res = self.ctl("start", "--by", "bench")
        if rc != 0:
            raise SystemExit(f"Fish did not start (rc={rc}): {res.get('message') or res}")
        res["wall_s"] = round(now() - t, 2)
        return res

    def stop(self):
        rc, res = self.ctl("stop", "--by", "bench")
        if rc != 0:
            raise SystemExit(f"Fish did not stop (rc={rc}): {res}")
        return res


def sampler(pid, stop, out):
    """Peak Metal allocation while a job runs (every 0.25 s)."""
    peak_gpu = 0
    while not stop.is_set():
        g = rp.gpu_allocation_bytes() or 0
        peak_gpu = max(peak_gpu, g)
        stop.wait(0.25)
    out["peak_gpu_alloc_gib"] = round(peak_gpu / GIB, 2)


def run_job(fish, text, refs, settings, progressive, pid, keep_audio=False):
    eng = fish.engine
    body = {"text": text, "references": refs, "settings": settings,
            "progressive": {"segment_frames": SEGMENT_FRAMES} if progressive else None}
    cpu0 = cpu_seconds(pid)
    stop, peaks = threading.Event(), {}
    th = threading.Thread(target=sampler, args=(pid, stop, peaks), daemon=True)
    th.start()
    t0 = now()
    st, job = eng.submit(body)
    if st != 202:
        stop.set()
        return {"error": f"submit HTTP {st}: {job}"}
    jid = job["id"]
    arrivals, have, state = [], 0, "queued"
    while True:
        if progressive:
            s2, hdr, data = eng.pcm(jid, have)
            n = len(data) // 2
            state = hdr.get("x-job-state", state)
            if n:
                have += n
                arrivals.append((round(now() - t0, 4), have))
        else:
            state = eng.job(jid)["state"]
        if state in ("done", "failed", "cancelled"):
            if not progressive or not n:
                break
        time.sleep(0.02 if progressive else 0.05)
    t_done = now() - t0
    j = eng.job(jid)
    stop.set()
    th.join(2)
    cpu1 = cpu_seconds(pid)
    out = {"state": j["state"], "client_total_s": round(t_done, 3), "error": j.get("error"),
           "engine_cpu_s": round(cpu1 - cpu0, 2) if cpu0 is not None and cpu1 is not None else None}
    out.update(peaks)
    res = j.get("result") or {}
    for k in ("seed", "frames", "audio_seconds", "synthesis_s", "first_frame_s", "first_audio_s",
              "realtime_factor", "peak_gib", "flags"):
        out[k] = res.get(k)
    out["chunks"] = [{k: c.get(k) for k in ("frames", "eos", "frames_per_s", "first_frame_s",
                                             "prompt_build_s", "decode_s", "audio_seconds")}
                     for c in res.get("chunks") or []]
    fps = [c.get("frames_per_s") for c in res.get("chunks") or [] if c.get("frames_per_s")]
    out["steady_frames_per_s"] = round(statistics.median(fps), 1) if fps else None
    if progressive and arrivals:
        rate = 44100.0
        first_t = arrivals[0][0]
        stalls, stall_s, prev_samples = 0, 0.0, arrivals[0][1]
        play_end = first_t + prev_samples / rate
        for t, total in arrivals[1:]:
            if t > play_end + 0.01:                     # the player ran dry before this arrived
                stalls += 1
                stall_s += t - play_end
                play_end = t
            play_end += (total - prev_samples) / rate
            prev_samples = total
        out["client_first_audio_s"] = first_t
        out["playback_stalls"] = stalls
        out["playback_stall_s"] = round(stall_s, 3)
        out["segments"] = len(arrivals)
    if j["state"] == "done":
        wav = eng.audio(jid)
        out["audio_sha256"] = hashlib.sha256(wav).hexdigest()
        if keep_audio:
            out["_wav"] = wav
    return out


def summarize(runs, keys):
    out = {}
    for k in keys:
        vals = [r[k] for r in runs if isinstance(r.get(k), (int, float))]
        if vals:
            out[k] = {"median": round(statistics.median(vals), 3), "min": round(min(vals), 3),
                      "max": round(max(vals), 3), "n": len(vals)}
    out["failed"] = sum(1 for r in runs if r.get("state") != "done")
    out["flags"] = {}
    for r in runs:
        for f in r.get("flags") or []:
            out["flags"][f["kind"]] = out["flags"].get(f["kind"], 0) + 1
    out["chunks_without_eos"] = sum(1 for r in runs for c in r.get("chunks") or []
                                    if c.get("eos") is False)
    return out


# ------------------------------------------------------------------ qwen
def qwen_probe(prompt="Explain in three sentences why the sky is blue.", max_tokens=160):
    """One streamed chat completion against the RUNNING slot-A model."""
    body = json.dumps({"model": "local-model", "stream": True, "max_tokens": max_tokens,
                       "temperature": 0, "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("http://127.0.0.1:8080/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = now()
    first, n = None, 0
    with urllib.request.urlopen(req, timeout=300) as r:
        for line in r:
            line = line.decode("utf-8", "replace").strip()
            if not line.startswith("data:") or line.endswith("[DONE]"):
                continue
            try:
                d = json.loads(line[5:])
            except ValueError:
                continue
            delta = ((d.get("choices") or [{}])[0].get("delta") or {})
            if delta.get("content") or delta.get("reasoning_content"):
                n += 1
                if first is None:
                    first = now() - t0
    total = now() - t0
    return {"ttft_s": round(first, 3) if first else None, "tokens": n, "total_s": round(total, 3),
            "decode_tokens_per_s": round((n - 1) / (total - first), 2) if first and n > 1 else None}


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--cold", action="store_true", help="measure a start with the model files "
                                                        "evicted from the page cache")
    ap.add_argument("--warm-starts", type=int, default=3)
    ap.add_argument("--worst-case", action="store_true",
                    help="also run one chunk to the frame cap, whole and progressive")
    ap.add_argument("--qwen-probe", action="store_true")
    ap.add_argument("--leave-running", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    fish = Fish(a.config or fc.CONFIG_PATH)
    m = fc.model(fish.cfg, fc.read_selection(fish.cfg)[0])
    snap = rp.snapshot()
    qwen_up = [q for q in snap["qwen"] if q["present"]]
    if any(rp.rule_for(snap["compatibility"], q["model"]) != "conditional" for q in qwen_up):
        raise SystemExit("a model that may never run beside Fish is present; not benchmarking")
    report = {"benchmark": "scripts/bench-fish.py", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "machine": machine(), "config": os.path.relpath(fish.cfg["_config_path"], REPO),
              "model": {"id": fc.read_selection(fish.cfg)[0], "display_name": m["display_name"],
                        "variant": m["variant"], "hf_repo": m["source"]["hf_repo"],
                        "hf_revision": m["source"]["hf_revision"]},
              "qwen_present": [{"slot": q["slot"], "model": q["model"], "phase": q["phase"]}
                               for q in qwen_up],
              "segment_frames": SEGMENT_FRAMES, "reps": a.reps}
    print(f"bench-fish: {m['display_name']} {m['variant']}; Qwen present: "
          f"{[q['model'] for q in qwen_up] or 'none'}")

    # ---- lifecycle
    life = {"before": memory_state()}
    if fish.status().get("state") != "STOPPED":
        life["initial_stop"] = fish.stop()
    life["stopped"] = memory_state()
    starts = []
    if a.cold:
        files = [os.path.join(fc.abspath(m["path"]), f["name"])
                 for f in json.load(open(fc.abspath(m["manifest"])))["files"]]
        ev = evict_page_cache(files)
        r = fish.start()
        starts.append({"kind": "cold (model files evicted from the page cache)",
                       "evicted_gib": round(ev / GIB, 2), "wall_s": r["wall_s"],
                       "load_to_ready_s": (r.get("last_start") or {}).get("load_to_ready_s"),
                       "engine_load": (r.get("last_start") or {}).get("engine_load")})
        print(f"  cold start: {r['wall_s']} s")
        life["stop_after_cold"] = fish.stop()
    for i in range(a.warm_starts):
        r = fish.start()
        starts.append({"kind": "warm (model files in the page cache)", "wall_s": r["wall_s"],
                       "load_to_ready_s": (r.get("last_start") or {}).get("load_to_ready_s"),
                       "engine_load": (r.get("last_start") or {}).get("engine_load")})
        print(f"  warm start {i + 1}: {r['wall_s']} s")
        if i < a.warm_starts - 1:
            life.setdefault("stops", []).append(fish.stop())
    life["starts"] = starts
    st = fish.status()
    pid = st["pid"]
    eng_status = fish.engine.json_call("GET", "/v1/status")[1] or {}
    life["idle_loaded"] = {"memory": memory_state(), "engine": eng_status.get("memory"),
                           "process_rss_gib": rss_gib(pid)}
    report["identity"] = {k: v for k, v in fish.engine.identity().items()
                          if k in ("model_id", "hf_revision", "manifest_sha256", "runtime_commit",
                                   "mlx", "mlx_audio", "adapter", "python", "network_sandbox")}
    report["other_workloads"] = other_workloads(exclude_pids=(pid,))

    # ---- the reference voice: the model's own no-reference voice, fixed seed
    eng = fish.engine
    base = {"temperature": 1.0, "top_p": 0.9, "top_k": 30}
    tmp = tempfile.mkdtemp(prefix="bench-fish-")
    ref = run_job(fish, REF_TEXT, [], dict(base, seed=REF_SEED), False, pid, keep_audio=True)
    if ref.get("state") != "done":
        raise SystemExit(f"could not make the test voice: {ref}")
    ref_wav = ref.pop("_wav")
    again = run_job(fish, REF_TEXT, [], dict(base, seed=REF_SEED), False, pid)
    sha = hashlib.sha256(ref_wav).hexdigest()
    path = os.path.join(tmp, "test-voice.wav")
    with open(path, "wb") as f:
        f.write(ref_wav)
    t = now()
    eng.ensure_reference(sha, path)
    encode_s = round(now() - t, 3)
    t = now()
    cached = not eng.ensure_reference(sha, path)
    report["reference"] = {"kind": "the model's own no-reference voice (no person)",
                           "text": REF_TEXT, "seed": REF_SEED, "seconds": ref["audio_seconds"],
                           "sha256": sha, "flags": ref.get("flags"),
                           "same_seed_same_audio": sha == again.get("audio_sha256"),
                           "first_put_encode_s": encode_s,
                           "reuse_check_s": round(now() - t, 3), "reused_from_cache": cached}
    refs = [{"sha256": sha, "transcript": REF_TEXT}]
    os.remove(path)
    os.rmdir(tmp)

    # ---- speech
    speech = {}
    for name, text in CASES:
        reps = a.reps if name != "long" else max(2, a.reps // 2)
        runs = []
        for i in range(reps):
            r = run_job(fish, text, refs, dict(base, seed=1000 + i), True, pid)
            runs.append(r)
            print(f"  {name:<9} seed {1000 + i}: first audio {r.get('first_audio_s')} s "
                  f"(client {r.get('client_first_audio_s')} s), {r.get('audio_seconds')} s audio in "
                  f"{r.get('synthesis_s')} s, RTF {r.get('realtime_factor')}, stalls "
                  f"{r.get('playback_stalls')}, flags {[f['kind'] for f in r.get('flags') or []]}")
        speech[name] = {"text": text, "progressive": True, "runs": runs,
                        "summary": summarize(runs, ("first_frame_s", "first_audio_s",
                                                    "client_first_audio_s", "synthesis_s",
                                                    "audio_seconds", "realtime_factor",
                                                    "steady_frames_per_s", "peak_gib",
                                                    "peak_gpu_alloc_gib", "engine_cpu_s",
                                                    "playback_stalls", "client_total_s"))}
    runs = []
    for i in range(min(3, a.reps)):
        runs.append(run_job(fish, CASES[2][1], refs, dict(base, seed=1000 + i), False, pid))
    speech["medium_whole_take"] = {
        "text": CASES[2][1], "progressive": False, "runs": runs,
        "summary": summarize(runs, ("first_frame_s", "first_audio_s", "synthesis_s",
                                    "audio_seconds", "realtime_factor", "peak_gib",
                                    "peak_gpu_alloc_gib"))}
    rr = run_job(fish, CASES[2][1], refs, dict(base, seed=1000), False, pid)
    report["determinism"] = {"medium_whole_take_seed_1000_twice":
                             rr.get("audio_sha256") == runs[0].get("audio_sha256")}
    report["speech"] = speech

    # ---- worst case: one chunk to the frame cap (the most memory a take needs)
    if a.worst_case:
        cap = int(m["limits"]["max_frames_per_chunk"])
        worst = {"text": CASES[4][1], "settings": dict(base, seed=7, single_chunk=True,
                                                       max_frames_per_chunk=cap)}
        for label, prog in (("not_progressive", False), ("progressive", True)):
            r = run_job(fish, worst["text"], refs, worst["settings"], prog, pid)
            worst[label] = {k: r.get(k) for k in (
                "state", "frames", "audio_seconds", "synthesis_s", "first_frame_s",
                "first_audio_s", "client_first_audio_s", "realtime_factor", "peak_gib",
                "peak_gpu_alloc_gib", "playback_stalls", "flags", "audio_sha256")}
            worst[label]["chunk_frames"] = [c["frames"] for c in r.get("chunks") or []]
            print(f"  worst case ({label}): {r.get('frames')} frames, peak model memory "
                  f"{r.get('peak_gib')} GiB, Metal {r.get('peak_gpu_alloc_gib')} GiB")
        worst["same_audio_either_way"] = (worst["not_progressive"].get("audio_sha256")
                                          == worst["progressive"].get("audio_sha256"))
        report["worst_case"] = worst

    # ---- contention (Qwen must already be running; never started here)
    if a.qwen_probe:
        if not qwen_up:
            report["contention"] = {"skipped": "no Qwen model is running"}
        else:
            alone = [qwen_probe() for _ in range(3)]
            during = []
            for i in range(3):
                box = {}
                th = threading.Thread(target=lambda: box.update(fish=run_job(
                    fish, CASES[4][1], refs, dict(base, seed=2000 + i), True, pid)))
                th.start()
                time.sleep(1.0)
                during.append(qwen_probe())
                th.join()
                during[-1]["fish"] = {k: box["fish"].get(k) for k in
                                      ("first_audio_s", "synthesis_s", "realtime_factor",
                                       "playback_stalls")}
            report["contention"] = {"qwen_alone": alone, "qwen_while_fish_generates": during,
                                    "memory": memory_state()}

    # ---- stop and reclamation
    if not a.leave_running:
        stop = fish.stop()
        time.sleep(2)
        life["final_stop"] = stop
        life["after_stop"] = memory_state()
    report["lifecycle"] = life
    out = a.out or os.path.join(REPO, "benchmarks", "fish",
                                f"bench-{time.strftime('%Y%m%d-%H%M%S')}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
