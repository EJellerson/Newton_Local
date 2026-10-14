#!/usr/bin/env python3
"""Newton_Local — lifecycle controller for the Fish speech service (docs/18).

Fish runs as its own on-demand user LaunchAgent (com.newton.fish:
RunAtLoad=false, KeepAlive=false), so it is off after every reboot and only
an explicit start loads it. This controller is the only thing that starts or
stops it. It applies the conventions of scripts/qwenctl.py and reuses its
primitives rather than copying them:

    * observation before every decision: launchd job, engine process, socket,
      /health; where the record and reality disagree, reality wins;
    * one flock serialises every mutation; the lock-free cancel marker lets a
      stop win over a loading start; an unlocked status persists only by
      compare-and-swap;
    * a start fails within seconds, with the engine's own reason, when the
      engine exits while loading (qwenctl.wait_ready).

What is Fish-specific: the shared resource policy (resource_policy.py) is
decided and the STARTING reservation written under the shared admission
lock, so Fish and a Qwen model can never both pass admission into an
incompatible pair; the engine gets a fresh bearer token per start; and a
start only counts as ready once the engine's identity (model, generation,
manifest, runtime commit, launchd pid, socket owner) has been verified.

DESIRED STATE (state/fish/desired.json, boot-scoped like Qwen-A's)
    This controller is its only writer, whichever entry point is used:
    stop and cancel record STOPPED together with the cancel marker; a
    successful start or restart records RUNNING unless a stop arrived while
    it ran; a failed start records STOPPED (except --recovery, the
    watchdog's retry, which leaves it alone). Newton AI Control's watchdog
    restarts Fish only while it reads RUNNING for this boot, so a stop from
    the terminal is never undone either, and a reboot lands on OFF.

COMMANDS
    status | start | stop | restart | cancel | health | identity
    verify [--deep] | admission [--qwen MODEL] | select <id> | reconcile | logs
    shim-argv | exec-engine (launchd only)
EXIT CODES
    0 ok  1 error  3 occupied  4 timeout  5 inconsistent (duplicate engine,
    foreign port owner, identity mismatch)  6 model unavailable  8 the engine
    exited while loading  9 cancelled by a stop  10 refused by the resource
    policy  78 exec-engine refused to launch
"""
import argparse
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import fish_config as fc  # noqa: E402
import qwenctl as q  # noqa: E402  (shared lifecycle primitives)
import resource_policy as rp  # noqa: E402

STOPPED, STARTING, READY, STOPPING = "STOPPED", "STARTING", "READY", "STOPPING"
STATES = (STOPPED, STARTING, READY, STOPPING)
OBS_DOWN, OBS_LOADING, OBS_UP = q.OBS_DOWN, q.OBS_LOADING, q.OBS_UP
SHIM_PYTHON = "/usr/bin/python3"
ENGINE_ENV = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
              "HF_HUB_DISABLE_TELEMETRY": "1", "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
              "PYTHONNOUSERSITE": "1", "TOKENIZERS_PARALLELISM": "false"}


def now():
    return time.time()


def iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts or now()))


def die(msg, code=1):
    sys.stderr.write(f"fishctl: {msg}\n")
    sys.exit(code)


# --------------------------------------------------------------- config
def load(path=None):
    try:
        cfg = fc.load_config(path)
    except fc.ConfigError as e:
        die(str(e), 1)
    p = fc.paths(cfg)
    svc = cfg["service"]
    ctl = {"instance": "fish", "label": svc["label"], "uid": os.getuid(),
           "host": svc["host"], "port": int(svc["port"]), "logdir": p["logdir"],
           "lock": p["lock"], "lease": p["state"],
           "start_timeout": int(svc["start_timeout_s"]),
           "stop_grace": int(svc.get("stop_grace_s", 30))}
    for d in (p["statedir"], p["logdir"]):
        os.makedirs(d, mode=0o700, exist_ok=True)
    return cfg, ctl


def log(ctl, event, **fields):
    q.log(ctl, event, **fields)


# ---------------------------------------------------------------- state
def default_state():
    return {"state": STOPPED, "pid": None, "model": None, "generation": 0,
            "start_deadline": None, "controller_pid": None, "last_start": None,
            "last_failure": None, "last_stop": None, "notes": [], "updated_at": None}


def read_state_raw(ctl):
    try:
        with open(ctl["lease"], "rb") as f:
            raw = f.read()
    except OSError:
        return default_state(), None
    try:
        d = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        d = None
    rec = default_state()
    if isinstance(d, dict):
        rec.update({k: v for k, v in d.items() if k in rec})
    if rec["state"] not in STATES:
        rec["state"] = STOPPED
    return rec, raw


