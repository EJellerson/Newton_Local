#!/usr/bin/env python3
"""
Newton_Local — dual-instance (A + B) capacity characterisation.

Proves ACTUAL OVERLAPPING INFERENCE rather than sequential completion, and
records the machine state sampled DURING the overlap window.

Overlap is established three ways, all recorded:
  1. client-side wall clocks   per-request t_start / t_first_token / t_end
  2. computed overlap window   [max(starts), min(ends)] and its duration
  3. server-side slot state    /slots polled on BOTH servers mid-flight, so
                               each server independently reports it was
                               processing at the same instant

No tuning happens here. Parameters are fixed across phases so the A-alone and
A+B numbers are comparable.

Usage:
  bench-dual.py --phase a-alone   --targets a    --out benchmarks/bench-a-alone.json
  bench-dual.py --phase concurrent --targets a,b --out benchmarks/bench-ab.json
"""
import argparse, json, os, subprocess, sys, threading, time
import urllib.request

ENDPOINTS = {"a": ("local-model", "http://127.0.0.1:8080"),
             "b": ("qwen-b", "http://127.0.0.1:8081")}

GEN_PROMPT = ("Write a precise, self-contained technical explanation of how a "
              "write-ahead log guarantees crash consistency in a database. "
              "Cover: the log record format, the commit protocol, checkpointing, "
              "and recovery via redo and undo passes. Use plain prose, no lists, "
              "no headings. Be thorough and keep writing until the topic is "
              "fully covered.")

SNAPSHOT = "/Users/ericjellerson/Newton_Local/scripts/snapshot-machine.py"

# Deterministic filler used to make prompt-processing rate measurable. The
# nonce makes every run's prompt unique so llama.cpp's prompt cache cannot
# turn a prompt-processing measurement into a cache hit.
_WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo "
          "lima mike november oscar papa quebec romeo sierra tango uniform "
          "victor whiskey xray yankee zulu").split()


def build_prompt(nonce, pad_words):
    if not pad_words:
        return f"[run {nonce}] " + GEN_PROMPT
    rng = 0
    for ch in str(nonce):
        rng = (rng * 131 + ord(ch)) & 0xFFFFFFFF
    filler = []
    for i in range(pad_words):
        rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
        filler.append(_WORDS[rng % len(_WORDS)])
    return ("Below is a reference corpus. Read it, then answer the question "
            f"that follows.\n\nCORPUS [{nonce}]:\n" + " ".join(filler)
            + "\n\nQUESTION:\n" + GEN_PROMPT)


def post_json(url, payload, timeout=1800):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r), r.status


def get_json(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r), r.status
    except Exception as e:
        return {"error": str(e)}, None


def run_generation(key, prompt, max_tokens, results, barrier, think=False):
    """One non-streaming generation. Records precise client-side timestamps."""
    alias, base = ENDPOINTS[key]
    payload = {"model": alias,
               "messages": [{"role": "user", "content": prompt}],
               "max_tokens": max_tokens, "temperature": 0.3, "top_p": 0.95,
               "seed": 12345}
    rec = {"target": key, "alias": alias, "endpoint": base,
           "max_tokens": max_tokens}
    try:
        barrier.wait(timeout=120)          # release both requests together
    except Exception:
        pass
    t0 = time.time()
    rec["t_start"] = t0
    try:
        body, st = post_json(base + "/v1/chat/completions", payload)
        t1 = time.time()
        rec["t_end"] = t1
        rec["wall_s"] = round(t1 - t0, 3)
        rec["http_status"] = st
        msg = body["choices"][0]["message"]
        text = (msg.get("content") or "")
        rec["finish_reason"] = body["choices"][0].get("finish_reason")
        rec["answer_chars"] = len(text)
        rec["answer_head"] = text.strip()[:260]
        rec["answer_tail"] = text.strip()[-160:]
        t = body.get("timings", {})
        rec["timings"] = t
        rec["gen_tok_s"] = round(t.get("predicted_per_second", 0.0), 3)
        rec["prompt_tok_s"] = round(t.get("prompt_per_second", 0.0), 3)
        rec["predicted_n"] = t.get("predicted_n")
        rec["prompt_n"] = t.get("prompt_n")
        rec["error"] = None
    except Exception as e:
        rec["t_end"] = time.time()
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["gen_tok_s"] = None
    results[key] = rec


