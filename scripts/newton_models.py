#!/usr/bin/env python3
"""Newton_Local — supported-model registry and global model selection (Commission #5).

ONE registry (config/models.json), ONE persisted selection
(state/model-selection.json), TWO execution slots (Qwen-A :8080, Qwen-B :8081).
The selected model governs both slots; nothing in the lifecycle code assumes a
particular runtime. This module is ordinary deterministic infrastructure: no
model ever decides which model runs.

CONTRACT
    * Registry: stable ids (qwen27b, flash-next), display names, runtime,
      paths, context, vision, launch args, health semantics, provenance.
    * Selection: exactly one id, defaults to the registry default (qwen27b);
      atomic writes; invalid ids rejected; missing/corrupt state fails safe
      to the default and says so; survives every restart and reboot (it is a
      file, not a process) — but selecting never starts anything.
    * Lock while occupied: selection cannot change while EITHER slot is
      anything but STOPPED+DOWN (starting/running/recovering/stopping/leased)
      or while any model-runtime process exists. Checked under both slot
      controller locks, so a concurrent start cannot race the write.
    * Availability: a model is selectable/startable only when its weights
      match the manifest (every file present, byte-size equal; --deep also
      re-hashes) AND its runtime executable exists, satisfies the host-OS
      requirement, and answers `--version`.
    * Launch plan: slot + model -> executable, argv, endpoint, health probe.
      `exec-slot` is what the LaunchAgents run: resolve, gate, execv.

COMMANDS
    list | status [--json] | select <id> | verify [<id>] [--deep] |
    plan <A|B> [--model <id>] [--argv] | shim-argv <A|B> | exec-slot <A|B>
EXIT CODES
    0 ok  1 error  2 invalid id  3 selection locked (slot occupied)
    4 model unavailable  5 registry invalid
"""
import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("NEWTON_ROOT") or os.path.dirname(SCRIPT_DIR)
REGISTRY_PATH = os.environ.get("NEWTON_REGISTRY_PATH") or os.path.join(ROOT, "config", "models.json")
SELECTION_PATH = os.environ.get("NEWTON_SELECTION_PATH") or os.path.join(ROOT, "state", "model-selection.json")
SLOT_CONFIGS = {
    "A": os.path.join(ROOT, "config", "qwen-a-controller.env"),
    "B": os.path.join(ROOT, "config", "qwen-b.env"),
}
PYTHON = "/usr/bin/python3"

EXIT_OK, EXIT_ERR, EXIT_INVALID, EXIT_LOCKED, EXIT_UNAVAILABLE, EXIT_REGISTRY = 0, 1, 2, 3, 4, 5


class RegistryError(Exception):
    pass


class SelectionError(Exception):
    def __init__(self, msg, code):
        super().__init__(msg)
        self.code = code


def now():
    return time.time()


def iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts or now()))


def abspath(rel):
    return rel if os.path.isabs(rel) else os.path.join(ROOT, rel)


# ------------------------------------------------------------------ registry
def load_registry(path=None):
    path = path or REGISTRY_PATH
    try:
        with open(path) as f:
            reg = json.load(f)
    except (OSError, ValueError) as e:
        raise RegistryError(f"cannot read registry {path}: {e}")
    validate_registry(reg)
    return reg


def validate_registry(reg):
    for key in ("schema", "default", "slots", "runtimes", "models"):
        if key not in reg:
            raise RegistryError(f"registry missing '{key}'")
    if reg["default"] not in reg["models"]:
        raise RegistryError(f"registry default '{reg['default']}' is not a model")
    for slot in ("A", "B"):
        s = reg["slots"].get(slot)
        if not s or not all(k in s for k in ("label", "host", "port", "alias", "logdir")):
            raise RegistryError(f"slot {slot} incomplete")
    if reg["slots"]["A"]["port"] == reg["slots"]["B"]["port"]:
        raise RegistryError("slots A and B must not share a port")
    for mid, m in reg["models"].items():
        for k in ("display_name", "runtime", "path", "manifest", "context_length", "args"):
            if k not in m:
                raise RegistryError(f"model {mid} missing '{k}'")
        if m["runtime"] not in reg["runtimes"]:
            raise RegistryError(f"model {mid} references unknown runtime {m['runtime']}")
        if not isinstance(m["args"], list) or not m["args"]:
            raise RegistryError(f"model {mid} args must be a non-empty list")
    for rid, r in reg["runtimes"].items():
        for k in ("executable", "health"):
            if k not in r:
                raise RegistryError(f"runtime {rid} missing '{k}'")