def read_state(ctl):
    return read_state_raw(ctl)[0]


def write_state(ctl, rec):
    rec["updated_at"] = now()
    fc.atomic_write_json(ctl["lease"], rec)


# ---------------------------------------------------------- observation
def engine_markers():
    """(engine argv substring, launch-shim regex) for THIS root."""
    shim = re.escape(os.path.join(SCRIPT_DIR, "fishctl.py")) + \
        r"\s+(?:--config\s+\S+\s+)?exec-engine(\s|$)"
    return fc.ENGINE_SCRIPT + " --config ", re.compile(shim)


def classify_engines(ps_lines):
    """Pure: this root's engine and launch-shim processes from ps lines."""
    eng, shim = engine_markers()
    out = []
    for line in ps_lines:
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        cmd = parts[2] + " "
        is_shim = bool(shim.search(cmd))
        if eng in cmd or is_shim:
            rss = parts[1]
            out.append({"pid": int(parts[0]), "shim": is_shim and eng not in cmd,
                        "rss_gib": round(int(rss) / 2**20, 2) if rss.isdigit() else None})
    return out


def obs_engines(ctl=None):
    _, out, _ = q.sh(["ps", "-Ao", "pid=,rss=,command="])
    return classify_engines(out.splitlines())


def http(ctl, path, token=None, method="GET", timeout=3):
    req = urllib.request.Request(f"http://{ctl['host']}:{ctl['port']}{path}", method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read(1 << 20).decode("utf-8") or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read(1 << 16).decode("utf-8") or "null")
        except ValueError:
            return e.code, None
    except Exception:
        return None, None


def probe_health(ctl, timeout=3):
    st, body = http(ctl, "/health", timeout=timeout)
    body = body if isinstance(body, dict) else {}
    return {"ok": bool(st == 200 and body.get("state") == "ready"), "status": st,
            "state": body.get("state"), "busy": bool(body.get("busy")),
            "model_id": body.get("model_id"), "generation": body.get("generation"),
            "error": body.get("error")}


def observe(ctl, health_timeout=3):
    ld = q.obs_launchd(ctl)
    procs = obs_engines(ctl)
    sock = q.obs_socket(ctl)
    hp = probe_health(ctl, health_timeout) if sock["listening"] else {
        "ok": False, "status": None, "state": None, "busy": False}
    if not procs and not sock["listening"]:
        state = OBS_DOWN
    elif hp["ok"]:
        state = OBS_UP
    else:
        state = OBS_LOADING
    pid = None
    if ld["pid"] and any(p["pid"] == ld["pid"] for p in procs):
        pid = ld["pid"]
    elif procs:
        pid = procs[0]["pid"]
    engines = [p for p in procs if not p["shim"]]
    return {"observed_state": state, "pid": pid, "launchd": ld, "processes": procs,
            "duplicate_processes": len(engines) > 1, "socket": sock, "health": hp,
            "rss_gib": next((p["rss_gib"] for p in procs if p["pid"] == pid), None),
            "socket_foreign": bool(sock["listening"] and sock["pid"] not in
                                   [p["pid"] for p in procs] and sock["pid"] != ld["pid"])}


def reconcile(ctl, locked=True):
    """Record vs reality; reality wins. Unlocked callers persist by CAS only."""
    rec, raw = read_state_raw(ctl)
    before = json.dumps(rec, sort_keys=True)
    o = observe(ctl)
    obs, st = o["observed_state"], rec["state"]
    corrections = []

    def move(to, why):
        corrections.append({"from": rec["state"], "to": to, "reason": why})
        rec["state"] = to

    if obs == OBS_DOWN:
        if st == READY:
            move(STOPPED, "engine_vanished")
        elif st == STOPPING:
            move(STOPPED, "stop_completed")
        elif st == STARTING:
            # Abandoned if its deadline passed, or the job is not running and
            # the start that wrote the record is gone: a caller holding the
            # lock knows that already; an observer checks the lock is free.
            ld = o["launchd"]
            gone = (ld.get("state") or "").strip() in ("not running", "") or not ld["bootstrapped"]
            dl = rec.get("start_deadline")
            if (dl and now() > dl) or (gone and (locked or rp.lock_is_free(ctl["lock"]))):
                move(STOPPED, "start_abandoned")
        if rec["state"] == STOPPED:
            rec.update(pid=None, start_deadline=None, controller_pid=None)
    else:
        if st == STOPPED:
            move(STARTING if obs == OBS_LOADING else READY, "engine_adopted")
            rec["start_deadline"] = now() + ctl["start_timeout"] if obs == OBS_LOADING else None
            rec["notes"] = [f"adopted an engine (pid {o['pid']}) that had no record at {iso()}"]
        elif st == STARTING and obs == OBS_UP:
            move(READY, "start_completed")
            rec["start_deadline"] = None
        rec["pid"] = o["pid"]
    if json.dumps(rec, sort_keys=True) != before:
        if locked:
            write_state(ctl, rec)
        else:
            with q.TryLock(ctl) as held:
                if held:
                    try:
                        with open(ctl["lease"], "rb") as f:
                            same = f.read() == raw
                    except OSError:
                        same = raw is None
                    if same:
                        write_state(ctl, rec)
    if corrections:
        log(ctl, "reconcile", corrections=corrections, observed=obs)
    return rec, o, corrections


