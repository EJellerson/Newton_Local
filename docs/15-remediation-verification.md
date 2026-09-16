# Tool-execution remediation verification — 2026-09-16

## A. Exact root cause

The recorded incident **did contain a real terminal inspection**. In Hermes
session `api-87df1ac8a413de81`, messages 487–494 show the exact request, narration,
recovery, a terminal call returning `iogpu.wired_limit_mb: 0`, and a malformed
`task_result` call without task/receipt IDs. The handler silently treated that
resolution as a read, leaving the prose promise pending. The old global blocked
word detector then matched “can't page” in the conceptual explanation and
blocked the whole turn.

A fresh exact-prompt baseline completed with a real receipt, confirming an
intermittent failure. Replaying the recorded turn reproduced the false block.
Separately, an unsupported “I just checked” fixture with no receipts was accepted
by the old gate. These are distinct defects; no missing tool was assumed.

## B. Existing-tool determination

`terminal` was enabled and offered by the real API-server tool context. The
profile uses its local backend, and the recorded calls prove it can read sysctl.
The remediation reuses that tool. There is no new shell subsystem, no new
inspection tool, and no approval-policy relaxation. Recovery permits recognized
read-only operations through the existing dispatcher; unexpected operations are
rejected before dispatch. No kernel setting was changed during testing.

## C. Recovery behavior

```text
Before: raw prose promise → generic continuation → fragile bookkeeping → global block
After: normalized required read → specific tool/arguments → actual receipt
       → affected task resolves → remaining answer parts complete
```

The implementation is in `integrations/newton_runtime/{execution,hermes,
observations}.py`. It binds observations to corresponding commands, subjects and
successful consumed receipts. It supplies targeted recovery instructions and
constrains the offered terminal operation when recovery is needed. Unrelated
receipts, compound commands masking failures and stale receipts cannot close a
normalized inspection.

Three consecutive bookkeeping calls that make no task progress are bounded,
alongside the existing three prose continuations. Rewording an accepted answer
or accumulating housekeeping receipts does not count as progress. Recovery views
include actual observations and task states without recursively echoing accepted
answer bodies inside every bookkeeping payload.

## D. Tool-claim validation

Known inspections remain required independently of the model's wording. Claim
checks additionally reject unsupported observation provenance and mismatched
literal readings. Candidate and interim prose are withheld until validation;
unsupported tool-call preambles are also filtered before persistence. Incorrect
claims trigger recovery or an explicit unverified/incomplete outcome.

The tests cover unsupported past-tense claims, wrong readings despite an existing
receipt, unrelated/unconsumed evidence, table/code-output claims, formatted
numbers, OS-version reads, quoted command boundaries, and conceptual examples
that require no inspection. Semantic limits are stated in section K.

## E. Subtask and partial completion

Mixed requests retain independent answer parts and inspection dependencies.
`task_result.answers` records the actual answer text for each part. A malformed
entry does not erase valid entries. A failed inspection blocks its dependent
recommendation while preserving independent answers. Reopened work requires an
affirmative retry and new evidence; “do not retry” does not reopen it.

A live Flash-Next test exposed repeated resubmission of an already completed
answer. The final implementation closes a fully dispositioned ledger through
Hermes' native final-response branch, without another provider request. This is
an explicit zero-token **ledger closing event**, not a fabricated model inference
or tool receipt. Last bookkeeping results remain unconsumed when no model saw
them. Required external-operation results must already have been consumed.

Partial results preserve useful text with `completed=false`, `partial=true`,
`failed=false`. The existing API non-completion finish marker remains. Pending
work survives frontend boundaries, and resumed answers remain renderable after
completion. The gate is retained.

## F. Exact reproduction results

The unchanged request is tracked in `tests/fixtures/remediation-prompt.txt`.
Its SHA-256 is
`476396ef802fd6cfe504f128df4c99c7378e7a2951b6f2396514f30b44723cca`.
The live ledgers' original request strings matched it exactly.

| Active model | Exact request | Actual local inspection | Result |
| --- | --- | --- | --- |
| Qwen 3.8 27B | Unmodified original prompt | `sysctl iogpu.wired_limit_mb` → `0`; also `sysctl hw.memsize` → `137438953472` | All seven answer parts completed; no global block |
| Qwen 3.8 Flash-Next | Unmodified original prompt | `sysctl iogpu.wired_limit_mb` → `0` | All seven answer parts completed; no global block |

Receipts record execution, outcome and consumption. This validates the raw
inspection and workflow, not every statement in the model-generated explanations.

A supplemental real-Flash test injected a narration reply and then a false
`999999` reading before allowing real inference. Flash recovered with one actual
read, returned `0`, and exposed neither injected claim in streamed/persisted
assistant text. It used three real provider requests after the two injected
replies. This focused fault injection supplements exact-prompt acceptance.

## G. Failure and normal-case results

Both models ran an intentional nonexistent-key inspection once. The command
exited 1, the dependent recommendation remained unresolved, and the independent
read-versus-write explanation was returned as **partial**, not globally blocked
or discarded. Flash-Next also passed this case through streaming.