def model_ids(reg):
    return list(reg["models"].keys())


# ----------------------------------------------------------------- selection
def read_selection(reg, path=None):
    """Return (selected_id, source) — source explains fail-safe defaulting."""
    path = path or SELECTION_PATH
    default = reg["default"]
    try:
        with open(path) as f:
            rec = json.load(f)
    except FileNotFoundError:
        return default, "default (no selection recorded yet)"
    except (OSError, ValueError) as e:
        return default, f"default (selection file unreadable/corrupt: {e.__class__.__name__})"
    sel = rec.get("selected") if isinstance(rec, dict) else None
    if sel not in reg["models"]:
        return default, f"default (recorded selection {sel!r} is not a registered model)"
    return sel, "recorded"


def read_selection_record(path=None):
    path = path or SELECTION_PATH
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_selection(reg, model_id, by, path=None):
    path = path or SELECTION_PATH
    if model_id not in reg["models"]:
        raise SelectionError(f"'{model_id}' is not a registered model "
                             f"(valid: {', '.join(model_ids(reg))})", EXIT_INVALID)
    rec = {"schema": 1, "selected": model_id, "set_at": now(),
           "set_at_iso": iso(), "set_by": by}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(rec, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return rec


# -------------------------------------------------------------- availability
def host_macos_version():
    v = platform.mac_ver()[0] or "0"
    return tuple(int(x) for x in v.split(".") if x.isdigit())


def _version_tuple(s):
    return tuple(int(x) for x in str(s).split(".") if x.isdigit())


def runtime_executable(reg, runtime_id):
    return abspath(reg["runtimes"][runtime_id]["executable"])


_probe_cache = {}


def check_runtime(reg, runtime_id, probe=True):
    r = reg["runtimes"][runtime_id]
    exe = runtime_executable(reg, runtime_id)
    res = {"runtime": runtime_id, "executable": exe, "ok": False, "reasons": [],
           "version": None}
    if not os.path.isfile(exe):
        res["reasons"].append(f"executable missing: {exe}")
        return res
    if not os.access(exe, os.X_OK):
        res["reasons"].append(f"executable not executable: {exe}")
        return res
    req = r.get("requires_macos")
    if req and host_macos_version() < _version_tuple(req):
        res["reasons"].append(
            f"runtime requires macOS {req}+ but host is "
            f"{'.'.join(map(str, host_macos_version())) or 'unknown'}")
        return res
    if probe:
        key = (exe, os.path.getmtime(exe))
        if key not in _probe_cache:
            try:
                p = subprocess.run([exe] + list(r.get("version_args", ["--version"])),
                                   capture_output=True, text=True, timeout=15)
                out = (p.stdout + p.stderr).strip()
                _probe_cache[key] = (p.returncode == 0, out.splitlines()[0][:160] if out else "")
            except (OSError, subprocess.TimeoutExpired) as e:
                _probe_cache[key] = (False, f"{e.__class__.__name__}: {e}")
        ok, ver = _probe_cache[key]
        res["version"] = ver
        if not ok:
            res["reasons"].append(f"`--version` probe failed: {ver}")
            return res
    res["ok"] = True
    return res


def _git_blob_sha1(path):
    h = hashlib.sha1()
    size = os.path.getsize(path)
    h.update(f"blob {size}\0".encode())
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def check_weights(reg, model_id, deep=False):
    m = reg["models"][model_id]
    base = abspath(m["path"])
    mpath = abspath(m["manifest"])
    res = {"model": model_id, "path": base, "manifest": mpath, "ok": False,
           "files_expected": 0, "files_ok": 0, "bytes_expected": 0,
           "bytes_present": 0, "problems": [], "deep": deep}
    try:
        with open(mpath) as f:
            man = json.load(f)
    except (OSError, ValueError) as e:
        res["problems"].append(f"manifest unreadable: {e.__class__.__name__}")
        return res
    files = man.get("files") or []
    if not files:
        res["problems"].append("manifest lists no files")
        return res
    res["files_expected"] = len(files)
    for entry in files:
        name, size = entry["name"], int(entry["size"])
        res["bytes_expected"] += size
        p = os.path.join(base, name)
        if not os.path.isfile(p):
            res["problems"].append(f"missing: {name}")
            continue
        actual = os.path.getsize(p)
        res["bytes_present"] += actual
        if actual != size:
            res["problems"].append(f"size mismatch: {name} ({actual} != {size})")
            continue
        if deep:
            if entry.get("sha256"):
                got = _sha256(p)
                if got != entry["sha256"]:
                    res["problems"].append(f"sha256 mismatch: {name}")
                    continue
            elif entry.get("git_blob_sha1"):
                got = _git_blob_sha1(p)
                if got != entry["git_blob_sha1"]:
                    res["problems"].append(f"git blob sha1 mismatch: {name}")
                    continue
        res["files_ok"] += 1
    res["ok"] = res["files_ok"] == res["files_expected"] and not res["problems"]
    return res


def check_model(reg, model_id, deep=False, probe_runtime=True):
    m = reg["models"][model_id]
    w = check_weights(reg, model_id, deep=deep)
    r = check_runtime(reg, m["runtime"], probe=probe_runtime)
    reasons = list(w["problems"][:5]) + list(r["reasons"])
    if len(w["problems"]) > 5:
        reasons.append(f"... {len(w['problems']) - 5} more weight problems")
    return {"model": model_id, "display_name": m["display_name"],
            "available": w["ok"] and r["ok"], "weights": w, "runtime": r,
            "reasons": reasons}


# --------------------------------------------------------------- launch plan
def slot_info(reg, slot):
    if slot not in reg["slots"]:
        raise RegistryError(f"unknown slot {slot!r}")
    return reg["slots"][slot]


def launch_plan(reg, slot, model_id=None):
    """Deterministic resolution: slot + model -> exactly what to exec."""
    if model_id is None:
        model_id, _ = read_selection(reg)
    if model_id not in reg["models"]:
        raise SelectionError(f"'{model_id}' is not a registered model", EXIT_INVALID)
    m = reg["models"][model_id]
    r = reg["runtimes"][m["runtime"]]
    s = slot_info(reg, slot)
    exe = runtime_executable(reg, m["runtime"])
    subst = {"path": abspath(m["path"]), "host": s["host"], "port": str(s["port"]),
             "alias": s["alias"], "context_length": str(m["context_length"]),
             "logdir": abspath(s["logdir"]), "executable": exe}
    argv = [exe] + [a.format(**subst) for a in m["args"]]
    return {
        "slot": slot, "model": model_id, "display_name": m["display_name"],
        "runtime": m["runtime"], "executable": exe, "argv": argv,
        "endpoint": f"http://{s['host']}:{s['port']}", "port": s["port"],
        "host": s["host"], "alias": s["alias"], "label": s["label"],
        "logdir": abspath(s["logdir"]), "cwd": ROOT,
        "context_length": m["context_length"],
        "health": r["health"], "start_timeout_s": int(m.get("start_timeout_s", 300)),
        "vision": (m.get("vision") or {}).get("enabled", False),
    }


def shim_argv(slot):
    """What the LaunchAgent for a slot runs: this module, resolving at spawn."""
    return [PYTHON, os.path.join(SCRIPT_DIR, "newton_models.py"), "exec-slot", slot]


# ------------------------------------------------------------ process/health
def runtime_signatures(reg):
    """Every executable path that identifies a large-model runtime process.
    Includes symlink targets so a process exec'd through tools/mlx-serve/current
    is recognised whichever path appears in its argv."""
    sigs = []
    for rid in reg["runtimes"]:
        exe = runtime_executable(reg, rid)
        sigs.append(exe)
        real = os.path.realpath(exe)
        if real != exe:
            sigs.append(real)
    return sigs


def _http_json(url, timeout):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read(1 << 20).decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body)
            except ValueError:
                return resp.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return None, None