# ------------------------------------------------------------ memory
def machine_memory():
    try:
        import newton_models as nm
        c, t = nm.vm_counts(), nm.host_memory_bytes()
        free = round(nm.engine_available_bytes(t, c) / 2**30, 2) if c and t else None
    except Exception:
        free = None
    g = rp.gpu_allocation_bytes()
    return {"free_for_models_gib": free, "gpu_alloc_gib": round(g / 2**30, 2) if g else None,
            "pressure": rp.pressure_level()}


# -------------------------------------------------------- desired state
def desired_lock(cfg):
    """Serialises desired-state writes with the cancel marker (milliseconds)."""
    return rp.admission_lock(os.path.join(fc.paths(cfg)["statedir"], "desired.lock"),
                             timeout=15)


def set_desired(cfg, desired, by):
    rec = {"desired": desired, "boot_sec": rp.boot_time_sec(), "set_at": now(),
           "set_at_iso": iso(), "set_by": by}
    fc.atomic_write_json(fc.paths(cfg)["desired"], rec)
    return rec


def read_desired(cfg):
    rec = fc.read_json(fc.paths(cfg)["desired"])
    return {"desired": rp.effective_desired(rec, rp.boot_time_sec()), "record": rec}


def stop_intent(cfg, ctl, by):
    """STOPPED and the cancel marker as one step: a start that is finishing
    either sees the marker (desire_running) or is overwritten by this."""
    with desired_lock(cfg):
        set_desired(cfg, STOPPED, by)
        return q.request_cancel(ctl, by=by)


def desire_running(cfg, ctl, by, t0):
    """After a successful start: RUNNING, unless a stop arrived meanwhile."""
    try:
        with desired_lock(cfg):
            if q.cancel_requested(ctl, t0):
                return False
            set_desired(cfg, "RUNNING", by)
            return True
    except rp.AdmissionBusy as e:
        log(ctl, "desired.not_recorded", error=str(e))
        return False


def start_failed_intent(cfg, ctl, args, code):
    """A failed explicit start must not leave the watchdog retrying it."""
    if code in (0, None) or getattr(args, "recovery", False):
        return
    try:
        with desired_lock(cfg):
            set_desired(cfg, STOPPED, f"{args.by or 'cli'}:start-failed")
    except rp.AdmissionBusy as e:
        log(ctl, "desired.not_recorded", error=str(e))


# --------------------------------------------------------------- token
def write_token(cfg, generation, model_id):
    tok = secrets.token_hex(32)
    fc.atomic_write_json(fc.paths(cfg)["token"], {"token": tok, "generation": generation,
                                                  "model_id": model_id, "written_at": iso()})
    return tok


def read_token(cfg):
    rec = fc.read_json(fc.paths(cfg)["token"]) or {}
    return rec.get("token")


# ---------------------------------------------------------- identity
def verify_identity(cfg, ctl, rec, token, o=None):
    """The engine on the port is the one this start launched: right model,
    generation, manifest and runtime commit, owned by the launchd job."""
    o = o or observe(ctl)
    st, ident = http(ctl, "/v1/identity", token=token, timeout=5)
    problems = []
    if st != 200 or not isinstance(ident, dict):
        return False, {"problems": [f"identity endpoint answered {st}"]}
    m = fc.model(cfg, rec["model"])
    rt = fc.runtime_of(cfg, rec["model"])
    want = {"model_id": rec["model"], "generation": rec["generation"],
            "manifest_sha256": fc.manifest_sha256(cfg, rec["model"]),
            "hf_revision": m["source"]["hf_revision"],
            "path": os.path.realpath(fc.abspath(m["path"]))}
    if not cfg.get("fixture"):
        want["runtime_commit"] = rt["source"]["commit"]
    for k, v in want.items():
        if ident.get(k) != v:
            problems.append(f"{k}: engine says {ident.get(k)!r}, expected {v!r}")
    ld_pid, sock_pid = o["launchd"].get("pid"), o["socket"].get("pid")
    if not (ident.get("pid") and ident.get("pid") == ld_pid == sock_pid):
        problems.append(f"pid mismatch: engine {ident.get('pid')}, launchd {ld_pid}, "
                        f"socket {sock_pid}")
    if not cfg.get("fixture") and ident.get("network_sandbox") != "enforced":
        problems.append(f"network sandbox is {ident.get('network_sandbox')!r}")
    return not problems, {"problems": problems, "identity": ident}


