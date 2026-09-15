#!/usr/bin/env python3
"""
Newton_Local — concurrent A+B correctness proof.

The capacity benchmark measures throughput; this measures CORRECTNESS under
genuine overlap. A and B are given DIFFERENT verifiable questions at the same
instant, with enough budget to actually finish answering, so:

  * each answer can be checked independently,
  * the answers cannot be confused with one another (different questions),
  * overlap is proven from client clocks AND from each server's own /slots.

Runtime n_ctx is queried on both servers WHILE the requests are in flight.
"""
import json, sys, threading, time, urllib.request

TASKS = {
    "a": {"alias": "local-model", "base": "http://127.0.0.1:8080",
          "prompt": "Answer in one short sentence. A freight train leaves at "
                    "14:20 and arrives at 21:05 the same day. How long is the "
                    "journey in hours and minutes? Then state the capital of "
                    "Portugal.",
          "must_contain": ["6", "45", "lisbon"]},
    "b": {"alias": "qwen-b", "base": "http://127.0.0.1:8081",
          "prompt": "Answer in one short sentence. What is 17 * 23, and what "
                    "is the chemical symbol for potassium?",
          "must_contain": ["391", "k"]},
}


def post(base, payload, timeout=900):
    req = urllib.request.Request(base + "/v1/chat/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def get(url, timeout=6):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:
        return {"error": str(e)}


def worker(key, out, barrier):
    t = TASKS[key]
    payload = {"model": t["alias"],
               "messages": [{"role": "user", "content": t["prompt"]}],
               "max_tokens": 4096, "temperature": 0.1}
    barrier.wait(timeout=120)
    t0 = time.time()
    try:
        body = post(t["base"], payload)
        t1 = time.time()
        m = body["choices"][0]["message"]
        text = (m.get("content") or "").strip()
        reasoning = (m.get("reasoning_content") or "").strip()
        tm = body.get("timings", {})
        low = text.lower()
        ok = all(s in low for s in t["must_contain"])
        out[key] = {"alias": t["alias"], "t_start": t0, "t_end": t1,
                    "wall_s": round(t1 - t0, 3),
                    "finish_reason": body["choices"][0].get("finish_reason"),
                    "answer": text, "reasoning_chars": len(reasoning),
                    "gen_tok_s": round(tm.get("predicted_per_second", 0), 3),
                    "prompt_tok_s": round(tm.get("prompt_per_second", 0), 3),
                    "predicted_n": tm.get("predicted_n"),
                    "expected_substrings": t["must_contain"],
                    "correct": ok, "error": None}
    except Exception as e:
        out[key] = {"alias": t["alias"], "t_start": t0, "t_end": time.time(),
                    "error": f"{type(e).__name__}: {e}", "correct": False}


def probe(stop, samples):
    while not stop.is_set():
        s = {"ts": time.time(), "slots": {}, "props": {}}
        for k, t in TASKS.items():
            sl = get(t["base"] + "/slots")
            s["slots"][k] = ([{"id": x.get("id"),
                               "is_processing": x.get("is_processing"),
                               "n_ctx": x.get("n_ctx")}
                              for x in sl if isinstance(x, dict)]
                             if isinstance(sl, list) else sl)
            p = get(t["base"] + "/props")
            s["props"][k] = {"alias": p.get("model_alias"),
                             "ftype": p.get("model_ftype"),
                             "n_ctx": (p.get("default_generation_settings") or {}).get("n_ctx")}
        samples.append(s)
        stop.wait(1.5)


def main():
    print("== concurrent A+B correctness proof")
    out, samples = {}, []
    stop = threading.Event()
    barrier = threading.Barrier(2)
    pr = threading.Thread(target=probe, args=(stop, samples), daemon=True)
    pr.start()
    ths = [threading.Thread(target=worker, args=(k, out, barrier)) for k in TASKS]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    stop.set(); pr.join(timeout=10)

    lo = max(r["t_start"] for r in out.values())
    hi = min(r["t_end"] for r in out.values())
    overlap = max(0.0, hi - lo)
    shortest = min(r["t_end"] - r["t_start"] for r in out.values())
    both = sum(1 for s in samples
               if all(any(x.get("is_processing") for x in (s["slots"].get(k) or []))
                      for k in TASKS))
    nctx = {k: sorted({s["props"][k]["n_ctx"] for s in samples
                       if s["props"].get(k, {}).get("n_ctx")}) for k in TASKS}
    ftype = {k: sorted({s["props"][k]["ftype"] for s in samples
                        if s["props"].get(k, {}).get("ftype")}) for k in TASKS}

    for k in ("a", "b"):
        r = out[k]
        print(f"\n-- {r['alias']}  correct={r['correct']}  "
              f"finish={r.get('finish_reason')}  wall={r.get('wall_s')}s  "
              f"gen={r.get('gen_tok_s')} tok/s  n={r.get('predicted_n')}")
        print(f"   question : {TASKS[k]['prompt']}")
        print(f"   answer   : {r.get('answer')!r}")
        print(f"   expected substrings {r.get('expected_substrings')} -> "
              f"{'ALL PRESENT' if r['correct'] else 'MISSING'}")

    print(f"\n-- overlap {overlap:.3f}s = {overlap/shortest:.0%} of the shorter "
          f"request; both-processing samples {both}/{len(samples)}")
    print(f"-- n_ctx observed DURING flight: a={nctx['a']} b={nctx['b']}")
    print(f"-- quant observed DURING flight: a={ftype['a']} b={ftype['b']}")

    ok = (all(r["correct"] for r in out.values()) and overlap > 0 and both > 0
          and nctx["a"] == [262144] and nctx["b"] == [262144])
    rec = {"ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "results": out,
           "overlap_seconds": round(overlap, 3),
           "overlap_fraction_of_shortest": round(overlap / shortest, 4),
           "samples_with_both_processing": both, "total_samples": len(samples),
           "n_ctx_during_flight": nctx, "ftype_during_flight": ftype,
           "pass": ok, "samples": samples}
    json.dump(rec, open("benchmarks/proving-concurrency.json", "w"), indent=2)
    print(f"\n{'PASS' if ok else 'FAIL'} — wrote benchmarks/proving-concurrency.json")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
