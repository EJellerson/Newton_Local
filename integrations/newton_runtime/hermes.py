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
from pathlib import Path

from .identity import IdentityError, http_json
from .execution import Ledger, promised_work

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
        "give the user their requested results, without task IDs, receipt IDs, or ledger commentary.",
    "parameters": {"type": "object", "properties": {
        "task_ids": {"type": "array", "items": {"type": "string"}},
        "disposition": {"type": "string", "enum": ["completed", "blocked", "not_required"]},
        "receipt_ids": {"type": "array", "items": {"type": "integer"}},
        "reason": {"type": "string"}}, "required": []}}


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
        return text
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
        if not args.get("task_ids"):
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
        ledger.consume()


@durable
def before_tools(agent, assistant_msg, task_id):
    ledger = getattr(agent, "_newton_execution", None)
    if ledger:
        _ACTIVE[task_id] = ledger
        agent._newton_receipt_batch = ledger.requested(assistant_msg.get("tool_calls") or [])


@durable
def after_tools(agent, messages, start):
    ledger = getattr(agent, "_newton_execution", None)
    if ledger:
        ledger.results(agent._newton_receipt_batch, messages[start:])
        if getattr(agent, "_todo_store", None):
            ledger.sync_todos(agent._todo_store.read())


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
    for promise in promised_work(result.get("final_response") or ""):
        ledger.add(promise, "promise", minimum=len(ledger.data["receipts"]) + 1)
    status = ledger.data["status"]
    if ledger.unresolved() and status in ("completed", "continuing"):
        status = "incomplete"
    if result.get("interrupted"):
        status = "cancelled"
    elif result.get("failed") and status == "completed":
        status = "failed"
    elif (result.get("partial") or result.get("completed") is False) and status == "completed":
        status = "incomplete"
    ledger.data["status"] = status
    ledger.save()
    result["execution_status"] = status
    if status != "completed":
        remaining = [t for t in ledger.data["tasks"].values()
                     if t["status"] != "completed" and t["source"] != "tool"]
        detail = "; ".join(t["description"] for t in remaining)[:3000]
        note = status.capitalize() + (" — remaining work: " + detail if detail else "")
        result.update(completed=False, failed=status == "failed", partial=status == "incomplete", error=note)
        result["final_response"] = note + "\n\n" + (result.get("final_response") or "")
    return result


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
        request = kwargs.get("user_message", args[0] if args else "")
        agent._newton_identity_question = identity_question(request)
        if agent._newton_identity_question:
            # Keep an unvalidated self-identification out of streamed prose.
            # The canonical answer is emitted once, after final verification.
            agent.stream_delta_callback = None
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
            result = fn(agent, *args, **kwargs)
            check_generation(agent)
            result["resident_identity"] = identity
            if ledger:
                result = enforce_result(ledger, result)
                if result["execution_status"] != "completed" and getattr(agent, "stream_delta_callback", None):
                    agent.stream_delta_callback("\n\n" + result["error"])
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
            if agent._newton_identity_question:
                agent.stream_delta_callback = original_delta
                if original_delta and result:
                    original_delta(result.get("final_response") or result.get("error", ""))
    return wrapped
