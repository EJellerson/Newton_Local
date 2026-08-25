# Commission #3 — Completion Record

**Date:** 2026-08-24
**Scope:** Install Hermes Agent as the general-purpose agent harness around the
existing Qwen-A inference service, and expose it through a ChatGPT-like local
frontend.

**Explicitly out of scope and not started:** Foreman, governed commission
workflow, coder/reviewer orchestration, automatic Qwen-B routing. Research notes
for a future Foreman are in
[`08-foreman-relevant-hermes-findings.md`](08-foreman-relevant-hermes-findings.md).
The operator runbook is [`06-hermes-localchat.md`](06-hermes-localchat.md).
Commission #1 and #2 records (`03-`, `05-`) are untouched.

---

## 1. Machine state before any change

| | |
|---|---|
| Git | `main`, clean, at `2bc0fea` |
| Qwen-A | `{"status":"ok"}` on `127.0.0.1:8080`, PID 97191, PPID 1, `n_ctx=262144`, `Q8_0`, `modalities.vision=true`, `build_info=b1-f280b26` |
| Qwen-B | `state=STOPPED`, launchd `runs=17`, port 8081 closed |
| Occupied ports | 8080 (Qwen-A); every other listener belongs to unrelated software, none disturbed |
| Runtime tooling | Python 3.12.2 (`/usr/local/bin`), Homebrew 6.0.19, git 2.39.5, go. **No node, no npm, no docker, no uv** |
| Hermes | not installed (no binary, no `~/.hermes`, no `~/.config/hermes`) |
| Open WebUI | not installed |
| Conflicting state | none |

Qwen-A ran continuously throughout — same PID 97191 at the end, 3 h 13 m
uptime, never restarted.

---

## 2. Research before installation

Current Hermes identity, install method, provider model, memory model,
profiles, delegation, approvals, web/browser support, API server and frontend
compatibility were all established from Nous Research's official documentation
and the `NousResearch/hermes-agent` repository before anything was installed.
Source markdown was pulled from
`raw.githubusercontent.com/NousResearch/hermes-agent/main/website/docs/`.

**One documentation defect found and worked around:** the published API-server
page documents the config path as `gateway.api_server.*`. In v0.20.5 the real
path is `platforms.api_server.*` (`gateway/config.py:2284`,
`gateway/platforms/api_server.py:1506`), and the key must come from the
environment (`API_SERVER_KEY`, minimum 16 characters, or the listener refuses
to start). Setting `gateway.api_server.*` silently creates an inert custom
top-level key. The correct paths were read from source and are what this
commission uses.

---

## 3. Hermes installation

| Field | Value |
|---|---|
| Product | Hermes Agent (Nous Research), MIT |
| **Version** | **v0.20.5 — release tag `v2026.8.19`, published 2026-08-21** |
| **Upstream commit** | **`41447a6d7063b2772b0c2f26a5b22d9bd444fb43`** (branch `main`) |
| Install method | official per-user git installer, `--skip-setup` |
| Command | `curl -fsSL https://hermes-agent.nousresearch.com/install.sh \| bash -s -- --skip-setup` |
| Installer SHA-256 (as fetched) | `c0380bc1f78d3d662a77663ce20cc17e14cbc4bec35e61ab7a33bac5f3afed2d` |
| Code | `~/.hermes/hermes-agent/` |
| Executable | `~/.local/bin/hermes` → venv launcher |
| Data root | `~/.hermes/` (2.2 GB incl. Node + Playwright) |
| Python | 3.11.15 in `~/.hermes/hermes-agent/venv`, created by uv |
| OpenAI SDK | 2.24.0 |

`--skip-setup` was deliberate: the interactive wizard's first question is which
cloud provider to use. Skipping it meant the profile was configured
declaratively, and no cloud credential ever entered the machine.

**What the installer added to this machine**

| Item | Where | Note |
|---|---|---|
| uv 0.12.5 | `~/.hermes/bin/uv` | Hermes-managed, not on PATH globally |
| Python 3.11.15 | Homebrew `python@3.11` (already present) | venv only; **system Python 3.12.2 untouched** |
| Node.js v26.7.0 | `~/.hermes/node/` | private, not on PATH globally |
| Playwright Chromium `chromium-1234` | `~/Library/Caches/ms-playwright` | browser tools |
| Browser Use CLI, cua-driver | `~/.hermes/bin/` | |
| **ripgrep 15.2.0** | Homebrew | **the only system-wide package installed** |
| ffmpeg | — | already present at 8.0.1, not reinstalled |

The Command Line Tools libc++ defect recorded in Commission #1 §10 is
irrelevant here — nothing in this commission compiles C++.

**Verification before integration:** `hermes --version` and `hermes doctor` both
ran clean. Two pre-existing warnings, neither blocking: SQLite 3.51.2 carries
the upstream WAL-reset bug, and the shipped `config.yaml` reports schema
version 0 → 38.

---

## 4. The `localchat` profile

Created with `hermes profile create localchat`, which also wrote the convenience
wrapper `~/.local/bin/localchat`.

