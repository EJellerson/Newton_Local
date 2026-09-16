"""Opt-in Hermes integration for a verified, replaceable local inference slot.

The tracked Hermes patch calls these narrow hooks. Other profiles are unchanged.
"""
from __future__ import annotations

import functools
import json
import logging
import re
import fcntl
import hashlib
import threading
import copy
from pathlib import Path
from types import SimpleNamespace

from .identity import IdentityError, http_json
from .execution import Ledger, promised_work, HOUSEKEEPING, ACTION
from . import observations as obs

LOG = logging.getLogger(__name__)
START = "<verified_resident_identity>"
END = "</verified_resident_identity>"
_ACTIVE = {}
_REGISTER_LOCK = threading.Lock()
_REGISTERED = False


class CompletionHalt(BaseException):
    """Bypass the upstream retry-on-Exception loop after ledger I/O failure."""


def durable(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            raise CompletionHalt("Execution evidence could not be persisted; automatic retry stopped: " + str(exc)) from exc
    return wrapped

TASK_SCHEMA = {
    "name": "task_result",
    "description": "Read persistent required tasks and actual tool receipts (no arguments). "
        "After doing the work, resolve task_ids as completed with successful, consumed receipt_ids "
        "and a reason explaining how the evidence satisfies the task. A tool call alone is not proof. "
        "Use blocked with a concrete reason when work cannot proceed. not_required is only for "
        "an unnecessary action YOU promised, never an explicit user request. Do not rerun a successful "
        "action just to obtain another receipt; reuse its receipt ID. This is internal bookkeeping: "
        "For answer subtasks use answers:[{task_id,text}], supplying the actual answer for each question. "
        "The harness checks dependencies and returns these answers to the user. "
        "Give the user their requested results, without task IDs, receipt IDs, or ledger commentary.",
    "parameters": {"type": "object", "properties": {
        "task_ids": {"type": "array", "items": {"type": "string"}},
        "disposition": {"type": "string", "enum": ["completed", "blocked", "not_required"]},
        "receipt_ids": {"type": "array", "items": {"type": "integer"}},
        "reason": {"type": "string"},
        "answers": {"type": "array", "items": {"type": "object", "properties": {
            "task_id": {"type": "string"}, "text": {"type": "string"}}, "required": ["task_id", "text"]}}}, "required": []}}


def identity_question(request):
    from .execution import text_content
    text = text_content(request).lower()
    if text.count("?") > 1 or re.search(
        r"\b(?:run|execute|inspect|search|download|install|write|create|compare|benchmark|rank|why|how|directory|folder|capable|strengths|weaknesses|explain|definition|performance|other|another|remote|studio)\b|what (?:time|date)", text
    ):
        return False
    if "verified resident identity block" in text:
        return True
    return bool(re.search(r"\b(?:which|what|name|identify|tell me)\b", text) and re.search(
        r"\b(?:(?:resident|loaded) (?:model|weights)|your (?:current )?(?:model|backend)|"
        r"(?:model|backend|weights)\b.{0,60}(?:are you|you (?:are|use|run)|powers you|serving this (?:chat|session)))", text))


def canonical_response(agent, text):
    """Identity-only answers are facts rendered by the harness, not model guesses."""
    if not getattr(agent, "_newton_identity_question", False):
        ledger = getattr(agent, "_newton_execution", None)
        return ledger.prepare_candidate(text) if ledger else text
    identity = agent._newton_identity
    return (f"AI Inference is serving {identity['display_name']} ({identity['registry_id']}).\n"
            f"Backend: {identity['backend']}. Context: {identity['effective_context']:,} tokens. "
            f"Vision: {'yes' if identity['capabilities']['vision'] else 'no'}.\n"
            f"Artifact: {Path(identity['artifact_path']).name}.\n"
            f"Routing alias: local-model. Resident generation: {identity['generation'][:12]}.")


def task_result(args, **kwargs):
    ledger = _ACTIVE.get(kwargs.get("task_id"))
    if ledger is None:
        return json.dumps({"error": "No active local task ledger"})
    with ledger.lock:
        if args.get("answers"):
            return json.dumps(ledger.answer(args["answers"]))
        if not args.get("task_ids"):
            if args.get("disposition"):
                return json.dumps({"error": "Disposition requires task_ids and completed requires receipt_ids. "
                                           "No task was changed. Read the exact IDs below.", **ledger.view()})
            return json.dumps(ledger.view())
        return json.dumps(ledger.resolve(args["task_ids"], args.get("disposition"),
                                         args.get("receipt_ids", []), args.get("reason")))


def register_task_tool(agent):
    global _REGISTERED
    with _REGISTER_LOCK:
        if not _REGISTERED:
            from tools.registry import registry
            registry.register(name="task_result", toolset="todo", schema=TASK_SCHEMA,
                              handler=task_result,
                              check_fn=lambda: bool(configuration(None)[1].get("completion_gate")), emoji="📋")
            _REGISTERED = True
    if "task_result" not in agent.valid_tool_names:
        agent.tools.append({"type": "function", "function": TASK_SCHEMA})
        agent.valid_tool_names.add("task_result")


@durable
def after_response(agent):
    ledger = getattr(agent, "_newton_execution", None)
    if ledger:
        if getattr(agent, "_newton_ledger_final", False):
            # No model saw the last bookkeeping result. Required operation
            # results were already consumed before the answers were accepted.
            for receipt in ledger.data["receipts"]:
                if receipt["tool"] in HOUSEKEEPING and not receipt["result_consumed"]:
                    receipt["consumption_not_required"] = True
            ledger.save()
            return
        ledger.consume()


@durable
def before_tools(agent, assistant_msg, task_id):
    ledger = getattr(agent, "_newton_execution", None)
    if ledger:
        calls = assistant_msg.get("tool_calls") or []
        if calls and all(c.get("function", {}).get("name") in HOUSEKEEPING for c in calls):
            for promise in promised_work(assistant_msg.get("content") or ""):
                if ACTION.search(promise):
                    ledger.record_promises(promise)
        allowed = getattr(agent, "_newton_forced_commands", None)
        if allowed:
            for call in assistant_msg.get("tool_calls") or []:
                fn = call.get("function", {})
                args = fn.get("arguments") or {}
                args = json.loads(args) if isinstance(args, str) else args
                if fn.get("name") != "terminal" or args.get("command") not in allowed or set(args) != {"command"}:
                    raise CompletionHalt("Constrained read-only recovery produced an unexpected tool operation; nothing was dispatched")
        _ACTIVE[task_id] = ledger
        agent._newton_progress_before = ledger.progress_signature()
        agent._newton_receipt_batch = ledger.requested(assistant_msg.get("tool_calls") or [])


@durable
def after_tools(agent, messages, start):
    ledger = getattr(agent, "_newton_execution", None)
    if ledger:
        ledger.results(agent._newton_receipt_batch, messages[start:])
        if getattr(agent, "_todo_store", None):
            ledger.sync_todos(agent._todo_store.read())
        batch = [ledger.data["receipts"][rid - 1] for rid in agent._newton_receipt_batch]
        if any(r["tool"] == "task_result" for r in batch):
            before = getattr(agent, "_newton_progress_before", None)
            ledger.data["bookkeeping_stalls"] = (ledger.data.get("bookkeeping_stalls", 0) + 1
                if before == ledger.progress_signature() else 0)
            ledger.save()
        budget = int(getattr(agent, "_newton_policy", {}).get("completion_retries", 3))
        stalled = ledger.data.get("bookkeeping_stalls", 0)
        if ledger.finalizable() or (stalled and stalled >= budget):
            # Reuse Hermes' existing final-answer grace pass even if the last
            # actual provider call exhausted the configured iteration budget.
            agent._budget_grace_call = True


@durable
def completion_gate(agent, text, messages):
    ledger = getattr(agent, "_newton_execution", None)
    if not ledger:
        return "completed", text
    status, message = ledger.gate(text, int(agent._newton_policy.get("completion_retries", 3)))
    LOG.info("Completion gate: session=%s status=%s pending=%d receipts=%d retry=%d",
             agent.session_id, status, len(ledger.unresolved()), len(ledger.data["receipts"]), ledger.retries)
    return status, message


def enforce_result(ledger, result):
    # Cover upstream early/fallback exits that bypass the normal text branch.
    safe = ledger.prepare_candidate(result.get("final_response") or "")
    result["final_response"] = safe
    for promise in promised_work(safe):
        ledger.add(promise, "promise", minimum=len(ledger.data["receipts"]) + 1)
    status = ledger.data["status"]
    if (ledger.unresolved() or ledger.data.get("last_rejection")) and status in ("completed", "continuing"):
        status = "incomplete"
    if result.get("interrupted"):
        status = "cancelled"
    elif result.get("failed") and status == "completed":
        status = "failed"
    elif (result.get("partial") or result.get("completed") is False) and status == "completed":
        status = "incomplete"
    if status in ("incomplete", "blocked") and ledger.render_answers():
        status = "partial"
    ledger.data["status"] = status
    ledger.save()
    result["execution_status"] = status
    result["subtasks"] = ledger.current_tasks()
    if status != "completed":
        remaining = [t for t in ledger.current_tasks()
                     if t["status"] != "completed" and t["source"] != "tool"]
        detail = "; ".join(t["description"] for t in remaining)[:3000]
        note = ("Partial completion" if status == "partial" else status.capitalize()) + (" — remaining work: " + detail if detail else "")
        result.update(completed=False, failed=status == "failed", partial=status in ("incomplete", "partial"), error=note)
        if not safe.startswith(("Partial completion", "Blocked", "Incomplete", "Failed", "Cancelled")):
            result["final_response"] = note + "\n\n" + safe
    return result


def recovery_request(agent, build):
    """Refine the existing provider request, never synthesize/execute a tool call."""
    def wrapped(*args, **kwargs):
        request = build(*args, **kwargs)
        ledger = agent._newton_execution
        agent._newton_forced_commands = None
        if not ledger:
            return request
        tasks = ledger.current_tasks()
        stalled = ledger.data.get("bookkeeping_stalls", 0)
        if stalled or any(t.get("kind") in ("observation", "answer") for t in tasks):
            notice = "Bookkeeping made no progress. Correct the remaining parts using the current tool-result errors, or finish directly.\n" if stalled else ""
            request["messages"] = list(request["messages"]) + [{"role": "user", "content": notice + ledger.context()}]
        missing = [t for t in ledger.unresolved() if t.get("kind") == "observation" and obs.read_operation(t.get("required_command") or "")
                   and not any(r.get("result_received") and not r["result_consumed"] and r["id"] >= t["minimum_receipt"]
                               and r.get("observation", {}).get("command") == obs.read_operation(t["required_command"])["command"]
                               for r in ledger.data["receipts"])]
        terminal = next((t for t in request.get("tools", []) if t.get("function", {}).get("name") == "terminal"), None)
        if (ledger.retries or stalled) and missing and terminal:
            commands = list(dict.fromkeys(t["required_command"] for t in missing))
            terminal = copy.deepcopy(terminal)
            terminal["function"]["parameters"] = {"type": "object", "properties": {
                "command": {"type": "string", "enum": commands}}, "required": ["command"], "additionalProperties": False}
            request["tools"] = [terminal]
            request["tool_choice"] = {"type": "function", "function": {"name": "terminal"}}
            agent._newton_forced_commands = commands
            LOG.info("Targeted recovery: session=%s retry=%d commands=%s", agent.session_id, ledger.retries, commands)
        return request
    return wrapped


def verified_message(agent, build):
    """Do not persist an unsupported tool-call preamble as an observation."""
    def wrapped(*args, **kwargs):
        message = build(*args, **kwargs)
        ledger = agent._newton_execution
        if ledger and isinstance(message.get("content"), str) and obs.claim_error(
                message["content"], ledger.observations(), ledger.requested_subjects()):
            message["content"] = "An observation claim was withheld pending corresponding tool evidence."
        return message
    return wrapped


@durable
def ledger_response(agent, reason="all known parts dispositioned"):
    """A local closing event, not an inference result or a fabricated receipt.

    Feeding accepted answer parts through the native final-response branch
    preserves Hermes' persistence, accounting and API finalization machinery.
    """
    ledger = agent._newton_execution
    ledger.data["final_response_source"] = "ledger"
    ledger.data["finalization_reason"] = reason
    ledger.save()
    agent._newton_ledger_final = True
    LOG.info("Ledger finalization: session=%s; %s; provider call skipped", agent.session_id, reason)
    return SimpleNamespace(id="newton-ledger-final", model=agent.model,
        choices=[SimpleNamespace(index=0, finish_reason="stop", message=SimpleNamespace(
            role="assistant", content=ledger.render_answers() or "Some requested work could not be completed.",
            tool_calls=None, reasoning_content=None))],
        usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0))


