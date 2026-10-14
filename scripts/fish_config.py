#!/usr/bin/env python3
"""Newton_Local — Fish speech service configuration (docs/18).

Loads and validates config/fish.json and answers the questions every Fish
component asks: where things live, which model variant is selected, and
whether that model and its runtime are installed, intact and pinned.

Fish is a speech-generation service with its own lifecycle. It is not a Qwen
execution slot and is not in config/models.json; this module only borrows
newton_models' manifest checker so both registries verify weights the same
way. Standard library only, and Python 3.9-compatible: the control service
imports it under /usr/bin/python3.
"""
import hashlib
import json
import os
import re
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("NEWTON_ROOT") or os.path.dirname(SCRIPT_DIR)
CONFIG_PATH = os.environ.get("NEWTON_FISH_CONFIG") or os.path.join(ROOT, "config", "fish.json")
ADMISSION_LOCK = os.environ.get("NEWTON_ADMISSION_LOCK") or os.path.join(
    ROOT, "state", "resource-admission.lock")

MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
COMPAT_RULES = ("conditional", "prohibited")


class ConfigError(Exception):
    pass


def abspath(path):
    """Repository-relative, absolute, or ~-relative path -> absolute path."""
    if path.startswith("~"):
        return os.path.expanduser(path)
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


def load_config(path=None):
    path = path or CONFIG_PATH
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigError(f"cannot read {path}: {e}")
    validate_config(cfg)
    cfg["_config_path"] = os.path.abspath(path)
    return cfg


def validate_config(cfg):
    for key in ("schema", "default_model", "service", "runtimes", "models",
                "compatibility", "admission"):
        if key not in cfg:
            raise ConfigError(f"fish config missing '{key}'")
    svc = cfg["service"]
    for key in ("label", "host", "port", "logdir", "statedir", "start_timeout_s", "store"):
        if key not in svc:
            raise ConfigError(f"fish service missing '{key}'")
    if svc["host"] not in ("127.0.0.1", "localhost"):
        raise ConfigError("the Fish service must bind to loopback only")
    if cfg["default_model"] not in cfg["models"]:
        raise ConfigError(f"default_model {cfg['default_model']!r} is not a model")
    for mid, m in cfg["models"].items():
        if not MODEL_ID_RE.match(mid):
            raise ConfigError(f"bad model id {mid!r}")
        for key in ("display_name", "variant", "runtime", "path", "manifest", "source",
                    "sample_rate", "sampling_defaults", "limits", "memory", "license"):
            if key not in m:
                raise ConfigError(f"fish model {mid} missing '{key}'")
        if m["runtime"] not in cfg["runtimes"]:
            raise ConfigError(f"fish model {mid} references unknown runtime {m['runtime']}")
        try:
            ok = float(m["memory"]["required_gib"]) > 0
        except (KeyError, TypeError, ValueError):
            ok = False
        if not ok:
            raise ConfigError(f"fish model {mid} needs a positive memory.required_gib")
    for rid, r in cfg["runtimes"].items():
        for key in ("python", "install_record", "source", "adapter", "pinned_versions"):
            if key not in r:
                raise ConfigError(f"fish runtime {rid} missing '{key}'")
        if not re.match(r"^[0-9a-f]{40}$", r["source"].get("commit", "")):
            raise ConfigError(f"fish runtime {rid} must pin a full commit id")
    for mid, rule in cfg["compatibility"].items():
        if mid.startswith("_"):
            continue
        if rule not in COMPAT_RULES:
            raise ConfigError(f"compatibility rule for {mid} must be one of {COMPAT_RULES}")


# ------------------------------------------------------------------ lookups
def model(cfg, model_id):
    if model_id not in cfg["models"]:
        raise ConfigError(f"{model_id!r} is not a Fish model "
                          f"(valid: {', '.join(cfg['models'])})")
    return cfg["models"][model_id]


def runtime_of(cfg, model_id):
    return cfg["runtimes"][model(cfg, model_id)["runtime"]]


def compatibility(cfg, qwen_model_id):
    """How Fish may coexist with a Qwen registry model. Anything not listed,
    including an unidentified model process (None), is prohibited."""
    rule = cfg["compatibility"].get(qwen_model_id) if qwen_model_id else None
    return rule if rule in COMPAT_RULES else "prohibited"


def paths(cfg):
    """Every filesystem location the Fish service uses, absolute."""
    svc = cfg["service"]
    state = abspath(svc["statedir"])
    return {
        "statedir": state,
        "logdir": abspath(svc["logdir"]),
        "store": abspath(svc["store"]),
        "state": os.path.join(state, "state.json"),
        "lock": os.path.join(state, "fish.lock"),
        "cancel": os.path.join(state, "fish.cancel"),
        "desired": os.path.join(state, "desired.json"),
        "selection": os.path.join(state, "selection.json"),
        "token": os.path.join(state, "engine.token"),
        "admission_lock": ADMISSION_LOCK,
    }


def endpoint(cfg):
    svc = cfg["service"]
    return f"http://{svc['host']}:{svc['port']}"