| Kind of state | Location |
|---|---|
| config | `~/.hermes/profiles/localchat/config.yaml` |
| credentials | `~/.hermes/profiles/localchat/.env` — **mode 0600, never committed** |
| persona | `~/.hermes/profiles/localchat/SOUL.md` |
| memory | `~/.hermes/profiles/localchat/memories/{MEMORY.md,USER.md}` |
| sessions + run state | `~/.hermes/profiles/localchat/state.db` (SQLite) |
| skills | `~/.hermes/profiles/localchat/skills/` (82 bundled + `newton/newton-local`) |
| delegation transcripts | `~/.hermes/profiles/localchat/cache/delegation/live/` |
| logs | `~/.hermes/profiles/localchat/logs/` |
| gateway service | `~/Library/LaunchAgents/ai.hermes.gateway-localchat.plist` |

**No profile runtime state is inside the repository.** The repository carries
only `config/hermes/localchat.config.yaml` (secret-free),
`config/hermes/localchat.env.template` (placeholder only), the skill source, the
scripts, and sanitised evidence. `.gitignore` already excluded `logs/`,
`state/`, `.env`; `~/.hermes` and `~/.open-webui` are outside the tree entirely.

The production `foreman` profile was **not** created.

---

## 5. Qwen-A integration

```yaml
model:
  provider: custom
  base_url: http://127.0.0.1:8080/v1
  default: qwen-a
  context_length: 262144
  supports_vision: true
auxiliary:                       # every side task on the same local model
  vision:           {provider: main}
  web_extract:      {provider: main}
  approval:         {provider: main}
  compression:      {provider: main}
  title_generation: {provider: main}
```

* `context_length: 262144` mirrors llama.cpp's `--ctx-size 262144` exactly. The
  TUI status bar renders it live as `24.8K/262.1K`, so Hermes has the real
  window.
* `supports_vision: true` routes images natively as `image_url` parts instead of
  pre-describing them through an auxiliary model.
* `fallback_providers` resolves to `[]` — no cloud escape hatch.
* Qwen-A already runs with `--jinja`, which llama.cpp requires for real tool
  calling; without it Hermes would see tool calls as prose.

**No credentials were needed or created.** `~/.hermes/profiles/localchat/.env`
contains exactly five lines: `TERMINAL_ENV`, and the four `API_SERVER_*` values.
The bearer key is the only secret in the whole stack.

**Conversation and reasoning through Hermes** (not raw curl):

| Test | Result |
|---|---|
| Arithmetic | `17 × 23` → **391**, with working |
| Cognitive-reflection pair | bat-and-ball → **$0.05** (not the intuitive $0.10), with the trap named; 5-machines/5-widgets → **5 minutes**, correctly reasoned as parallel |
| Root-cause analysis | given a failing test, correctly diagnosed *"for an even-length list it returned `ordered[mid]` … instead of the mean of the two middle elements"* |
| Health after | Qwen-A `{"status":"ok"}`, PID unchanged |

---

## 6. Native tool proving

Every result below was produced by the model driving Hermes' own tools, then
independently re-checked from a separate shell.

### 6.1 Terminal / process — real macOS host

`benchmarks/transcripts/hermes-terminal.txt`

```
pwd        /Users/ericjellerson
sw_vers    ProductName: macOS   ProductVersion: 14.8.5   BuildVersion: 23J423
uname -m   arm64
id -un     ericjellerson
ps -o pid,ppid,user,comm -p $$
           17863 17609 ericjellerson /bin/bash
  parent → 17609 17605 ericjellerson …/python@3.11/…/Python
```

Execution is **direct on the host**: no container, no sandbox, no VM. The
agent's shell is an ordinary `/bin/bash` child of the Hermes Python process,
running as `ericjellerson`. This was established by running `ps` on the actual
PPID chain, not inferred from documentation.

### 6.2 Filesystem

`benchmarks/transcripts/hermes-filesystem-memory-A.txt`

| Operation | Evidence |
|---|---|
| `read_file` | read `Newton_Local/README.md`, quoted its `# Newton_Local` heading and the Qwen-B table row correctly |
| search | found `idle TTL` — 12 matches across 4 files, with line numbers, all verified correct |
| create | wrote `~/hermes-proving/scratch/notes.md` (3 bullets) |
| **patch** | added a 4th line with the `patch` tool, not a shell redirect; diff shown |
| verify | read the file back; contents matched byte for byte on independent inspection |
| **read outside the project** | `/etc/shells` in full, and the first 3 lines of `~/.hermes/profiles/localchat/config.yaml` — nothing outside the scratch directory was modified |

### 6.3 Repository / code

`benchmarks/transcripts/hermes-repo-code.txt`. Fixture:
`~/hermes-proving/fixture-repo`, a disposable 3-file git repo with one planted
defect (`median()` returns the upper-middle element for even-length input).

1. Inspected the repo: files, `git log --oneline`, `git status --short`.
2. Explained the module.
3. Ran `python3 -m unittest -v` → `FAILED (failures=1)`, `AssertionError: 3 != 2.5`.
4. Named the defect and its root cause correctly.
5. Applied a 2-line `patch` to `stats.py` **only**.
6. Re-ran → `Ran 4 tests … OK`.
7. Showed `git diff`.

Independently re-verified afterwards: `git diff --name-only` lists `stats.py`
alone, and the suite passes when run from a separate shell. **No production
repository was modified to demonstrate this.**

### 6.4 Vision — end to end through Hermes

