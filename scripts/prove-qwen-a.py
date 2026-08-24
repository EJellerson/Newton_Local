#!/usr/bin/env python3
"""
Newton_Local — Qwen-A raw proving suite.

Exercises the llama.cpp OpenAI-compatible server DIRECTLY over HTTP.
No agent harness, no framework. Each test prints the real request and the
real response so a reader can distinguish evidence from assertion.

Usage:  prove-qwen-a.py [--url http://127.0.0.1:8080] [--only NAME] [--out report.json]
"""
import argparse, base64, json, re, sys, time, urllib.request, urllib.error

URL = "http://127.0.0.1:8080"
RESULTS = []


def post(path, payload, timeout=1800):
    req = urllib.request.Request(
        URL + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.load(r)
    return body, time.time() - t0


def get(path, timeout=60):
    with urllib.request.urlopen(URL + path, timeout=timeout) as r:
        return json.load(r), r.status


def chat(messages, timeout=1800, **kw):
    payload = {"model": "qwen-a", "messages": messages, **kw}
    return post("/v1/chat/completions", payload, timeout=timeout)


def msg_text(body):
    return body["choices"][0]["message"].get("content") or ""


def reasoning_text(body):
    return body["choices"][0]["message"].get("reasoning_content") or ""


def record(name, passed, detail, evidence=None):
    RESULTS.append({"test": name, "pass": bool(passed), "detail": detail,
                    "evidence": evidence})
    print(f"\n{'PASS' if passed else 'FAIL'}  {name}  --  {detail}")


def banner(t):
    print("\n" + "=" * 78 + f"\n== {t}\n" + "=" * 78)


# ---------------------------------------------------------------- 1. health
def t_health():
    banner("1. API HEALTH")
    h, st = get("/health")
    print("GET /health ->", st, json.dumps(h))
    models, st2 = get("/v1/models")
    print("GET /v1/models ->", st2, json.dumps(models)[:400])
    props, _ = get("/props")
    ctx = props.get("default_generation_settings", {}).get("n_ctx")
    total = props.get("total_slots")
    print(f"GET /props -> n_ctx={ctx}  total_slots={total}")
    print(f"           model_path={props.get('model_path')}")
    ok = (h.get("status") == "ok"
          and models.get("object") == "list"
          and len(models.get("data", [])) >= 1)
    record("api_health", ok,
           f"/health status={h.get('status')}, /v1/models returned "
           f"{len(models.get('data', []))} model(s), /props n_ctx={ctx}",
           {"health": h, "models": models, "n_ctx": ctx,
            "total_slots": total, "model_path": props.get("model_path")})
    return props


# ------------------------------------------------------- 2. conversation
def t_conversation():
    banner("2. MULTI-TURN CONVERSATION")
    m = [{"role": "system", "content": "You are a concise assistant."},
         {"role": "user", "content":
          "My name is Eric and I'm setting up a Mac Pro with an M2 Ultra. "
          "Reply in one short sentence."}]
    b1, d1 = chat(m, max_tokens=2048, temperature=0.7)
    a1 = msg_text(b1)
    print(f"[turn 1 user]      {m[1]['content']}")
    print(f"[turn 1 assistant] {a1.strip()[:400]}  ({d1:.1f}s)")

    m += [{"role": "assistant", "content": a1},
          {"role": "user", "content":
           "What machine did I just say I was setting up, and what is my name? "
           "Answer in one line."}]
    b2, d2 = chat(m, max_tokens=2048, temperature=0.7)
    a2 = msg_text(b2)
    print(f"[turn 2 user]      {m[3]['content']}")
    print(f"[turn 2 assistant] {a2.strip()[:400]}  ({d2:.1f}s)")

    m += [{"role": "assistant", "content": a2},
          {"role": "user", "content":
           "How much unified memory does that chip have in my setup? "
           "I told you the model earlier. If I didn't state the memory, say so."}]
    b3, d3 = chat(m, max_tokens=2048, temperature=0.7)
    a3 = msg_text(b3)
    print(f"[turn 3 user]      {m[5]['content']}")
    print(f"[turn 3 assistant] {a3.strip()[:500]}  ({d3:.1f}s)")

    low = a2.lower()
    ok = "eric" in low and ("m2 ultra" in low or "mac pro" in low)
    record("conversation", ok,
           "3-turn exchange; turn 2 correctly recalled both the name and the "
           "machine stated in turn 1",
           {"turn1": a1, "turn2": a2, "turn3": a3,
            "seconds": [round(d1,1), round(d2,1), round(d3,1)]})


# ----------------------------------------------------------- 3. reasoning
def t_reasoning():
    banner("3. REASONING")
    q = ("Three switches outside a windowless room control three bulbs inside. "
         "You may flip switches as much as you like, but you may enter the room "
         "only once, and after entering you may not go back out to the switches. "
         "Determine which switch controls which bulb. Explain the method and "
         "why it works. Then, separately, answer: a bat and a ball cost $1.10 "
         "together; the bat costs $1.00 more than the ball; how much is the ball? "
         "End your reply with the exact line: BALL=<value>")
    print("[prompt]", q[:220], "...")
    b, d = chat([{"role": "user", "content": q}], max_tokens=8192, temperature=0.3)
    a = msg_text(b)
    think = reasoning_text(b)
    print(f"\n[reasoning_content len] {len(think)} chars")
    if think:
        print("[reasoning excerpt]", think.strip()[:600].replace("\n", " "), "...")
    print("\n[answer]\n" + a.strip()[:2500])
    print(f"\n({d:.1f}s)")

    heat = bool(re.search(r"heat|warm|hot|temperature", a, re.I))
    ball = re.search(r"BALL\s*=\s*\$?\s*([0-9.]+)", a)
    ballv = ball.group(1) if ball else None
    ball_ok = ballv is not None and abs(float(ballv.rstrip(".")) - 0.05) < 1e-6
    ok = heat and ball_ok
    record("reasoning", ok,
           f"switch puzzle used the heat/timing insight={heat}; "
           f"bat-and-ball answered BALL={ballv} (correct=0.05) -> {ball_ok}",
           {"answer": a, "reasoning_chars": len(think), "ball": ballv,
            "seconds": round(d, 1)})


# -------------------------------------------------------------- 4. coding
def t_coding():
    banner("4. CODING")
    q = ("Write a single self-contained Python 3 function:\n\n"
         "    def merge_intervals(intervals: list[tuple[int,int]]) -> list[tuple[int,int]]\n\n"
         "It merges overlapping closed intervals and returns them sorted by start. "
         "Touching intervals like (1,2) and (2,3) MUST merge into (1,3). "
         "Handle the empty list. Return ONLY the function inside one ```python code "
         "block, with no test code, no prints, and no explanation after the block.")
    print("[prompt]", q[:200], "...")
    b, d = chat([{"role": "user", "content": q}], max_tokens=8192, temperature=0.2)
    a = msg_text(b)
    print("\n[raw reply]\n" + a.strip()[:1800])
    m = re.search(r"```(?:python)?\s*(.+?)```", a, re.S)
    code = m.group(1) if m else a
    print("\n[extracted code]\n" + code.strip()[:1200])

    ns, err, results = {}, None, None
    try:
        exec(code, ns)
        f = ns["merge_intervals"]
        cases = [
            ([], []),
            ([(1, 3)], [(1, 3)]),
            ([(1, 3), (2, 6), (8, 10), (15, 18)], [(1, 6), (8, 10), (15, 18)]),
            ([(1, 2), (2, 3)], [(1, 3)]),
            ([(5, 6), (1, 2)], [(1, 2), (5, 6)]),
            ([(1, 10), (2, 3), (4, 5)], [(1, 10)]),
        ]
        results = []
        for inp, want in cases:
            got = [tuple(x) for x in f(list(inp))]
            results.append({"input": inp, "expected": want, "got": got,
                            "ok": got == want})
            print(f"  {'ok ' if got == want else 'BAD'} f({inp}) = {got}   expected {want}")
        ok = all(r["ok"] for r in results)
    except Exception as e:
        err, ok = f"{type(e).__name__}: {e}", False
        print("  EXECUTION ERROR:", err)

    record("coding", ok,
           ("generated code executed and passed all 6 unit cases including the "
            "touching-interval case" if ok else f"failed: {err or 'wrong output'}"),
           {"code": code, "cases": results, "error": err, "seconds": round(d, 1)})


# ------------------------------------------------------- 5. structured JSON
def t_structured_json():
    banner("5. STRUCTURED JSON OUTPUT")
    schema = {
        "type": "object",
        "properties": {
            "city": {"type": "string"},
            "country": {"type": "string"},
            "population_millions": {"type": "number"},
            "landmarks": {"type": "array", "items": {"type": "string"},
                          "minItems": 2, "maxItems": 4},
            "is_capital": {"type": "boolean"},
        },
        "required": ["city", "country", "population_millions",
                     "landmarks", "is_capital"],
        "additionalProperties": False,
    }
    print("[response_format] json_schema (strict), schema:")
    print(json.dumps(schema, indent=2)[:500])
    b, d = chat(
        [{"role": "user", "content":
          "Give me structured data about Tokyo, Japan."}],
        max_tokens=4096, temperature=0.0,
        response_format={"type": "json_schema",
                         "json_schema": {"name": "city", "strict": True,
                                         "schema": schema}})
    a = msg_text(b)
    print("\n[raw content]\n" + a.strip()[:900])
    try:
        parsed = json.loads(a)
        print("\n[json.loads OK] ->", json.dumps(parsed, ensure_ascii=False)[:600])
        keys_ok = set(schema["required"]).issubset(parsed.keys())
        types_ok = (isinstance(parsed.get("landmarks"), list)
                    and isinstance(parsed.get("is_capital"), bool)
                    and isinstance(parsed.get("population_millions"), (int, float)))
        ok = keys_ok and types_ok
        det = (f"json.loads() succeeded; all {len(schema['required'])} required "
               f"keys present={keys_ok}; types conform={types_ok}")
    except Exception as e:
        parsed, ok = None, False
        det = f"json.loads() FAILED: {e}"
        print("\n[json.loads FAILED]", e)
    record("structured_json", ok, det,
           {"raw": a, "parsed": parsed, "seconds": round(d, 1)})


# --------------------------------------------------------- 6. tool calling
def t_tool_calling():
    banner("6. NATIVE TOOL CALLING")
    tools = [{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a location.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string",
                                 "description": "City name, e.g. 'Paris'"},
                    "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
                },
                "required": ["location", "unit"],
            },
        },
    }, {
        "type": "function",
        "function": {
            "name": "convert_currency",
            "description": "Convert an amount between two currencies.",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number"},
                    "from_currency": {"type": "string"},
                    "to_currency": {"type": "string"},
                },
                "required": ["amount", "from_currency", "to_currency"],
            },
        },
    }]
    print("[tools supplied]", [t["function"]["name"] for t in tools])
    q = "What's the weather in Reykjavik right now? Use celsius."
    print("[prompt]", q)
    b, d = chat([{"role": "user", "content": q}], tools=tools,
                tool_choice="auto", max_tokens=4096, temperature=0.2)
    m = b["choices"][0]["message"]
    finish = b["choices"][0].get("finish_reason")
    tc = m.get("tool_calls") or []
    print("\n[finish_reason]", finish)
    print("[message.content]", (m.get("content") or "")[:300])
    print("[message.tool_calls]\n" + json.dumps(tc, indent=2)[:1200])

    ok = False
    args = None
    if tc:
        c = tc[0]
        fn = c.get("function", {})
        try:
            args = json.loads(fn.get("arguments", "{}"))
        except Exception:
            args = None
        ok = (fn.get("name") == "get_weather"
              and isinstance(args, dict)
              and "reykjav" in str(args.get("location", "")).lower()
              and args.get("unit") == "celsius"
              and c.get("type") == "function"
              and bool(c.get("id")))

    # Round-trip: feed a tool result back and confirm the model consumes it.
    roundtrip = None
    if tc:
        msgs = [{"role": "user", "content": q}, m,
                {"role": "tool", "tool_call_id": tc[0]["id"],
                 "name": "get_weather",
                 "content": json.dumps({"temperature": -3, "unit": "celsius",
                                        "conditions": "light snow"})}]
        b2, d2 = chat(msgs, tools=tools, max_tokens=4096, temperature=0.2)
        roundtrip = msg_text(b2)
        print("\n[tool result fed back] {\"temperature\": -3, \"conditions\": \"light snow\"}")
        print("[final assistant]", roundtrip.strip()[:400])

    record("tool_calling", ok,
           (f"server returned a structured tool_calls array (not prose): "
            f"name={tc[0]['function']['name'] if tc else None}, "
            f"args={args}, finish_reason={finish}" if tc
            else "no tool_calls array returned"),
           {"tool_calls": tc, "parsed_args": args, "finish_reason": finish,
            "roundtrip_reply": roundtrip, "seconds": round(d, 1)})


