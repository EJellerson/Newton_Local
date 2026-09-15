"""Verify residency from process/socket evidence and backend-specific metadata.

No selection file or catalog alias can independently establish residency.
This module is shared unchanged with the portable deployment and Hermes.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import urllib.request
from pathlib import Path

SCHEMA = 1
ROUTING_ALIAS = "local-model"
DISPLAY_NAME = "AI Inference"


class IdentityError(RuntimeError):
    pass


def http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read(1 << 20))


def process_snapshot(pid):
    def ps(field):
        result = subprocess.run(["ps", "-p", str(pid), "-o", field + "="],
                                capture_output=True, text=True, timeout=5)
        if result.returncode or not result.stdout.strip():
            raise IdentityError("serving process disappeared")
        return result.stdout.strip()
    return {"pid": int(pid), "started": ps("lstart"), "argv": shlex.split(ps("command"))}


def socket_pid(port):
    result = subprocess.run(["lsof", "-nP", "-iTCP:" + str(port),
                             "-sTCP:LISTEN", "-Fp"], capture_output=True,
                            text=True, timeout=5)
    pids = {int(line[1:]) for line in result.stdout.splitlines() if line.startswith("p")}
    if len(pids) != 1:
        raise IdentityError("inference socket must have exactly one owner")
    return pids.pop()


def argument(argv, name):
    values = [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == name]
    if len(values) != 1:
        raise IdentityError("missing or ambiguous process argument " + name)
    return values[0]


def canonical(path):
    return os.path.realpath(os.path.expanduser(str(path)))


def verify(model, process, owner_pid, metadata, *, slot="production"):
    """Pure verification. ``model`` is a normalized registry/launch spec.

    MLX-LM's normal /health and /v1/models are insufficient; its small
    project launcher exposes a loaded-provider record through /health.
    """
    argv = process.get("argv") or []
    if not process.get("started") or process.get("pid") != owner_pid:
        raise IdentityError("process start identity/socket ownership mismatch")
    if canonical(argument(argv, "--model")) != canonical(model["artifact_path"]):
        raise IdentityError("process artifact differs from registry")
    if int(argument(argv, "--port")) != int(model["port"]):
        raise IdentityError("process endpoint differs from registry")
    if argument(argv, "--host") != "127.0.0.1":
        raise IdentityError("inference process is not bound to loopback")
    expected_command = model["command"]
    if canonical(argv[0]) != canonical(expected_command[0]):
        raise IdentityError("unexpected serving executable")
    if argv[1:len(expected_command)] != expected_command[1:]:
        raise IdentityError("unexpected serving module")
    backend = model["backend"]
    health = metadata.get("health") or {}
    props = metadata.get("props") or {}
    if health.get("status") not in ("ok", "healthy"):
        raise IdentityError("backend is not ready")
    if backend == "llama.cpp":
        if canonical(props.get("model_path", "")) != canonical(model["artifact_path"]):
            raise IdentityError("llama.cpp properties disagree with process artifact")
        context = (props.get("default_generation_settings") or {}).get("n_ctx")
        vision = (props.get("modalities") or {}).get("vision") is True
        version = props.get("build_info")
    elif backend == "mlx-serve":
        rows = (metadata.get("models") or {}).get("data") or []
        rows = [r for r in rows if r.get("loaded") is True and r.get("state") == "ready"
                and r.get("owned_by") == "mlx-serve"
                and r.get("id") == Path(model["artifact_path"]).name]
        if len(rows) != 1:
            raise IdentityError("mlx-serve has no unique matching ready resident")
        row = rows[0]
        context = row.get("context_length")
        if context != (props.get("default_generation_settings") or {}).get("n_ctx"):
            raise IdentityError("mlx-serve context metadata disagrees")
        vision = "vision" in (row.get("capabilities") or [])
        version = model.get("runtime_version")
    elif backend in ("mlx_vlm", "mlx_lm"):
        if canonical(health.get("loaded_model", "")) != canonical(model["artifact_path"]):
            raise IdentityError("MLX loaded-model metadata disagrees with process")
        if health.get("load_failed") or health.get("loading"):
            raise IdentityError("MLX provider has not finished loading")
        context = (health.get("effective_context_limit") or
                   health.get("loaded_context_size"))
        vision = backend == "mlx_vlm" and model["capabilities"].get("vision") is True
        version = health.get("runtime_version") or model.get("runtime_version")
    else:
        raise IdentityError("unsupported runtime " + backend)
    if not isinstance(context, int) or context <= 0:
        raise IdentityError("effective context is unavailable")
    if vision != bool(model["capabilities"].get("vision")):
        raise IdentityError("runtime vision capability differs from registry")
    identity = {"schema": SCHEMA, "project": "Newton_Local", "slot_id": slot,
                "routing_alias": ROUTING_ALIAS, "service_display_name": DISPLAY_NAME,
                "registry_id": model["id"], "display_name": model["display_name"],
                "artifact_path": canonical(model["artifact_path"]),
                "manifest_sha256": model.get("manifest_sha256"),
                "backend": backend, "runtime_version": version,
                "process": process, "effective_context": context,
                "capabilities": {"vision": vision, "tools": bool(model["capabilities"].get("tools"))},
                "verification_status": "verified"}
    generation_material = {k: identity[k] for k in
                           ("registry_id", "artifact_path", "manifest_sha256", "backend", "process",
                            "effective_context", "capabilities")}
    identity["generation"] = hashlib.sha256(json.dumps(
        generation_material, sort_keys=True).encode()).hexdigest()
    return identity


def manifest_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def unavailable(reason, status="unknown"):
    return {"schema": SCHEMA, "project": "Newton_Local", "slot_id": "production",
            "routing_alias": ROUTING_ALIAS, "service_display_name": DISPLAY_NAME,
            "verification_status": status, "registry_id": None, "generation": None,
            "reason": str(reason)}