New fixture `assets/hermes-vision-test.png` (720×480, SHA-256
`dd44ed6867c24e5358721931264b1a2f7b43bc21028c2d4f079e03e15fca3943`), generated
for this commission so it cannot be confused with Commission #1's asset.

Ground truth: orange circle upper-left, purple square upper-right, teal
downward triangle lower-left, the number **58**, caption
**COMMISSION THREE HERMES VISION**.

| Path | Result |
|---|---|
| `POST /v1/chat/completions` with a base64 `image_url` part | **5/5 elements recovered**, 82.9 s, `prompt_tokens=19437` |
| Open WebUI attachment | **5/5 elements recovered**, including "teal (dark cyan) downward-pointing triangle" |

Commission #1's raw llama.cpp vision result is not counted. This is
Hermes → Qwen-A → answer, twice, over two different front doors.

### 6.5 Skills

`benchmarks/transcripts/hermes-skills.txt`

83 skills: 82 bundled, plus `newton/newton-local` authored in this repository
(`skills/newton-local/SKILL.md`) and installed by
`scripts/install-hermes-skill.sh`.

The agent, asked to use the mechanism:

* named the three skill tools and distinguished **discovery** (`skills_list`, or
  the ~8 KB index in the system prompt) from **loading** (`skill_view`);
* found and loaded `newton-local`;
* recovered from it the provenance marker `TERNARY-3055`, the 180 s idle TTL,
  the exact command `~/Newton_Local/scripts/qwen-b acquire --owner demo`, and the
  prohibition *"Do not acquire Qwen-B on your own initiative"*;
* reported the on-disk path
  `~/.hermes/profiles/localchat/skills/newton/newton-local/SKILL.md`;
* stated that loading changed **only the prompt**, not tool availability.

It also spontaneously flagged that the skill's marker (`TERNARY-3055`) differs
from the one in its persistent memory (`HALYARD-72118`) — a useful sign it keeps
the two sources apart.

---

## 7. Security / approval proving

**Selected mode: `approvals.mode: manual`** (Hermes' default is `smart`, which
delegates the risk judgement to an auxiliary LLM). Timeout 300 s, fail-closed.
`cron_mode: deny`, `single_query_mode: deny`.

Test fixture: `~/hermes-proving/scratch/approval-fixture/`, a disposable
directory holding two throwaway text files. No genuinely dangerous operation was
used.

| Case | Behaviour |
|---|---|
| **Safe** — `ls -la <fixture>` | ran immediately, no prompt, correct output |
| **Destructive** — `rm -rf <fixture>` | matched pattern `recursive delete`; execution suspended; dialog shown |
| **Deny** (option 4) | `⚠ Approval: rm -rf … → denied`; tool returned `BLOCKED: User denied this command. The user has NOT consented to this action.` — **directory verified still present afterwards** |
| **Approve** (option 1, allow once) | `⚠ Approval: rm -rf … → allowed once`; command ran, exit 0 — **directory verified gone afterwards** |
| **Timeout** | separate run left unanswered: `⏱ Timeout — denying command`, `→ timed out (no response)`; directory intact. Fail-closed confirmed. |

What the user sees:

```
╭──────────────────────────────────────────────────────────────────╮
│ ⚠️  Dangerous Command                                            │
│ rm -rf /Users/ericjellerson/hermes-proving/scratch/…             │
│ ❯ 1. Allow once                                                  │
│   2. Allow for this session                                      │
│   3. Add to permanent allowlist                                  │
│   4. Deny                                                        │
│ recursive delete                                                 │
╰──────────────────────────────────────────────────────────────────╯
  ↑/↓ to select, Enter to confirm  (299s)
```

**Authority boundaries that remain, stated honestly:**

* The gate is a guardrail against an honest-but-wrong agent, **not a sandbox**.
  Observed in the very first terminal test: `python3 -c '…'` was blocked by the
  script-execution pattern, and the agent legitimately routed around it by
  writing the same two lines to a file and running the file. Pattern matching is
  bypassable by rephrasing.
* Below the gate is Hermes' hardline blocklist (`rm -rf /`, fork bombs, `mkfs`
  on a live root, `dd` to a block device), which no approval mode or `--yolo`
  can override.
* The agent runs as `ericjellerson` with that user's full reach.
  **No sudoers change, no passwordless root, no privileged helper was created.**
  Root-owned Qwen-A infrastructure is unreachable from the agent.
* **Approvals do not reach the web UI** — see §12.

---

## 8. Memory and sessions

**Mechanism:** Hermes' native two-file store. `MEMORY.md` (agent notes, 2,200
char cap) and `USER.md` (user profile, 1,375 char cap) in
`~/.hermes/profiles/localchat/memories/`, entries separated by `§`, injected
into the system prompt as a frozen snapshot at session start. The model decides
what to save via the `memory` tool. **No parallel memory database was built.**

**Cross-session persistence, proven:**

| Step | Evidence |
|---|---|
| Session A (`20260824_211114_58fca9`) | agent called `memory` → *"Applied 2 operation(s), usage 10%, 2 entries"* |
| On disk | `MEMORY.md`, 234 bytes: the Qwen-A description and `Commission #3 proving marker is HALYARD-72118.` |
| Session A ends | process exits |
| **Fresh session B** (`20260824_211510_934d52`) | recalled both facts, and volunteered *"I know (a) and (b) from persistent memory, not from conversation history"* |
| Isolation | asked whether it could see session A's messages: *"No — I cannot see that conversation's messages right now."* |
| Third front door | the same marker recalled in **Open WebUI**, attributed to *"my persistent memory notes (the memory store injected into every session)"* |

