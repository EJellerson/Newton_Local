# Architecture repair verification — 2026-09-15

## 1. Result

The applicable active-machine acceptance checks passed. Verified resident
identity, generation-aware Hermes context, deterministic identity-only answers,
and durable completion enforcement are installed and reproducible. Both portable
projects are renamed, repaired and synchronized. No GLM model was initialized,
no portable production service was started, and nothing was pushed.

The active production selection was restored to **Flash-Next, stopped**.
Hermes, Open WebUI and the control service remain available.

## 2. Cause → fix → proof

| Confirmed cause | Implementation | Evidence |
| --- | --- | --- |
| Generic alias and static memory confused routing with resident weights | Separate verified record from `local-model`; migrate current memory/skill assertions | Real process/socket/argument/backend checks; both active models report their actual artifacts |
| Alias stayed unchanged across switches, retaining old prompts | Persisted and in-memory prompts bind to resident generation; route and generation checked during the turn | One resumed session: 27B → Flash-Next → 27B, three generations, two logged prompt rebuilds |
| Flash-Next could copy an old answer despite a correct refreshed prompt | Identity-only answers are rendered from verified facts before streaming/persistence | Live switch cycle passes; fake provider deliberately gives stale 27B answer and cannot expose it |
| Permanent vision assertion and alias-based guidance | Context/vision derive from bound identity; local execution guidance is explicit policy | Unit capability tests, active runtime metadata and live answers |
| Text stop accepted unresolved promised/requested work | Durable session tasks and receipts gate candidate final responses, with three bounded continuations | Four incident shapes, real Hermes mock-loop exhaustion, live A–F cases |
| Tool invocation could be confused with success | Distinct requested/executed/result/success/failure/consumed fields and explicit task resolution | Actual `false` exits 1 and returns blocked/error; successful multi-action cases retain receipts |
| Portable switch lock did not cover a whole agent turn | Owner lease covers inference, tools and stream lifetime; backend streams borrow the generation; admin changes wait | Concurrent real router-handler tests, cancellation/drain and terminal-generation rejection |
| Portable MLX request IDs could become unintended load targets | Adapter maps bound registry entry to its artifact path; MLX-LM wrapper refuses unbound model/adapter/draft changes | Real proxy translation and fake loaded-provider tests; no MLX imports in those tests |
| Copied launchers retained staging paths; bootstrap repaired the wrong environment | Root-derived launchers and path repair; bootstrap targets Hermes `venv` explicitly and reapplies pinned integration | Real bundled interpreter imports, relocation fixture with spaces, zero pending path repairs on both drives |
| Lifecycle proof had an always-true assertion | Actual old-process/port checks plus continuous resident-count sampling | Real tiny HTTP stubs: maximum resident count 1 on both drives |
| Runtime hand-edits would not survive installation | Shared modules and three exact, hash-bound patch stages | Pristine pinned upstream source reproduced final patched hash; installed-source checks passed in all three deployments |

## 3. Changed surfaces

### Active repository

Identity/control: `integrations/newton_runtime/identity.py`,
`integrations/newton_runtime/hermes.py`, `scripts/resident_identity.py`,
`scripts/newton_control.py`, `scripts/control-panel.html`, `scripts/qwenctl.py`,
`config/models.json`, `config/qwen-a.env`, and current operator/client aliases.

Completion/reproduction: `integrations/newton_runtime/execution.py`,
`integrations/hermes*.patch`, `integrations/hermes-patch.json`,
`scripts/install-hermes-integration.py`, `scripts/install-hermes-localchat.sh`,
`scripts/migrate-resident-memory.py`, `scripts/repair-portable-paths.py`,
`config/hermes/localchat.config.yaml`, `skills/newton-local/SKILL.md`,
the three new proving scripts, four regression test files, and current docs.

### Installed active Hermes

`~/.hermes/hermes-agent/agent/conversation_loop.py` and
`agent/newton_runtime/*.py`; the `localchat` profile configuration, audited
current memory assertions and operations skill. Historical chats were preserved.
Pinned upstream: Hermes 0.20.5, commit
`41447a6d7063b2772b0c2f26a5b22d9bd444fb43`.

### SSD and HDD portable projects

Both contain equivalent changes to `services/localfable/{manager,router,runtimes,
lease,mlx_lm_server,paths,config}.py`, project/profile configuration, LaunchAgent
templates, bootstrap/install scripts, root-derived CLI/launchers, lifecycle and
router tests, current documentation and the identical shared Hermes integration.
Generated environment pointers and console launchers were repaired locally.
The active profile is `localchat`; host state marks bootstrap as required on the
destination. Secrets and historical transcripts were not rewritten.

## 4. Naming

| Meaning | Final name |
| --- | --- |
| Project | `Newton_Local` |
| Generic inference request alias | `local-model` |
| Human service/slot label | `AI Inference` |
| Resident model | Specific controller-verified registry/artifact/backend identity |

Portable service labels use `com.newton.portable.*`. Internal Python namespace
`localfable` and JSON extension key `local_fable` remain for compatibility.
Historical instance identifiers and experimental worker behavior are preserved.
The active catalog remains 27B + Flash-Next; the portable catalog remains
27B + GLM 5.3 Flash + GLM 5.3 Full.