def final_dispatch(agent, dispatch):
    def wrapped(*args, **kwargs):
        ledger = getattr(agent, "_newton_execution", None)
        if ledger and ledger.finalizable():
            return ledger_response(agent)
        if ledger:
            budget = int(getattr(agent, "_newton_policy", {}).get("completion_retries", 3))
            stalled = ledger.data.get("bookkeeping_stalls", 0)
            if stalled and stalled >= budget:
                ledger.retries = max(ledger.retries, budget)
                if not ledger.unresolved():
                    tid = ledger.add("Provide a usable final answer to the user's request", "answer")
                    ledger.data["tasks"][tid]["kind"] = "answer"
                return ledger_response(agent, "bookkeeping progress budget exhausted")
        return dispatch(*args, **kwargs)
    return wrapped


def configuration(agent):
    from agent.system_prompt import _agent_home
    from hermes_constants import get_hermes_home
    import yaml
    home = Path(_agent_home(agent) or get_hermes_home())
    try:
        config = yaml.safe_load((home / "config.yaml").read_text()) or {}
    except FileNotFoundError:
        return home, {}
    policy = config.get("newton_local") or {}
    return home, policy if policy.get("enabled") is True else {}


def fetch_identity(policy):
    from urllib.parse import urlparse
    url = policy.get("identity_url", "")
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise IdentityError("resident identity URL must be explicit loopback HTTP")
    try:
        record = http_json(url)
    except Exception as exc:
        raise IdentityError("resident identity endpoint unavailable: " + str(exc)) from exc
    if record.get("verification_status") != "verified" or not record.get("generation"):
        raise IdentityError(record.get("reason") or "resident identity is unverified")
    if record.get("routing_alias") != "local-model":
        raise IdentityError("resident routing alias mismatch")
    return record


