# Agent completion enforcement

The opt-in `newton_local.completion_gate` policy extends the pinned Hermes loop.
It does not add a planner or another model. Shared source is in
`integrations/newton_runtime/execution.py` and `hermes.py`; the exact loop hooks
are in `integrations/hermes-completion.patch`.

## State and termination

Known user actions, detected commitments, and native Todo items enter a durable
per-session ledger under the profile's `state/newton-execution.db`. Reconstructing
only user/assistant text at a frontend boundary cannot delete this state.
One process-held lock serializes turns for the same session. Different sessions
have independent ledgers. Background memory reviews do not change the user's
completion ledger.

The loop records requested calls before dispatch and results afterward. Receipts
distinguish request, execution, returned result, success/failure/unknown outcome,
and delivery to a subsequent model pass. An approval block is not execution.
Ledger I/O failure halts outside the provider retry loop, including failures
after dispatch, so an accounting error cannot automatically repeat an action.
"Consumed" means a model pass received the result, not proof of the model's
internal understanding. Failed receipts remain failed after successful retries.

An exact requested command can resolve through its own successful, consumed
receipt. Otherwise the `task_result` tool records an explicit association between
task IDs and successful receipts, or an explicit blocker. Native Todo completion
alone cannot manufacture this evidence. Deleting Todo items does not delete known
ledger obligations. A model-created commitment may be explicitly dispositioned
as unnecessary; explicit user actions cannot be dismissed that way.

At a candidate final response the gate checks the ledger and commitment clauses,
including clauses after a long partial answer. Pending work reuses Hermes'
continuation loop, bounded by `completion_retries` (default 3). Exhaustion and
upstream early/budget exits cannot return successful completion with known
unfinished work. API metadata reports non-completion; a visible remaining-work
notice accompanies streaming failures. Completed, blocked, failed, cancelled,
incomplete and continuing are separate outcomes.

Plain final answers without known work do not incur a continuation or a separate
completion-model call. Natural-language obligation extraction and whether a
receipt substantively satisfies an abstract task remain fallible. The structural
guarantee is about recorded/detected work and real execution evidence, not a claim
to perfectly infer every implicit task or verify every natural-language answer.

## Proofs

```sh
python3 -B -m unittest discover -s tests -v
python3 -B scripts/prove-agent-completion.py --model qwen27b
python3 -B scripts/prove-agent-completion.py --model flash-next
python3 -B scripts/prove-agent-completion.py --model flash-next --stream --cases DEF
~/.hermes/hermes-agent/venv/bin/python -B scripts/prove-hermes-completion-loop.py
```

Unit cases cover all four September 15 failure shapes, post-tool commitments,
failed/missing/unconsumed results, reused tool-call IDs, frontend/resume state,
Todo deletion, explicit receipt resolution, retry exhaustion, approval blocks,
and normal final answers. Live proofs use harmless terminal checks, deliberate
`false` failure, no-tool final answers, and generic task resolution. The live
scripts are for the active Mac Pro only and restore the original selection with
the model stopped. Proof records under `state/` are intentionally untracked.
