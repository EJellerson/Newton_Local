#!/usr/bin/env python3
"""Newton_Local — Qwen-A throughput baseline via the live server API."""
import json, urllib.request, statistics, sys

URL = "http://127.0.0.1:8080"

def chat(msgs, **kw):
    req = urllib.request.Request(URL + "/v1/chat/completions",
        data=json.dumps({"model": "local-model", "messages": msgs, **kw}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3600) as r:
        return json.load(r)

# Deterministic, thinking disabled, so we measure the runtime not the model's mood.
COMMON = dict(max_tokens=400, temperature=0.0,
              chat_template_kwargs={"enable_thinking": False})

rows = []
print(f"{'test':28s} {'prompt_n':>9s} {'pp tok/s':>10s} {'gen_n':>7s} {'tg tok/s':>10s}")
print("-" * 70)

# Short prompt, pure generation speed.
for i in range(3):
    b = chat([{"role": "user", "content":
               "Write a 300-word description of a lighthouse in winter."}], **COMMON)
    t = b["timings"]
    rows.append(("short_gen", t))
    print(f"{'short prompt / gen '+str(i+1):28s} {t['prompt_n']:9d} "
          f"{t['prompt_per_second']:10.1f} {t['predicted_n']:7d} {t['predicted_per_second']:10.2f}")

# Medium prompt, prompt-processing speed.
filler = ("The quarterly maintenance report notes that all subsystems "
          "operated within nominal parameters during the review period. ") * 200
for i in range(2):
    b = chat([{"role": "user", "content": filler +
               "\n\nSummarize the above in one sentence."}], **COMMON)
    t = b["timings"]
    rows.append(("medium_pp", t))
    print(f"{'medium prompt / pp '+str(i+1):28s} {t['prompt_n']:9d} "
          f"{t['prompt_per_second']:10.1f} {t['predicted_n']:7d} {t['predicted_per_second']:10.2f}")

# Long-ish prompt.
filler2 = ("Sensor telemetry for bay four recorded a stable thermal gradient "
           "with no anomalies detected across the sampling window. ") * 1200
b = chat([{"role": "user", "content": filler2 + "\n\nReply with one sentence."}], **COMMON)
t = b["timings"]
rows.append(("long_pp", t))
print(f"{'long prompt / pp':28s} {t['prompt_n']:9d} "
      f"{t['prompt_per_second']:10.1f} {t['predicted_n']:7d} {t['predicted_per_second']:10.2f}")

print("-" * 70)
sg = [t["predicted_per_second"] for n, t in rows if n == "short_gen"]
print(f"generation (short ctx)  median {statistics.median(sg):.2f} tok/s  "
      f"min {min(sg):.2f}  max {max(sg):.2f}")
mp = [t["prompt_per_second"] for n, t in rows if n == "medium_pp"]
print(f"prompt processing (~4k) median {statistics.median(mp):.1f} tok/s")
lp = [t["prompt_per_second"] for n, t in rows if n == "long_pp"]
print(f"prompt processing (~26k)       {lp[0]:.1f} tok/s")

json.dump([{"test": n, **t} for n, t in rows], open(sys.argv[1], "w"), indent=2)
print(f"wrote {sys.argv[1]}")
