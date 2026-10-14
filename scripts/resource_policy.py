#!/usr/bin/env python3
"""Newton_Local — shared resource policy for the large-model services (docs/18 §4).

Two services can load a large model into the one unified-memory pool: the
Qwen execution slots (qwenctl.py) and the Fish speech service (fishctl.py).
This module decides whether a start may proceed. Both controllers call it at
the moment they reserve the start, and both launch shims call it as a
backstop. Standard library only; Python 3.9-compatible.

COMPATIBILITY (config/fish.json "compatibility"; built-in fallback below)
    Fish + Qwen 3.8 27B     conditional: only if memory admission passes
    Fish + Flash-Next       prohibited in both directions, whatever memory says
    anything else           prohibited, including model processes that cannot
                            be identified (fail closed)

PRESENCE (read-only; nothing here writes or reconciles a record)
    Fish   an engine or launch-shim process, the launchd job running, a live
           STARTING / READY / STOPPING record, or desired=RUNNING this boot
           (the watchdog may restart it: "recovering").
    Qwen   per slot: a model-runtime process, a live lease that is not
           STOPPED, the slot's launchd job running (the exec-slot shim), or
           for slot A desired=RUNNING this boot (recovering, with the current
           selection). Runtime processes on no slot port count too.
    A STARTING record is stale when nothing is observed and either its
    controller lock is free (the start that wrote it has gone) or its
    deadline passed; a READY or STOPPING record with nothing observed is
    stale. Stale records do not count.

ADMISSION, ATOMIC
    admission_lock() is an flock on state/resource-admission.lock, held only
    across "decide + write STARTING". The STARTING record is the reservation
    the other side sees, so two simultaneous starts cannot both pass. Lock
    order: own controller lock, then the admission lock; inside it, only
    non-blocking attempts on other locks, so it cannot deadlock.

    Starting X while the other service is present:
      prohibited pair            refused
      the other is in transition refused (one large load at a time)
      conditional pair           memory admission for X
    Starting Fish always runs memory admission, even alone.
    Starting a Qwen model while Fish is absent: no new check at all, so the
    existing Qwen behaviour is unchanged.

MEMORY ADMISSION (measured, not nominal)
    available = hw.memsize - (wired + compressor + max(0, anon - purgeable))
                (the engine formula of newton_models, from vm_stat)
              - weights of resident llama.cpp models (memory-mapped, so the
                formula counts them as reclaimable file cache)
    admit only if available >= requirement + reserve, memory pressure is
    normal, and Metal's allocation now + requirement stays within the GPU
    working-set limit minus a margin.
"""
import errno
import fcntl
import json
import os
import re
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import fish_config as fc  # noqa: E402
import newton_models as nm  # noqa: E402

GIB = 1 << 30
DEFAULT_COMPATIBILITY = {"qwen27b": "conditional", "flash-next": "prohibited"}
DEFAULT_ADMISSION = {"reserve_gib": 4.0, "gpu_working_set_margin_gib": 4.0,
                     "require_pressure": "normal"}
FISH_DEFAULT_LABEL = "com.newton.fish"
FISH_DEFAULT_PORT = 8770
RUNTIME_BASENAMES = ("llama-server", "mlx-serve")
PRESSURE_LEVELS = {"1": "normal", "2": "warning", "4": "critical"}
EXIT_POLICY = 10


class AdmissionBusy(Exception):
    pass


# ------------------------------------------------------------------ helpers
def sh(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def boot_time_sec():
    _, out, _ = sh(["sysctl", "-n", "kern.boottime"])
    m = re.search(r"sec\s*=\s*(\d+)", out)
    return int(m.group(1)) if m else 0


def effective_desired(record, boot_sec):
    """RUNNING only for a well-formed RUNNING record written during this
    boot (the newton_control rule: every reboot lands on OFF)."""
    if not isinstance(record, dict) or record.get("desired") != "RUNNING":
        return "STOPPED"
    return "RUNNING" if record.get("boot_sec") == boot_sec else "STOPPED"


def lock_is_free(path):
    """Non-blocking probe of a controller flock. True when nobody holds it
    (taken and released at once); False when held. Never waits."""
    if not os.path.exists(path):
        return True
    try:
        fh = open(path, "a+")
    except OSError:
        return False
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        fh.close()
        if e.errno in (errno.EACCES, errno.EAGAIN):
            return False
        raise
    fcntl.flock(fh, fcntl.LOCK_UN)
    fh.close()
    return True


class admission_lock:
    """The shared admission lock. Held for milliseconds: decide + reserve."""

    def __init__(self, path=None, timeout=30.0):
        self.path = path or fc.ADMISSION_LOCK
        self.timeout = timeout
        self.fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "a+")
        deadline = time.time() + self.timeout
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.time() >= deadline:
                    self.fh.close()
                    raise AdmissionBusy(f"the resource admission lock is busy ({self.path})")
                time.sleep(0.05)

    def __exit__(self, *a):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()


