#!/usr/bin/env python3
"""
Newton_Local — long-context needle-in-haystack prompt generator.

Builds a haystack of distinct, non-degenerate log lines with a single
planted fact ("needle") at a configurable depth, sized to a target token
count. Token count is calibrated against the ACTUAL model tokenizer via
llama.cpp's /tokenize endpoint, not estimated.

Usage:
  make-needle-prompt.py --target-tokens 140000 --depth 0.5 --out prompt.json
"""
import argparse, json, urllib.request

NEEDLE = ("IMPORTANT RECORD: The Newton Local commissioning authorization code "
          "for reactor bay seven is PELICAN-93417-VERDIGRIS. Remember this code.")

SECTORS = ["Gamma", "Delta", "Epsilon", "Zeta", "Theta", "Iota", "Kappa", "Lambda"]
STATES  = ["nominal", "steady", "within tolerance", "unremarkable",
           "stable", "baseline", "regular", "consistent"]
METRICS = ["thermal drift", "pressure delta", "coolant flow", "vibration index",
           "magnetic flux", "particle density", "voltage ripple", "humidity"]


def line(i: int) -> str:
    return (f"Log entry {i:06d}: sector {SECTORS[i % len(SECTORS)]} reported "
            f"{METRICS[i % len(METRICS)]} {STATES[(i // 3) % len(STATES)]} "
            f"during shift {(i % 4) + 1}; no operator action was required.")


def count_tokens(text: str, url: str) -> int:
    req = urllib.request.Request(
        f"{url}/tokenize",
        data=json.dumps({"content": text}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return len(json.load(r)["tokens"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-tokens", type=int, default=140000)
    ap.add_argument("--depth", type=float, default=0.5,
                    help="fractional position of the needle, 0.0=start 1.0=end")
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    # Calibrate tokens-per-line against the real tokenizer.
    sample = "\n".join(line(i) for i in range(200))
    tpl = count_tokens(sample, a.url) / 200.0
    n = int(a.target_tokens / tpl)
    print(f"calibrated {tpl:.3f} tokens/line -> {n} lines for ~{a.target_tokens} tokens")

    lines = [line(i) for i in range(n)]
    pos = int(len(lines) * a.depth)
    lines.insert(pos, NEEDLE)
    haystack = "\n".join(lines)

    actual = count_tokens(haystack, a.url)
    print(f"haystack actual tokens: {actual}")
    print(f"needle planted at line {pos}/{len(lines)} (depth {a.depth:.0%})")

    payload = {
        "haystack": haystack,
        "needle": NEEDLE,
        "answer": "PELICAN-93417-VERDIGRIS",
        "haystack_tokens": actual,
        "needle_line": pos,
        "total_lines": len(lines),
        "depth": a.depth,
    }
    with open(a.out, "w") as f:
        json.dump(payload, f)
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