# ------------------------------------------------------------- start
def _supervise_procs(ctl):
    """Alive = the launch shim or the engine (the shim execs into the engine)."""
    return obs_engines(ctl)


def _supervise_health(ctl, timeout=3):
    return probe_health(ctl, timeout)


def refuse_policy(ctl, args, d, model_id):
    log(ctl, "start.refused_policy", code=d["code"], message=d["message"])
    if getattr(args, "json", False):
        print(json.dumps({"ok": False, "error": "policy", "exit_code": rp.EXIT_POLICY,
                          "message": d["message"], "policy_code": d["code"],
                          "model_id": model_id}, indent=2))
    die(d["message"], rp.EXIT_POLICY)


def fail_start(cfg, ctl, args, rec, failure, timeout, err_path, err_off):
    failure = failure or {"kind": "timeout"}
    kind = failure.get("kind", "timeout")
    reason = None
    if kind != "cancelled" and err_path:
        reason = q.diagnose_start_failure(q.read_log_since(err_path, err_off))
    stopped = q._do_stop(ctl, grace=ctl["stop_grace"], procs_fn=_supervise_procs)
    name = fc.model(cfg, rec["model"])["display_name"] if rec.get("model") else "Fish"
    if kind == "cancelled":
        code, message = 9, f"start of {name} was cancelled by a stop request; the engine is stopped"
    elif kind == "exited":
        code = 8
        message = (f"{name} failed to start: the engine exited"
                   + (f" — {reason['summary']}" if reason else " (its log gave no reason)"))
    elif kind == "identity":
        code, message = 5, f"{name} started but failed identity verification: " + \
            "; ".join(failure.get("problems", []))
    else:
        code = 4
        message = f"{name} did not become ready within {timeout}s; the engine was stopped"
        if reason:
            message += f" — last engine output: {reason['summary']}"
    if not stopped:
        message += " (WARNING: the engine could not be confirmed stopped)"
    rec.update(state=STOPPED if stopped else STOPPING, pid=None, start_deadline=None,
               controller_pid=None,
               last_failure={"at": iso(), "kind": kind, "message": message, "reason": reason})
    write_state(ctl, rec)
    log(ctl, f"start.{kind}", reason=reason, engine_exit_code=failure.get("last_exit"),
        worker_stopped=stopped)
    if getattr(args, "json", False):
        print(json.dumps({"ok": False, "error": kind, "exit_code": code, "message": message,
                          "model_id": rec.get("model"), "display_name": name,
                          "engine_exit_code": failure.get("last_exit"), "reason": reason,
                          "log": err_path, "worker_stopped": stopped}, indent=2))
    die(message, code)