def fish_settings():
    """(config or None, compatibility table, admission settings). An
    unreadable Fish config never blocks the Qwen side: the built-in table
    keeps Flash-Next prohibited and the 27B conditional."""
    try:
        cfg = fc.load_config()
    except fc.ConfigError:
        return None, dict(DEFAULT_COMPATIBILITY), dict(DEFAULT_ADMISSION)
    compat = {k: v for k, v in cfg["compatibility"].items() if not k.startswith("_")}
    adm = dict(DEFAULT_ADMISSION)
    adm.update(cfg.get("admission") or {})
    return cfg, compat, adm


def rule_for(compat, qwen_model):
    rule = compat.get(qwen_model) if qwen_model else None
    return rule if rule in fc.COMPAT_RULES else "prohibited"


def display_name(reg, model_id):
    try:
        return reg["models"][model_id]["display_name"]
    except (KeyError, TypeError):
        return "an unidentified model"


# -------------------------------------------------------------- processes
def _argument(cmd, flag):
    m = re.search(r"(?:^|\s)" + re.escape(flag) + r"\s+(\S+)", cmd)
    return m.group(1) if m else None


def classify_processes(ps_lines, reg, signatures=None):
    """Pure: every Fish engine/shim and model-runtime process, from
    `ps -Ao pid=,rss=,command=` lines. Runtime processes are identified by
    their --model argument against the registry (None if no match)."""
    sigs = signatures if signatures is not None else nm.runtime_signatures(reg)
    model_paths = {}
    for mid in reg.get("models", {}):
        for slot in ("A", "B"):
            try:
                arg = _argument(" ".join(nm.launch_plan(reg, slot, mid)["argv"]), "--model")
            except Exception:
                arg = None
            if arg:
                model_paths[os.path.realpath(arg)] = mid
    out = []
    for line in ps_lines:
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        pid, rss, cmd = int(parts[0]), parts[1], parts[2]
        rss_gib = round(int(rss) / 2**20, 2) if rss.isdigit() else None
        if re.search(r"/scripts/fish_engine\.py\s+--config\s", cmd + " ") or \
                re.search(r"/scripts/fishctl\.py\s+(?:--config\s+\S+\s+)?exec-engine(\s|$)", cmd):
            out.append({"pid": pid, "rss_gib": rss_gib, "kind": "fish",
                        "shim": "exec-engine" in cmd, "cmd": cmd[:300]})
            continue
        exe = cmd.split(None, 1)[0]
        is_runtime = any(cmd == s or cmd.startswith(s + " ") for s in sigs) or \
            os.path.basename(exe) in RUNTIME_BASENAMES
        if not is_runtime or re.search(r"\s--(version|help)(\s|$)", cmd):
            continue
        port = _argument(cmd, "--port")
        model_arg = _argument(cmd, "--model")
        mid = model_paths.get(os.path.realpath(model_arg)) if model_arg else None
        out.append({"pid": pid, "rss_gib": rss_gib, "kind": "qwen",
                    "runtime": os.path.basename(exe), "port": int(port) if port and port.isdigit() else None,
                    "model": mid, "cmd": cmd[:300]})
    return out


def list_processes():
    _, out, _ = sh(["ps", "-Ao", "pid=,rss=,command="])
    return out.splitlines()


# ------------------------------------------------------------- observation
def _launchd(label):
    import qwenctl as q
    return q.obs_launchd({"uid": os.getuid(), "label": label})


def _socket(port):
    import qwenctl as q
    return q.obs_socket({"port": port})


