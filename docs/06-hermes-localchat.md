# Hermes `localchat` — the general local assistant

**Runbook and architecture note for Commission #3.**
Proving results and provenance live in
[`07-commission-3-completion-record.md`](07-commission-3-completion-record.md);
research findings for a future Foreman live in
[`08-foreman-relevant-hermes-findings.md`](08-foreman-relevant-hermes-findings.md).

---

## 1. What this is

```
                  ┌──────────────────────────────┐
   you  ───────►  │  Open WebUI    127.0.0.1:3000│   presentation
                  └───────────────┬──────────────┘
                                  │  OpenAI-compatible HTTP + SSE
                                  │  Authorization: Bearer <API_SERVER_KEY>
                  ┌───────────────▼──────────────┐
                  │  Hermes gateway / API server │   agency
                  │            127.0.0.1:8642    │
                  │  ┌────────────────────────┐  │
                  │  │  Hermes AIAgent        │  │   tools · memory · sessions
                  │  │  profile: localchat    │  │   skills · delegation
                  │  └───────────┬────────────┘  │   browser · vision
                  └──────────────┼───────────────┘
                                 │  OpenAI-compatible HTTP
                  ┌──────────────▼───────────────┐
                  │  llama-server  127.0.0.1:8080│   intelligence
                  │  Qwen3.8-27B Q8_0 · 262144   │
                  │  alias qwen-a · Metal        │
                  └──────────────────────────────┘
```

Three responsibilities, three layers, and they do not overlap:

* **Qwen-A owns intelligence.** It is a plain llama.cpp server. It has no
  tools, no memory and no idea that Hermes exists.
* **Hermes owns agency.** Every tool call, every memory write, every session,
  every subagent runs inside the Hermes process on this Mac.
* **Open WebUI owns presentation.** It is a chat client. It has no tools of its
  own in this deployment and never talks to port 8080.

The elastic worker **Qwen-B (`127.0.0.1:8081`) is not part of this path.** It
stays stopped. Nothing in this stack acquires it.

---

## 2. Quick start

```bash
~/Newton_Local/scripts/hermes-localchat status
```

Then either open the web UI:

```bash
open http://127.0.0.1:3000
```

or talk to the same agent in the terminal:

```bash
~/Newton_Local/scripts/hermes-localchat chat
```

Both routes are the *same* agent, the same memory and the same session store.
Only the front door differs.

---

## 3. Components and where their state lives

| Component | Version | Runs as | Bind | State |
|---|---|---|---|---|
| Hermes Agent | v0.20.5 (`v2026.8.19`), upstream `41447a6d` | `ericjellerson` | — | `~/.hermes/` |
| `localchat` profile | — | `ericjellerson` | — | `~/.hermes/profiles/localchat/` |
| Hermes gateway + API server | same | user LaunchAgent `ai.hermes.gateway-localchat` | `127.0.0.1:8642` | profile dir |
| Open WebUI | 0.11.0 (uv tool, Python 3.11.15) | user LaunchAgent `com.newton.openwebui` | `127.0.0.1:3000` | `~/.open-webui/` |
| Qwen-A | llama.cpp `b10615` | system LaunchDaemon `com.newton.qwen38server.a` | `127.0.0.1:8080` | unchanged by this commission |

### `~/.hermes/profiles/localchat/` — everything the assistant is

```
config.yaml        model/provider, approvals, terminal, memory, delegation, api_server
.env               API_SERVER_KEY and any future credentials      ← mode 0600, never committed
SOUL.md            persona / standing instructions
memories/
  MEMORY.md        durable agent notes, entries separated by "§"   (cap 2,200 chars)
  USER.md          durable user profile                            (cap 1,375 chars)
sessions/          (empty — conversations live in state.db)
state.db           SQLite: sessions, messages, run state, FTS index
skills/            83 skills: 82 bundled + newton/newton-local from this repo
logs/gateway.log   the API server / gateway log
cron/ hooks/ plans/ workspace/ skins/ pairing/   unused here
```

A Hermes **profile** is a complete, isolated Hermes home. The wrapper
`~/.local/bin/localchat` simply sets `HERMES_HOME` to that directory before
exec'ing `hermes`; `hermes -p localchat …` does the same thing.