def sampler(stop_evt, samples, targets, interval=2.0):
    """Sample machine state + BOTH servers' slot state while requests fly."""
    while not stop_evt.is_set():
        s = {"ts": time.time()}
        try:
            out = subprocess.run([sys.executable, SNAPSHOT, "inflight"],
                                 capture_output=True, text=True, timeout=25).stdout
            d = json.loads(out)
            s["memory"] = d["memory"]
            s["swap"] = d["swap"]
            s["pressure"] = d["pressure"]
            s["gpu"] = d["gpu"]
            s["llama_servers"] = d["llama_servers"]
            s["unrelated_hot_processes"] = d["unrelated_hot_processes"]
        except Exception as e:
            s["snapshot_error"] = str(e)
        # Independent server-side evidence that each instance is mid-generation.
        s["slots"] = {}
        for k in targets:
            _, base = ENDPOINTS[k]
            sl, _ = get_json(base + "/slots", timeout=5)
            if isinstance(sl, list) and sl:
                rows = []
                for x in sl:
                    if not isinstance(x, dict):
                        continue
                    nt = x.get("next_token")
                    rows.append({
                        "id": x.get("id"),
                        "is_processing": x.get("is_processing"),
                        "n_ctx": x.get("n_ctx"),
                        "n_decoded": (nt.get("n_decoded")
                                      if isinstance(nt, dict) else None),
                    })
                s["slots"][k] = rows
            else:
                s["slots"][k] = sl
        samples.append(s)
        stop_evt.wait(interval)


def _sampler_guard(stop_evt, samples, targets, interval=2.0):
    """Never let a sampling error abort the measurement run."""
    try:
        sampler(stop_evt, samples, targets, interval)
    except Exception as e:                      # pragma: no cover - defensive
        samples.append({"ts": time.time(), "sampler_fatal": repr(e)})


def runtime_props(targets):
    """Query live n_ctx / quant on each target — never inferred from argv."""
    out = {}
    for k in targets:
        _, base = ENDPOINTS[k]
        p, _ = get_json(base + "/props", timeout=10)
        out[k] = {
            "alias": p.get("model_alias"),
            "model_ftype": p.get("model_ftype"),
            "n_ctx": (p.get("default_generation_settings") or {}).get("n_ctx"),
            "total_slots": p.get("total_slots"),
            "model_path": p.get("model_path"),
            "modalities": p.get("modalities"),
            "build_info": p.get("build_info"),
        }
    return out