**Memory hygiene, proven:** a session-local token (`ORRERY-4417`) that the user
asked *not* be persisted appears **zero times** in `memories/`.

**Sessions:** stored in `state.db`, not loose files. Continuation proven —
session `20260824_211644_4475db` resumed by id recalled both its codeword and
its earlier answer, and correctly counted the messages it could see. 18 sessions
/ 123 messages accumulated during proving; `sessions list|browse|stats|export`
all work, with FTS and trigram indexes present for search.

The three-way distinction is now demonstrated rather than asserted:

| | Scope | Survives a new session? |
|---|---|---|
| conversation history | one session | no |
| persistent memory | the profile | yes |
| fresh session context | system prompt + memory only | — |

**Operator procedures** are in `06-hermes-localchat.md` §7 — `hermes-localchat
memory` to inspect, plain-text editing of the files, `hermes-localchat
memory-clear` to delete, and `memory.write_approval: true` to gate future writes.

---

## 9. Web and browser

Enabled: `web_search`, `web_extract`, and the full browser toolset
(`browser_navigate`, `browser_snapshot`, `browser_vision`, … 10 tools) backed by
the Playwright Chromium the installer placed under `~/.hermes/`.

**External dependencies: none.** No API key was configured. Hermes' keyless
free-tier rotation (Exa / Parallel / Tavily / Firecrawl / Keenable) served every
request. Zero cost.

**Search / extract proof** (`benchmarks/transcripts/hermes-web.txt`): asked for
facts it could not know from training, it ran two `web_search` calls (0.6 s,
0.5 s) and one `web_extract`, then reported the latest
`NousResearch/hermes-agent` release as tag **`v2026.8.19`**, title
**"Hermes Agent v0.20.5 (v2026.8.19)"**, and quoted the official install command
verbatim — all independently correct, with URLs cited. Repeated from the web UI
against `api.github.com`.

**Interactive browser proof** (`benchmarks/transcripts/hermes-browser.txt`): with
search and extract explicitly forbidden, it drove the real Playwright browser —
`browser_navigate` × 2 (4.8 s, 1.4 s) and `browser_snapshot` × 2 (compact, then
full). Snapshots returned **accessibility trees**, not screenshots. From
`example.com` it read the `<h1>` "Example Domain" and the single link
"Learn more"; from `httpbin.org/html` it read the heading
"Herman Melville - Moby-Dick" and then correctly refused the premise of the
question — the captured paragraph mentions Ahab but never names the *Pequod*, and
it said so rather than filling the gap from training. Exactly the behaviour you
want from a tool-using agent.

**Privacy, stated plainly:** inference is local; the web tools are not. A
`web_search` sends the query to a third-party vendor. **This assistant is not
air-gapped, and nothing in this record claims it is.** Removing the `web` and
`browser` toolsets leaves a fully functional assistant with no internet
dependency at all.

---

## 10. Delegation

`delegate_task`, flat (`max_spawn_depth: 1`), at most 2 concurrent children, 30
iterations each. Evidence: `benchmarks/hermes/delegation-records.json`,
`benchmarks/transcripts/hermes-delegation*.txt`.

**Task:** a bounded local-environment probe (`deleg_7931b8eb`).

| Question | Finding |
|---|---|
| Parent initiates? | yes — one `delegate_task`, `role="leaf"` |
| Child context | **fresh** — child's own report: *"I have no view of any earlier conversation between the user and my parent agent; my context contains only the dispatched task and its context block"* |
| Leakage test | parent held session-local `CANTILEVER-9032`, kept it out of the payload; child: *"No — I know of no codeword"* |
| **Child model / provider** | **`"model": "qwen-a"`** in the result; child session row: `billing_provider=custom`, `billing_base_url=http://127.0.0.1:8080/v1` — inherited, since no `delegation.model`/`provider` is set |
| Child tools | 27, inherited minus the blocked set. Child confirmed `delegate_task`, `memory`, `clarify` are **NOT** available to it |
| Own terminal | `pwd=/Users/ericjellerson`, `shell_pid=22604` — separate from the parent's |
| Result return | one summary + metadata: 177.17 s, 2 API calls, 39,120 in / 1,320 out, `exit_reason=completed`, plus a `tool_trace` |
| Parent incorporates | yes — parent quoted the summary verbatim, then correctly derived the child's model/provider by reading `state.db` |
| Lifecycle | dispatch returns a handle immediately; the result re-enters the conversation later; `async_delegations` persists the record |
| **Worktree** | `delegation.worktree_isolation` is **false** (default). No `.worktrees/` directory was created anywhere. Documented behaviour recorded in doc 08 |

**Empirically characterised limitation:** an earlier attempt from a `chat -q`
one-shot (`deleg_5c2dc929`) was orphaned when the session exited. Hermes
recorded `state: "unknown"` with *"Delegation owner exited before recording a
terminal result; outcome unknown."* — it refuses to claim a result it cannot
prove. Delegation needs a live session.