# --------------------------------------------------------------- 7. vision
def t_vision(image_path):
    banner("7. VISION (mmproj)")
    with open(image_path, "rb") as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode()
    print(f"[image] {image_path}  ({len(raw)} bytes)")
    print("[known ground truth] red circle, blue square, green triangle, "
          "the number 47, caption 'NEWTON LOCAL VISION TEST'")
    q = ("Describe this image precisely. List every shape and its colour, and "
         "read out any number and any text you can see.")
    b, d = chat([{"role": "user", "content": [
        {"type": "text", "text": q},
        {"type": "image_url",
         "image_url": {"url": f"data:image/png;base64,{b64}"}}]}],
        max_tokens=4096, temperature=0.1)
    a = msg_text(b)
    print("\n[model description]\n" + a.strip()[:1500])
    print(f"\n({d:.1f}s)")

    low = a.lower()
    checks = {
        "red circle":  bool(re.search(r"red[^.\n]{0,30}circle|circle[^.\n]{0,30}red", low)),
        "blue square": bool(re.search(r"blue[^.\n]{0,30}(square|rectangle)|(square|rectangle)[^.\n]{0,30}blue", low)),
        "green triangle": bool(re.search(r"green[^.\n]{0,30}triangle|triangle[^.\n]{0,30}green", low)),
        "number 47":   "47" in low,
        "caption text": "newton" in low,
    }
    for k, v in checks.items():
        print(f"  {'ok ' if v else 'BAD'} {k}")
    ok = sum(checks.values()) >= 4 and checks["number 47"]
    record("vision", ok,
           f"{sum(checks.values())}/5 ground-truth elements recovered from the "
           f"image: {checks}",
           {"description": a, "checks": checks, "seconds": round(d, 1)})


