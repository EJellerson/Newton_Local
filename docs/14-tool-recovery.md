# Tool recovery and partial answers

The managed Hermes integration retains the existing completion ledger and native
tool loop. It adds operation-specific recovery, observation-claim checks, and
answer subtasks with explicit dependencies. No new shell or inspection tool is
installed: the existing `terminal` tool already runs on the configured host and
can perform read-only sysctl queries. Its manual approvals remain unchanged.

## Diagnosis from the actual incident

In the recorded `api-87df1ac8a413de81` incident, messages 487–494 show:

1. The exact multi-question request created no initial obligations.
2. A narration-only reply created one unnormalized promise.
3. The generic continuation did elicit a real terminal call. Its output included
   `iogpu.wired_limit_mb: 0`; this incident was not a missing-tool/exposure failure.
4. `task_result` was called with `disposition` and `reason`, but no task or receipt
   IDs. The handler silently treated that malformed resolution as a read request.
5. The promise therefore remained pending despite a successful terminal receipt.
6. `Ledger.gate` applied its broad blocked-word regex to the entire final answer.
   The conceptual explanation that macOS “can't page” wired memory marked every
   pending task blocked. `enforce_result` then prepended a global blocked notice.

A fresh baseline run of the exact prompt completed with an actual terminal
receipt; the failure is intermittent. Replaying the stored incident through the
old ledger deterministically reproduced the false blocked result. Separately,
the old ledger accepted a fabricated “I just checked” response with no receipts.
Both are regression cases; neither is inferred merely from visible chat prose.

## Operation and evidence binding

`observations.py` normalizes identifiable local sysctl questions and recognizes a
small grammar of simple read-only commands. It executes nothing. A normalized
observation records its operation, expected subject and minimum receipt ID.
Explicit terminal commands and existing Todo obligations still use the ledger.

Receipts retain requested/executed/result/success/failure/consumed distinctions.
Matching simple observations resolve automatically after the model receives
their result; omitting bookkeeping IDs cannot strand an otherwise verified read.
Compound shell lists cannot prove each command's result from the final exit
status, so they cannot resolve normalized observations. Recovery asks for
separate reads. Unknown, unrelated, failed or unconsumed receipts cannot verify
a claim. Mutating tools and bookkeeping calls are not observation evidence.

Current-value claims are checked against the corresponding scalar receipt,
including table values and sysctl output shown in code fences. Shell-setting
examples are distinguished from current observation output. Named file/web
claims require a corresponding read target. General semantic relevance remains
limited by natural-language interpretation; this is not an entailment verifier
for arbitrary assertions or retrieved documents.

## Recovery and delivery

The first missing-action response receives the actual task ID, operation, tool
name, arguments and evidence deficit. Subsequent recovery requests for recognized
read-only operations constrain the existing terminal schema and tool choice to
those operations. The normal tool dispatcher still executes the model's tool
call; the harness never evaluates model-generated shell directly. Unexpected
operations during constrained recovery fail closed before dispatch.

The bounded continuation budget remains three. A result awaiting consumption
does not force a duplicate read. Failed reads are retained as failed; retry
requires explicit user intent and a new receipt. A malformed `task_result`
resolution returns an actionable error instead of silently succeeding as a read.
Three consecutive bookkeeping calls that change no task disposition also exhaust
recovery. Rewording an accepted answer does not reset that budget. Remaining
parts stay durable and return partial/incomplete rather than looping indefinitely.
Recovery views expose actual action receipts and observed values; accepted answer
prose and recursive bookkeeping payloads stay in storage instead of repeatedly
expanding the model context.

For gated profiles, candidate prose is buffered until validation. Unsupported
claims are replaced before candidate persistence; tool-call preambles are also
checked before persistence. Interim prose callbacks are withheld. The validated
final or truthful partial result is delivered once. This delays visible text
streaming but requires no extra model call for ordinary no-tool answers.

## Independent and dependent answers

Mixed inspection requests are split into small answer obligations. Answers are
supplied through `task_result.answers`, using existing tool-loop infrastructure.
The model supplies their semantics; the ledger checks IDs, dependencies and
observation provenance. Machine-specific recommendations depend on the required
inspection. A failed dependency prevents accepting a confident answer for that
branch, while completed independent answers remain persisted and returnable.

An actual failed read is not inferred from words such as “can't” in unrelated
explanations. On partial completion the API returns useful answer text with
`completed=false`, `partial=true`, `failed=false`; the existing API transport's
non-completion finish marker is retained. A pure blocked action still reports
blocked. Old terminal task states do not poison unrelated later turns, while
pending work survives fresh frontend requests and resumes.
Resumed answer parts remain visible when completed; completion does not drop
them from the current response merely because they originated in an earlier turn.

Once all known answer parts and operations have a terminal disposition, a local
ledger closing event enters Hermes' existing final-response branch. It reuses
the native final-answer grace pass when needed, performs no further provider
call, and preserves normal persistence/API finalization. Its source is recorded
as `ledger`, with zero inference tokens. It is not a tool receipt or a claimed
model inference. Unseen bookkeeping results remain explicitly unconsumed;
required external-operation results must already have been consumed. Additional
promised actions in bookkeeping preambles prevent premature closure.

This boundary was required by a live Flash-Next failure test: the model repeatedly
resubmitted an already accepted independent answer even though the failed
inspection and dependent question were correctly isolated. A final-text-only
gate could not stop a loop consisting entirely of bookkeeping calls.

## Reproduction and tests

```sh
python3 -B -m unittest discover -s tests -v
python3 -B scripts/install-hermes-integration.py --runtime "$HOME/.hermes/hermes-agent"
"$HOME/.hermes/hermes-agent/venv/bin/python3" -B scripts/prove-action-recovery-loop.py
"$HOME/.hermes/hermes-agent/venv/bin/python3" -B scripts/prove-hermes-completion-loop.py
python3 -B scripts/prove-action-recovery.py --model qwen27b
python3 -B scripts/prove-action-recovery.py --model flash-next
python3 -B scripts/prove-action-recovery.py --model flash-next --stream --cases failure
python3 -B scripts/prove-runtime-identity.py
```

The live proof reads the unchanged original request from
`tests/fixtures/remediation-prompt.txt`. The scripted-provider proof uses the real
Hermes loop and terminal tool, including harmless actual sysctl reads and one
nonexistent-key failure; no inference model is loaded by that script.
Live scripts are active-Mac-Pro-only and restore selection with the model stopped.
Raw responses, receipts and histories remain outside Git in `state/` and the
Hermes profile. Portable validation must remain model-free; actual GLM behavior
still requires sufficiently capable destination hardware.

With active Flash-Next already running, this supplemental proof injects two bad
text-only replies into a test agent, then uses real Flash-Next inference and the
real terminal tool to recover. It does not replace exact-prompt acceptance:

```sh
"$HOME/.hermes/hermes-agent/venv/bin/python3" -B scripts/prove-live-recovery.py
```