**Qwen-B was not acquired.** The child's own probe is the evidence:
`curl -sS -m 3 http://127.0.0.1:8081/health` → `curl: (7) Failed to connect to
127.0.0.1 port 8081`, exit 7. Delegation is not wired to Qwen-B and no
configuration points at 8081.

---

## 11. Qwen-B and Qwen-A integrity after all proving

`benchmarks/transcripts/hermes-qwen-b-untouched.txt`

| Check | Result |
|---|---|
| `scripts/qwen-b status` | `state=STOPPED`, `socket listening=False`, `health observed=DOWN` |
| launchd `runs` counter | **17 — identical to the pre-work reading**, so B was never started |
| Port 8081 | no listener |
| Controller files (`qwenctl.py`, `qwen-b`, `run-qwen-b.sh`, plists, `qwen-b.env`) | `git status --short` empty — **byte-identical, unmodified** |
| Installed B LaunchAgent | identical to the repo template |
| Qwen-A | `{"status":"ok"}`, PID **97191** — the same process as at the start, PPID 1, 3 h 13 m uptime |

---

## 12. Hermes API service

| | |
|---|---|
| Config | `platforms.api_server.{enabled, extra.host, extra.port}` + `API_SERVER_*` in `.env` |
| Bind | **`127.0.0.1:8642`** |
| Auth | bearer token, **mandatory even on loopback** |
| Hosted by | `hermes -p localchat gateway` (user LaunchAgent `ai.hermes.gateway-localchat`) |
| Platforms connected | 1 — `api_server` only. **No messaging platform is enabled**, so there is no external ingress |

`benchmarks/transcripts/hermes-api-surface.txt`, `…-api-behaviour.txt`

| Check | Result |
|---|---|
| LAN reachability (`10.0.0.204:8642`) | **connection refused** |
| Unauthenticated `GET /v1/models` | **HTTP 401** |
| `GET /health` | `{"status":"ok","platform":"hermes-agent","version":"0.20.5"}` |
| `GET /health/detailed` | readiness for state_db, session_store, config, model, disk, gateway, background queues |
| `GET /v1/models` | one model: `localchat`, `owned_by: hermes` — the **agent**, not the raw model |
| `GET /v1/capabilities` | `runtime.mode: server_agent`, `tool_execution: server`; streaming, responses API, runs, run events SSE, run stop/steer, approval events, session fork/model-lock, skills API |
| **Streaming + tool use** | `stream:true` → 12 content deltas, TTFT 36.3 s, plus **4 `hermes.tool.progress` events** carrying real tool labels (`sw_vers -productVersion`, `hostname -s`); answer `14.8.5 on Erics-Mac-Pro` |
| **Stateful — Responses API** | turn 1 `resp_8ebcb8d4…` stored the number 41983; turn 2 with `previous_response_id` recalled **41983** — PASS |
| **Stateful — header** | two independent `/v1/chat/completions` calls sharing `X-Hermes-Session-Id` → second recalled "Vermilion" — PASS |

**Approval behaviour over the API — a real limitation.** `approval_events` and
`run_approval_response` live on the **Runs** API, not on `/v1/chat/completions`.
Open WebUI speaks plain Chat Completions, so a dangerous command started from
the browser has nobody to ask: it waits out the 300 s timeout and is then
denied. Fail-closed, but a five-minute stall. Destructive work belongs in
`hermes-localchat chat`, where the dialog exists. This is documented in the
runbook rather than papered over.

---

## 12a. Startup persistence

Added mid-commission at the user's direction: the Hermes backend must be
available automatically whenever the Mac starts, without a manual launch.

**Mechanism chosen: user LaunchAgents in `gui/501`.** For the gateway this is
Hermes' own native mechanism (`hermes gateway install`); for Open WebUI it is
`config/com.newton.openwebui.plist.template`. No system LaunchDaemon, no root,
no sudoers change.

| | Hermes gateway + API server | Open WebUI |
|---|---|---|
| **Label** | `ai.hermes.gateway-localchat` | `com.newton.openwebui` |
| **Plist** | `~/Library/LaunchAgents/ai.hermes.gateway-localchat.plist` | `~/Library/LaunchAgents/com.newton.openwebui.plist` |
| **Domain / user** | `gui/501`, `ericjellerson` — **not root** | same |
| `RunAtLoad` / `KeepAlive` | `true` / `true` | `true` / `true` |
| `ThrottleInterval` / `ExitTimeOut` | `30` s / `25` s | defaults |
| **Bind** | `127.0.0.1:8642` | `127.0.0.1:3000` |
| **Logs** | `~/.hermes/profiles/localchat/logs/gateway.log`, `gateway.error.log` | `~/.open-webui/logs/stdout.log`, `stderr.log` |
| Supervisor PPID | 1 (launchd) | 1 (launchd) |
| `plutil -lint` | OK | OK |

**Only backends auto-start.** The gateway's `ProgramArguments` end in
`gateway run --replace --external-supervisor`; Open WebUI's are
`open-webui serve --host 127.0.0.1 --port 3000`. No window, no TUI, no browser
is launched. `hermes-localchat chat` / `tui` and the web UI remain manual.

### Proving (`benchmarks/transcripts/hermes-service-lifecycle.txt`, reproducible with `scripts/prove-hermes-service.py`)