def observe_qwen(reg, procs, boot_sec=None):
    """Per-slot presence of a Qwen model, read-only."""
    import qwenctl as q
    boot_sec = boot_time_sec() if boot_sec is None else boot_sec
    slots, ports = [], set()
    for slot, env in nm.SLOT_CONFIGS.items():
        cfg = q.load_config(env)
        ports.add(cfg["port"])
        lease = q.read_lease(cfg)
        ld = q.obs_launchd(cfg)
        sock = q.obs_socket(cfg)
        mine = [p for p in procs if p["kind"] == "qwen" and p["port"] == cfg["port"]]
        running = (ld.get("state") or "").strip() == "running" or bool(ld.get("pid"))
        observed = bool(mine) or sock["listening"] or running
        st = lease["state"]
        entry = {"slot": slot, "lease_state": st, "observed": observed, "pid": None,
                 "model": None, "phase": "down", "present": False, "source": None}
        if mine:
            # Identified by its --model argument. None means it matched no
            # registered artifact: treated as prohibited (fail closed).
            entry.update(present=True, pid=mine[0]["pid"], model=mine[0]["model"],
                         source="process",
                         phase={"STARTING": "starting", "STOPPING": "stopping"}.get(st, "up"))
        elif observed:
            # The job runs (or the port is bound) but no runtime process yet:
            # the exec-slot shim is about to exec the leased or selected model.
            leased = lease.get("model") if st != "STOPPED" else None
            entry.update(present=True, model=leased or nm.read_selection(reg)[0],
                         source="launchd" if running else "socket",
                         phase="stopping" if st == "STOPPING" else "starting")
        elif st == "STARTING":
            dl = lease.get("start_deadline")
            stale = lock_is_free(cfg["lock"]) or (dl is not None and time.time() > dl)
            if not stale:
                entry.update(present=True, phase="starting", model=lease.get("model"),
                             source="lease")
        if not entry["present"] and slot == "A":
            rec = fc.read_json(os.path.join(nm.ROOT, "state", "qwen-a-desired.json"))
            if effective_desired(rec, boot_sec) == "RUNNING":
                sel, _ = nm.read_selection(reg)
                entry.update(present=True, phase="recovering", model=sel, source="desired")
        slots.append(entry)
    for p in procs:
        if p["kind"] == "qwen" and p["port"] not in ports:
            slots.append({"slot": None, "lease_state": None, "observed": True, "pid": p["pid"],
                          "model": p["model"], "phase": "up", "present": True,
                          "source": "unmanaged process"})
    return slots


def observe_fish(cfg, procs, boot_sec=None):
    """Presence of the Fish service, read-only."""
    boot_sec = boot_time_sec() if boot_sec is None else boot_sec
    if cfg is not None:
        p = fc.paths(cfg)
        label, port = cfg["service"]["label"], int(cfg["service"]["port"])
    else:
        sd = os.path.join(nm.ROOT, "state", "fish")
        p = {"state": os.path.join(sd, "state.json"), "lock": os.path.join(sd, "fish.lock"),
             "desired": os.path.join(sd, "desired.json")}
        label, port = FISH_DEFAULT_LABEL, FISH_DEFAULT_PORT
    rec = fc.read_json(p["state"]) or {}
    st = rec.get("state") or "STOPPED"
    mine = [x for x in procs if x["kind"] == "fish"]
    ld = _launchd(label)
    running = (ld.get("state") or "").strip() == "running" or bool(ld.get("pid"))
    sock = _socket(port)
    ours_listening = sock["listening"] and (sock["pid"] in [x["pid"] for x in mine]
                                            or sock["pid"] == ld.get("pid"))
    out = {"present": False, "phase": "down", "state": st, "pids": [x["pid"] for x in mine],
           "model": rec.get("model"), "source": None,
           "rss_gib": round(sum(x["rss_gib"] or 0 for x in mine), 2) if mine else None}
    if mine or running or ours_listening:
        out.update(present=True, source="process" if mine else "launchd",
                   phase={"STARTING": "starting", "STOPPING": "stopping"}.get(st, "up"))
        return out
    if st == "STARTING":
        dl = rec.get("start_deadline")
        if not (lock_is_free(p["lock"]) or (dl is not None and time.time() > dl)):
            out.update(present=True, phase="starting", source="record")
            return out
    if effective_desired(fc.read_json(p["desired"]), boot_sec) == "RUNNING":
        out.update(present=True, phase="recovering", source="desired")
    return out


# --------------------------------------------------------------- memory
def gpu_allocation_bytes():
    _, out, _ = sh(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "AGXAccelerator"])
    m = re.search(r'"Alloc system memory"=(\d+)', out)
    return int(m.group(1)) if m else None


def gpu_working_set_limit(cfg, total):
    rec = None
    if cfg is not None:
        rec = fc.read_install_record(cfg, fc.model(cfg, cfg["default_model"])["runtime"])
    lim = ((rec or {}).get("metal") or {}).get("max_recommended_working_set_size")
    return int(lim) if lim else int(total * 0.75)