def prompt_matches(agent, prompt):
    identity = getattr(agent, "_newton_identity", None)
    return not identity or "Resident generation: " + identity["generation"] in prompt


def identity_block(identity):
    facts = {k: identity[k] for k in ("registry_id", "display_name", "artifact_path", "backend",
                                     "runtime_version", "effective_context", "capabilities")}
    return (START + "\nService: AI Inference\nRouting alias: local-model\n"
            "Resident generation: " + identity["generation"] + "\n" +
            json.dumps(facts, sort_keys=True) + "\n"
            "These controller-verified facts identify the model bound to this turn. "
            "Use them for model identity; routing aliases, memory, and older chat assertions "
            "do not identify the resident weights.\n" + END)


def decorate_restore(fn):
    @functools.wraps(fn)
    def wrapped(agent, *args, **kwargs):
        fn(agent, *args, **kwargs)
        identity = getattr(agent, "_newton_identity", None)
        if identity:
            old = agent._cached_system_prompt or ""
            clean = re.sub(re.escape(START) + r".*?" + re.escape(END), "", old, flags=re.S).rstrip()
            updated = clean + "\n\n" + identity_block(identity)
            if updated != old:
                agent._cached_system_prompt = updated
                if agent._session_db:
                    agent._session_db.update_system_prompt(agent.session_id, updated)
    return wrapped