def scrape_metrics(targets):
    out = {}
    for k in targets:
        _, base = ENDPOINTS[k]
        try:
            with urllib.request.urlopen(base + "/metrics", timeout=10) as r:
                body = r.read().decode()
            keep = {}
            for line in body.splitlines():
                if line.startswith("#") or not line.strip():
                    continue
                name, _, val = line.partition(" ")
                if any(s in name for s in ("tokens_predicted", "prompt_tokens",
                                           "requests_processing", "requests_deferred",
                                           "kv_cache", "n_decode_total",
                                           "n_busy_slots")):
                    keep[name] = val.strip()
            out[k] = keep
        except Exception as e:
            out[k] = {"error": str(e)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True)
    ap.add_argument("--targets", default="a", help="comma list from a,b")
    ap.add_argument("--tokens", type=int, default=700)
    ap.add_argument("--long-prompt-tokens", type=int, default=0,
                    help="if >0, also run a prompt-processing measurement")
    ap.add_argument("--out", default=None)
    ap.add_argument("--nonce", default=None,
                    help="unique marker so the prompt cache cannot be hit")
    ap.add_argument("--prompt-pad-words", type=int, default=0,
                    help="filler words prepended to make prompt-processing "
                         "rate measurable")
    args = ap.parse_args()
    if not args.nonce:
        args.nonce = f"{args.phase}-{int(time.time())}"
    prompt = build_prompt(args.nonce, args.prompt_pad_words)
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    for t in targets:
        if t not in ENDPOINTS:
            sys.exit(f"unknown target {t}")

    print(f"== phase '{args.phase}'  targets={targets}  max_tokens={args.tokens}  "
          f"prompt_chars={len(prompt)}  nonce={args.nonce}")
    props_before = runtime_props(targets)
    for k, v in props_before.items():
        print(f"   {ENDPOINTS[k][0]}: ftype={v['model_ftype']} n_ctx={v['n_ctx']} "
              f"slots={v['total_slots']}")

    pre = json.loads(subprocess.run(
        [sys.executable, SNAPSHOT, f"{args.phase}-pre"],
        capture_output=True, text=True).stdout)

    results, samples = {}, []
    stop_evt = threading.Event()
    barrier = threading.Barrier(len(targets))
    smp = threading.Thread(target=_sampler_guard,
                           args=(stop_evt, samples, targets), daemon=True)
    smp.start()

    threads = [threading.Thread(target=run_generation,
                                args=(k, prompt, args.tokens, results, barrier))
               for k in targets]
    t_phase0 = time.time()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    t_phase1 = time.time()

    # runtime config queried again WHILE the phase is still hot
    props_during = runtime_props(targets)
    stop_evt.set()
    smp.join(timeout=30)

    post = json.loads(subprocess.run(
        [sys.executable, SNAPSHOT, f"{args.phase}-post"],
        capture_output=True, text=True).stdout)

    # ---- overlap analysis -------------------------------------------------
    overlap = None
    if len(targets) > 1 and all(results.get(k, {}).get("t_end") for k in targets):
        starts = [results[k]["t_start"] for k in targets]
        ends = [results[k]["t_end"] for k in targets]
        lo, hi = max(starts), min(ends)
        dur = max(0.0, hi - lo)
        spans = [results[k]["t_end"] - results[k]["t_start"] for k in targets]
        overlap = {
            "overlap_window_start": lo, "overlap_window_end": hi,
            "overlap_seconds": round(dur, 3),
            "per_request_wall_s": {k: round(results[k]["t_end"] - results[k]["t_start"], 3)
                                   for k in targets},
            "overlap_fraction_of_shortest": round(dur / min(spans), 4) if min(spans) else None,
            "requests_truly_concurrent": dur > 0,
        }
        # server-side corroboration: samples where BOTH report is_processing
        both = 0
        for s in samples:
            sl = s.get("slots", {})
            try:
                if all(any(x.get("is_processing") for x in sl.get(k, []))
                       for k in targets):
                    both += 1
            except Exception:
                pass
        overlap["samples_with_all_servers_processing"] = both
        overlap["total_samples"] = len(samples)

    # ---- in-flight machine state ------------------------------------------
    def agg(path, fn):
        vals = []
        for s in samples:
            cur = s
            for p in path:
                cur = (cur or {}).get(p) if isinstance(cur, dict) else None
            if isinstance(cur, (int, float)):
                vals.append(cur)
        return fn(vals) if vals else None

    inflight = {
        "samples": len(samples),
        "gpu_alloc_gib_max": agg(["gpu", "alloc_system_memory_gib"], max),
        "gpu_util_pct_max": agg(["gpu", "device_utilization_pct"], max),
        "free_gib_min": agg(["memory", "free_gib"], min),
        "app_memory_gib_max": agg(["memory", "app_memory_gib"], max),
        "cached_files_gib_min": agg(["memory", "cached_files_gib"], min),
        "reclaimable_headroom_gib_min": agg(["memory", "reclaimable_headroom_gib"], min),
        "compressor_gib_max": agg(["memory", "compressor_occupied_gib"], max),
        "wired_gib_max": agg(["memory", "wired_gib"], max),
        "swapouts_max": agg(["memory", "swapouts"], max),
        "swap_used_mib_max": agg(["swap", "used_mib"], max),
        "pressure_free_pct_min": agg(["pressure", "free_percentage"], min),
        "pressure_levels": sorted({s.get("pressure", {}).get("vm_pressure_level")
                                   for s in samples if s.get("pressure")} - {None}),
    }
    rss = {}
    for s in samples:
        for srv in s.get("llama_servers", []) or []:
            if srv.get("alias"):
                rss.setdefault(srv["alias"], []).append(srv["rss_gib"])
    inflight["rss_gib_max"] = {k: max(v) for k, v in rss.items()}
    hot = []
    for s in samples:
        for h in s.get("unrelated_hot_processes", []) or []:
            hot.append({"pid": h["pid"], "cpu_pct": h["cpu_pct"],
                        "rss_gib": h["rss_gib"], "comm": h["comm"]})
    inflight["unrelated_hot_processes_seen"] = hot
    inflight["contaminated"] = (None if not samples else bool(hot))

    record = {
        "phase": args.phase, "targets": targets, "max_tokens": args.tokens,
        "ts": t_phase0, "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z",
                                                time.localtime(t_phase0)),
        "phase_wall_s": round(t_phase1 - t_phase0, 3),
        "prompt_nonce": args.nonce,
        "prompt_pad_words": args.prompt_pad_words,
        "prompt_chars": len(prompt),
        "prompt": prompt if len(prompt) < 2000 else prompt[:800] + " ...[truncated]... " + prompt[-400:],
        "runtime_props_before": props_before,
        "runtime_props_during": props_during,
        "results": results,
        "overlap": overlap,
        "inflight": inflight,
        "metrics_after": scrape_metrics(targets),
        "snapshot_pre": pre,
        "snapshot_post": post,
        "samples": samples,
    }

    print(f"\n-- phase wall {record['phase_wall_s']}s")
    for k in targets:
        r = results.get(k, {})
        print(f"   {ENDPOINTS[k][0]}: gen={r.get('gen_tok_s')} tok/s  "
              f"prompt={r.get('prompt_tok_s')} tok/s  n={r.get('predicted_n')}  "
              f"wall={r.get('wall_s')}s  finish={r.get('finish_reason')}  "
              f"err={r.get('error')}")
    if overlap:
        print(f"   overlap: {overlap['overlap_seconds']}s "
              f"({overlap['overlap_fraction_of_shortest']:.0%} of the shorter "
              f"request); both-processing samples "
              f"{overlap['samples_with_all_servers_processing']}/{overlap['total_samples']}")
    print(f"   in-flight: gpu_alloc_max={inflight['gpu_alloc_gib_max']} GiB  "
          f"swapouts={inflight['swapouts_max']}  "
          f"compressor_max={inflight['compressor_gib_max']} GiB  "
          f"pressure_levels={inflight['pressure_levels']}")
    print(f"   contaminated_by_unrelated_load = {inflight['contaminated']}")

    if args.out:
        with open(args.out, "w") as f:
            json.dump(record, f, indent=2)
        print(f"   wrote {args.out}")


if __name__ == "__main__":
    main()