Both models passed two distinct inspections with separate receipts and retained
their explanatory answers. Both no-tool cases completed in one provider call
with zero tool receipts. The real-loop fake-provider tests also prove bounded
termination for repeated narration and repeated invalid bookkeeping.

## H. Regression totals and commands

- Active unit suite: **76 passed**.
- Existing registry suite: **68 passed**; controller suite: **42 passed**.
- Real Hermes loop with scripted provider: **six scenarios passed**, using only
  harmless real sysctl reads and one intentional missing-key failure.
- Previous real-loop narration and cached-identity regressions: passed.
- Normal live cases: exact, failure, multi-action and no-tool passed on both
  active models; supplemental streaming failure and injected recovery passed.
- Identity: one resumed session passed **27B → Flash-Next → 27B** with three
  verified generations, stable `local-model` routing and `AI Inference` labeling.
- A pristine pinned Hermes source reconstructed the exact installed integration.

Run from the active repository:

```sh
python3 -B -m unittest discover -s tests -q
python3 -B scripts/test-model-registry.py
python3 -B scripts/test-qwenctl-logic.py
~/.hermes/hermes-agent/venv/bin/python3 -B scripts/prove-action-recovery-loop.py
~/.hermes/hermes-agent/venv/bin/python3 -B scripts/prove-hermes-completion-loop.py
python3 -B scripts/prove-action-recovery.py --model qwen27b
python3 -B scripts/prove-action-recovery.py --model flash-next
python3 -B scripts/prove-action-recovery.py --model flash-next --stream --cases failure
# With active Flash-Next already running:
~/.hermes/hermes-agent/venv/bin/python3 -B scripts/prove-live-recovery.py
python3 -B scripts/prove-runtime-identity.py
python3 -B scripts/install-hermes-integration.py --runtime ~/.hermes/hermes-agent --check
git diff --check
```

Interrupted diagnostic runs and the corrected test-fixture formatting error are
retained as diagnostics under `state/`; they are not counted as passes. Raw
responses, receipts and histories remain untracked.

## I. Portable propagation

- SSD: `/Volumes/PRO-G40/Newton_Local`
- HDD: `/Volumes/Newton_Local/Newton_Local`

The HDD's mount name changed outside this commission. Its unchanged volume UUID
`1B6E6BE2-9F26-48ED-85E3-B5834565AE2C` established that it is the authorized HDD.
No physical volume was renamed by this work. Its generated paths were repaired
(38 entries); the follow-up check found zero pending changes. The staging
hardware/date facts in `state/host.yaml` were preserved, with its root corrected
and `needs_bootstrap: true` retained.

Both copies received the identical shared modules, completion/recovery tests,
exact-prompt fixture and shared documentation. The existing installer/bootstrap
already copies all managed modules, so no new dependency or tool wiring was
needed. Their bundled Hermes installations passed the managed-source check.

Each copy passed **66 tests**, registry checks, real tiny-HTTP-stub lifecycle
proofs and the portability audit. Both lifecycle samplers observed a maximum of
one resident stub. Final comparison matched **432 architecture files** and
**200 generated entries** after root normalization. Active managed modules match
both portable source and installed-runtime modules.

Portable commands, from each root with `NEWTON_LOCAL_ROOT` set to that root:

```sh
./services/.venv/bin/python3 -B -m unittest discover -s tests -v
./services/.venv/bin/python3 -B scripts/prove-registry.py
./services/.venv/bin/python3 -B scripts/prove-lifecycle.py
bash scripts/portability-audit.sh
python3 -B scripts/repair-portable-paths.py --check
python3 -B scripts/install-hermes-integration.py --runtime hermes/runtime/hermes-agent --check
```

**No GLM model/server or portable production service was started.** The catalogs
remain intentionally different. Large model payloads were not freshly hashed.

## J. Git and reproducibility

Active branch: `main`; clean starting baseline:
`0ea1deb34bbed1087818dd81457eb0536e8dbced`. The implementation, tests, exact fixture
and proof scripts are project-managed. The pinned Hermes loop patch is unchanged;
the shared modules extend its existing hooks. No essential behavior exists only
as a hand-edit under `~/.hermes`.

The delivery response records the final commit hash and clean status. The report's
commit can also be identified with:

```sh
git log -1 --format='%H %s' -- docs/15-remediation-verification.md
```

No push or external-drive Git operation was performed. Logs, histories, secrets,
runtime state, model weights and external-drive contents are excluded from Git.

## K. Remaining limits

Natural-language obligation extraction, abstract task/receipt relevance and
answer semantics remain model-assisted. The guard checks recorded obligations
and recognized observation claims; it is not a general factual-entailment engine.

The generated explanations still disagree about the implications of `0` and
contain unverified system-tuning procedures and comparative model claims. The
raw reading is verified; those procedures and comparisons are not validated
deployment instructions. No proposed setting changes were executed.

Buffering candidate prose delays visible text streaming. Complex prompts can
also spend substantial time in the model's own inference; the remediation does
not retune model sampling or reasoning settings. Actual GLM behavior and fresh
bootstrap on the future high-memory destination remain untested here.