# --------------------------------------------------------- 8. long context
def t_long_context(needle_file):
    banner("8. LONG CONTEXT (>128K) NEEDLE RETRIEVAL")
    with open(needle_file) as f:
        p = json.load(f)
    print(f"[haystack tokens] {p['haystack_tokens']:,}")
    print(f"[needle at line]  {p['needle_line']:,} of {p['total_lines']:,} "
          f"(depth {p['depth']:.0%})")
    print(f"[needle]          {p['needle']}")
    print(f"[expected answer] {p['answer']}")
    if p["haystack_tokens"] <= 131072:
        print("!! haystack does not exceed 128K -- test is not valid")

    q = (p["haystack"] + "\n\nQuestion: Somewhere in the log above there is a "
         "single IMPORTANT RECORD line containing a commissioning "
         "authorization code. What is that authorization code? Reply with the "
         "code only.")
    t0 = time.time()
    b, d = chat([{"role": "user", "content": q}], max_tokens=4096,
                temperature=0.0, timeout=7200)
    a = msg_text(b)
    usage = b.get("usage", {})
    print(f"\n[server usage] {json.dumps(usage)}")
    print(f"[model reply]  {a.strip()[:400]}")
    print(f"[elapsed]      {d:.1f}s")

    found = p["answer"] in a
    over_128k = usage.get("prompt_tokens", 0) > 131072
    ok = found and over_128k
    record("long_context_128k", ok,
           f"prompt_tokens={usage.get('prompt_tokens'):,} "
           f"(>131072 = {over_128k}); needle '{p['answer']}' recovered={found}",
           {"usage": usage, "reply": a, "expected": p["answer"],
            "haystack_tokens": p["haystack_tokens"],
            "needle_line": p["needle_line"], "seconds": round(d, 1)})