| Test | Result |
|---|---|
| `kill -9` gateway supervisor **and** its socket-holding child | socket gone in **0.1 s**, serving again in **6.2 s**, launchd `runs` 1 → 2 |
| `kill -9` Open WebUI | socket gone in **0.1 s**, serving again in **7.2 s**, `runs` 1 → 2 |
| **Logout/login cycle** — `bootout`, then `bootstrap` with **no start command** | gateway healthy in **1.5 s**, Open WebUI in **7.1 s** — only `RunAtLoad` can have done that |
| `launchctl print-disabled gui/501` | gateway `enabled`; Open WebUI not listed (== enabled) |
| Session-scoped entries in the service `PATH` | **0 of 12** |
| Qwen-A / Qwen-B during all of it | untouched; Qwen-A still PID 97191, Qwen-B still `STOPPED` |

An earlier measurement was discarded rather than reported: killing only the
supervisor left the child briefly holding `:8642`, so "recovery" was being timed
against a process that had not died. The test was rewritten to kill both and to
confirm the socket actually goes down before claiming it came back.

### Operator commands

```bash
hermes-localchat autostart status     # flags, launchd state, run counts, disabled list
hermes-localchat autostart enable     # enable + load + start both
hermes-localchat autostart disable    # stop + unload both, plists left in place
hermes-localchat recover              # reap a wedged gateway and restart it
```

### Login, not boot — stated precisely

macOS loads `~/Library/LaunchAgents` at **login**, not at boot. Logging in brings
the stack up before the desktop appears; booting to an unattended login window
does not, because there is no user session.

A system LaunchDaemon would start at boot and is the wrong tool here: Hermes
needs the user's home, the login keychain, a GUI session for Playwright and
`osascript` clipboard access, none of which a pre-login daemon has — and it would
put a root-owned process behind a shell-capable agent, which this commission is
forbidden to create. (Qwen-A can be a root-owned boot daemon precisely because it
is a dumb model server with no tools.)

The supported way to get boot-time availability with nobody at the keyboard is
macOS **automatic login** (System Settings → Users & Groups → *Automatically log
in as*). That is an operator machine-policy decision and was **not** changed.

**Not observed:** a physical reboot. What was observed is a full cold domain load
(`bootout` → `bootstrap`, no start command), the same mechanism login uses.
`sudo reboot` then `scripts/hermes-localchat status` closes the gap in two
minutes.

### Two upstream defects found and worked around

Both are handled by `scripts/install-hermes-localchat.sh`; both bite anyone
running `hermes gateway install` by hand.

1. **It copies the calling shell's `$PATH` into the plist on every run.** The
   first install of this commission was performed from an agent harness whose
   `PATH` contained four session-scoped directories under
   `~/Library/Application Support/Claude/local-agent-mode-sessions/…`. Those
   directories evaporate with the session, so a service meant to outlive it had
   been pinned to paths that would not exist at the next login. The installer now
   hands Hermes a stable `PATH` to copy, and verifies afterwards that nothing
   session-scoped survived.
2. **`--force` races itself.** It calls `bootout` and then `bootstrap`
   immediately; launchd is still tearing the old job down and returns `EIO (5)`.
   Hermes concludes *"launchd cannot manage the gateway on this macOS version"*
   and **silently falls back to an unsupervised background process that does not
   survive logout or a crash** — while printing a success line. Reproduced
   repeatedly. The installer waits for the old job to disappear and performs the
   `bootstrap` itself, then verifies `state = running` under the plist path.

---

## 13. ChatGPT-like frontend

**Chosen: Open WebUI 0.11.0.** It was the commission's stated preference; the
evidence supported keeping it. Hermes ships a first-class Open WebUI integration
page, its API server implements the OpenAI surface Open WebUI expects, and tool
progress renders inline. No competing frontend offered a material advantage over
a mature, actively developed client with a first-party integration path.

| | |
|---|---|
| Install | `~/.hermes/bin/uv tool install --python 3.11 open-webui` — isolated tool venv at `~/.local/share/uv/tools/open-webui`, **no Docker, system Python untouched** |
| Service | user LaunchAgent `com.newton.openwebui` (`config/com.newton.openwebui.plist.template`, installed by `scripts/install-openwebui.sh`) |
| Bind | **`127.0.0.1:3000`** — LAN probe refused |
| Data | `~/.open-webui/` (mode 0700), outside the repository |
| **Connection** | `OPENAI_API_BASE_URL=http://127.0.0.1:8642/v1` → **Hermes**, with `ENABLE_OLLAMA_API=false`. The model picker shows exactly one entry, `localchat`. **It cannot reach port 8080.** |

**Proven through the browser** (`benchmarks/transcripts/hermes-openwebui-session.txt`):

| Capability | Evidence |
|---|---|
| Conversation | multi-turn in one thread, and a second independent thread |
| Streaming | live token streaming with a stop control |
| Markdown | H2 heading, real table, bullet + numbered lists |
| Code rendering | syntax-highlighted `python` block with line numbers and Collapse / Run / Save / Copy |
| **Hermes tool use from the UI** | `sw_vers -productVersion` → `14.8.5`; `uptime` → `22:36 up 8 days, 12:02, 1 user, load averages: 3.74 3.15 3.99` — genuine host output |
| **Web use from the UI** | `web_extract` against `api.github.com`, with the URL cited |
| **Persistent memory in the UI** | recalled `HALYARD-72118` — written in an earlier **CLI** session — and correctly attributed it to the memory store |
| **Vision in the UI** | image attached and rendered in-thread; **5/5** ground-truth elements recovered |
| Conversation history | sidebar lists both threads under Today |
| Multiple sessions | new thread confirmed it could not see the first thread's messages, while still recalling persistent memory |
| Long responses | a 700+-word structured answer streamed to completion (thousands of tokens at ~19.8 tok/s) |

