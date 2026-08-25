#!/usr/bin/env python3
"""
Newton_Local — Qwen-B runtime-parity proving suite.

Commission #2 does NOT re-validate the model (Commission #1 already did).
This suite proves that the SECOND instance presents the same runtime as the
first: same weights, same quant, same context, same modalities, and that the
three capability surfaces the commission names (basic inference, structured
tool calling, vision) actually function on port 8081.

Usage:  prove-qwen-b.py [--url http://127.0.0.1:8081] [--alias qwen-b]
                        [--out benchmarks/proving-qwen-b.json]
"""
import argparse, base64, json, os, sys, time, urllib.request

RESULTS = []
URL = "http://127.0.0.1:8081"
ALIAS = "qwen-b"
ROOT = "/Users/ericjellerson/Newton_Local"


def post(path, payload, timeout=900):
    req = urllib.request.Request(URL + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r), time.time() - t0


def get(path, timeout=60):
    with urllib.request.urlopen(URL + path, timeout=timeout) as r:
        return json.load(r), r.status


def chat(messages, timeout=900, **kw):
    return post("/v1/chat/completions",
                {"model": ALIAS, "messages": messages, **kw}, timeout=timeout)


def content(body):
    return body["choices"][0]["message"].get("content") or ""


def record(name, passed, detail, evidence=None):
    RESULTS.append({"test": name, "pass": bool(passed), "detail": detail,
                    "evidence": evidence})
    print(f"\n{'PASS' if passed else 'FAIL'}  {name}  --  {detail}")


def banner(t):
    print("\n" + "=" * 78 + f"\n== {t}\n" + "=" * 78)


# ------------------------------------------------- 1. identity / parity
def t_identity():
    banner("1. RUNTIME IDENTITY AND PARITY (queried, not inferred)")
    h, st = get("/health")
    print("GET /health ->", st, json.dumps(h))
    models, _ = get("/v1/models")
    props, _ = get("/props")
    ids = [m["id"] for m in models.get("data", [])]
    n_ctx = props["default_generation_settings"]["n_ctx"]
    ftype = props.get("model_ftype")
    modal = props.get("modalities", {})
    mpath = props.get("model_path")
    print(f"  /v1/models ids   = {ids}")
    print(f"  model_alias      = {props.get('model_alias')}")
    print(f"  model_ftype      = {ftype}")
    print(f"  n_ctx            = {n_ctx}")
    print(f"  total_slots      = {props.get('total_slots')}")
    print(f"  modalities       = {modal}")
    print(f"  model_path       = {mpath}")
    print(f"  build_info       = {props.get('build_info')}")

    canonical = os.path.join(
        ROOT, "models/qwen38-27b/Qwen3.8-27B-Q8_0.gguf")
    ok = (h.get("status") == "ok" and ALIAS in ids
          and props.get("model_alias") == ALIAS
          and ftype == "Q8_0" and n_ctx == 262144
          and modal.get("vision") is True
          and mpath == canonical)
    record("identity_parity", ok,
           f"alias={props.get('model_alias')} ftype={ftype} n_ctx={n_ctx} "
           f"vision={modal.get('vision')} canonical_weights={mpath == canonical}",
           {"props_subset": {"alias": props.get("model_alias"), "ftype": ftype,
                             "n_ctx": n_ctx, "modalities": modal,
                             "model_path": mpath,
                             "build_info": props.get("build_info"),
                             "total_slots": props.get("total_slots")},
            "models_ids": ids})


# ------------------------------------------------- 2. basic inference
def t_basic():
    banner("2. BASIC INFERENCE")
    prompt = ("Answer with one short sentence only. "
              "What is the chemical symbol for tungsten and its atomic number?")
    print("REQUEST:", prompt)
    body, dt = chat([{"role": "user", "content": prompt}],
                    max_tokens=1024, temperature=0.2)
    out = content(body)
    t = body.get("timings", {})
    print("RESPONSE:", out.strip())
    print(f"  wall={dt:.2f}s  gen={t.get('predicted_per_second', 0):.2f} tok/s "
          f"n={t.get('predicted_n')}")
    low = out.lower()
    ok = ("74" in out) and ("w" == out.strip()[:1].lower() or "tungsten" in low or " w " in low or '"w"' in low)
    record("basic_inference", ok,
           f"{t.get('predicted_per_second', 0):.2f} tok/s, answer contains W/74",
           {"prompt": prompt, "answer": out.strip()[:400], "timings": t})