def start_locked(cfg, ctl, args, t0):
    rec, o, _ = reconcile(ctl)
    if o["duplicate_processes"]:
        die(f"refusing to start: several engine processes exist "
            f"({[p['pid'] for p in o['processes']]}); run: fishctl stop", 5)
    if rec["state"] == STOPPING:
        dl = now() + ctl["stop_grace"] + 15
        while now() < dl and observe(ctl)["observed_state"] != OBS_DOWN:
            time.sleep(0.5)
        rec, o, _ = reconcile(ctl)
    if rec["state"] == STARTING:
        timeout = args.timeout or ctl["start_timeout"]
        ok, _, _, failure = q.wait_ready(ctl, now() + timeout,
                                         runs_before=q._runs_before_adopted_start(o),
                                         cancel_since=t0, procs_fn=_supervise_procs,
                                         health_fn=_supervise_health)
        rec, o, _ = reconcile(ctl)
        if (failure or {}).get("kind") == "cancelled" or (not ok and rec["state"] != READY):
            fail_start(cfg, ctl, args, rec, failure, timeout,
                       q._stderr_path(ctl, o["launchd"]), None)
    if rec["state"] == READY:
        good, detail = verify_identity(cfg, ctl, rec, read_token(cfg))
        if good:
            log(ctl, "start.already_running", pid=rec["pid"])
            return result(cfg, ctl, args, rec, t0, started=False, note="already running")
        die("an engine is running but failed identity verification: "
            + "; ".join(detail["problems"]) + " — run: fishctl stop", 5)

    # ---- STOPPED: a cold start
    if not o["launchd"]["bootstrapped"]:
        die(f"launchd job {ctl['label']} is not bootstrapped in gui/{ctl['uid']}; "
            f"run scripts/install-fish.sh", 1)
    if o["socket"]["listening"]:
        die(f"port {ctl['port']} is held by another process; refusing to start", 5)
    model_id, _ = fc.read_selection(cfg)
    check = fc.check_model(cfg, model_id)
    if not check["available"]:
        log(ctl, "start.refused_unavailable", model=model_id, reasons=check["reasons"])
        die(f"refusing to start: {check['display_name']} ({model_id}) is not available — "
            + "; ".join(check["reasons"]), 6)
    pending = q._consume_stale_cancel(ctl, t0)
    if pending is not None:
        log(ctl, "start.cancelled_before_spawn", by=pending.get("by"))
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": "cancelled", "exit_code": 9,
                              "message": "start was cancelled by a stop request before it began"}))
        die("start was cancelled by a stop request before it began", 9)
    timeout = args.timeout or ctl["start_timeout"]
    generation = int(rec.get("generation") or 0) + 1
    token = write_token(cfg, generation, model_id)

    # Decide and reserve atomically: the STARTING record is what a
    # concurrent Qwen admission sees.
    try:
        with rp.admission_lock():
            d = rp.check_fish_start(model_id)
            if not d["ok"]:
                refuse_policy(ctl, args, d, model_id)
            rec.update(state=STARTING, generation=generation, model=model_id, pid=None,
                       start_deadline=now() + timeout, controller_pid=os.getpid(), notes=[])
            write_state(ctl, rec)
    except rp.AdmissionBusy as e:
        refuse_policy(ctl, args, {"code": "busy", "message": str(e)}, model_id)
    mem = d["details"].get("memory") or {}
    log(ctl, "start.begin", generation=generation, model=model_id,
        admission={"available_gib": round(mem.get("available_bytes", 0) / 2**30, 1),
                   "alongside": d["details"].get("alongside")})

    ld0 = q.obs_launchd(ctl)
    err_path = q._stderr_path(ctl, ld0)
    err_off = q.log_size(err_path)
    t_kick = now()
    rc, msg = q._kickstart(ctl)
    if rc != 0:
        rec.update(state=STOPPED, controller_pid=None, start_deadline=None)
        write_state(ctl, rec)
        die(f"launchctl kickstart failed (rc={rc}): {msg}", 1)
    ok, t_spawn, t_ready, failure = q.wait_ready(
        ctl, t_kick + timeout, runs_before=ld0.get("runs") or 0, cancel_since=t0,
        procs_fn=_supervise_procs, health_fn=_supervise_health)
    if not ok:
        fail_start(cfg, ctl, args, rec, failure, timeout, err_path, err_off)
    o2 = observe(ctl)
    rec["pid"] = o2["pid"]
    good, detail = verify_identity(cfg, ctl, rec, token, o2)
    if not good:
        fail_start(cfg, ctl, args, rec, {"kind": "identity", "problems": detail["problems"]},
                   timeout, err_path, err_off)
    _, status = http(ctl, "/v1/status", token=token, timeout=5)
    rec.update(state=READY, start_deadline=None, controller_pid=None, last_failure=None,
               last_start={"kickstart_at": iso(t_kick),
                           "spawn_latency_s": round(t_spawn - t_kick, 2) if t_spawn else None,
                           "load_to_ready_s": round(t_ready - t_kick, 2),
                           "generation": generation,
                           "engine_load": (status or {}).get("load")})
    write_state(ctl, rec)
    log(ctl, "start.ready", pid=rec["pid"], **{k: v for k, v in rec["last_start"].items()
                                              if k != "engine_load"})
    return result(cfg, ctl, args, rec, t0, started=True, note="cold start; model loaded",
                  identity=detail.get("identity"))


def result(cfg, ctl, args, rec, t0, started, note, identity=None):
    res = {"ok": True, "state": rec["state"], "pid": rec["pid"], "model_id": rec["model"],
           "generation": rec["generation"], "started": started, "note": note,
           "wall_s": round(now() - t0, 2), "last_start": rec.get("last_start"),
           "endpoint": fc.endpoint(cfg), "identity": identity}
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"{'STARTED' if started else 'start:'} fish {rec['state']} pid={rec['pid']} "
              f"model={rec['model']} gen={rec['generation']} — {note} ({res['wall_s']}s)")
    return 0