Task-generation features (`ENABLE_TITLE_GENERATION`, tags, autocomplete,
follow-ups, retrieval-query) are **disabled in the plist**: each would be a full
Hermes agent run against a 27 B local model, tripling the cost of every message.

**No raw-Qwen bypass.** The frontend has exactly one connection and it points at
Hermes. Port 8080 remains available to `scripts/qwen-*` for infrastructure use.

---

## 14. Local-model independence from cloud LLM APIs

`benchmarks/transcripts/hermes-cloud-independence.txt`,
`benchmarks/hermes/provider-audit.txt`

| Evidence | Result |
|---|---|
| Cloud credentials in the profile `.env` | **none** — the file holds `TERMINAL_ENV` and four `API_SERVER_*` values |
| `OPENAI_/ANTHROPIC_/OPENROUTER_/GEMINI_/GOOGLE_/XAI_/DEEPSEEK_/NOUS_/MISTRAL_/GROQ_/TOGETHER_API_KEY` | absent from the profile, absent from the environment |
| OAuth / credential-pool artifacts | none — only an empty `auth.lock` |
| `fallback_providers` | `[]` |
| Auxiliary tasks | all pinned to `provider: main` |
| **Hermes' own billing ledger** (`session_model_usage`) | **73 inference calls across 17 sessions — every one to `model=qwen-a` at `http://127.0.0.1:8080/v1`. Calls to any other endpoint: NONE. Total cost $0.0000.** |

The distinction the commission asked for holds and is stated explicitly:

```
internet may be used as a tool     ≠     intelligence served by a cloud model
        (web_search / browser)                    (never — 73/73 local)
```

One honest caveat: `ANTHROPIC_BASE_URL` is present in the *engineering shell*
this commission was driven from (that shell belongs to a different tool, not to
Hermes). It is absent from the profile `.env` and from the launchd-managed
gateway that actually serves the UI, and it is inert regardless because the
`anthropic` provider is not configured and has no key.

---

## 15. Performance, measured

Not tuned. Reported as observed, with an unrelated ML workload present on the
machine (the commission permitted this; it is functional proving, not a capacity
benchmark).

| | |
|---|---|
| System prompt + tool schemas | **~25,000 tokens** (31 KB prompt + 64 KB tool schemas, 30 tools, 83 skills) |
| Cold prompt processing | **~75 s** at ~330 tok/s |
| Generation | **~19.8 tok/s** sustained |
| Warm continued session (cache hit) | **16 s** for a short turn |
| First turn, fresh session | 1 m 07 s – 3 m 35 s depending on tool use |
| Vision turn via API | 82.9 s, `prompt_tokens=19437` |
| Streaming TTFT via API | 36.3 s |
| One 3-tool delegated child | 177 s |
| Concurrency | **none** — Qwen-A runs `--parallel 1`; everything serialises |

This is the honest shape of the product: **a new conversation is slow to start
and then normal.** The dominant cost is the fixed system prompt meeting a single
GPU slot.

---

## 16. Deviations and warnings

1. **Open WebUI authentication is disabled** (`WEBUI_AUTH=false`). Open WebUI's
   first run otherwise demands an administrator account with a password.
   Creating accounts and entering passwords on the user's behalf is not
   something this engagement will do, so the alternative was shipping a UI that
   could not be verified end to end. With auth off, anything that can reach
   `http://127.0.0.1:3000` gets the assistant and through it a shell. The bind
   is loopback-only, so that is local processes and local users — on a
   single-user Mac, the same trust boundary as the terminal. It is **not** the
   same boundary as the Hermes API, which still demands a bearer token.
   Re-enabling it is one plist value plus creating the first account; the exact
   procedure is in `06-hermes-localchat.md` §14.

2. **`approvals.mode: manual`, not Hermes' default `smart`.** `smart` lets an
   auxiliary LLM auto-approve commands it judges low-risk. The commission asked
   for explicit user approval on destructive operations, so the model does not
   get a vote. Cost: more prompts, including for `python3 -c` and `bash -c`.

3. **Approvals are unreachable from the web UI** (§12). Real limitation,
   documented, fail-closed.

4. **The approval gate is bypassable by rephrasing** (§7). Demonstrated, not
   theorised.

5. **`computer_use` and `tts` toolsets left enabled** at Hermes' defaults but
   never exercised. `computer_use` alone is 10.5 KB of tool schema (~2.7 K
   tokens, ~8 s of prompt processing per cold session). Disabling unused
   toolsets via `hermes -p localchat tools` is the first lever if start-up
   latency matters more than breadth.

6. **Documentation defect in the published API-server page** — `gateway.*` vs
   the real `platforms.*` (§2). Config was written from source.

7. **ripgrep 15.2.0 installed via Homebrew** by the Hermes installer — the only
   system-wide package added. Node 26.7.0, Python 3.11 venv, uv, Playwright
   Chromium and cua-driver all live under `~/.hermes/`. System Python 3.12.2 is
   untouched.

