# Hermes primitives relevant to a future Foreman

**Research note. Nothing here is implemented.** Commission #3 built a general
assistant, not Foreman. This document records what was learned about Hermes
v0.20.5 while doing that, specifically so a later Foreman design can start from
measurements instead of assumptions.

Everything below was observed on this machine unless marked *(documented, not
observed)*.

The intended future shape, for context only:

```
discussion → formal commission → human approval → immutable execution artifact
   → fresh coder → evidence → fresh independent reviewer → PASS / FAIL
```

---

## 1. Hermes already provides

### 1.1 Fresh-context delegation — the single most reusable primitive

`delegate_task` spawns a child `AIAgent`. Empirically, on this machine
(`benchmarks/hermes/delegation-records.json`, `deleg_7931b8eb`):

| Property | Observed |
|---|---|
| Child conversation | **completely fresh** — the child's own words: *"I have no view of any earlier conversation between the user and my parent agent; my context contains only the dispatched task and its context block"* |
| Leakage test | parent held a session-local token; the token was kept out of the payload; child correctly reported it knew no codeword |
| Child input | exactly the `goal` + `context` strings the parent writes, plus the workspace's project-context files (`.hermes.md` / AGENTS.md / CLAUDE.md / `.cursorrules`) — **`SOUL.md` is excluded** |
| Child model | `"model": "qwen-a"` recorded in the result; child session row carries `billing_provider=custom`, `billing_base_url=http://127.0.0.1:8080/v1` |
| Child terminal | its own shell — `shell_pid=22604`, `pwd=/Users/ericjellerson` |
| Child tools | 27, inherited from the parent minus the blocked set |
| Blocked for leaf children | `delegate_task`, `clarify`, `memory`, `send_message`, `cronjob` |
| Return to parent | one final summary string, plus structured metadata |
| Parent visibility | summary, `duration_seconds`, `api_calls`, `tokens{input,output}`, `model`, `exit_reason`, `truncated`, `cost_usd`, and a `tool_trace` array (per call: tool name, arg byte count, result byte count, status) |
| Live observability | an append-only transcript at `~/.hermes/profiles/<p>/cache/delegation/live/<deleg_id>/task-<n>.log`, streaming while the child runs — `tail -f` works |
| Durable record | table `async_delegations` in `state.db`: id, parent session, state, dispatch/completion timestamps, full `task_json` and `result_json`, `delivery_state` |

**This is most of an evidence channel already.** A commissioned coder run has an
immutable input (`goal` + `context`), a recorded output (`result_json`), a
measured cost, and a replayable transcript — all persisted, all queryable with
SQL.

**Two behaviours Foreman must design around:**

1. **Top-level delegation is asynchronous and session-owned.** The parent gets a
   handle immediately and the result re-enters the conversation later. If the
   owning session dies first, the child is orphaned and its record is written as
   `state: "unknown"` with `"Delegation owner exited before recording a terminal
   result; outcome unknown."` — observed here as `deleg_5c2dc929`. Hermes is
   deliberately honest rather than optimistic: it will not claim a result it
   cannot prove. A `chat -q` one-shot therefore **cannot** host a delegation.
2. **The live transcript truncates the final summary** (~500 chars + `…(N chars)`).
   The complete summary is only in `async_delegations.result_json`. Read the
   database, not the log, for verdicts.

### 1.2 Git worktree isolation *(documented, not observed — off here)*

`delegation.worktree_isolation: true` gives each child its own worktree branched
from the repo's current `HEAD`:

* child works in `<repo>/.worktrees/subagent-<id>` on branch
  `hermes-subagent/subagent-<id>`, and its goal message tells it to commit there;
* the parent's checkout is untouched; children cannot clobber each other;
* the child's result entry gains `worktree: {path, branch, commits, dirty}`;
* a worktree with **no commits and a clean tree is pruned automatically**
  (`pruned: true`); anything holding work is kept;
* pruning requires proof — if the git inspection probe fails, the worktree is
  kept and the entry carries `inspection_failed: true`, so `commits`/`dirty`
  become defaults rather than measurements.

Scope limits worth knowing now: **opt-in, git-only, local-terminal-backend only**.
In a non-git directory, on docker/ssh/modal backends, or if worktree creation
fails, it degrades *silently* to the shared workspace. Silent degradation is
exactly the failure mode a governance layer must not inherit — Foreman should
verify the branch exists rather than trust the flag.

This is the natural substrate for "fresh coder works in isolation, reviewer
reads the branch."

### 1.3 Session isolation and history