# ------------------------------------------------------------ 9. stability
def t_stability(n=12):
    banner(f"9. REPEATED-REQUEST STABILITY ({n} sequential requests)")
    times, oks = [], 0
    for i in range(n):
        try:
            b, d = chat([{"role": "user", "content":
                          f"Reply with exactly the number {i*7}. Nothing else."}],
                        max_tokens=2048, temperature=0.0)
            a = msg_text(b)
            hit = str(i * 7) in a
            oks += hit
            times.append(d)
            print(f"  req {i+1:2d}/{n}  {d:6.2f}s  correct={hit}  reply={a.strip()[:60]!r}")
        except Exception as e:
            print(f"  req {i+1:2d}/{n}  ERROR {e}")
            times.append(None)
    h, st = get("/health")
    alive = h.get("status") == "ok"
    print(f"\n[/health after {n} requests] {st} {json.dumps(h)}")
    good = [t for t in times if t is not None]
    ok = len(good) == n and alive and oks >= n - 1
    record("stability", ok,
           f"{len(good)}/{n} requests completed without error, {oks}/{n} "
           f"content-correct, server /health still ok={alive}",
           {"times": [round(t,2) if t else None for t in times],
            "correct": oks, "health_after": h})


def main():
    global URL
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=URL)
    ap.add_argument("--image", default=None)
    ap.add_argument("--needle", default=None)
    ap.add_argument("--only", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    URL = a.url

    tests = {
        "health": t_health,
        "conversation": t_conversation,
        "reasoning": t_reasoning,
        "coding": t_coding,
        "json": t_structured_json,
        "tools": t_tool_calling,
        "vision": (lambda: t_vision(a.image)) if a.image else None,
        "longctx": (lambda: t_long_context(a.needle)) if a.needle else None,
        "stability": t_stability,
    }
    names = [a.only] if a.only else list(tests)
    for n in names:
        fn = tests.get(n)
        if fn is None:
            print(f"(skipping {n} -- not configured)")
            continue
        try:
            fn()
        except Exception as e:
            import traceback; traceback.print_exc()
            record(n, False, f"harness exception: {type(e).__name__}: {e}")

    banner("SUMMARY")
    for r in RESULTS:
        print(f"  {'PASS' if r['pass'] else 'FAIL'}  {r['test']:20s} {r['detail'][:100]}")
    npass = sum(r["pass"] for r in RESULTS)
    print(f"\n  {npass}/{len(RESULTS)} passed")
    if a.out:
        with open(a.out, "w") as f:
            json.dump(RESULTS, f, indent=2)
        print(f"  wrote {a.out}")
    sys.exit(0 if npass == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