def cmd_start(cfg, ctl, args):
    t0 = now()
    try:
        with q.Lock(ctl, timeout=args.lock_timeout):
            rc = start_locked(cfg, ctl, args, t0)
            desire_running(cfg, ctl, args.by or "cli", t0)
            return rc
    except SystemExit as e:
        start_failed_intent(cfg, ctl, args, e.code)
        raise


def stop_locked(cfg, ctl, args, rec, o):
    before = {"pid": o["pid"], "rss_gib": o["rss_gib"], "memory": machine_memory()}
    prev = rec["state"]
    rec["state"] = STOPPING
    write_state(ctl, rec)
    log(ctl, "stop.begin", pid=o["pid"], previous_state=prev)
    t0 = now()
    ok = q._do_stop(ctl, o, grace=ctl["stop_grace"], procs_fn=_supervise_procs)
    secs = round(now() - t0, 2)
    time.sleep(1.0)                     # let the kernel return the pages
    after = {"memory": machine_memory(), "processes": len(obs_engines(ctl)),
             "socket_listening": q.obs_socket(ctl)["listening"]}
    rec.update(state=STOPPED if ok else STOPPING, pid=None, start_deadline=None,
               controller_pid=None,
               last_stop={"at": iso(), "ok": ok, "stop_s": secs, "before": before, "after": after})
    write_state(ctl, rec)
    log(ctl, "stop.end", ok=ok, stop_s=secs,
        free_before=before["memory"]["free_for_models_gib"],
        free_after=after["memory"]["free_for_models_gib"])
    return ok, rec["last_stop"]


def cmd_stop(cfg, ctl, args):
    stop_intent(cfg, ctl, args.by or "stop")          # stop wins over a loading start
    with q.Lock(ctl, timeout=args.lock_timeout):
        rec, o, _ = reconcile(ctl)
        if rec["state"] == STOPPED and o["observed_state"] == OBS_DOWN:
            out = {"ok": True, "state": STOPPED, "note": "already stopped"}
            print(json.dumps(out) if args.json else "stop: fish already STOPPED (idempotent)")
            return 0
        ok, info = stop_locked(cfg, ctl, args, rec, o)
        out = {"ok": ok, "state": rec["state"], **info}
        print(json.dumps(out, indent=2) if args.json else
              f"{'STOPPED' if ok else 'STOP INCOMPLETE'} fish in {info['stop_s']}s; free for "
              f"models {info['before']['memory']['free_for_models_gib']} -> "
              f"{info['after']['memory']['free_for_models_gib']} GiB")
        return 0 if ok else 1


def cmd_restart(cfg, ctl, args):
    t0 = now()
    try:
        with q.Lock(ctl, timeout=args.lock_timeout):
            rec, o, _ = reconcile(ctl)
            if rec["state"] != STOPPED or o["observed_state"] != OBS_DOWN:
                ok, _ = stop_locked(cfg, ctl, args, rec, o)
                if not ok:
                    die("restart: could not stop the running engine; not starting another", 1)
            rc = start_locked(cfg, ctl, args, t0)
            desire_running(cfg, ctl, args.by or "cli", t0)
            return rc
    except SystemExit as e:
        start_failed_intent(cfg, ctl, args, e.code)
        raise


def cmd_cancel(cfg, ctl, args):
    rec = stop_intent(cfg, ctl, args.by or "cancel")
    st = read_state(ctl)["state"]
    out = {"ok": True, "cancel_requested_at": rec["requested_at_iso"], "state": st,
           "start_in_progress": st == STARTING}
    print(json.dumps(out) if args.json else f"CANCEL requested for fish (state {st})")
    return 0