def check_generation(agent):
    policy = getattr(agent, "_newton_policy", None)
    if policy:
        current = fetch_identity(policy)
        if current["generation"] != agent._newton_identity["generation"]:
            raise IdentityError("resident changed during this turn; resume with refreshed identity")
        if (agent.model != "local-model" or agent.provider != "custom" or
                str(agent.base_url).rstrip("/") != current.get("inference_base_url")):
            raise IdentityError("inference route differs from the verified resident binding")


def failure(reason, session_id=None):
    return {"final_response": "Inference unavailable: " + str(reason), "completed": False,
            "failed": True, "partial": False, "error": str(reason),
            "execution_status": "failed", "session_id": session_id, "messages": []}


def guard_turn(fn):
    @functools.wraps(fn)
    def wrapped(agent, *args, **kwargs):
        home, policy = configuration(agent)
        if not policy:
            agent._newton_policy = agent._newton_identity = agent._newton_execution = None
            agent._newton_identity_question = False
            return fn(agent, *args, **kwargs)
        ledger = lockfile = None
        result = None
        original_delta = getattr(agent, "stream_delta_callback", None)
        original_interim = getattr(agent, "interim_assistant_callback", None)
        original_build = getattr(agent, "_build_api_kwargs", None)
        original_message = getattr(agent, "_build_assistant_message", None)
        original_calls = {name: getattr(agent, name) for name in (
            "_interruptible_streaming_api_call", "_interruptible_api_call") if hasattr(agent, name)}
        agent._newton_ledger_final = False
        buffered = policy.get("completion_gate") is True
        request = kwargs.get("user_message", args[0] if args else "")
        agent._newton_identity_question = identity_question(request)
        if agent._newton_identity_question or buffered:
            # Keep an unvalidated self-identification out of streamed prose.
            # The canonical answer is emitted once, after final verification.
            agent.stream_delta_callback = None
            agent.interim_assistant_callback = None
        try:
            if agent.model != "local-model":
                raise IdentityError("this profile must route through local-model")
            identity = fetch_identity(policy)
            agent._newton_identity, agent._newton_policy = identity, policy
            check_generation(agent)
            cached = getattr(agent, "_cached_system_prompt", None)
            if cached and not prompt_matches(agent, cached):
                agent._cached_system_prompt = None
                agent._cached_system_prompt_static = None
            agent._tool_use_enforcement = True
            agent._execution_guidance = True
            agent._model_supports_vision = lambda: identity["capabilities"]["vision"]
            agent._config_context_length = identity["effective_context"]
            if getattr(agent, "context_compressor", None):
                agent.context_compressor.context_length = identity["effective_context"]
            LOG.info("Resident bound: session=%s model=%s backend=%s generation=%s",
                     agent.session_id, identity["registry_id"], identity["backend"], identity["generation"])
            if policy.get("completion_gate") is True and not threading.current_thread().name.startswith("bg-review"):
                state_dir = home / "state"
                state_dir.mkdir(parents=True, exist_ok=True)
                key = hashlib.sha256(str(agent.session_id).encode()).hexdigest()
                lockfile = (state_dir / ("execution-" + key + ".lock")).open("a+")
                try:
                    fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise IdentityError("another turn is already executing in this session") from exc
                ledger = Ledger(state_dir / "newton-execution.db", agent.session_id)
                ledger.begin(request)
                agent._newton_execution = ledger
                register_task_tool(agent)
                if original_build:
                    agent._build_api_kwargs = recovery_request(agent, original_build)
                if original_message:
                    agent._build_assistant_message = verified_message(agent, original_message)
                for name, dispatch in original_calls.items():
                    setattr(agent, name, final_dispatch(agent, dispatch))
            result = fn(agent, *args, **kwargs)
            check_generation(agent)
            result["resident_identity"] = identity
            if ledger:
                result = enforce_result(ledger, result)
            return result
        except (IdentityError, CompletionHalt) as exc:
            result = failure(exc, getattr(agent, "session_id", None))
            if ledger:
                ledger.data["status"] = "failed"
                try:
                    result = enforce_result(ledger, result)
                except Exception:
                    pass  # storage is unavailable; preserve the explicit failed result
            return result
        finally:
            if ledger:
                for key, value in list(_ACTIVE.items()):
                    if value is ledger:
                        _ACTIVE.pop(key, None)
                ledger.close()
                agent._newton_execution = None
            if lockfile:
                lockfile.close()
            if original_build:
                agent._build_api_kwargs = original_build
            if original_message:
                agent._build_assistant_message = original_message
            for name, dispatch in original_calls.items():
                setattr(agent, name, dispatch)
            agent._newton_ledger_final = False
            agent._newton_forced_commands = None
            if agent._newton_identity_question or buffered:
                agent.stream_delta_callback = original_delta
                agent.interim_assistant_callback = original_interim
                if original_delta and result:
                    original_delta(result.get("final_response") or result.get("error", ""))
    return wrapped
