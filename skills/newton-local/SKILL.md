---
name: newton-local
description: "Operating the Newton_Local machine: the Qwen-A always-on inference service, the Qwen-B elastic worker and its lease protocol, and the Hermes localchat assistant stack. Use this whenever asked to start, stop, inspect, or reason about local inference on this Mac."
version: 1.0.0
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

This Mac runs two local Qwen3.8-27B Q8_0 instances off **one** copy of the
weights, plus the Hermes assistant stack you are running inside.

Skill provenance marker: TERNARY-3055

## The two model services

| | Qwen-A | Qwen-B |
|---|---|---|
| Bind | `127.0.0.1:8080` | `127.0.0.1:8081` |
| Alias | `qwen-a` | `qwen-b` |
| Availability | always on | **normally stopped** |
| launchd | `com.newton.qwen38server.a` (system daemon, needs sudo) | `com.newton.qwen38server.b` (user agent, no sudo) |
| Idle TTL | n/a | 180 s after the last lease is released |

Qwen-A is the brain you are thinking with right now. **Never restart Qwen-A
to "fix" something** — you would be sawing off the branch you are sitting on,
and it needs a password you do not have.

## Commands that are safe to run

```bash
~/Newton_Local/scripts/qwen-status.sh          # both services + machine memory
~/Newton_Local/scripts/qwen-a-health.sh        # A only
~/Newton_Local/scripts/qwen-b status           # B state, pid, idle countdown
~/Newton_Local/scripts/hermes-localchat status # the assistant stack
curl -sS http://127.0.0.1:8080/health
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