# ---------------------------------------------------------------- selection
def read_selection(cfg, path=None):
    """(model_id, source). Missing or invalid state fails safe to the default."""
    path = path or paths(cfg)["selection"]
    try:
        with open(path) as f:
            rec = json.load(f)
    except FileNotFoundError:
        return cfg["default_model"], "default"
    except (OSError, ValueError):
        return cfg["default_model"], "default (selection file unreadable)"
    sel = rec.get("selected") if isinstance(rec, dict) else None
    if sel not in cfg["models"]:
        return cfg["default_model"], f"default ({sel!r} is not a Fish model)"
    return sel, "recorded"


def write_selection(cfg, model_id, by, path=None):
    model(cfg, model_id)
    path = path or paths(cfg)["selection"]
    rec = {"schema": 1, "selected": model_id, "set_at": time.time(),
           "set_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "set_by": by}
    atomic_write_json(path, rec)
    return rec


# -------------------------------------------------------------- integrity
def check_weights(cfg, model_id, deep=False):
    """newton_models.check_weights over this model's manifest (same contract:
    every file present with its exact size; deep re-hashes)."""
    import newton_models as nm  # lazy: keeps this module importable alone
    m = model(cfg, model_id)
    shim = {"models": {model_id: {"path": abspath(m["path"]),
                                  "manifest": abspath(m["manifest"])}}}
    return nm.check_weights(shim, model_id, deep=deep)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_install_record(cfg, runtime_id):
    try:
        with open(abspath(cfg["runtimes"][runtime_id]["install_record"])) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def check_runtime(cfg, runtime_id):
    """The venv exists, was built from the pinned commit with the current
    lock files, and carries the pinned package versions. No process runs."""
    r = cfg["runtimes"][runtime_id]
    res = {"runtime": runtime_id, "ok": False, "reasons": [],
           "python": abspath(r["python"]), "commit": None, "packages": {}}
    if not os.access(res["python"], os.X_OK):
        res["reasons"].append(f"runtime python missing: {res['python']} "
                              f"(run scripts/install-fish-runtime.sh)")
        return res
    rec = read_install_record(cfg, runtime_id)
    if not rec:
        res["reasons"].append("install record missing; run scripts/install-fish-runtime.sh")
        return res
    res["commit"] = rec.get("mlx_audio_commit")
    if res["commit"] != r["source"]["commit"]:
        res["reasons"].append(f"runtime built from {res['commit']}, pinned "
                              f"{r['source']['commit']}")
    pk = rec.get("packages") or {}
    for name, want in r["pinned_versions"].items():
        res["packages"][name] = pk.get(name)
        if pk.get(name) != want:
            res["reasons"].append(f"{name} is {pk.get(name)}, pinned {want}")
    for key, field in (("requirements_lock", "requirements_lock_sha256"),
                       ("build_requirements_lock", "build_requirements_lock_sha256")):
        lock = r.get(key)
        if lock and os.path.exists(abspath(lock)) and \
                _sha256_file(abspath(lock)) != rec.get(field):
            res["reasons"].append(f"{lock} changed since the runtime was installed")
    res["ok"] = not res["reasons"]
    return res


def check_model(cfg, model_id, deep=False):
    m = model(cfg, model_id)
    w = check_weights(cfg, model_id, deep=deep)
    r = check_runtime(cfg, m["runtime"])
    reasons = list(w["problems"][:5]) + list(r["reasons"])
    if len(w["problems"]) > 5:
        reasons.append(f"... {len(w['problems']) - 5} more weight problems")
    return {"model": model_id, "display_name": m["display_name"], "variant": m["variant"],
            "available": bool(w["ok"] and r["ok"]), "weights": w, "runtime": r,
            "reasons": reasons}


def manifest_sha256(cfg, model_id):
    try:
        return _sha256_file(abspath(model(cfg, model_id)["manifest"]))
    except OSError:
        return None


# ---------------------------------------------------------------- launch
ENGINE_SCRIPT = os.path.join(SCRIPT_DIR, "fish_engine.py")
SANDBOX_PROFILE = os.path.join(ROOT, "config", "fish-engine.sb")


def engine_argv(cfg, runtime_id=None):
    """What the com.newton.fish LaunchAgent runs: the engine under the
    network sandbox, from the pinned venv, in isolated mode. The model is
    resolved by the engine at spawn from the selection, like exec-slot."""
    runtime_id = runtime_id or model(cfg, cfg["default_model"])["runtime"]
    py = abspath(cfg["runtimes"][runtime_id]["python"])
    return ["/usr/bin/sandbox-exec", "-f", SANDBOX_PROFILE, py, "-I",
            ENGINE_SCRIPT, "--config", cfg.get("_config_path") or CONFIG_PATH]


def engine_signature():
    """argv substring that identifies an engine process, whichever Python
    stub launched it (the framework build re-execs Python.app)."""
    return ENGINE_SCRIPT + " --config "


# ---------------------------------------------------------------- files
def atomic_write_json(path, obj, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path) as f:
            v = json.load(f)
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None