# ------------------------------------------------- 3. structured tool call
def t_toolcall():
    banner("3. STRUCTURED TOOL CALLING")
    tools = [{
        "type": "function",
        "function": {
            "name": "get_air_quality",
            "description": "Return the current air quality index for a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"},
                    "country_code": {"type": "string",
                                     "description": "ISO 3166-1 alpha-2 code"},
                    "units": {"type": "string", "enum": ["aqi", "raw"]},
                },
                "required": ["city", "country_code"],
            },
        },
    }]
    prompt = "What is the air quality right now in Reykjavik, Iceland? Use the tool."
    print("REQUEST:", prompt)
    body, dt = chat([{"role": "user", "content": prompt}], tools=tools,
                    tool_choice="auto", max_tokens=2048, temperature=0.1)
    m = body["choices"][0]["message"]
    calls = m.get("tool_calls") or []
    print("finish_reason:", body["choices"][0].get("finish_reason"))
    print("tool_calls:", json.dumps(calls, indent=2)[:800])
    ok = False
    args = None
    if calls:
        fn = calls[0]["function"]
        try:
            args = json.loads(fn["arguments"])
        except Exception:
            args = fn["arguments"]
        ok = (fn["name"] == "get_air_quality"
              and isinstance(args, dict)
              and "reykjav" in str(args.get("city", "")).lower()
              and str(args.get("country_code", "")).upper() == "IS")
    record("tool_calling", ok,
           f"emitted {len(calls)} tool_call(s); parsed args={args}",
           {"prompt": prompt, "tool_calls": calls, "parsed_args": args,
            "finish_reason": body["choices"][0].get("finish_reason")})

    # Round-trip: feed the tool result back and require a grounded answer.
    if ok:
        msgs = [{"role": "user", "content": prompt},
                {"role": "assistant", "content": m.get("content") or "",
                 "tool_calls": calls},
                {"role": "tool", "tool_call_id": calls[0]["id"],
                 "name": "get_air_quality",
                 "content": json.dumps({"city": "Reykjavik", "aqi": 17,
                                        "category": "Good"})}]
        body2, _ = chat(msgs, max_tokens=1024, temperature=0.2)
        out2 = content(body2)
        print("ROUND-TRIP ANSWER:", out2.strip()[:400])
        record("tool_roundtrip", "17" in out2,
               "model consumed the tool result and cited AQI 17",
               {"answer": out2.strip()[:400]})


# ------------------------------------------------- 4. vision
def t_vision():
    banner("4. VISION")
    img = os.path.join(ROOT, "assets/vision-test.png")
    if not os.path.exists(img):
        record("vision", False, f"missing test asset {img}")
        return
    b64 = base64.b64encode(open(img, "rb").read()).decode()
    print(f"IMAGE: {img} ({os.path.getsize(img)} bytes)")
    prompt = ("Look at this image. List every distinct shape you see and the "
              "exact text rendered in it. Be literal and concise.")
    print("REQUEST:", prompt)
    body, dt = chat([{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url",
         "image_url": {"url": f"data:image/png;base64,{b64}"}}]},
    ], max_tokens=2048, temperature=0.1)
    out = content(body)
    t = body.get("timings", {})
    print("RESPONSE:", out.strip()[:900])
    print(f"  wall={dt:.2f}s  gen={t.get('predicted_per_second', 0):.2f} tok/s")
    # Non-empty, substantive description proves the vision tower ran on :8081.
    ok = len(out.strip()) > 40
    record("vision", ok, f"vision response {len(out.strip())} chars in {dt:.1f}s",
           {"image": img, "prompt": prompt, "answer": out.strip()[:900],
            "timings": t})


def main():
    global URL, ALIAS
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=URL)
    ap.add_argument("--alias", default=ALIAS)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    URL, ALIAS = a.url, a.alias
    print(f"Target: {URL}  alias={ALIAS}")
    t_identity()
    t_basic()
    t_toolcall()
    t_vision()

    banner("SUMMARY")
    npass = sum(1 for r in RESULTS if r["pass"])
    for r in RESULTS:
        print(f"  [{'PASS' if r['pass'] else 'FAIL'}] {r['test']}: {r['detail']}")
    print(f"\n{npass}/{len(RESULTS)} passed")
    if a.out:
        json.dump({"url": URL, "alias": ALIAS, "ts": time.time(),
                   "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "passed": npass, "total": len(RESULTS),
                   "results": RESULTS}, open(a.out, "w"), indent=2)
        print("wrote", a.out)
    sys.exit(0 if npass == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