def probe_health(reg, runtime_id, base_url, timeout=5):
    """Runtime-specific readiness. Returns {"ok", "status", "props"}."""
    h = reg["runtimes"][runtime_id]["health"]
    st, body = _http_json(base_url + h["path"], timeout)
    props = None
    if h["ready"] == "json_status_ok":
        ok = bool(st == 200 and isinstance(body, dict) and body.get("status") == "ok")
        if ok:
            _, p = _http_json(base_url + h["identity_path"], timeout)
            if isinstance(p, dict):
                props = {
                    "alias": p.get("model_alias"), "ftype": p.get("model_ftype"),
                    "n_ctx": (p.get("default_generation_settings") or {}).get("n_ctx"),
                    "model_path": p.get("model_path"), "modalities": p.get("modalities"),
                    "total_slots": p.get("total_slots"), "build_info": p.get("build_info"),
                }
    elif h["ready"] == "http_200_and_models_listed":
        ok = st == 200
        if ok:
            st2, models = _http_json(base_url + h["identity_path"], timeout)
            rows = (models or {}).get("data") if isinstance(models, dict) else None
            ok = bool(st2 == 200 and rows)
            if ok:
                first = rows[0]
                props = {"alias": first.get("id"),
                         "n_ctx": first.get("context_length") or (first.get("meta") or {}).get("context_length"),
                         "engine": first.get("engine") or (first.get("meta") or {}).get("engine"),
                         "modalities": first.get("capabilities")}
    else:
        ok = st == 200
    return {"ok": ok, "status": st, "props": props}