**Profiles isolate Hermes state, not the filesystem.** The agent still runs as
`ericjellerson` and can read and write anything that user can. That is
deliberate here — see §6.

Nothing under `~/.hermes/` is in this repository. The repository carries the
reproducible *configuration* (`config/hermes/localchat.config.yaml`), the
skill source (`skills/`), the scripts, and sanitised proving evidence.

---

## 4. How Hermes is pointed at Qwen-A

`~/.hermes/profiles/localchat/config.yaml` (sparse — merged over Hermes'
shipped defaults):

```yaml
model:
  provider: custom                     # not "openai", not "anthropic", not "nous"
  base_url: http://127.0.0.1:8080/v1
  default: qwen-a
  context_length: 262144               # mirrors llama.cpp --ctx-size exactly
  supports_vision: true                # send images natively, don't pre-describe them
auxiliary:                             # every side task uses the SAME local model
  vision:            {provider: main}
  web_extract:       {provider: main}
  approval:          {provider: main}
  compression:       {provider: main}
  title_generation:  {provider: main}
```

Three separate things make the local path deterministic:

1. `model.provider: custom` + an explicit `base_url`. There is no cloud
   provider configured and no credential for one.
2. `fallback_providers` resolves to `[]`. Hermes' fallback chain is empty, so a
   Qwen-A failure surfaces as an error rather than silently re-routing to a
   vendor API.
3. `auxiliary.*.provider: main`. Hermes runs side tasks — image description,
   web-page summarisation, the dangerous-command risk classifier, context
   compression, session titles — on a separate "auxiliary" model. Left at
   `auto` these already follow the main model, but they are pinned explicitly
   so the audit answer is a config line rather than an inference about a
   default.

`supports_vision: true` matters: Qwen-A serves an F16 `mmproj`, so images are
sent to it as native `image_url` parts. Without that flag Hermes would try to
route images through an auxiliary *describer* model first, losing detail for
no reason.

**Cost of the local path.** Hermes' system prompt plus tool schemas is about
**25,000 tokens**. On a cold prompt cache that is ~75 s of prompt processing at
~330 tok/s before the first token appears; generation then runs at ~19 tok/s.
llama.cpp's prompt cache absorbs the repeat, so a *continued* session answers in
seconds. This is the honest shape of the product: a fresh conversation is slow to
start and then normal. `hermes -p localchat prompt-size` shows the breakdown.

---

## 5. Approval model — what needs a human

```yaml
approvals:
  mode: manual              # every dangerous-pattern match asks a human
  timeout: 300              # no answer in 5 min = denied (fail-closed)
  cron_mode: deny           # headless cron: deny, never auto-approve
  single_query_mode: deny   # `chat -q` one-shots: deny, never auto-approve
```

`manual` was chosen over Hermes' default `smart`. `smart` asks an auxiliary LLM
to grade each command and auto-approves the ones it judges low-risk. That is a
model deciding what is safe. `manual` asks the person.

In the interactive CLI a match renders as:

```
╭──────────────────────────────────────────────────────────────────╮
│ ⚠️  Dangerous Command                                            │
│                                                                  │
│ rm -rf /Users/ericjellerson/hermes-proving/scratch/…             │
│                                                                  │
│ ❯ 1. Allow once                                                  │
│   2. Allow for this session                                      │
│   3. Add to permanent allowlist                                  │
│   4. Deny                                                        │
│                                                                  │
│ recursive delete                                                 │
╰──────────────────────────────────────────────────────────────────╯
  ↑/↓ to select, Enter to confirm  (299s)
```

Denying returns `BLOCKED: User denied this command.` to the model with an
instruction not to retry. Option 3 writes a pattern into `command_allowlist:`
in `config.yaml`; review it with `hermes-localchat config`.

**What the gate is and is not.** It is a guardrail against an honest-but-wrong
agent — Hermes' own threat model, and ours. It is *not* a sandbox. The agent
runs as your user; a determined or confused model can often reach the same
outcome by a route that does not match a dangerous pattern. Observed during
proving: `python3 -c '…'` was blocked (script-execution pattern), so the agent
wrote the same two lines to a file and ran the file. That is worth knowing
before trusting the gate with something that actually matters.