def cmd_status(cfg, ctl, args):
    rec, o, corr = reconcile(ctl, locked=False)
    model_id = rec.get("model") or fc.read_selection(cfg)[0]
    if model_id not in cfg["models"]:
        model_id = fc.read_selection(cfg)[0]
    m = fc.model(cfg, model_id)
    out = {"service": "fish", "state": rec["state"], "observed_state": o["observed_state"],
           "pid": rec["pid"] or o["pid"], "rss_gib": o["rss_gib"], "generation": rec["generation"],
           "model_id": model_id, "display_name": m["display_name"], "variant": m["variant"],
           "selected_model": fc.read_selection(cfg)[0], "endpoint": fc.endpoint(cfg),
           "health": o["health"], "launchd": o["launchd"], "socket": o["socket"],
           "duplicate_processes": o["duplicate_processes"], "last_start": rec.get("last_start"),
           "last_failure": rec.get("last_failure"), "last_stop": rec.get("last_stop"),
           "notes": rec.get("notes"), "corrections": corr, "desired": read_desired(cfg),
           "memory": machine_memory(), "engine": None}
    if o["observed_state"] == OBS_UP:
        st, body = http(ctl, "/v1/status", token=read_token(cfg), timeout=3)
        out["engine"] = body if st == 200 else {"error": f"status answered {st}"}
    if args.admission:
        try:
            out["policy"] = rp.snapshot()
            if rec["state"] == STOPPED and o["observed_state"] == OBS_DOWN:
                out["admission"] = rp.check_fish_start(model_id)
        except Exception as e:                       # status must never die
            out["policy_error"] = f"{type(e).__name__}: {e}"
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"fish  state={out['state']} observed={out['observed_state']}"
              + (f" pid={out['pid']}" if out["pid"] else "")
              + f"  {out['display_name']} ({out['variant']})  gen={out['generation']}")
        print(f"  endpoint  {out['endpoint']}  health={o['health'].get('state')}"
              + (" busy" if o["health"].get("busy") else ""))
        print(f"  launchd   state={o['launchd']['state']} bootstrapped="
              f"{o['launchd']['bootstrapped']} runs={o['launchd']['runs']}")
        if out["rss_gib"] is not None:
            print(f"  rss       {out['rss_gib']} GiB")
        mm = out["memory"]
        print(f"  machine   free for models {mm['free_for_models_gib']} GiB, GPU alloc "
              f"{mm['gpu_alloc_gib']} GiB, pressure {mm['pressure']}")
        if out.get("admission"):
            a = out["admission"]
            print(f"  admission {'would start' if a['ok'] else 'REFUSED'}: {a['message']}")
        if rec.get("last_failure"):
            print(f"  last failure  {rec['last_failure']['message']}")
    return 0


def cmd_health(cfg, ctl, args):
    o = observe(ctl)
    print(json.dumps(o["health"]) if args.json else f"health {o['health']}")
    return 0 if o["health"]["ok"] else 1


def cmd_identity(cfg, ctl, args):
    rec = read_state(ctl)
    good, detail = verify_identity(cfg, ctl, rec, read_token(cfg)) if rec.get("model") else \
        (False, {"problems": ["no model has been started"]})
    out = {"verified": good, **detail}
    print(json.dumps(out, indent=2, default=str))
    return 0 if good else 1


def cmd_verify(cfg, ctl, args):
    ids = [args.model_id] if args.model_id else list(cfg["models"])
    rc = 0
    for mid in ids:
        c = fc.check_model(cfg, mid, deep=args.deep)
        w, r = c["weights"], c["runtime"]
        print(f"{mid}: {'AVAILABLE' if c['available'] else 'UNAVAILABLE'}  "
              f"({c['display_name']}, {c['variant']})")
        print(f"  weights  {w['files_ok']}/{w['files_expected']} files ok, "
              f"{w['bytes_present']:,}/{w['bytes_expected']:,} bytes"
              + ("  (sha256/blob verified)" if args.deep and w["ok"] else ""))
        for p in w["problems"][:8]:
            print(f"           ! {p}")
        print(f"  runtime  {r['runtime']} commit={r['commit']} "
              f"{'ok' if r['ok'] else 'NOT USABLE'} {r['packages']}")
        for p in r["reasons"]:
            print(f"           ! {p}")
        if not c["available"]:
            rc = 6
    return rc


def cmd_admission(cfg, ctl, args):
    d = rp.check_qwen_start(args.qwen) if args.qwen else rp.check_fish_start()
    print(json.dumps(d, indent=2, default=str) if args.json else
          f"{'ADMIT' if d['ok'] else 'REFUSE'} ({d['code']}): {d['message']}")
    return 0 if d["ok"] else rp.EXIT_POLICY


def cmd_select(cfg, ctl, args):
    with q.Lock(ctl, timeout=10):
        rec, o, _ = reconcile(ctl)
        if rec["state"] != STOPPED or o["observed_state"] != OBS_DOWN:
            die("the Fish model can only be changed while Fish is stopped", 3)
        c = fc.check_model(cfg, args.model_id)
        if not c["available"]:
            die(f"{args.model_id} is not available: " + "; ".join(c["reasons"]), 6)
        fc.write_selection(cfg, args.model_id, by=args.by)
        log(ctl, "select", model=args.model_id, by=args.by)
    print(f"selected {args.model_id}; nothing was started")
    return 0