## 5. Active verification

Run from the active repository:

```sh
python3 -B -m unittest discover -s tests -q
python3 -B scripts/test-model-registry.py
python3 -B scripts/test-qwenctl-logic.py
python3 -B scripts/prove-runtime-identity.py
python3 -B scripts/prove-agent-completion.py --model qwen27b
python3 -B scripts/prove-agent-completion.py --model flash-next
python3 -B scripts/prove-agent-completion.py --model flash-next --stream --cases DEF
/Users/ericjellerson/.hermes/hermes-agent/venv/bin/python3 -B scripts/prove-hermes-completion-loop.py
python3 -B scripts/install-hermes-integration.py --runtime /Users/ericjellerson/.hermes/hermes-agent --check
git diff --check
```

Results: **40 unit tests, 68 registry checks and 42 controller checks passed**.
The real-loop mock proof rejected four narration-only responses, continued three
times and returned incomplete. It also rejected model-supplied stale identity
and refreshed a reused agent after a generation change. Clean-source patch
application reproduced the exact final upstream-file hash.

Both real models passed A (one action), B (three independent actions),
C (two sequential actions), D (intentional failure), E (no-tool final), and
F (generic task resolved against a successful receipt). D returned blocked with
one executed failed receipt and API `finish_reason=error`. E used no tools and
one model pass. Flash-Next streaming D/E/F also passed, including a visible
remaining-work notice for D. Manual approval policy was not weakened.

Final identity proof session: `newton-identity-proof-fa1f893a8e18`.
It performed three actual model inference calls with these verified generations:

| Resident | Backend | Context | Vision | Generation prefix |
| --- | --- | ---: | --- | --- |
| Qwen 3.8 27B | llama.cpp, b1-f280b26 | 262144 | yes | `b283f27282c4` |
| Qwen 3.8 Flash-Next | mlx-serve, v26.9.1 | 262144 | yes | `569b319a7115` |
| Qwen 3.8 27B | llama.cpp, b1-f280b26 | 262144 | yes | `04491fc5a8d1` |

All returned identity fields matched the records. Logs confirmed two stale-prompt
rebuilds. The UI displayed AI Inference with the verified resident while running
and no resident name after stopping. Raw proof JSON and runtime history stay
untracked under `state/` and the Hermes profile.

## 6. Portable verification

For each final root, the following commands passed, with `NEWTON_LOCAL_ROOT`
set to that root and paths resolved from it:

```sh
./services/.venv/bin/python3 -B -m unittest discover -s tests -q
./services/.venv/bin/python3 -B scripts/prove-lifecycle.py
./services/.venv/bin/python3 -B scripts/prove-registry.py
./services/.venv/bin/python3 -B scripts/verify-models.py
python3 -B scripts/repair-portable-paths.py --check
python3 -B scripts/install-hermes-integration.py --runtime hermes/runtime/hermes-agent --check
./scripts/portability-audit.sh
```

Each copy passed **11 router/concurrency/adapter tests**, the real-stub lifecycle
proof, registry/hardware gates, portability audit and zero-pending-repair check.
All three rendered LaunchAgents passed `plutil -lint`. Bundled service Python
3.12.13/Starlette 0.47.2 and Hermes Python 3.11.15 imported successfully.

Inventory presence/size checks passed for 2 Qwen artifacts (29.98 GB),
74 Flash artifacts (289.15 GB), and 99 Full artifacts (418.34 GB) on each drive.
These are inventory checks, not a fresh hash comparison of all model payloads.
**No GLM model or server was initialized or started.**

## 7. Limits

Actual GLM loading, inference/tool behavior, memory/context performance and a
fresh bootstrap on the future high-memory machine still require destination
verification. Hardware gating on this 128 GiB Mac is intentional.

Completion enforcement preserves recorded/detected obligations and checks real
receipts. Natural-language task extraction and the relevance of a receipt to an
abstract task remain model-assisted. It does not prove arbitrary semantic task
completion. Runtime identity verifies operational process/artifact/metadata
binding; it does not hash the model's in-memory weight tensors.

## 8. Git scope

Work stayed on active `main`, starting from clean baseline
`e60394adb01571e7acf08c86638a61776d2616be`. Logical fixes were tested and committed
separately. Runtime state, histories, secrets, model weights and external-drive
contents are excluded. The final response records the resulting commit hashes
and final status. No push and no external Git mutation were performed.

## 9. External state

- SSD: `/Volumes/PRO-G40/Newton_Local`
- HDD: `/Volumes/Fable_Local/Newton_Local`

The user-confirmed HDD volume rename was independently matched by volume UUID
`1B6E6BE2-9F26-48ED-85E3-B5834565AE2C`; this commission did not rename a physical
volume. Both project directories were renamed from `Local_Fable` to `Newton_Local`.

Final comparison: **426 architecture files matched**, including current source,
templates, scripts, registry, documentation and bundled Hermes agent/gateway
code. **200 generated launcher/config entries matched after normalizing their
project roots.** Shared integration modules match the active managed source in
both portable source and installed-runtime locations. Root-specific generated
paths, host/bootstrap state, secrets and runtime histories are intentionally
outside a blanket whole-tree equality claim. No unexplained architecture
divergence remains; large model byte-for-byte identity was not claimed.