Below the gate sits Hermes' **hardline blocklist** (`rm -rf /`, fork bombs,
`mkfs` on a live root, `dd` to a block device, piping an untrusted URL to a
root-level shell). Those refuse to run regardless of approval mode, `--yolo`,
or allowlist entries.

### Approvals from the web UI

The API server advertises `approval_events: true` and
`run_approval_response: true` on `/v1/capabilities`, but those live on the
**Runs** API (`/v1/runs/{id}/events`), not on `/v1/chat/completions`. Open WebUI
speaks plain Chat Completions, so a dangerous command started from the browser
has no one to ask: it waits out `approvals.timeout` and is then **denied**.
Fail-closed, but a five-minute stall. **Do destructive work from
`hermes-localchat chat`, where the dialog exists.** See §12.

---

## 6. Reach — what the assistant can touch

```yaml
terminal:
  backend: local
  cwd: /Users/ericjellerson
```

Deliberately the whole home directory, not `~/Newton_Local`. This is a general
machine assistant; scoping it to one project would have made it useless for the
work it is meant to do. Concretely it can:

* run any command your login shell can run, as `ericjellerson`, on the real
  macOS host — no container, no sandbox;
* read and write any file that user owns;
* reach the network through its web/browser tools.

It **cannot** escalate to root. No sudoers change was made, no passwordless
sudo, no privileged helper. A `sudo` command from the agent will sit waiting for
a password it does not have and eventually fail. Root-owned Qwen-A
infrastructure is unchanged and unreachable from the agent.

If you ever want a hard filesystem boundary, Hermes offers
`HERMES_WRITE_SAFE_ROOT` (write sandbox for `write_file`/`patch`) and container
terminal backends. Neither is enabled here.

---

## 7. Memory

Two files, both plain Markdown, both in
`~/.hermes/profiles/localchat/memories/`:

| File | Contents | Cap |
|---|---|---|
| `MEMORY.md` | agent's durable notes about the environment and lessons | 2,200 chars |
| `USER.md` | durable facts about you and how you like to work | 1,375 chars |

Entries are separated by a `§` line. The agent decides what to save — it calls
the `memory` tool with `add` / `replace` / `remove` during a turn, and a
background review after each turn may add more. Both files are injected into the
system prompt **as a frozen snapshot at session start**; a write during a session
lands on disk immediately but does not change that session's prompt.

Memory is **per profile**. It is not conversation history: a new session sees the
memory files and nothing else of what was said before.

```bash
hermes-localchat memory                     # print both files
$EDITOR ~/.hermes/profiles/localchat/memories/MEMORY.md   # edit by hand — it is just text
hermes-localchat memory-clear               # delete both (asks first)
```

To make the agent ask before saving, set `memory.write_approval: true`; writes
then queue for `/memory pending`, `/memory approve <id>`, `/memory reject <id>`.

---

## 8. Sessions

Conversations live in `state.db` (SQLite), not in loose files.

```bash
hermes-localchat sessions list      # recent sessions with ids
hermes-localchat sessions browse    # interactive picker with search
hermes-localchat sessions stats
hermes-localchat resume             # continue the most recent
hermes-localchat resume 20260824_211644_4475db
```

Three things that are easy to confuse:

| | Scope | Survives a new session? |
|---|---|---|
| **conversation history** | one session | no |
| **persistent memory** | the profile | yes |
| **fresh session context** | nothing but the system prompt + memory | — |

`hermes -p localchat sessions export` writes JSONL if you want a session out of
the database.

---

## 9. Web and browser

`web_search` and `web_extract` are enabled and work with **no API key**: a fresh
Hermes install rotates across five vendors' public free tiers (Exa, Parallel,
Tavily, Firecrawl, Keenable) and retries on the next when one throttles. Nothing
was configured; nothing was paid for. Full browser automation
(`browser_navigate`, `browser_snapshot`, …) is available through the bundled
Playwright/Chromium the installer placed in `~/.hermes/`.

**This assistant is not air-gapped.** The split to keep straight:

* **Inference is local.** Your prompts and the model's reasoning never leave
  this Mac.
* **Web tools reach the internet when the model calls them.** A `web_search`
  sends a query to a third-party search vendor. Anything the agent puts in that
  query leaves the machine.

Disable the network side entirely by removing `web` and `browser` from the
enabled toolsets (`hermes -p localchat tools`); the assistant keeps working
against Qwen-A with no internet dependency at all.

---

## 10. Delegation

`delegate_task` spawns a child `AIAgent` with a **completely fresh
conversation**. The child sees only the `goal` and `context` the parent writes
for it; the parent gets back only the child's final summary.

```yaml
delegation:
  max_spawn_depth: 1            # flat — children cannot delegate further
  max_concurrent_children: 2
  max_iterations: 30
```

No `delegation.model`, `delegation.provider` or `delegation.base_url` is set, so
children **inherit the parent's provider and credentials**: they run on Qwen-A
at `127.0.0.1:8080`, same as the parent.

**Delegation is not wired to Qwen-B and must not be.** Qwen-B's lifecycle is
owned by `scripts/qwenctl.py` — deterministic code with a lease protocol —
precisely so a model never decides when 46 GiB of Metal allocation appears. The
bundled `newton-local` skill tells the agent this in as many words.

Children inherit the parent's toolset but are denied `delegate_task` (leaf role),
`clarify`, `memory`, `send_message` and `cronjob`. Hermes can give each child its
own git worktree (`delegation.worktree_isolation: true`); that is **off** here
and is one of the primitives a future Foreman will care about — see doc 08.

---

## 11. Skills

83 skills: 82 bundled with Hermes, plus `newton/newton-local` installed from
`~/Newton_Local/skills/` by `scripts/install-hermes-skill.sh`.

* **Discovery** — every skill's name + one-line description sits in the system
  prompt as an index (~8 KB); `skills_list` enumerates them on demand.
* **Loading** — `skill_view <name>` pulls the full `SKILL.md` into context.
* **Effect** — loading a skill changes *the prompt only*. It does not add or
  remove tools.

A skill is a directory with a `SKILL.md` carrying YAML frontmatter
(`name`, `description`, `version`, `platforms`, `prerequisites`). To add one:
create `skills/<name>/SKILL.md` in this repo and re-run
`scripts/install-hermes-skill.sh`. Skills are **copied**, not symlinked —
Hermes rewrites its own skill index, and a symlink would let the agent edit
tracked repository files.

---

## 12. The two front doors, and which to use

| | `hermes-localchat chat` (CLI/TUI) | Open WebUI |
|---|---|---|
| Same agent, memory, sessions | yes | yes |
| Streaming, Markdown, code blocks | yes | yes |
| Image attachment | `/paste` from clipboard | drag/drop or paste |
| Tool progress | inline, live | inline, live |
| **Dangerous-command approval dialog** | **yes** | **no — request stalls 300 s then denies** |
| Conversation list / search | `sessions browse` | sidebar |

Use the web UI for conversation, research, reading, explaining and light work.
Use the CLI when a task may touch something destructive.

### Do not point the UI at raw Qwen-A

Open WebUI is configured with `OPENAI_API_BASE_URL=http://127.0.0.1:8642/v1` and
`ENABLE_OLLAMA_API=false`. Its model picker shows exactly one entry,
`localchat`, which is the *agent*. Adding `http://127.0.0.1:8080/v1` as a second
connection would give you a bare model with no tools, no memory and no
approval gate, indistinguishable in the UI from the real assistant. Port 8080
stays available for `scripts/qwen-*` and other infrastructure use — not for the
chat window.

---

## 13. Operator commands

Everything below is `~/Newton_Local/scripts/hermes-localchat <cmd>`. No sudo.