# ------------------------------------------------------------- occupancy
def slots_occupancy(reg):
    """Truth about both execution slots, from the slot controllers themselves.
    Occupied = lease not STOPPED, or a process/socket observed, or ANY
    model-runtime process anywhere (heterogeneous singleton resource)."""
    import qwenctl  # lazy: qwenctl imports this module
    out = {"occupied": False, "slots": {}, "reasons": []}
    for slot, env in SLOT_CONFIGS.items():
        cfg = qwenctl.load_config(env)
        lease = qwenctl.read_lease(cfg)
        o = qwenctl.observe(cfg, health_timeout=2)
        busy = lease["state"] != qwenctl.STOPPED or o["observed_state"] != qwenctl.OBS_DOWN
        out["slots"][slot] = {"lease_state": lease["state"], "observed": o["observed_state"],
                              "pid": o["pid"], "owner": lease.get("owner"),
                              "model": lease.get("model"), "occupied": busy}
        if busy:
            out["reasons"].append(f"slot {slot} ({cfg['instance']}) is {lease['state']}"
                                  f"/{o['observed_state']}" + (f" owner={lease['owner']}" if lease.get("owner") else ""))
        if o.get("peer_processes"):
            out["reasons"].append(f"model runtime process(es) present: "
                                  f"{[p['pid'] for p in o['peer_processes']]}")
    out["occupied"] = bool(out["reasons"])
    return out


def selection_locks():
    """Both slot controller locks, so a select cannot race a start/acquire."""
    import qwenctl
    return [qwenctl.Lock(qwenctl.load_config(env), timeout=10) for env in SLOT_CONFIGS.values()]


def select_model(reg, model_id, by="cli", occupancy_fn=None, check_fn=None,
                 lock_fn=None):
    """The one way to change the global selection. Returns the new record.
    occupancy_fn/check_fn/lock_fn are injection points for no-load tests."""
    if model_id not in reg["models"]:
        raise SelectionError(f"'{model_id}' is not a registered model "
                             f"(valid: {', '.join(model_ids(reg))})", EXIT_INVALID)
    check = (check_fn or check_model)(reg, model_id)
    if not check["available"]:
        raise SelectionError(f"{reg['models'][model_id]['display_name']} is not available: "
                             + "; ".join(check["reasons"]), EXIT_UNAVAILABLE)
    locks = (lock_fn or selection_locks)()
    held = []
    try:
        for lk in locks:
            lk.__enter__()
            held.append(lk)
        occ = (occupancy_fn or slots_occupancy)(reg)
        if occ["occupied"]:
            raise SelectionError("selection is locked while a slot is occupied: "
                                 + "; ".join(occ["reasons"]), EXIT_LOCKED)
        current, _ = read_selection(reg)
        rec = write_selection(reg, model_id, by)
        _log("select", from_model=current, to_model=model_id, by=by)
        return rec
    finally:
        for lk in reversed(held):
            lk.__exit__(None, None, None)