* Sessions live in `state.db` (`sessions`, `messages`, plus FTS and trigram
  indexes). A fresh session carries the system prompt and memory files and
  nothing else — demonstrated in both the CLI and the web UI.
* `session_model_usage` is a per-session ledger of `(model, billing_provider,
  billing_base_url, task, api_call_count, input/output/cache tokens, cost)`.
  This is a ready-made audit trail: the Commission #3 audit
  (`benchmarks/hermes/provider-audit.txt`) used it to prove all 73 inference
  calls went to `http://127.0.0.1:8080/v1` and nowhere else.
* Child sessions record `parent_session_id`, so a run tree is reconstructible
  after the fact.
* `hermes sessions export` emits JSONL.

### 1.4 Profiles as isolation boundaries

`~/.hermes/profiles/<name>/` is a complete Hermes home — own `config.yaml`,
`.env`, `SOUL.md`, memory, sessions, skills, cron, state DB, logs, gateway.
Selected with `--profile` / the `HERMES_HOME` env var. A `foreman` profile can
therefore have a *completely different* persona, approval policy, toolset and
model pin from `localchat` without either affecting the other.

**But profiles isolate Hermes state, not the filesystem.** Both profiles run as
the same Unix user with the same reach. A Foreman profile is not a sandbox.

Note the known upstream caveat: `hermes profile create --clone` copies memory,
and there is an open issue about agents reading across profile boundaries
(NousResearch/hermes-agent#10376). Foreman should not rely on profile
separation as a *security* boundary.

### 1.5 Approvals

* `approvals.mode: manual | smart | off`, `timeout` (fail-closed on expiry),
  and separate `cron_mode` / `single_query_mode` for headless contexts — both
  default to `deny`, which is the right default for automation.
* `approvals.deny:` — user-defined fnmatch globs that block **unconditionally,
  before** `--yolo` and `mode: off` are consulted. Matched against
  normalised/deobfuscated command variants. This is how a Foreman could enforce
  "a coder may never `git push --force`, never touch `main`, never `rm -rf`
  outside its worktree" without depending on the model's cooperation.
* A hardline code-shipped blocklist sits below everything (`rm -rf /`, fork
  bombs, `mkfs` on a live root, `dd` to a block device).
* `command_allowlist:` in `config.yaml` for the permanent "always allow" set,
  and `hermes approvals suggest` mines history into allowlist proposals.

Threat model, per Hermes' own docs and confirmed here: these are guardrails
against an **honest-but-wrong agent**, not a sandbox against an adversarial one.
Observed during proving: `python3 -c '…'` was blocked by the script-execution
pattern, so the agent wrote the same code to a file and ran the file. For a real
boundary, Hermes offers container terminal backends (docker / singularity /
modal / daytona / vercel_sandbox) and `HERMES_WRITE_SAFE_ROOT`.

### 1.6 API-driven runs

The API server (`platforms.api_server`) is the most Foreman-shaped surface,
because a governance layer wants to *drive* agents, not chat with them:

| Endpoint | Use to Foreman |
|---|---|
| `POST /v1/runs` | submit a run, get a `run_id` — the natural "execute this commission" call |
| `GET /v1/runs/{id}` | poll state |
| `GET /v1/runs/{id}/events` | SSE stream of tool progress and lifecycle events — a live evidence feed |
| `POST /v1/runs/{id}/stop` | hard interrupt |
| `POST /v1/responses` | server-side conversation state via `previous_response_id` |
| `GET /v1/capabilities` | machine-readable feature discovery |
| `GET /health/detailed` | readiness incl. `active_delegations`, `active_api_runs` |

`/v1/capabilities` on this install advertises `run_approval_response: true` and
`approval_events: true` — meaning **approvals can be brokered programmatically
over the Runs API**, which plain `/v1/chat/completions` cannot do. A Foreman
approval gate should be built on Runs, not Chat Completions.

Also present and unexercised here: `/api/jobs`, `/api/sessions/*`, `/v1/skills`,
`/v1/toolsets`, `session_fork`, `session_model_lock`, and a
`max_concurrent_runs` cap (default 10, HTTP 429 beyond).

### 1.7 Skills as injectable doctrine

A skill is a directory with a `SKILL.md` carrying YAML frontmatter. Proven
here: the index (name + description) sits in the system prompt; `skills_list`
enumerates; `skill_view` loads the body. Loading changes **the prompt only** —
it does not alter tool availability. Skills are per-profile, so a `foreman`
profile could carry commission-format and review-rubric doctrine that
`localchat` never sees. `/review` already injects "the skills the primary agent
had loaded" into a reviewer's briefing.

### 1.8 A reviewer rail that already exists

`/review` spawns an independent, full-privilege background subagent whose only
job is to review what the conversation just produced. It gets the normal
subagent toolset (so it opens the PR and runs the code rather than judging from
an excerpt), inherits the parent's loaded skills as binding conventions, and its
finding re-enters the same session. `auxiliary.review.{provider,model}` pins a
dedicated reviewer model.

That is structurally close to "fresh independent reviewer" — with one important
gap noted below.

### 1.9 Other pieces worth remembering

* `execute_code` — programmatic tool calling for mechanical pipelines; cheaper
  than a reasoning child.
* `cronjob` — durable execution that survives session closure and process
  restart, unlike delegation.
* `terminal(background=True, notify_on_complete=True)` — long-running commands.
* `hermes hooks` — shell hooks with an explicit approval step.
* `checkpoints` / `/rollback` — a shadow git store of agent edits.
* `hermes kanban` — a multi-profile board with a dispatcher and **per-task model
  override** (which `delegate_task` lacks). If Foreman needs different models for
  coder and reviewer within one run, kanban is the existing mechanism.
* `delegation.max_spawn_depth` / `orchestrator_enabled` — bounded trees, with a
  global kill switch.

---

## 2. Foreman still needs to provide

Not built, not started. Listed so the gap is explicit.

1. **The commission artifact itself.** Hermes has no notion of an immutable,
   human-approved specification. `delegate_task`'s `goal`/`context` are strings
   a model composes at runtime — the opposite of immutable. Foreman must own
   authoring, hashing, storing, and pinning the artifact, and must pass it to
   the child rather than letting a model paraphrase it.

2. **The human approval gate on the commission.** Hermes' approvals gate
   *commands*, not *plans*. Nothing asks a human to sign off on a body of work
   before it starts.

3. **Coder/reviewer orchestration as a lifecycle.** `/review` is
   conversation-scoped and model-initiated. Foreman needs a deterministic
   state machine — commission → coder → evidence → *fresh* reviewer → verdict —
   that a model cannot skip, reorder, or re-run until it likes the answer.

4. **Reviewer independence guarantees.** `/review` seeds the reviewer with the
   last 10 messages of the parent conversation. That is deliberately *not*
   independent. A Foreman reviewer must be seeded from the commission and the
   diff only, never from the coder's narrative.

5. **A PASS/FAIL verdict type.** Hermes returns prose summaries. There is no
   structured verdict, no schema, no place a verdict is recorded as a fact.
   (Hermes does support forcing structured output via schemas at the tool layer;
   the *governance semantics* are what is missing.)

6. **Evidence policy.** Hermes records what happened. It does not require that a
   claim be accompanied by reproducible evidence, nor verify that the evidence
   supports the claim. Note the concrete failure mode observed in Commission #3:
   the assistant reported a llama.cpp release tag from `web_extract` that an
   independent check did not reproduce. Self-report is not evidence.

7. **Worker acquisition policy.** Delegation inherits the parent's provider.
   Routing children to Qwen-B means (a) pinning `delegation.base_url` to
   `http://127.0.0.1:8081/v1`, and (b) — the hard part — acquiring and releasing
   the lease around the child's lifetime. `scripts/qwenctl.py` owns that
   protocol today and must keep owning it: `delegation.*` is static config, so
   nothing in Hermes will call `acquire`/`release` for you. **A pinned
   `base_url` with no lease would send children to a dead port.**

8. **Merge policy.** Worktree isolation produces branches. Deciding which
   branches merge, on what evidence, is entirely Foreman's.

9. **A real sandbox, if the boundary must hold against a wrong agent.** Approval
   patterns are bypassable by rephrasing. Container backends or
   `HERMES_WRITE_SAFE_ROOT` are the available primitives.

---

## 3. Measurements a Foreman design should budget against

From this machine, Qwen-A, `--parallel 1`:

| | |
|---|---|
| Hermes system prompt + tool schemas | ~25,000 tokens (~95 KB) |
| Cold prompt processing | ~75 s at ~330 tok/s |
| Generation | ~19 tok/s |
| Warm continued session | seconds |
| One trivial 3-tool child, end to end | **177 s**, 2 API calls, 39,120 in / 1,320 out |
| Concurrency | **none** — Qwen-A is a single slot; parallel children serialise |

A coder/reviewer pair on this hardware is minutes per exchange, not seconds, and
`max_concurrent_children` buys nothing while both children share one slot. That
is the strongest architectural argument for the elastic Qwen-B worker — and the
reason worker acquisition (item 7) is the first real problem Foreman has to
solve, not the last.