def pressure_level():
    _, out, _ = sh(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"])
    return PRESSURE_LEVELS.get(out.strip(), out.strip() or "unknown")


def mmap_weight_bytes(reg, model_id):
    """Bytes a resident llama.cpp model maps from disk (file cache that the
    engine formula would otherwise count as free)."""
    m = (reg.get("models") or {}).get(model_id)
    if not m or m.get("runtime") != "llama.cpp":
        return 0
    try:
        with open(nm.abspath(m["manifest"])) as f:
            return int(json.load(f).get("total_bytes") or 0)
    except (OSError, ValueError):
        return 0


def measure_memory(reg, qwen_slots, cfg=None, counts=None, total=None, gpu=None,
                   pressure=None):
    counts = nm.vm_counts() if counts is None else counts
    total = nm.host_memory_bytes() if total is None else total
    if not counts or not total:
        return None
    mmap = sum(mmap_weight_bytes(reg, s["model"]) for s in qwen_slots
               if s["present"] and s["phase"] in ("up", "stopping"))
    gpu = gpu_allocation_bytes() if gpu is None else gpu
    return {"total_bytes": total,
            "engine_available_bytes": nm.engine_available_bytes(total, counts),
            "mmap_resident_bytes": mmap,
            "available_bytes": max(0, nm.engine_available_bytes(total, counts) - mmap),
            "pressure": pressure_level() if pressure is None else pressure,
            "gpu_alloc_bytes": gpu,
            "gpu_limit_bytes": gpu_working_set_limit(cfg, total)}


def memory_check(name, required_gib, mem, adm):
    """None if the newcomer fits, else (code, sentence)."""
    if mem is None:
        return "memory", "free memory could not be measured, so the start was refused"
    if adm.get("require_pressure") and mem["pressure"] != adm["require_pressure"]:
        return "pressure", (f"memory pressure is {mem['pressure']} right now; {name} only "
                            f"starts while it is {adm['require_pressure']}")
    avail = mem["available_bytes"] / GIB
    need = float(required_gib) + float(adm["reserve_gib"])
    if avail < need:
        return "memory", (f"not enough free memory for {name}: it needs about "
                          f"{float(required_gib):.1f} GiB plus {float(adm['reserve_gib']):.0f} GiB "
                          f"of headroom ({need:.1f} GiB) and {avail:.1f} GiB is free")
    if mem.get("gpu_alloc_bytes") is not None:
        after = mem["gpu_alloc_bytes"] / GIB + float(required_gib)
        limit = mem["gpu_limit_bytes"] / GIB - float(adm["gpu_working_set_margin_gib"])
        if after > limit:
            return "gpu", (f"the GPU working set would reach {after:.1f} GiB with {name} "
                           f"loaded; the limit is {limit:.1f} GiB")
    return None


# -------------------------------------------------------------- decisions
def _decision(ok, code, message, **details):
    return {"ok": ok, "code": code, "message": message, "details": details}


def decide_fish_start(fish_gib, compat, adm, qwen_slots, mem, reg, fish_name="Fish"):
    """Pure: may the Fish speech service start now?"""
    present = [s for s in qwen_slots if s["present"]]
    for s in present:
        rule = rule_for(compat, s["model"])
        where = f"slot {s['slot']}" if s["slot"] else "outside the slots"
        if rule == "prohibited":
            what = display_name(reg, s["model"])
            verb = {"recovering": "set to run (it is recovering)", "starting": "starting",
                    "stopping": "still stopping"}.get(s["phase"], "running")
            return _decision(False, "prohibited",
                             f"{fish_name} can't start while {what} is {verb} ({where}). "
                             f"They are never allowed to run at the same time. Stop the AI "
                             f"model first.", slot=s)
    for s in present:
        if s["phase"] in ("starting", "stopping"):
            return _decision(False, "transition",
                             f"{fish_name} can't start while {display_name(reg, s['model'])} is "
                             f"{s['phase']}. Only one large model loads at a time; try again "
                             f"when it has finished.", slot=s)
    why = memory_check(fish_name, fish_gib, mem, adm)
    if why:
        return _decision(False, why[0], why[1][0].upper() + why[1][1:] + ".", memory=mem,
                         required_gib=fish_gib, alongside=[s["model"] for s in present])
    alongside = [s["model"] for s in present if s["phase"] != "recovering"]
    return _decision(True, "ok", f"{fish_name} fits" + (
        f" next to {', '.join(display_name(reg, m) for m in alongside)}" if alongside else "")
        + ".", memory=mem, required_gib=fish_gib, alongside=alongside)


def decide_qwen_start(model_id, compat, adm, fish, mem_fn, reg, fish_name="the Fish speech model"):
    """Pure (memory measured lazily): may this Qwen model start now?
    With Fish absent there is no new condition: the existing Qwen
    behaviour is unchanged."""
    if not fish["present"]:
        return _decision(True, "ok", "Fish is not running; no shared-resource condition applies.")
    name = display_name(reg, model_id)
    rule = rule_for(compat, model_id)
    verb = {"recovering": "set to run (it is recovering)", "starting": "starting",
            "stopping": "still stopping"}.get(fish["phase"], "running")
    if rule == "prohibited":
        return _decision(False, "prohibited",
                         f"{name} can't start while {fish_name} is {verb}. They are never "
                         f"allowed to run at the same time. Stop Fish in Voice Studio first"
                         + (", or choose Qwen 3.8 27B." if model_id == "flash-next" else "."),
                         fish=fish)
    if fish["phase"] in ("starting", "stopping"):
        return _decision(False, "transition",
                         f"{name} can't start while {fish_name} is {fish['phase']}. Only one "
                         f"large model loads at a time; try again when it has finished.",
                         fish=fish)
    if fish["phase"] == "recovering":
        return _decision(True, "ok", f"{fish_name} is recovering; {name} may run beside it.",
                         fish=fish)
    m = (reg.get("models") or {}).get(model_id) or {}
    req = nm.memory_requirement(reg, model_id) if m else None
    if req is None:
        return _decision(False, "memory", f"{name} has no memory requirement registered, so it "
                         f"can't be admitted next to {fish_name}.", fish=fish)
    mem = mem_fn()
    why = memory_check(name, req["required_bytes"] / GIB, mem, adm)
    if why:
        return _decision(False, why[0], why[1][0].upper() + why[1][1:] + f" while {fish_name} "
                         f"is running.", memory=mem, required_gib=round(req["required_bytes"] / GIB, 1),
                         fish=fish)
    return _decision(True, "ok", f"{name} fits next to {fish_name}.", memory=mem,
                     required_gib=round(req["required_bytes"] / GIB, 1), fish=fish)


# ------------------------------------------------------- entry points
def check_fish_start(model_id=None, ps_lines=None):
    """The Fish controller's admission (call inside admission_lock())."""
    cfg, compat, adm = fish_settings()
    if cfg is None:
        return _decision(False, "error", "the Fish configuration could not be read")
    model_id = model_id or fc.read_selection(cfg)[0]
    m = fc.model(cfg, model_id)
    reg = nm.load_registry()
    procs = classify_processes(ps_lines if ps_lines is not None else list_processes(), reg)
    boot = boot_time_sec()
    slots = observe_qwen(reg, procs, boot)
    mem = measure_memory(reg, slots, cfg)
    d = decide_fish_start(float(m["memory"]["required_gib"]), compat, adm, slots, mem, reg,
                          fish_name=f"{m['display_name']} ({m['variant']})")
    d["details"]["qwen"] = slots
    return d


def check_qwen_start(model_id, ps_lines=None):
    """A Qwen controller's admission (call inside admission_lock())."""
    cfg, compat, adm = fish_settings()
    reg = nm.load_registry()
    procs = classify_processes(ps_lines if ps_lines is not None else list_processes(), reg)
    boot = boot_time_sec()
    fish = observe_fish(cfg, procs, boot)

    def mem_fn():
        return measure_memory(reg, observe_qwen(reg, procs, boot), cfg)
    return decide_qwen_start(model_id, compat, adm, fish, mem_fn, reg)


def backstop_qwen_launch(model_id):
    """exec-slot's last check before execv: the absolute rule only (memory
    admission already happened in the controller, under the lock)."""
    d = check_qwen_start(model_id)
    if not d["ok"] and d["code"] != "prohibited":
        return _decision(True, "ok", "backstop enforces the prohibited pairs only")
    return d


def backstop_fish_launch(model_id=None):
    """exec-engine's last check before execv: the absolute rule only."""
    cfg, compat, adm = fish_settings()
    reg = nm.load_registry()
    procs = classify_processes(list_processes(), reg)
    slots = observe_qwen(reg, procs)
    for s in slots:
        if s["present"] and rule_for(compat, s["model"]) == "prohibited":
            return _decision(False, "prohibited",
                             f"{display_name(reg, s['model'])} is {s['phase']} "
                             f"(slot {s['slot']}); Fish must never run beside it", slot=s)
    return _decision(True, "ok", "no prohibited model present")


def snapshot():
    """For status pages: both sides' presence and the current rules."""
    cfg, compat, adm = fish_settings()
    reg = nm.load_registry()
    procs = classify_processes(list_processes(), reg)
    boot = boot_time_sec()
    return {"fish": observe_fish(cfg, procs, boot), "qwen": observe_qwen(reg, procs, boot),
            "compatibility": compat, "admission": adm}


if __name__ == "__main__":
    print(json.dumps(snapshot(), indent=2))