def cmd_reconcile(cfg, ctl, args):
    with q.Lock(ctl, timeout=args.lock_timeout):
        rec, o, corr = reconcile(ctl)
    print(json.dumps({"state": rec["state"], "observed": o["observed_state"],
                      "corrections": corr}, indent=2))
    return 0


def cmd_logs(cfg, ctl, args):
    for name in ("controller.log", "stderr.log", "stdout.log"):
        p = os.path.join(ctl["logdir"], name)
        print(f"\n=== {p} (last {args.lines}) ===")
        _, out, _ = q.sh(["tail", "-n", str(args.lines), p])
        print(out.rstrip() or "  (empty or missing)")
    return 0


def shim_argv():
    return [SHIM_PYTHON, os.path.join(SCRIPT_DIR, "fishctl.py"), "exec-engine"]


def cmd_shim_argv(cfg, ctl, args):
    print("\n".join(shim_argv()))
    return 0


def cmd_exec_engine(cfg, ctl, args):
    """The LaunchAgent entry point: gate, then REPLACE this process with the
    sandboxed engine, so the pid launchd tracks is the engine itself."""
    model_id, source = fc.read_selection(cfg)
    check = fc.check_model(cfg, model_id)
    argv = fc.engine_argv(cfg, fc.model(cfg, model_id)["runtime"])
    if cfg.get("fixture") and (cfg.get("fake") or {}).get("enabled"):
        argv.append("--fake-model")
    log(ctl, "exec_engine", model=model_id, selection=source, available=check["available"],
        reasons=check["reasons"], argv=argv)
    if not check["available"]:
        sys.stderr.write(f"fishctl exec-engine: refusing to launch {model_id}: "
                         + "; ".join(check["reasons"]) + "\n")
        return 78
    if not read_token(cfg):
        sys.stderr.write("fishctl exec-engine: refusing to launch: no engine token; "
                         "start Fish with fishctl, not launchctl\n")
        return 78
    try:
        d = rp.backstop_fish_launch(model_id)
    except Exception as e:                          # fail closed
        d = {"ok": False, "message": f"resource policy check failed: {type(e).__name__}: {e}"}
    if not d["ok"]:
        sys.stderr.write(f"fishctl exec-engine: refusing to launch {model_id}: {d['message']}\n")
        return 78
    env = dict(os.environ)
    env.update(ENGINE_ENV)
    env["HF_HOME"] = os.path.join(fc.paths(cfg)["statedir"], "hf-home")
    sys.stdout.flush()
    sys.stderr.flush()
    os.chdir(fc.ROOT)
    os.execve(argv[0], argv, env)


def main():
    ap = argparse.ArgumentParser(prog="fishctl", description="Fish speech service lifecycle")
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--json", action="store_true")
        p.add_argument("--lock-timeout", type=int, default=300)
        return p
    s = common(sub.add_parser("status"))
    s.add_argument("--admission", action="store_true",
                   help="include the shared policy view and a start preview")
    for name in ("start", "restart"):
        p = common(sub.add_parser(name))
        p.add_argument("--timeout", type=int, default=None)
        p.add_argument("--by", default=None)
        p.add_argument("--recovery", action="store_true",
                       help="a watchdog retry: a failure leaves the desired state alone")
    for name in ("stop", "cancel"):
        p = common(sub.add_parser(name))
        p.add_argument("--by", default=None)
    common(sub.add_parser("health"))
    common(sub.add_parser("identity"))
    p = common(sub.add_parser("verify"))
    p.add_argument("model_id", nargs="?")
    p.add_argument("--deep", action="store_true")
    p = common(sub.add_parser("admission"))
    p.add_argument("--qwen", default=None, help="preview a Qwen start of this model instead")
    p = common(sub.add_parser("select"))
    p.add_argument("model_id")
    p.add_argument("--by", default="cli")
    common(sub.add_parser("reconcile"))
    p = common(sub.add_parser("logs"))
    p.add_argument("-n", "--lines", type=int, default=40)
    common(sub.add_parser("shim-argv"))
    common(sub.add_parser("exec-engine"))
    args = ap.parse_args()
    cfg, ctl = load(args.config)
    fn = {"status": cmd_status, "start": cmd_start, "stop": cmd_stop, "restart": cmd_restart,
          "cancel": cmd_cancel, "health": cmd_health, "identity": cmd_identity,
          "verify": cmd_verify, "admission": cmd_admission, "select": cmd_select,
          "reconcile": cmd_reconcile, "logs": cmd_logs, "shim-argv": cmd_shim_argv,
          "exec-engine": cmd_exec_engine}[args.cmd]
    sys.exit(fn(cfg, ctl, args) or 0)


if __name__ == "__main__":
    main()
