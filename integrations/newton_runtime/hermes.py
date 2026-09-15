"""Opt-in Hermes integration for a verified, replaceable local inference slot.

The tracked Hermes patch calls these narrow hooks. Other profiles are unchanged.
"""
from __future__ import annotations

import functools
import json
import logging
import re
from pathlib import Path

from .identity import IdentityError, http_json

LOG = logging.getLogger(__name__)
START = "<verified_resident_identity>"
END = "</verified_resident_identity>"


def configuration(agent):
    from agent.system_prompt import _agent_home
    import yaml
    home = Path(_agent_home(agent))
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
    if policy and fetch_identity(policy)["generation"] != agent._newton_identity["generation"]:
        raise IdentityError("resident changed during this turn; resume with refreshed identity")


def failure(reason, session_id=None):
    return {"final_response": "Inference unavailable: " + str(reason), "completed": False,
            "failed": True, "partial": False, "error": str(reason),
            "execution_status": "failed", "session_id": session_id, "messages": []}


def guard_turn(fn):
    @functools.wraps(fn)
    def wrapped(agent, *args, **kwargs):
        home, policy = configuration(agent)
        if not policy:
            return fn(agent, *args, **kwargs)
        try:
            if agent.model != "local-model":
                raise IdentityError("this profile must route through local-model")
            identity = fetch_identity(policy)
            agent._newton_identity, agent._newton_policy = identity, policy
            agent._tool_use_enforcement = True
            agent._execution_guidance = True
            agent._model_supports_vision = lambda: identity["capabilities"]["vision"]
            agent._config_context_length = identity["effective_context"]
            if getattr(agent, "context_compressor", None):
                agent.context_compressor.context_length = identity["effective_context"]
            LOG.info("Resident bound: session=%s model=%s backend=%s generation=%s",
                     agent.session_id, identity["registry_id"], identity["backend"], identity["generation"])
            result = fn(agent, *args, **kwargs)
            check_generation(agent)
            result["resident_identity"] = identity
            return result
        except IdentityError as exc:
            return failure(exc, getattr(agent, "session_id", None))
    return wrapped