def _log(event, **fields):
    try:
        d = os.path.join(ROOT, "logs", "control")
        os.makedirs(d, exist_ok=True)
        rec = {"ts": round(now(), 3), "ts_iso": iso(), "event": "model." + event}
        rec.update(fields)
        with open(os.path.join(d, "control.log"), "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------- status
def status(reg, probe_runtime=True, occupancy=True):
    sel, source = read_selection(reg)
    rec = read_selection_record()
    models = {}
    for mid in model_ids(reg):
        c = check_model(reg, mid, probe_runtime=probe_runtime)
        m = reg["models"][mid]
        models[mid] = {"id": mid, "display_name": m["display_name"],
                       "long_name": m.get("long_name"), "runtime": m["runtime"],
                       "context_length": m["context_length"],
                       "vision": (m.get("vision") or {}).get("enabled", False),
                       "available": c["available"], "reasons": c["reasons"],
                       "selected": mid == sel, "default": mid == reg["default"],
                       "memory": m.get("memory")}
    out = {"selected": sel, "selection_source": source,
           "selection_record": rec, "default": reg["default"], "models": models,
           "slots": {s: {"port": v["port"], "alias": v["alias"], "label": v["label"]}
                     for s, v in reg["slots"].items()}}
    if occupancy:
        try:
            occ = slots_occupancy(reg)
        except (Exception, SystemExit) as e:       # never let status die; unknown = locked
            occ = {"occupied": True, "reasons": [f"occupancy unknown: {e}"], "slots": {}}
        out["occupancy"] = occ
        out["selection_locked"] = occ["occupied"]
    return out


# ---------------------------------------------------------------------- CLI
def cmd_list(reg, args):
    st = status(reg, occupancy=False)
    for mid, m in st["models"].items():
        mark = "●" if m["selected"] else "○"
        avail = "available" if m["available"] else "UNAVAILABLE"
        print(f"{mark} {mid:<11} {m['display_name']:<24} {m['runtime']:<10} {avail}"
              + ("   (default)" if m["default"] else ""))
        for r in m["reasons"]:
            print(f"      - {r}")
    print(f"selected: {st['selected']}   [{st['selection_source']}]")
    return 0


def cmd_status(reg, args):
    st = status(reg)
    if args.json:
        print(json.dumps(st, indent=2))
        return 0
    print(f"selected model : {st['selected']}  ({reg['models'][st['selected']]['display_name']})")
    print(f"source         : {st['selection_source']}")
    print(f"default        : {st['default']}")
    print(f"selection lock : {'LOCKED — ' + '; '.join(st['occupancy']['reasons']) if st['selection_locked'] else 'free (both slots stopped)'}")
    for mid, m in st["models"].items():
        print(f"  {'●' if m['selected'] else '○'} {mid:<11} {m['display_name']:<24} "
              f"{m['runtime']:<10} {'available' if m['available'] else 'UNAVAILABLE: ' + '; '.join(m['reasons'])}")
    for slot, s in st["occupancy"]["slots"].items():
        print(f"  slot {slot}: lease={s['lease_state']} observed={s['observed']}"
              + (f" pid={s['pid']}" if s["pid"] else "")
              + (f" model={s['model']}" if s.get("model") else ""))
    return 0


def cmd_select(reg, args):
    try:
        rec = select_model(reg, args.model_id, by=args.by)
    except SelectionError as e:
        sys.stderr.write(f"newton-model: {e}\n")
        return e.code
    print(f"selected {rec['selected']} ({reg['models'][rec['selected']]['display_name']}) "
          f"at {rec['set_at_iso']} — nothing was started; Start uses it next time.")
    return 0


def cmd_verify(reg, args):
    ids = [args.model_id] if args.model_id else model_ids(reg)
    rc = 0
    for mid in ids:
        if mid not in reg["models"]:
            sys.stderr.write(f"newton-model: unknown model {mid}\n")
            return EXIT_INVALID
        c = check_model(reg, mid, deep=args.deep)
        w, r = c["weights"], c["runtime"]
        print(f"{mid}: {'AVAILABLE' if c['available'] else 'UNAVAILABLE'}")
        print(f"  weights  {w['files_ok']}/{w['files_expected']} files ok, "
              f"{w['bytes_present']:,}/{w['bytes_expected']:,} bytes"
              + ("  (sha256/blob verified)" if args.deep and w["ok"] else ""))
        for p in w["problems"][:8]:
            print(f"           ! {p}")
        print(f"  runtime  {r['runtime']}  {'ok' if r['ok'] else 'NOT USABLE'}"
              + (f"  [{r['version']}]" if r.get("version") else ""))
        for p in r["reasons"]:
            print(f"           ! {p}")
        if not c["available"]:
            rc = EXIT_UNAVAILABLE
    return rc


def cmd_plan(reg, args):
    try:
        plan = launch_plan(reg, args.slot, args.model)
    except SelectionError as e:
        sys.stderr.write(f"newton-model: {e}\n")
        return e.code
    if args.argv:
        print("\n".join(plan["argv"]))
    else:
        print(json.dumps(plan, indent=2))
    return 0


def cmd_shim_argv(reg, args):
    print("\n".join(shim_argv(args.slot)))
    return 0


def cmd_exec_slot(reg, args):
    """The LaunchAgent entry point: resolve the global selection for this
    slot, gate on availability, then REPLACE this process with the runtime.
    Only launchd (kickstarted by qwenctl) should ever run this."""
    sel, source = read_selection(reg)
    plan = launch_plan(reg, args.slot, sel)
    check = check_model(reg, sel)
    os.makedirs(plan["logdir"], exist_ok=True)
    _log("exec_slot", slot=args.slot, model=sel, runtime=plan["runtime"],
         selection_source=source, available=check["available"],
         reasons=check["reasons"], argv=plan["argv"])
    if not check["available"]:
        sys.stderr.write(f"newton-model exec-slot {args.slot}: refusing to launch "
                         f"{sel}: {'; '.join(check['reasons'])}\n")
        return 78                                 # EX_CONFIG
    if args.dry_run:
        print(json.dumps({"would_exec": plan["argv"], "cwd": plan["cwd"]}, indent=2))
        return 0
    os.chdir(plan["cwd"])
    os.execv(plan["executable"], plan["argv"])


def main():
    ap = argparse.ArgumentParser(prog="newton-model",
                                 description="Newton_Local model registry and global selection")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="supported models, availability, current selection")
    s = sub.add_parser("status", help="selection, availability, slot occupancy")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("select", help="change the global selection (both slots stopped)")
    s.add_argument("model_id")
    s.add_argument("--by", default="cli")
    s = sub.add_parser("verify", help="availability/integrity of installed models")
    s.add_argument("model_id", nargs="?")
    s.add_argument("--deep", action="store_true", help="re-hash every file")
    s = sub.add_parser("plan", help="resolved launch plan for a slot")
    s.add_argument("slot", choices=["A", "B"])
    s.add_argument("--model", default=None)
    s.add_argument("--argv", action="store_true", help="print argv one token per line")
    s = sub.add_parser("shim-argv", help="what the slot's LaunchAgent must run")
    s.add_argument("slot", choices=["A", "B"])
    s = sub.add_parser("exec-slot", help="[launchd only] resolve + exec the runtime")
    s.add_argument("slot", choices=["A", "B"])
    s.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        reg = load_registry()
    except RegistryError as e:
        sys.stderr.write(f"newton-model: {e}\n")
        sys.exit(EXIT_REGISTRY)
    fn = {"list": cmd_list, "status": cmd_status, "select": cmd_select,
          "verify": cmd_verify, "plan": cmd_plan, "shim-argv": cmd_shim_argv,
          "exec-slot": cmd_exec_slot}[args.cmd]
    sys.exit(fn(reg, args) or 0)


if __name__ == "__main__":
    main()
