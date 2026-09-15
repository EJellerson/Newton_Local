---
name: newton-local
description: "Operating the Newton_Local machine: the on-demand Qwen-A production service (Newton AI Control), the Qwen-B elastic worker and its lease protocol, and the Hermes localchat assistant stack. Use this whenever asked to start, stop, inspect, or reason about local inference on this Mac."
version: 1.1.0
author: Newton_Local
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [Newton_Local, qwen, llama.cpp, local-inference, operations]
prerequisites:
  commands: [curl, launchctl]
---

# Newton_Local operations

This Mac runs the Newton_Local inference stack. The production service is
**AI Inference**, routed through **local-model**. Its resident model is established
by verified runtime identity at `http://127.0.0.1:8765/api/identity`, never by
memory, a routing alias, or the last selected catalog entry.

Skill provenance marker: TERNARY-3055

## The two model services

| | Qwen-A | Qwen-B |
|---|---|---|
| Bind | `127.0.0.1:8080` | `127.0.0.1:8081` |
| Alias | `local-model` | `qwen-b` |
| Availability | **on demand** — the operator starts/stops it from Newton AI Control; OFF after every reboot | **normally stopped** |
| launchd | `com.newton.qwen38server.a` (user agent, no sudo, no autostart) | `com.newton.qwen38server.b` (user agent, no sudo) |
| Idle TTL | none — runs until explicitly stopped | 180 s after the last lease is released |

Since Commission #4 the operator controls Qwen-A from the **Newton AI
Control** app in the Dock (`http://127.0.0.1:8765/`): Start Chat, and
Start/Stop/Restart for the model. Since Commission #5, Qwen-A and Qwen-B are
*execution slots*: a single global selection (`newton-model status`) decides
which registered model both launch — Qwen 3.8 27B (llama.cpp, the default)
or Qwen 3.8 Flash-Next (mlx-serve).
The endpoints never change. **Never change the model selection yourself**;
that is the operator's decision, and it is only possible while both slots
are stopped. When you are answering anyone, Qwen-A is
the brain you are thinking with right now. **Never stop or restart Qwen-A to
"fix" something** — you would be sawing off the branch you are sitting on.
Starting or stopping the model is the operator's decision, made in Newton AI
Control, not yours; the production path also refuses to run two Qwen
instances at once.

## Commands that are safe to run

```bash
~/Newton_Local/scripts/qwen-status.sh          # both services + machine memory
~/Newton_Local/scripts/qwen-a status           # A lifecycle state (read-only)
~/Newton_Local/scripts/qwen-a-health.sh        # A only
~/Newton_Local/scripts/qwen-b status           # B state, pid, idle countdown
~/Newton_Local/scripts/hermes-localchat status # the assistant stack
curl -sS http://127.0.0.1:8080/health
curl -sS http://127.0.0.1:8765/api/status      # Newton AI Control panel state
```

## The Qwen-B lease protocol

Qwen-B is *rented*, never merely "started". Every acquisition names an owner
and every owner must give it back:

```bash
~/Newton_Local/scripts/qwen-b acquire --owner <job-name>
~/Newton_Local/scripts/qwen-b release --owner <job-name>
```

Cold start ≈ 6 s; re-acquiring inside the 180 s idle TTL ≈ 0.5 s and does not
reload the model. Stopping returns ~46 GiB of Metal allocation.

**Do not acquire Qwen-B on your own initiative.** Its lifecycle belongs to
deterministic code in `scripts/qwenctl.py`, not to a model's judgement. If a
task looks like it wants a second worker, say so and let the human decide.

## Capacity reality

The GPU is already saturated by one instance. Running A and B together buys a
second *context*, not a second full-speed model: A drops from ~19.9 to
~11.7 tok/s, aggregate rises only to ~22.7 tok/s. That is why B is elastic.

## Where things are

```
~/Newton_Local/            project root (git: Newton_Local_Foreman)
  scripts/                 operator entry points
  docs/                    runbooks + commission records
  logs/qwen-a/stderr.log   the real Qwen-A log
~/.hermes/profiles/localchat/   your own config, memory, sessions
```