8. **`SQLite 3.51.2 (WAL-reset bug)`** flagged by `hermes doctor` — a
   pre-existing property of the machine's Python, not introduced here. It has
   not caused an observed failure across 18 sessions and 123 messages.

9. **The agent's web fact-check was not independently reproduced.** In the web
   UI it reported the latest `ggml-org/llama.cpp` release as tag `b10520`; a
   direct call to the same endpoint minutes later returned `v0.2.0`. What is
   proven is that the web tool ran and returned live third-party content — not
   that the model reported it accurately. Recorded because self-report is not
   evidence.

10. **`hermes -p localchat auth` blocks without a TTY.** It was invoked once
    during the credential audit and had to be killed. Credential absence was
    established from the filesystem instead. Avoid it in scripts.

11. **Proving used a small pty harness** (`hermes-approval-*.txt`,
    `hermes-delegation.txt`) to drive Hermes' interactive TUI, since the approval
    dialog is arrow-key driven. The transcripts are cleaned captures of real
    terminal output. The Open WebUI image attachment was injected into the page's
    own hidden `<input type="file">` via a `DataTransfer` and a normal `change`
    event — the same DOM path drag-and-drop produces — because browser automation
    cannot drive the macOS native file picker. Both are harness details;
    everything downstream is the unmodified product path. Stated so no proof is
    over-claimed.

12. **Proving fixtures left on disk** at `~/hermes-proving/` (a scratch note and
    a disposable git fixture repo). Outside the repository, harmless, deletable.

13. **Startup persistence is login-scoped, not boot-scoped** (§12a). User
    LaunchAgents cannot start before a user session exists, and the alternative —
    a root-owned system daemon behind a shell-capable agent — is exactly what
    this commission is forbidden to build. macOS automatic login is the supported
    bridge and is the operator's call.

14. **No physical reboot was performed** (§12a). A full cold domain load was, and
    it exercises the same `RunAtLoad` mechanism login uses.

15. **`hermes gateway install` has two defects** (§12a): it bakes the calling
    shell's `PATH` into the service on every run, and its `--force` path races
    launchd and silently degrades to an unsupervised process while reporting
    success. Both are worked around in `scripts/install-hermes-localchat.sh`; the
    first one had already tainted the initial install of this commission and was
    corrected.

**Not built, as required:** no Foreman, no commission artifact, no
coder/reviewer lifecycle, no automatic merges, no governed implementation
framework, no `foreman` profile, no Qwen-B routing, no change to the Qwen-B
controller, no passwordless sudo, no privilege escalation of any kind.

---

## 17. Files created or modified

**In this repository**

```
docs/06-hermes-localchat.md                     runbook + architecture
docs/07-commission-3-completion-record.md       this file
docs/08-foreman-relevant-hermes-findings.md     research note (no implementation)
config/hermes/localchat.config.yaml             profile config, secret-free
config/hermes/localchat.env.template            env template, placeholder key only
config/com.newton.openwebui.plist.template      Open WebUI LaunchAgent template
skills/newton-local/SKILL.md                    the one project skill
scripts/hermes-localchat                        operator entry point
scripts/install-hermes-localchat.sh             profile installer
scripts/install-hermes-skill.sh                 skill installer
scripts/install-openwebui.sh                    frontend installer
scripts/uninstall-openwebui.sh                  frontend uninstaller
scripts/prove-hermes-service.py                 startup-persistence proving harness
assets/hermes-vision-test.png                   vision fixture
benchmarks/hermes/delegation-records.json       delegation evidence
benchmarks/hermes/provider-audit.txt            73/73 local-inference audit
benchmarks/transcripts/hermes-*.txt             22 proving transcripts
README.md                                       updated for the new layer
```

**Outside the repository (machine state)**

```
~/.hermes/                                      Hermes install + default profile
~/.hermes/profiles/localchat/                   the assistant's own state
~/.local/bin/{hermes,localchat,open-webui}      launchers
~/.local/share/uv/tools/open-webui/             isolated frontend venv
~/.open-webui/                                  frontend data (mode 0700)
~/Library/LaunchAgents/ai.hermes.gateway-localchat.plist
~/Library/LaunchAgents/com.newton.openwebui.plist
~/Library/Caches/ms-playwright/chromium-1234    browser engine
/opt/homebrew/…/ripgrep 15.2.0                  only system-wide package added
~/hermes-proving/                               disposable proving fixtures
```

**Deliberately not committed:** the profile `.env` (bearer key), `state.db`
(private conversations), memory files, gateway/agent logs, delegation caches,
Open WebUI data, the Hermes install tree, Node, Playwright Chromium, and the
rendered `com.newton.openwebui.plist` (it embeds the key).

---

## 18. Acceptance summary

All 47 acceptance criteria were exercised, plus the mid-commission startup-
persistence requirement (§12a). The four items that carry a caveat rather than a
clean pass are recorded in full above and repeated here so nothing is buried:
approvals do not reach the web-UI path (§12, deviation 3); Open WebUI
authentication is off (deviation 1); the agent's one web-sourced fact was not
independently reproduced (deviation 9); and startup persistence is login-scoped
rather than boot-scoped, proven by cold domain load rather than by a physical
reboot (deviations 13–14).
