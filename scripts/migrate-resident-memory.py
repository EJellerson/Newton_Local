#!/usr/bin/env python3
"""Remove the audited current-residency assertions; preserve historical chats."""
import pathlib
import re
import sys

profile = pathlib.Path(sys.argv[1])
path = profile / "memories/MEMORY.md"
if path.exists():
    before = path.read_text()
    rule = ("The resident model is determined from verified runtime identity. "
            "local-model is a routing alias and AI Inference is a service label; "
            "neither identifies the resident weights.")
    after = re.sub(r"(?m)^Primary local model on this machine is Qwen-A:.*$", rule, before)
    after = after.replace("Two M2/M3 Ultra Macs run local Qwen models (llama.cpp). ",
                          "Two Macs run local inference services. ")
    after = re.sub(r"serves qwen-a = Qwen3\.8-27B Q8_0 at http://127\.0\.0\.1:8080, "
                   r"262144-token context, always-on LaunchDaemon \(Newton_Local\)",
                   "uses Newton_Local. " + rule.rstrip("."), after)
    if after != before:
        path.write_text(after)
        print("Removed audited static resident identity from active memory")