```bash
hermes-localchat status            # Qwen-A, Qwen-B, Hermes API, Open WebUI, all at once
hermes-localchat url               # http://127.0.0.1:3000

# talking to it
hermes-localchat chat              # interactive CLI
hermes-localchat tui               # full-screen TUI
hermes-localchat ask "…"           # one-shot, non-interactive
hermes-localchat resume [id]       # continue a conversation

# services
hermes-localchat up                # API + UI
hermes-localchat down              # UI + API
hermes-localchat api start|stop|restart|status|logs
hermes-localchat ui  start|stop|restart|status|logs

# inspection
hermes-localchat sessions list|browse|stats
hermes-localchat memory            # what it durably remembers
hermes-localchat memory-clear      # forget it (asks first)
hermes-localchat config            # resolved config, secrets redacted
hermes-localchat logs              # agent logs
hermes-localchat doctor            # self-diagnosis

# when something is wedged
hermes-localchat recover           # stop service, reap orphans, restart, re-status
```

Raw equivalents, if you prefer them:

```bash
hermes -p localchat gateway status|start|stop|restart
launchctl print gui/$(id -u)/ai.hermes.gateway-localchat
launchctl print gui/$(id -u)/com.newton.openwebui
tail -f ~/.hermes/profiles/localchat/logs/gateway.log
tail -f ~/.open-webui/logs/stderr.log
```

### Rebuilding from scratch

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --skip-setup
hermes profile create localchat
cp ~/Newton_Local/config/hermes/localchat.config.yaml ~/.hermes/profiles/localchat/config.yaml
# then write API_SERVER_ENABLED / API_SERVER_KEY / API_SERVER_HOST / API_SERVER_PORT
# into ~/.hermes/profiles/localchat/.env  (see config/hermes/localchat.env.template)
hermes -p localchat gateway install
~/.hermes/bin/uv tool install --python 3.11 open-webui
~/Newton_Local/scripts/install-hermes-skill.sh
~/Newton_Local/scripts/install-openwebui.sh
```

### Recovery

| Symptom | Do this |
|---|---|
| UI loads, every message errors | `curl -sS 127.0.0.1:8080/health` — Qwen-A is the usual cause |
| UI model picker empty | `hermes-localchat api status`; then `hermes-localchat recover` |
| Gateway wedged / port 8642 stuck | `hermes-localchat recover` |
| First message takes ~90 s | expected on a cold prompt cache — §4 |
| Command hangs then "denied" | an approval prompt with no one to answer it — §5 |
| `hermes: command not found` | `export PATH="$HOME/.local/bin:$PATH"` |

---

## 14. Security posture, stated plainly

| | |
|---|---|
| Hermes API `127.0.0.1:8642` | loopback only; bearer key required even on loopback; LAN probe refused |
| Open WebUI `127.0.0.1:3000` | loopback only; LAN probe refused |
| Open WebUI authentication | **disabled** (`WEBUI_AUTH=false`) — see below |
| Privilege | ordinary user `ericjellerson`; no sudoers change; no passwordless root |
| Agent reach | full home directory, real macOS host, no sandbox |
| Destructive commands | human approval required in the CLI; fail-closed everywhere else |
| Cloud LLM APIs | none configured, none required, empty fallback chain |
| Internet | reachable *by tools only*, when the model calls them |

**`WEBUI_AUTH=false` is a deliberate trade-off.** Open WebUI's first run
otherwise demands that an administrator account be created with a password;
nobody but you should ever do that, so the alternative was to ship a UI that
could not be verified end to end. With authentication off, anything that can
open `http://127.0.0.1:3000` on this Mac gets the assistant — and through it,
your shell. The bind is loopback-only, so that means *local* processes and
*local* users, which on a single-user machine is the same trust boundary as the
terminal. It is not the same boundary as the Hermes API, which still demands a
bearer token.

To turn authentication on, flip the value in the repository template and
reinstall — never hand-edit the rendered plist, since the installer overwrites
it:

```bash
/usr/bin/sed -i '' '/<key>WEBUI_AUTH<\/key>/{n;s|<string>false</string>|<string>true</string>|;}' \
  ~/Newton_Local/config/com.newton.openwebui.plist.template
~/Newton_Local/scripts/install-openwebui.sh
```

Then create the first account in the browser — it becomes the admin. Nobody
else can sign up afterwards (`ENABLE_SIGNUP=false`).

**Never widen either bind.** `API_SERVER_HOST=0.0.0.0` would publish a shell on
this Mac to the network with a single bearer token in front of it.
