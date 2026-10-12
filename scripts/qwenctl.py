#!/usr/bin/env python3
"""
Newton_Local — deterministic lifecycle controller for the local Qwen instances.

Commission #2 built this for the Qwen-B elastic worker. Commission #4
generalised it: the SAME controller now drives any instance described by a
config env file (--config, default config/qwen-b.env), and the canonical
production instance (Qwen-A, config/qwen-a-controller.env) gains explicit
start/restart verbs with production semantics — no lease owner, no idle TTL,
and a refusal to coexist with a peer instance. Qwen-B behaviour is unchanged.

This is ORDINARY INFRASTRUCTURE CODE. The model never decides when it starts,
when it stops, which launchctl verbs run, or whether another worker should
exist. Every decision below is a plain state transition over observed facts.

GROUND TRUTH
    Controller state is never trusted on its own. Before any decision the
    controller observes reality from four independent sources:

        launchd job state   launchctl print gui/<uid>/<label>
        process existence   ps, matched on the exact llama-server argv
        listening socket    lsof on 127.0.0.1:<port>
        service health      GET /health, GET /props

    The recorded lease is then reconciled against that observation. Where they
    disagree, reality wins and the correction is logged.

LIFECYCLE
    STOPPED -> STARTING -> READY -> BUSY -> IDLE -> STOPPING -> STOPPED
                                     ^        |
                                     +--------+   (re-acquire inside idle TTL,
                                                   no model reload)

COMMANDS
    status      report reconciled lifecycle state (read-only)
    health      probe the worker's HTTP health/identity
    start       ensure the instance is running, ownerless (production verb;
                refuses while another Qwen instance is active)
    restart     stop, confirm termination, then start — never two instances
    acquire     ensure a READY worker and take the lease (starts B if needed)
    release     give the lease back; worker goes IDLE and starts its TTL
    terminate   stop the worker now and return to STOPPED (idempotent when
                already stopped); first cancels any start still loading
    cancel      ask a start/acquire in progress (or queued) to abort; lock-free,
                returns at once (terminate = cancel + stop)
    reconcile   force reconciliation, handle orphans and stale leases
    reap        terminate the worker if its idle TTL has expired (a no-op for
                instances whose idle TTL is disabled)
    heartbeat   refresh the activity timestamp of a held lease
    logs        tail the worker + controller logs

EXIT CODES
    0 ok   1 error   3 lease conflict   4 timeout   5 inconsistent (duplicates
    or peer instance)   6 selected model unavailable   7 restart would switch
    the model   8 the engine exited while loading (reason taken from its log)
    9 start cancelled by an operator stop

START SUPERVISION (2026-10-12 incident fixes)
    * A start no longer only polls /health: it also watches the launchd job,
      and fails within about a second when the engine exits while loading
      (e.g. mlx-serve refusing for lack of memory) instead of sitting out
      the whole start timeout.
    * The failure carries the engine's own reason, read from the slot's
      launchd stderr log written since the kickstart.
    * Stop wins over start: `terminate` (and `cancel`) drop a cancel marker
      that an in-progress start checks on every poll, so a stop never queues
      behind a load for the full start timeout.
    * Unlocked observers (`status`) persist a reconciliation only by
      compare-and-swap, so a stale poll can never overwrite a record that a
      lock holder wrote after the poll read it.

MODEL / RUNTIME (Commission #5)
    A and B are EXECUTION SLOTS. Which model (and therefore which runtime —
    llama.cpp or mlx-serve) a slot runs is resolved at start time from the
    global selection via scripts/newton_models.py; the LaunchAgents exec
    `newton_models.py exec-slot <slot>`. Process detection, health probing and
    peer (singleton) detection are runtime-agnostic: any registered runtime
    executable occupies the one large-model resource.
"""
import argparse
import copy
import errno
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.environ.get(
    "QWENCTL_CONFIG",
    os.path.normpath(os.path.join(SCRIPT_DIR, "..", "config", "qwen-b.env")))

sys.path.insert(0, SCRIPT_DIR)
import newton_models as nm  # noqa: E402  (registry, selection, plans, health)

_REGISTRY = None


def registry():
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = nm.load_registry()
    return _REGISTRY

STOPPED, STARTING, READY, BUSY, IDLE, STOPPING = (
    "STOPPED", "STARTING", "READY", "BUSY", "IDLE", "STOPPING")
LIFECYCLE = [STOPPED, STARTING, READY, BUSY, IDLE, STOPPING]

# Observed (physical) states, distinct from the recorded (logical) lifecycle.
OBS_DOWN, OBS_LOADING, OBS_UP = "DOWN", "LOADING", "UP"


# --------------------------------------------------------------- configuration
def load_config(path=CONFIG):
    """Source the env file in bash so nested `. qwen-a.env` inheritance works."""
    r = subprocess.run(["bash", "-c", f'set -a; . "{path}"; set +a; /usr/bin/env'],
                       capture_output=True, text=True)
    if r.returncode != 0:
        die(f"cannot source {path}: {r.stderr.strip()}")
    env = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k] = v

    def ctl(name, default=None):
        # Generic controller keys (CTL_*) with fallback to the legacy
        # Commission-#2 names (QWEN_B_*), so config/qwen-b.env keeps working
        # unchanged while other instances use the generic spelling.
        for k in (f"CTL_{name}", f"QWEN_B_{name}"):
            if k in env:
                return env[k]
        return default

    # Slot identity only. Model/runtime come from the registry + selection.
    need_runtime = ["ALIAS", "HOST", "PORT", "CTX"]
    missing = [k for k in need_runtime if k not in env]
    need_ctl = ["LABEL", "LOGDIR", "LEASE", "LOCK",
                "IDLE_TTL", "START_TIMEOUT", "LEASE_MAX_AGE"]
    missing += [f"CTL_{k} (or QWEN_B_{k})" for k in need_ctl if ctl(k) is None]
    if missing:
        die(f"{path} is missing required keys: {missing}")
    slot = ctl("SLOT") or ("A" if ctl("LABEL", "").endswith(".a") else "B")
    return {
        # legacy llama.cpp fields, kept for the manual runners; NOT used to
        # decide what a slot launches any more
        "llama_bin": env.get("LLAMA_BIN"), "model": env.get("MODEL"),
        "mmproj": env.get("MMPROJ"), "alias": env["ALIAS"], "host": env["HOST"],
        "port": int(env["PORT"]), "ctx": int(env["CTX"]),
        "slot": slot,
        "instance": ctl("INSTANCE", env["ALIAS"]),
        "label": ctl("LABEL"), "logdir": ctl("LOGDIR"),
        "lease": ctl("LEASE"), "lock": ctl("LOCK"),
        # idle_ttl <= 0 disables the idle TTL entirely (production instances
        # run until explicitly stopped; no reaper ever collects them).
        "idle_ttl": int(ctl("IDLE_TTL")),
        "start_timeout": int(ctl("START_TIMEOUT")),
        "lease_max_age": int(ctl("LEASE_MAX_AGE")),
        "uid": os.getuid(),
    }


def die(msg, code=1):
    sys.stderr.write(f"qwenctl: {msg}\n")
    sys.exit(code)


def now():
    return time.time()


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts)) if ts else None


# ------------------------------------------------------------------- logging
def log(cfg, event, **fields):
    os.makedirs(cfg["logdir"], exist_ok=True)
    rec = {"ts": round(now(), 3), "ts_iso": iso(now()), "event": event}
    rec.update(fields)
    try:
        with open(os.path.join(cfg["logdir"], "controller.log"), "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------- lease
def default_lease(cfg):
    return {
        "state": STOPPED,
        "owner": None,
        "pid": None,
        "label": cfg["label"],
        "domain": f"gui/{cfg['uid']}",
        "endpoint": f"http://{cfg['host']}:{cfg['port']}",
        "alias": cfg["alias"],
        "acquired_at": None,
        "last_activity": None,
        "idle_since": None,
        "start_deadline": None,
        "stop_deadline": None,
        "idle_ttl_seconds": cfg["idle_ttl"],
        "generation": 0,
        "updated_at": now(),
        "last_start": None,
        "model": None,          # registry id the running process was launched as
        "runtime": None,
        "notes": [],
    }


def _lease_file_bytes(cfg):
    try:
        with open(cfg["lease"], "rb") as f:
            return f.read()
    except OSError:
        return None


def read_lease_raw(cfg):
    """(lease, raw) where raw is the exact file content the lease was parsed
    from (None when the file is absent). raw is the compare-and-swap token an
    unlocked writer must still match before it may persist anything."""
    raw = _lease_file_bytes(cfg)
    try:
        d = json.loads(raw.decode("utf-8")) if raw is not None else None
    except (ValueError, UnicodeDecodeError):
        d = None
    if not isinstance(d, dict):
        return default_lease(cfg), raw
    base = default_lease(cfg)
    base.update({k: v for k, v in d.items() if k in base})
    # Routing configuration can change independently of the last lease.
    base["alias"] = cfg["alias"]
    if base["state"] not in LIFECYCLE:
        base["state"] = STOPPED
    return base, raw


def read_lease(cfg):
    return read_lease_raw(cfg)[0]


def write_lease(cfg, lease):
    lease["updated_at"] = now()
    os.makedirs(os.path.dirname(cfg["lease"]), exist_ok=True)
    # Per-process tmp name: an unlocked status reconcile racing a locked
    # mutation must never steal the other's rename source. The rename is
    # atomic and last-writer-wins, which reconciliation already assumes.
    tmp = f"{cfg['lease']}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(lease, f, indent=2)
        f.write("\n")
    os.replace(tmp, cfg["lease"])


class Lock:
    """Exclusive controller lock. Serialises every mutating operation so two
    concurrent acquires can never both start a worker."""

    def __init__(self, cfg, timeout=300):
        self.path = cfg["lock"]
        self.timeout = timeout
        self.fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "a+")
        deadline = now() + self.timeout
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if now() >= deadline:
                    self.fh.close()
                    die(f"timed out after {self.timeout}s waiting for the "
                        f"controller lock ({self.path})", 4)
                time.sleep(0.25)

    def __exit__(self, *a):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()


class TryLock:
    """Non-blocking attempt at the controller lock. `with TryLock(cfg) as
    held:` — held is False when a mutation owns the lock right now."""

    def __init__(self, cfg):
        self.path = cfg["lock"]
        self.fh = None
        self.held = False

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.held = True
        except OSError as e:
            if e.errno not in (errno.EACCES, errno.EAGAIN):
                self.fh.close()
                raise
        return self.held

    def __exit__(self, *a):
        try:
            if self.held:
                fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()


# --------------------------------------------------------------- observation
def sh(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def obs_launchd(cfg):
    rc, out, _ = sh(["launchctl", "print", f"gui/{cfg['uid']}/{cfg['label']}"])
    if rc != 0:
        return {"bootstrapped": False, "state": None, "pid": None,
                "runs": None, "last_exit": None, "stderr_path": None}
    g = lambda pat: (re.search(pat, out, re.M).group(1).strip()
                     if re.search(pat, out, re.M) else None)
    pid = g(r'^\s*pid\s*=\s*(\d+)')
    runs = g(r'^\s*runs\s*=\s*(\d+)')
    return {"bootstrapped": True,
            "state": g(r'^\s*state\s*=\s*(.+)$'),
            "pid": int(pid) if pid else None,
            "runs": int(runs) if runs else None,
            "last_exit": g(r'^\s*last exit code\s*=\s*(.+)$'),
            "stderr_path": g(r'^\s*stderr path\s*=\s*(.+)$')}


def classify_model_processes(cfg, ps_lines, signatures):
    """Split every model-runtime process into THIS slot vs peers.

    Pure decision logic (unit-testable with injected ps output). A process is
    a MODEL PROCESS when its argv starts with ANY registered runtime
    executable (llama-server, mlx-serve, ... — the registry's signatures), so
    the singleton guard is runtime-agnostic. It is OURS when it carries this
    slot's --port; every other model process is a PEER occupying the one
    large-model resource, which the production control path refuses to share.
    """
    ours, peers = [], []
    for line in ps_lines:
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid, rss_kb, cmd = parts
        sig = next((s for s in signatures if cmd == s or cmd.startswith(s + " ")), None)
        if sig is None:
            continue
        if re.search(r'\s--(version|help)(\s|$)', cmd):
            continue          # a --version/--help probe is not a model server
        m_port = re.search(r'--port\s+(\d+)', cmd)
        m_alias = re.search(r'--alias\s+(\S+)', cmd)
        rec = {"pid": int(pid), "rss_gib": round(int(rss_kb) / 2**20, 2),
               "port": int(m_port.group(1)) if m_port else None,
               "alias": m_alias.group(1) if m_alias else None,
               "executable": sig}
        if rec["port"] == cfg["port"]:
            ours.append(rec)
        else:
            peers.append(rec)
    return ours, peers


def signatures():
    """Absolute executable paths of every registered runtime (+ symlink targets)."""
    return nm.runtime_signatures(registry())


def obs_processes(cfg):
    """Every process that IS this slot's model server, matched on argv."""
    rc, out, _ = sh(["ps", "-Ao", "pid=,rss=,command="])
    ours, _ = classify_model_processes(cfg, out.splitlines(), signatures())
    return ours


def obs_peers(cfg):
    """Every model-runtime process that is NOT this slot."""
    rc, out, _ = sh(["ps", "-Ao", "pid=,rss=,command="])
    _, peers = classify_model_processes(cfg, out.splitlines(), signatures())
    return peers


def _fmt_peers(peers):
    return ", ".join(f"pid={p['pid']} alias={p.get('alias')} port={p.get('port')} "
                     f"exe={os.path.basename(p.get('executable') or '?')}"
                     for p in peers)


def obs_socket(cfg):
    rc, out, _ = sh(["lsof", "-nP", f"-iTCP:{cfg['port']}", "-sTCP:LISTEN", "-Fpcn"])
    if rc != 0 or not out.strip():
        return {"listening": False, "pid": None, "name": None}
    pid = name = None
    for line in out.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n"):
            name = line[1:]
    return {"listening": True, "pid": pid, "name": name}


def http_json(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r), r.status
    except Exception:
        return None, None


def obs_health(cfg, timeout=5):
    """Runtime-agnostic readiness: probe with the selected model's runtime
    semantics first, then every other registered runtime (an orphan of a
    different runtime still counts as UP)."""
    reg = registry()
    base = f"http://{cfg['host']}:{cfg['port']}"
    sel, _ = nm.read_selection(reg)
    order = [reg["models"][sel]["runtime"]]
    order += [r for r in reg["runtimes"] if r not in order]
    last = {"ok": False, "status": None, "props": None}
    for rid in order:
        res = nm.probe_health(reg, rid, base, timeout=timeout)
        if res["ok"]:
            res["runtime"] = rid
            return res
        last = res
        if res["status"] is None:        # connection refused: nothing listening
            break
    return last


def observe(cfg, health_timeout=5):
    ld = obs_launchd(cfg)
    rc, ps_out, _ = sh(["ps", "-Ao", "pid=,rss=,command="])
    procs, peers = classify_model_processes(cfg, ps_out.splitlines(), signatures())
    sock = obs_socket(cfg)
    hp = obs_health(cfg, health_timeout)

    if not procs and not sock["listening"]:
        state = OBS_DOWN
    elif hp["ok"]:
        state = OBS_UP
    else:
        state = OBS_LOADING

    pid = None
    if ld["pid"] and any(p["pid"] == ld["pid"] for p in procs):
        pid = ld["pid"]                     # launchd-owned worker is authoritative
    elif procs:
        pid = procs[0]["pid"]

    return {
        "observed_state": state,
        "pid": pid,
        "launchd": ld,
        "processes": procs,
        "peer_processes": peers,
        "duplicate_processes": len(procs) > 1,
        "launchd_owned": bool(ld["pid"] and pid == ld["pid"]),
        "socket": sock,
        "health": hp,
        "rss_gib": next((p["rss_gib"] for p in procs if p["pid"] == pid), None),
    }


# ------------------------------------------------------------- reconciliation
def reconcile(cfg, lease=None, orphan_policy="adopt", health_timeout=5,
              locked=True):
    """Bring the recorded lease into agreement with observed reality.

    Returns (lease, observation, corrections). Never trusts the recorded state
    when observation contradicts it.

    locked=True   the caller holds the controller Lock; the result is written.
    locked=False  an observer such as `status`: destructive orphan policies are
                  downgraded to 'report', and the result is persisted only by
                  compare-and-swap (see _persist_observation), so a stale poll
                  can never overwrite a record a lock holder wrote meanwhile.
    """
    raw = None
    if lease is None:
        lease, raw = read_lease_raw(cfg)
    elif not locked:
        raise ValueError("an unlocked reconcile must read the lease itself")
    before = copy.deepcopy(lease)
    if not locked and orphan_policy == "terminate":
        orphan_policy = "report"
    o = observe(cfg, health_timeout)
    corrections = []
    rec = lease["state"]
    obs = o["observed_state"]

    def move(to, why, **extra):
        corrections.append({"from": rec, "to": to, "reason": why, **extra})
        lease["state"] = to

    # -- invariant: never more than one Qwen-B worker ------------------------
    if o["duplicate_processes"]:
        pids = [p["pid"] for p in o["processes"]]
        keep = o["launchd"]["pid"] if o["launchd_owned"] else None
        if orphan_policy == "terminate":
            for p in pids:
                if p != keep:
                    sh(["kill", "-TERM", str(p)])
                    corrections.append({"action": "killed_duplicate", "pid": p})
            time.sleep(2)
            o = observe(cfg, health_timeout)
        else:
            corrections.append({"action": "duplicate_workers_detected",
                                "pids": pids, "launchd_owned_pid": keep,
                                "policy": "reported (use --orphan-policy "
                                          "terminate to kill non-launchd copies)"})

    obs = o["observed_state"]

    # -- Case B: the controller believed a worker existed; it does not -------
    if obs == OBS_DOWN and rec in (READY, BUSY, IDLE):
        stale_owner = lease["owner"]
        move(STOPPED, "worker_vanished", cleared_owner=stale_owner)
        lease["owner"] = None
        lease["pid"] = None
        lease["acquired_at"] = None
        lease["idle_since"] = None
        lease["notes"] = [f"stale {rec} lease cleared at {iso(now())}"
                          + (f"; owner was {stale_owner}" if stale_owner else "")]

    elif obs == OBS_DOWN and rec == STARTING:
        dl = lease.get("start_deadline")
        ld = o["launchd"]
        # A recorded pid that no longer exists, with launchd reporting the job
        # is not running, is a definitive failed start — there is nothing left
        # to wait for, so do not sit out the remaining deadline.
        died = (lease.get("pid") is not None
                and ld.get("bootstrapped")
                and (ld.get("state") or "").strip() == "not running")
        if died:
            move(STOPPED, "start_failed_worker_process_gone",
                 dead_pid=lease.get("pid"), launchd_state=ld.get("state"))
            lease["owner"] = None
            lease["pid"] = None
            lease["start_deadline"] = None
            lease["notes"] = [f"STARTING worker pid={lease.get('pid')} "
                              f"disappeared and launchd reports 'not running'; "
                              f"state reset to STOPPED at {iso(now())}"]
        elif dl and now() > dl:
            move(STOPPED, "start_failed_no_process_before_deadline")
            lease["owner"] = None
            lease["pid"] = None
            lease["start_deadline"] = None
        # else: still inside the start window; STARTING stands.

    elif obs == OBS_DOWN and rec == STOPPING:
        move(STOPPED, "stop_completed")
        lease["owner"] = None
        lease["pid"] = None
        lease["idle_since"] = None

    # -- Case A / C: a worker is running the controller did not expect ------
    elif obs in (OBS_UP, OBS_LOADING) and rec == STOPPED:
        if orphan_policy == "terminate":
            _do_stop(cfg, o)
            move(STOPPED, "orphan_terminated_by_policy")
        elif orphan_policy == "report":
            corrections.append({"action": "orphan_reported_not_adopted",
                                "pid": o["pid"], "observed": obs})
        else:  # adopt (default, safe: never silently kills work in flight)
            target = IDLE if obs == OBS_UP else STARTING
            move(target, "orphan_adopted")
            lease["owner"] = None
            lease["pid"] = o["pid"]
            lease["idle_since"] = now() if target == IDLE else None
            lease["start_deadline"] = (now() + cfg["start_timeout"]
                                       if target == STARTING else None)
            ttl_note = (f" and is subject to the {cfg['idle_ttl']}s idle TTL"
                        if cfg["idle_ttl"] > 0
                        else "; the idle TTL is disabled for this instance")
            lease["notes"] = [
                f"adopted orphan worker pid={o['pid']} at {iso(now())}; "
                f"no lease existed, so it enters {target}" + ttl_note]

    elif obs == OBS_UP and rec == STARTING:
        move(READY, "start_completed")
        lease["pid"] = o["pid"]
        lease["start_deadline"] = None

    elif obs == OBS_UP and rec == BUSY:
        la = lease.get("last_activity") or lease.get("acquired_at") or 0
        if now() - la > cfg["lease_max_age"]:
            move(IDLE, "busy_lease_abandoned",
                 age_s=round(now() - la, 1), max_age_s=cfg["lease_max_age"])
            lease["notes"] = [f"BUSY lease from owner {lease['owner']} exceeded "
                              f"{cfg['lease_max_age']}s without heartbeat; "
                              f"released to IDLE at {iso(now())}"]
            lease["owner"] = None
            lease["idle_since"] = now()
        lease["pid"] = o["pid"]

    elif obs == OBS_UP and rec in (READY, IDLE):
        lease["pid"] = o["pid"]

    elif obs == OBS_UP and rec == STOPPING:
        dl = lease.get("stop_deadline")
        if dl and now() > dl:
            corrections.append({"action": "stop_deadline_exceeded",
                                "pid": o["pid"]})
        lease["pid"] = o["pid"]

    elif obs == OBS_LOADING and rec in (READY, BUSY, IDLE):
        corrections.append({"action": "health_degraded_while_" + rec.lower(),
                            "pid": o["pid"]})
        lease["pid"] = o["pid"]

    if lease["state"] == STOPPED:
        lease["pid"] = None

    if locked:
        write_lease(cfg, lease)
    else:
        _persist_observation(cfg, raw, before, lease)
    if corrections:
        log(cfg, "reconcile", corrections=corrections,
            recorded=rec, observed=obs, state=lease["state"])
    return lease, o, corrections


def _persist_observation(cfg, raw_before, before, after):
    """Compare-and-swap write for an unlocked reconcile.

    Writes only when the reconciliation changed something, no mutation holds
    the controller lock, and the lease file is still byte-identical to what
    this observer read. On 2026-10-12 a status poll that read the lease just
    before a start wrote STARTING (generation 42) wrote back its stale
    STOPPED/41 copy afterwards; this is the fix. Returns True if written."""
    def strip(d):
        return {k: v for k, v in d.items() if k != "updated_at"}
    if strip(before) == strip(after):
        return False
    state_moved = before.get("state") != after.get("state")
    with TryLock(cfg) as held:
        if not held:
            if state_moved:
                log(cfg, "reconcile.persist_skipped",
                    reason="controller lock held by a mutation",
                    observed_state=after.get("state"))
            return False
        if _lease_file_bytes(cfg) != raw_before:
            if state_moved:
                log(cfg, "reconcile.persist_skipped",
                    reason="lease changed since this observer read it",
                    observed_state=after.get("state"))
            return False
        write_lease(cfg, after)
        return True


# ------------------------------------------------------------ start and stop
def _kickstart(cfg):
    rc, out, err = sh(["launchctl", "kickstart",
                       f"gui/{cfg['uid']}/{cfg['label']}"], timeout=30)
    return rc, (out + err).strip()


def _do_stop(cfg, o=None, grace=45):
    """Stop the worker via launchd, then verify process exit and port closure."""
    sh(["launchctl", "kill", "TERM", f"gui/{cfg['uid']}/{cfg['label']}"], timeout=30)
    deadline = now() + grace
    while now() < deadline:
        if not obs_processes(cfg) and not obs_socket(cfg)["listening"]:
            return True
        time.sleep(0.5)
    # launchd could not reap it (e.g. an orphan it does not own) — escalate.
    for p in obs_processes(cfg):
        sh(["kill", "-TERM", str(p["pid"])])
    deadline = now() + 20
    while now() < deadline:
        if not obs_processes(cfg) and not obs_socket(cfg)["listening"]:
            return True
        time.sleep(0.5)
    for p in obs_processes(cfg):
        sh(["kill", "-KILL", str(p["pid"])])
    deadline = now() + 15
    while now() < deadline:
        if not obs_processes(cfg) and not obs_socket(cfg)["listening"]:
            return True
        time.sleep(0.5)
    return False


# --------------------------------------------------------- start supervision
def cancel_path(cfg):
    return os.path.splitext(cfg["lock"])[0] + ".cancel"


def request_cancel(cfg, by):
    """Ask any start/acquire in progress on this slot to abort. Lock-free on
    purpose: the start that must notice it is the one holding the lock."""
    rec = {"requested_at": now(), "requested_at_iso": iso(now()), "by": by,
           "pid": os.getpid()}
    path = cancel_path(cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(rec, f)
        f.write("\n")
    os.replace(tmp, path)
    log(cfg, "cancel.requested", by=by)
    return rec


def read_cancel(cfg):
    """The pending cancel marker, or None. An unreadable marker counts as a
    request of unknown age (requested_at 0), which every start treats as
    stale — a corrupt file can never wedge the slot."""
    path = cancel_path(cfg)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) else {"requested_at": 0}
    except (OSError, ValueError):
        return {"requested_at": 0}


def clear_cancel(cfg):
    try:
        os.remove(cancel_path(cfg))
    except FileNotFoundError:
        pass


def cancel_requested(cfg, since):
    """True when a cancel was requested at or after `since` (the moment the
    operation being cancelled began). Older markers are stale."""
    rec = read_cancel(cfg)
    return bool(rec) and float(rec.get("requested_at") or 0) >= since


def _consume_stale_cancel(cfg, t0):
    """Drop a marker older than this operation (left by a terminate that
    died early); return a fresh one if an operator already asked to stop."""
    rec = read_cancel(cfg)
    if rec is None:
        return None
    if float(rec.get("requested_at") or 0) < t0:
        clear_cancel(cfg)
        log(cfg, "cancel.stale_cleared", requested_at=rec.get("requested_at_iso"))
        return None
    return rec


def log_size(path):
    try:
        return os.path.getsize(path)
    except (OSError, TypeError):
        return 0


def read_log_since(path, offset, limit=256 * 1024, tail=64 * 1024):
    """Text appended to a log since byte `offset` (at most `limit` bytes, the
    newest). offset None means "just the last `tail` bytes" (used when the
    start being diagnosed was begun by another controller process). A log
    that shrank (rotated or truncated) is read from its start."""
    try:
        size = os.path.getsize(path)
    except (OSError, TypeError):
        return ""
    if offset is None:
        start = max(0, size - tail)
    else:
        if offset > size:
            offset = 0
        start = max(offset, size - limit)
    try:
        with open(path, "rb") as f:
            f.seek(start)
            return f.read(size - start).decode("utf-8", "replace")
    except OSError:
        return ""


_INSUFFICIENT_RE = re.compile(
    r"Insufficient memory to load model: needs ~([\d.]+) GB free "
    r"\(([\d.]+) GB of weights.*?only ([\d.]+) GB is available")
_PREFLIGHT_RE = re.compile(
    r"\[preflight\] weights ~([\d.]+) GB, available ([\d.]+) GB")
# Ranked: the first pattern with any match wins, and its LAST matching line
# is quoted (the most recent attempt in a shared log).
_REASON_PATTERNS = [
    re.compile(r"refusing to launch"),                       # exec-slot gate
    re.compile(r"(?i)model load failed|error loading model|failed to load "
               r"model|unable to load model"),
    re.compile(r"(?i)address already in use|AddressInUse|couldn'?t bind|"
               r"could not bind|failed to bind|bind failed"),
    re.compile(r"(?im)^[ \t]*(?:error|fatal)\b[: ]"),
    re.compile(r"(?m)^[A-Za-z_][\w.]*(?:Error|Exception): "),
]


def _line_at(text, pos):
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    return text[start:end if end != -1 else len(text)].strip()


def diagnose_start_failure(text):
    """Pure: the decisive reason a runtime printed while failing to start.

    Returns None when the text is empty, else {"kind", "summary", "line"}
    plus parsed figures for a memory refusal. mlx-serve's preflight prints
    "GB" but divides by 1024**3 (src/scheduler.zig), so its figures are GiB.
    """
    text = text or ""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return None
    hits = list(_INSUFFICIENT_RE.finditer(text))
    if hits:
        m = hits[-1]
        need, weights, avail = (float(x) for x in m.groups())
        res = {"kind": "insufficient_memory", "required_gib": need,
               "weights_gib": weights, "available_gib": avail,
               "short_by_gib": round(max(0.0, need - avail), 1),
               "line": _line_at(text, m.start()),
               "summary": f"not enough free memory: it needs {need:.1f} GiB "
                          f"free and only {avail:.1f} GiB was available"}
        pre = [p for p in _PREFLIGHT_RE.finditer(text) if p.start() < m.start()]
        if pre:
            res["preflight_weights_gib"] = float(pre[-1].group(1))
            res["preflight_available_gib"] = float(pre[-1].group(2))
        return res
    for pat in _REASON_PATTERNS:
        found = list(pat.finditer(text))
        if found:
            line = _line_at(text, found[-1].start())
            return {"kind": "engine_error", "line": line, "summary": line[:300]}
    return {"kind": "unknown", "line": lines[-1], "summary": lines[-1][:300]}


def wait_ready(cfg, deadline, poll=0.5, runs_before=None, cancel_since=None):
    """Poll until the worker answers /health.

    Returns (ok, t_spawn, t_ready, failure); failure is None on success, else
    {"kind": "exited" | "cancelled" | "timeout", ...}.

    Fails FAST when the launchd job has run and is no longer running: an
    engine that refused or crashed while loading is reported within about a
    second instead of after the whole deadline (the 2026-10-12 ten-minute
    hang). "Has run" means launchd's run counter rose above `runs_before`, or
    the job or a slot process was seen alive during this wait, so the window
    before launchd registers the spawn can never be mistaken for an exit.
    Two consecutive not-running observations are required. Aborts promptly
    when an operator cancel newer than `cancel_since` appears."""
    t_spawn = None
    seen_alive = False
    gone_streak = 0
    while now() < deadline:
        if cancel_since is not None and cancel_requested(cfg, cancel_since):
            return False, t_spawn, None, {"kind": "cancelled",
                                          "cancel": read_cancel(cfg)}
        procs = obs_processes(cfg)
        if t_spawn is None and procs:
            t_spawn = now()
        if obs_health(cfg, timeout=3)["ok"]:
            return True, t_spawn, now(), None
        ld = obs_launchd(cfg)
        state = (ld.get("state") or "").strip()
        alive = state == "running" or bool(ld.get("pid")) or bool(procs)
        if alive:
            seen_alive = True
            gone_streak = 0
        else:
            ran = seen_alive or (runs_before is not None
                                 and ld.get("runs") is not None
                                 and ld["runs"] > runs_before)
            if ran and state == "not running":
                gone_streak += 1
                if gone_streak >= 2:
                    return False, t_spawn, None, {
                        "kind": "exited", "last_exit": ld.get("last_exit"),
                        "runs": ld.get("runs"),
                        "stderr_path": ld.get("stderr_path")}
            else:
                gone_streak = 0
        time.sleep(poll)
    return False, t_spawn, None, {"kind": "timeout"}


def _fail_start(cfg, args, lease, plan, failure, timeout, err_path, err_off,
                verb, timeout_message=None):
    """Common ending for a start/acquire that did not become healthy: read the
    engine's reason, make sure nothing is left half-started, record it in the
    lease, emit a machine-readable result, and exit with a precise code."""
    failure = failure or {"kind": "timeout"}
    kind = failure.get("kind", "timeout")
    reason = None
    if kind != "cancelled" and err_path:
        reason = diagnose_start_failure(read_log_since(err_path, err_off))
    stopped = _do_stop(cfg)
    lease["state"] = STOPPED if stopped else STOPPING
    lease["pid"] = None
    lease["start_deadline"] = None
    if verb == "acquire":
        lease["owner"] = None
    name = (plan or {}).get("display_name") or cfg["instance"]
    runtime = (plan or {}).get("runtime") or lease.get("runtime") or "model"
    if kind == "cancelled":
        code = 9
        message = f"start of {name} was cancelled by a stop request; the worker is stopped"
    elif kind == "exited":
        code = 8
        ex = failure.get("last_exit")
        message = (f"{name} failed to start: the {runtime} engine exited"
                   + (f" with code {ex}" if ex and ex != "(never exited)" else "")
                   + (f" — {reason['summary']}" if reason
                      else " (its log gave no reason)"))
    else:
        code = 4
        message = timeout_message or (
            f"{cfg['instance']} did not become healthy within {timeout}s; "
            f"worker stopped")
        if reason:
            message += f" — last engine output: {reason['summary']}"
    if not stopped:
        message += " (WARNING: the worker could not be confirmed stopped)"
    lease["notes"] = [f"{verb} {kind} at {iso(now())}: {message}"]
    write_lease(cfg, lease)
    log(cfg, f"{verb}.{kind}", timeout_s=timeout, reason=reason,
        engine_exit_code=failure.get("last_exit"), worker_stopped=stopped)
    if getattr(args, "json", False):
        print(json.dumps({
            "ok": False, "error": kind, "exit_code": code, "message": message,
            "worker": cfg["instance"], "model_id": (plan or {}).get("model"),
            "display_name": name, "runtime": runtime,
            "engine_exit_code": failure.get("last_exit"), "reason": reason,
            "log": err_path, "worker_stopped": stopped}, indent=2))
    die(message, code)


def _stderr_path(cfg, ld=None):
    """Where the slot's runtime writes stderr: launchd's StandardErrorPath,
    falling back to the slot log directory's stderr.log."""
    return ((ld or {}).get("stderr_path")
            or os.path.join(cfg["logdir"], "stderr.log"))


def _plan_for_lease(lease):
    """Plan-like identity of the model a recorded lease says is loading."""
    mid = lease.get("model")
    try:
        disp = registry()["models"][mid].get("display_name")
    except (KeyError, TypeError, AttributeError):
        disp = None
    return {"model": mid, "runtime": lease.get("runtime"), "display_name": disp}


def _runs_before_adopted_start(o):
    """For a STARTING lease left by another controller process: if launchd
    already reports the job not running and no slot process exists, the
    worker ran and is gone, so let wait_ready report the exit immediately."""
    ld = o.get("launchd") or {}
    if ((ld.get("state") or "").strip() == "not running"
            and not o.get("processes")):
        return max(0, (ld.get("runs") or 1) - 1)
    return None


def _refuse_cancelled_before_spawn(cfg, args, t0, plan, verb):
    """A stop that arrived while this operation waited for the lock wins
    before anything is spawned; nothing needs stopping."""
    rec = _consume_stale_cancel(cfg, t0)
    if rec is None:
        return
    name = (plan or {}).get("display_name") or cfg["instance"]
    message = f"start of {name} was cancelled by a stop request before it began"
    log(cfg, f"{verb}.cancelled_before_spawn", by=rec.get("by"))
    if getattr(args, "json", False):
        print(json.dumps({"ok": False, "error": "cancelled", "exit_code": 9,
                          "message": message, "worker": cfg["instance"],
                          "model_id": (plan or {}).get("model"),
                          "display_name": name}, indent=2))
    die(message, 9)


# ----------------------------------------------- production start / restart
def _resolve_launch(cfg):
    """The globally selected model as THIS slot will launch it. Refused
    (exit 6) unless the registry says it is installed, intact and runnable —
    a name in a config file is never enough to start anything."""
    reg = registry()
    plan = nm.launch_plan(reg, cfg["slot"])
    check = nm.check_model(reg, plan["model"])
    if not check["available"]:
        log(cfg, "start.refused_model_unavailable", model=plan["model"],
            reasons=check["reasons"])
        die(f"refusing to start {cfg['instance']}: selected model "
            f"{plan['display_name']} ({plan['model']}) is not available — "
            + "; ".join(check["reasons"]), 6)
    return plan, check


def _start_locked(cfg, args, t0):
    """Bring THIS instance to a healthy running state with no lease owner.

    Production semantics (Commission #4 / Newton AI Control): the instance
    runs until an explicit stop. With the idle TTL disabled no reaper ever
    collects it. Refuses to create a second Qwen instance: both a duplicate
    of THIS instance and a running PEER instance abort the start.

    Caller must hold the controller Lock. Returns a process exit code.
    """
    lease, o, corr = reconcile(cfg, orphan_policy=args.orphan_policy)

    peers = o.get("peer_processes") or []
    if peers and not args.allow_peer:
        log(cfg, "start.refused_peer_active", peers=peers)
        die(f"refusing to start {cfg['instance']}: another Qwen instance is "
            f"already active ({_fmt_peers(peers)}). Stop it first, or pass "
            f"--allow-peer for a deliberate dual-instance experiment.", 5)

    if o["duplicate_processes"]:
        die(f"refusing to start: multiple {cfg['instance']} processes exist "
            f"({[p['pid'] for p in o['processes']]}). Run: "
            f"reconcile --orphan-policy terminate", 5)

    state = lease["state"]

    if state == STOPPING:
        dl = now() + 60
        while now() < dl and observe(cfg)["observed_state"] != OBS_DOWN:
            time.sleep(0.5)
        lease, o, _ = reconcile(cfg, orphan_policy=args.orphan_policy)
        state = lease["state"]

    if state == STARTING:
        timeout = args.timeout or cfg["start_timeout"]
        ok, _, _, failure = wait_ready(
            cfg, deadline=now() + timeout,
            runs_before=_runs_before_adopted_start(o), cancel_since=t0)
        lease, o, _ = reconcile(cfg, orphan_policy=args.orphan_policy)
        state = lease["state"]
        cancelled = (failure or {}).get("kind") == "cancelled"
        if cancelled or (not ok and state not in (READY, IDLE, BUSY)):
            _fail_start(cfg, args, lease, _plan_for_lease(lease), failure,
                        timeout, _stderr_path(cfg, o["launchd"]), None,
                        "start", timeout_message=(
                            f"a worker was already STARTING but did not "
                            f"become healthy within {timeout}s; it has been "
                            f"stopped"))

    if state in (READY, IDLE, BUSY):
        if not observe(cfg)["health"]["ok"]:
            lease, o, _ = reconcile(cfg, orphan_policy=args.orphan_policy)
            state = lease["state"]
    if state in (READY, IDLE, BUSY):
        if state == IDLE:
            # An explicit start cancels any pending idle TTL.
            lease["state"] = READY
            lease["idle_since"] = None
            write_lease(cfg, lease)
        log(cfg, "start.already_running", pid=lease["pid"], previous_state=state)
        return _start_result(cfg, read_lease(cfg), started=False, t0=t0,
                             args=args, note=f"already running (was {state})")

    if state == STOPPED:
        if not o["launchd"]["bootstrapped"]:
            die(f"launchd job {cfg['label']} is not bootstrapped in "
                f"gui/{cfg['uid']}. Run the matching install script first.", 1)
        if obs_socket(cfg)["listening"]:
            die(f"port {cfg['port']} is occupied by a foreign process; "
                f"refusing to start {cfg['instance']}", 5)

        plan, _ = _resolve_launch(cfg)
        _refuse_cancelled_before_spawn(cfg, args, t0, plan, "start")
        timeout = args.timeout or max(cfg["start_timeout"], plan["start_timeout_s"])
        lease["state"] = STARTING
        lease["owner"] = None
        lease["acquired_at"] = None
        lease["last_activity"] = now()
        lease["idle_since"] = None
        lease["generation"] = int(lease.get("generation") or 0) + 1
        lease["start_deadline"] = now() + timeout
        lease["model"] = plan["model"]
        lease["runtime"] = plan["runtime"]
        lease["notes"] = []
        write_lease(cfg, lease)
        log(cfg, "start.begin", generation=lease["generation"],
            model=plan["model"], runtime=plan["runtime"])

        ld0 = obs_launchd(cfg)
        err_path = _stderr_path(cfg, ld0)
        err_off = log_size(err_path)
        t_kick = now()
        rc, msg = _kickstart(cfg)
        if rc != 0:
            lease["state"] = STOPPED
            write_lease(cfg, lease)
            die(f"launchctl kickstart failed (rc={rc}): {msg}", 1)

        ok, t_spawn, t_ready, failure = wait_ready(
            cfg, deadline=t_kick + timeout,
            runs_before=ld0.get("runs") or 0, cancel_since=t0)
        if not ok:
            _fail_start(cfg, args, lease, plan, failure, timeout, err_path,
                        err_off, "start")

        o2 = observe(cfg)
        lease["state"] = READY
        lease["pid"] = o2["pid"]
        lease["start_deadline"] = None
        lease["last_activity"] = now()
        lease["last_start"] = {
            "kickstart_at": iso(t_kick),
            "spawn_latency_s": round(t_spawn - t_kick, 2) if t_spawn else None,
            "load_to_ready_s": round(t_ready - t_kick, 2),
            "generation": lease["generation"],
        }
        write_lease(cfg, lease)
        log(cfg, "start.ready", pid=lease["pid"], **lease["last_start"])
        return _start_result(cfg, lease, started=True, t0=t0, args=args,
                             note="cold start; model loaded")

    die(f"unhandled lifecycle state {state}", 1)


def _start_result(cfg, lease, started, t0, args, note):
    o = observe(cfg)
    res = {
        "ok": True,
        "worker": cfg["instance"],
        "state": lease["state"],
        "pid": lease["pid"],
        "endpoint": lease["endpoint"],
        "openai_base_url": lease["endpoint"] + "/v1",
        "model": lease["alias"],
        "model_id": lease.get("model"),
        "model_runtime": lease.get("runtime"),
        "started": started,
        "wall_s": round(now() - t0, 2),
        "last_start": lease.get("last_start"),
        "runtime": o["health"]["props"],
        "generation": lease["generation"],
        "note": note,
    }
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        if started:
            print(f"STARTED {cfg['instance']}  state={res['state']} "
                  f"pid={res['pid']}  wall={res['wall_s']}s")
        else:
            print(f"start: {cfg['instance']} {res['state']} pid={res['pid']} "
                  f"— {note}")
        print(f"  endpoint  {res['openai_base_url']}  (model={res['model']})")
        if res["last_start"] and started:
            ls = res["last_start"]
            print(f"  start     spawn={ls['spawn_latency_s']}s  "
                  f"ready={ls['load_to_ready_s']}s  gen={ls['generation']}")
    return 0


def cmd_start(cfg, args):
    t0 = now()
    with Lock(cfg, timeout=args.lock_timeout):
        return _start_locked(cfg, args, t0)


def cmd_restart(cfg, args):
    """Stop, CONFIRM termination, then start — under one controller lock, so
    no interleaved operation can ever produce two overlapping instances."""
    t0 = now()
    with Lock(cfg, timeout=args.lock_timeout):
        lease, o, _ = reconcile(cfg, orphan_policy=args.orphan_policy)
        # A restart replaces the running model with ITSELF. The selection is
        # locked while a slot is occupied, so this can only trip on
        # inconsistent state — and then it must refuse rather than switch.
        sel, _ = nm.read_selection(registry())
        if (lease.get("model") and lease["model"] != sel
                and o["observed_state"] != OBS_DOWN):
            die(f"restart refused: {cfg['instance']} is running "
                f"{lease['model']} but the global selection is {sel}; restart "
                f"never switches models — stop, then start explicitly", 7)
        if o["observed_state"] != OBS_DOWN or lease["state"] != STOPPED:
            lease["state"] = STOPPING
            lease["stop_deadline"] = now() + 60
            write_lease(cfg, lease)
            log(cfg, "restart.stop", pid=o["pid"])
            if not _do_stop(cfg, o):
                write_lease(cfg, lease)
                die(f"restart: could not stop the running {cfg['instance']}; "
                    f"refusing to start a second instance", 1)
            lease["state"] = STOPPED
            lease["owner"] = None
            lease["pid"] = None
            lease["idle_since"] = None
            lease["stop_deadline"] = None
            lease["acquired_at"] = None
            write_lease(cfg, lease)
            log(cfg, "restart.stopped")
        return _start_locked(cfg, args, t0)


# ------------------------------------------------------------------ commands
def cmd_status(cfg, args):
    # An observer: never holds the lock, so it never queues behind a start,
    # and persists what it learned only by compare-and-swap.
    lease, o, corr = reconcile(cfg, orphan_policy=args.orphan_policy,
                               locked=False)
    idle_remaining = None
    if lease["state"] == IDLE and lease.get("idle_since"):
        idle_remaining = round(lease["idle_ttl_seconds"]
                               - (now() - lease["idle_since"]), 1)
    sel, sel_source = nm.read_selection(registry())
    out = {
        "worker": cfg["instance"],
        "slot": cfg["slot"],
        "state": lease["state"],
        "owner": lease["owner"],
        "pid": lease["pid"],
        "rss_gib": o["rss_gib"],
        "model": lease.get("model"),
        "model_runtime": lease.get("runtime"),
        "selected_model": sel,
        "selection_source": sel_source,
        "peer_processes": o["peer_processes"],
        "endpoint": lease["endpoint"],
        "domain": lease["domain"],
        "label": lease["label"],
        "launchd": o["launchd"],
        "socket_listening": o["socket"]["listening"],
        "socket_bind": o["socket"]["name"],
        "health_ok": o["health"]["ok"],
        "runtime": o["health"]["props"],
        "observed_state": o["observed_state"],
        "acquired_at": iso(lease.get("acquired_at")),
        "last_activity": iso(lease.get("last_activity")),
        "idle_since": iso(lease.get("idle_since")),
        "idle_ttl_seconds": lease["idle_ttl_seconds"],
        "idle_remaining_seconds": idle_remaining,
        "generation": lease["generation"],
        "corrections": corr,
        "notes": lease.get("notes") or [],
    }
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"{cfg['instance']}  state={out['state']}"
              + (f"  owner={out['owner']}" if out["owner"] else "")
              + (f"  pid={out['pid']}" if out["pid"] else ""))
        print(f"  endpoint   {out['endpoint']}   ({out['domain']}/{out['label']})")
        print(f"  model      selected={sel}"
              + (f"  running={out['model']} via {out['model_runtime']}"
                 if out["model"] else "")
              + f"   [{sel_source}]")
        print(f"  launchd    state={o['launchd']['state']} "
              f"bootstrapped={o['launchd']['bootstrapped']} "
              f"runs={o['launchd']['runs']} last_exit={o['launchd']['last_exit']}")
        print(f"  socket     listening={o['socket']['listening']} "
              f"bind={o['socket']['name']}")
        print(f"  health     ok={o['health']['ok']}   observed={o['observed_state']}")
        if out["runtime"]:
            r = out["runtime"]
            print(f"  runtime    alias={r.get('alias')} ftype={r.get('ftype')} "
                  f"n_ctx={r.get('n_ctx')} "
                  f"vision={(r.get('modalities') or {}).get('vision')} "
                  f"slots={r.get('total_slots')}")
        if out["rss_gib"] is not None:
            print(f"  rss        {out['rss_gib']} GiB")
        if idle_remaining is not None:
            print(f"  idle       {idle_remaining}s left of "
                  f"{out['idle_ttl_seconds']}s TTL")
        if o["peer_processes"]:
            print(f"  PEER       another Qwen instance is active: "
                  f"{_fmt_peers(o['peer_processes'])}")
        for c in corr:
            print(f"  RECONCILED {json.dumps(c)}")
        for n in out["notes"]:
            print(f"  note       {n}")
    return 0


def cmd_health(cfg, args):
    o = observe(cfg)
    ok = o["health"]["ok"]
    payload = {"health_ok": ok, "observed_state": o["observed_state"],
               "pid": o["pid"], "socket": o["socket"],
               "runtime": o["health"]["props"]}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"health_ok={ok} observed={o['observed_state']} pid={o['pid']}")
        if o["health"]["props"]:
            for k, v in o["health"]["props"].items():
                print(f"  {k} = {v}")
    return 0 if ok else 1


def cmd_acquire(cfg, args):
    t0 = now()
    with Lock(cfg, timeout=args.lock_timeout):
        lease, o, corr = reconcile(cfg, orphan_policy=args.orphan_policy)

        # acquire keeps its historical dual-instance capability (the capacity
        # benchmarks lease B while A serves), but is no longer silent about it.
        peers = o.get("peer_processes") or []
        if peers:
            log(cfg, "acquire.peer_present", peers=peers)
            sys.stderr.write(
                f"qwenctl: WARNING — another Qwen instance is active "
                f"({_fmt_peers(peers)}). Two concurrent instances roughly "
                f"halve per-instance throughput; the production control path "
                f"(start/Newton AI Control) refuses this state.\n")

        if o["duplicate_processes"] and args.orphan_policy != "terminate":
            die(f"refusing to acquire: multiple {cfg['instance']} processes "
                f"exist ({[p['pid'] for p in o['processes']]}). Run: "
                "reconcile --orphan-policy terminate", 5)

        state = lease["state"]

        # Idempotent re-acquire by the same owner.
        if state == BUSY and lease["owner"] == args.owner:
            lease["last_activity"] = now()
            write_lease(cfg, lease)
            log(cfg, "acquire.reentrant", owner=args.owner, pid=lease["pid"])
            return _acquire_result(cfg, lease, reused=True, reloaded=False,
                                   t0=t0, args=args, note="re-entrant acquire "
                                   "by the existing owner")

        if state == BUSY and lease["owner"] != args.owner:
            if not args.steal:
                msg = {"error": "lease_conflict", "state": BUSY,
                       "held_by": lease["owner"], "requested_by": args.owner,
                       "pid": lease["pid"],
                       "acquired_at": iso(lease.get("acquired_at")),
                       "last_activity": iso(lease.get("last_activity")),
                       "hint": "wait for release, or pass --steal to take it "
                               "over deliberately"}
                if args.json:
                    print(json.dumps(msg, indent=2))
                else:
                    sys.stderr.write(
                        f"qwenctl: lease conflict — qwen-b is BUSY, held by "
                        f"'{lease['owner']}' since {iso(lease.get('acquired_at'))} "
                        f"(pid {lease['pid']}). '{args.owner}' was refused.\n")
                log(cfg, "acquire.conflict", requested_by=args.owner,
                    held_by=lease["owner"])
                sys.exit(3)
            log(cfg, "acquire.steal", requested_by=args.owner,
                previous_owner=lease["owner"])
            lease["notes"] = [f"lease stolen from {lease['owner']} by "
                              f"{args.owner} at {iso(now())}"]

        if state == STOPPING:
            dl = now() + 60
            while now() < dl and observe(cfg)["observed_state"] != OBS_DOWN:
                time.sleep(0.5)
            lease, o, _ = reconcile(cfg, orphan_policy=args.orphan_policy)
            state = lease["state"]

        if state == STARTING:
            # Someone (or an adopted orphan) is already loading. Wait for it
            # rather than starting a second worker.
            timeout = args.timeout or cfg["start_timeout"]
            ok, _, _, failure = wait_ready(
                cfg, deadline=now() + timeout,
                runs_before=_runs_before_adopted_start(o), cancel_since=t0)
            lease, o, _ = reconcile(cfg, orphan_policy=args.orphan_policy)
            state = lease["state"]
            cancelled = (failure or {}).get("kind") == "cancelled"
            if cancelled or (not ok and state not in (READY, IDLE, BUSY)):
                _fail_start(cfg, args, lease, _plan_for_lease(lease), failure,
                            timeout, _stderr_path(cfg, o["launchd"]), None,
                            "acquire", timeout_message=(
                                f"a worker was already STARTING but did not "
                                f"become healthy within {timeout}s; it has "
                                f"been stopped"))

        # ---- reuse path: worker already loaded, no model reload -------------
        if state in (READY, IDLE, BUSY):
            if not observe(cfg)["health"]["ok"]:
                lease, o, _ = reconcile(cfg, orphan_policy=args.orphan_policy)
                state = lease["state"]
        if state in (READY, IDLE, BUSY):
            prev = state
            lease["state"] = BUSY
            lease["owner"] = args.owner
            lease["acquired_at"] = now()
            lease["last_activity"] = now()
            lease["idle_since"] = None
            write_lease(cfg, lease)
            log(cfg, "acquire.reuse", owner=args.owner, pid=lease["pid"],
                previous_state=prev)
            return _acquire_result(cfg, lease, reused=True, reloaded=False,
                                   t0=t0, args=args,
                                   note=f"reused loaded worker from {prev}; "
                                        "no model reload")

        # ---- cold start path ------------------------------------------------
        if state == STOPPED:
            if not o["launchd"]["bootstrapped"]:
                die(f"launchd job {cfg['label']} is not bootstrapped in "
                    f"gui/{cfg['uid']}. Run scripts/install-launchagent-b.sh", 1)
            if obs_socket(cfg)["listening"]:
                die(f"port {cfg['port']} is occupied by a foreign process; "
                    f"refusing to start {cfg['instance']}", 5)

            plan, _ = _resolve_launch(cfg)
            _refuse_cancelled_before_spawn(cfg, args, t0, plan, "acquire")
            timeout = args.timeout or max(cfg["start_timeout"], plan["start_timeout_s"])
            lease["state"] = STARTING
            lease["owner"] = args.owner
            lease["acquired_at"] = now()
            lease["last_activity"] = now()
            lease["idle_since"] = None
            lease["generation"] = int(lease.get("generation") or 0) + 1
            lease["start_deadline"] = now() + timeout
            lease["model"] = plan["model"]
            lease["runtime"] = plan["runtime"]
            lease["notes"] = []
            write_lease(cfg, lease)
            log(cfg, "acquire.start", owner=args.owner,
                generation=lease["generation"], model=plan["model"],
                runtime=plan["runtime"])

            ld0 = obs_launchd(cfg)
            err_path = _stderr_path(cfg, ld0)
            err_off = log_size(err_path)
            t_kick = now()
            rc, msg = _kickstart(cfg)
            if rc != 0:
                lease["state"] = STOPPED
                lease["owner"] = None
                write_lease(cfg, lease)
                die(f"launchctl kickstart failed (rc={rc}): {msg}", 1)

            ok, t_spawn, t_ready, failure = wait_ready(
                cfg, deadline=t_kick + timeout,
                runs_before=ld0.get("runs") or 0, cancel_since=t0)
            if not ok:
                _fail_start(cfg, args, lease, plan, failure, timeout, err_path,
                            err_off, "acquire", timeout_message=(
                                f"{cfg['instance']} did not become healthy "
                                f"within {timeout}s; worker stopped, lease "
                                f"cleared"))

            o2 = observe(cfg)
            lease["state"] = BUSY
            lease["pid"] = o2["pid"]
            lease["start_deadline"] = None
            lease["last_activity"] = now()
            lease["last_start"] = {
                "kickstart_at": iso(t_kick),
                "spawn_latency_s": round(t_spawn - t_kick, 2) if t_spawn else None,
                "load_to_ready_s": round(t_ready - t_kick, 2),
                "generation": lease["generation"],
            }
            write_lease(cfg, lease)
            log(cfg, "acquire.ready", owner=args.owner, pid=lease["pid"],
                **lease["last_start"])
            return _acquire_result(cfg, lease, reused=False, reloaded=True,
                                   t0=t0, args=args,
                                   note="cold start; model loaded")

        die(f"unhandled lifecycle state {state}", 1)


def _acquire_result(cfg, lease, reused, reloaded, t0, args, note):
    o = observe(cfg)
    res = {
        "ok": True,
        "worker": cfg["instance"],
        "state": lease["state"],
        "owner": lease["owner"],
        "pid": lease["pid"],
        "endpoint": lease["endpoint"],
        "openai_base_url": lease["endpoint"] + "/v1",
        "model": lease["alias"],
        "domain": lease["domain"],
        "label": lease["label"],
        "reused_loaded_worker": reused,
        "model_reloaded": reloaded,
        "acquire_wall_s": round(now() - t0, 2),
        "last_start": lease.get("last_start"),
        "runtime": o["health"]["props"],
        "idle_ttl_seconds": lease["idle_ttl_seconds"],
        "generation": lease["generation"],
        "note": note,
    }
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"ACQUIRED {cfg['instance']}  state={res['state']} "
              f"owner={res['owner']} pid={res['pid']}")
        print(f"  endpoint  {res['openai_base_url']}  (model={res['model']})")
        print(f"  reused_loaded_worker={reused}  model_reloaded={reloaded}  "
              f"wall={res['acquire_wall_s']}s")
        if res["last_start"]:
            ls = res["last_start"]
            print(f"  start     spawn={ls['spawn_latency_s']}s  "
                  f"ready={ls['load_to_ready_s']}s  gen={ls['generation']}")
        if res["runtime"]:
            r = res["runtime"]
            print(f"  runtime   alias={r.get('alias')} ftype={r.get('ftype')} "
                  f"n_ctx={r.get('n_ctx')} "
                  f"vision={(r.get('modalities') or {}).get('vision')}")
        print(f"  {note}")
    return 0


def cmd_release(cfg, args):
    with Lock(cfg, timeout=args.lock_timeout):
        lease, o, _ = reconcile(cfg, orphan_policy=args.orphan_policy)
        if lease["state"] != BUSY:
            print(f"release: {cfg['instance']} is {lease['state']}, not BUSY — "
                  f"nothing to release (idempotent)")
            return 0
        if lease["owner"] != args.owner and not args.force:
            sys.stderr.write(
                f"qwenctl: refusing to release — lease is held by "
                f"'{lease['owner']}', not '{args.owner}'. Use --force to "
                f"override deliberately.\n")
            log(cfg, "release.conflict", requested_by=args.owner,
                held_by=lease["owner"])
            sys.exit(3)
        prev_owner = lease["owner"]
        lease["state"] = IDLE
        lease["owner"] = None
        lease["idle_since"] = now()
        lease["last_activity"] = now()
        write_lease(cfg, lease)
        log(cfg, "release", previous_owner=prev_owner, pid=lease["pid"],
            idle_ttl_s=lease["idle_ttl_seconds"])
        print(f"RELEASED {cfg['instance']}  state=IDLE  pid={lease['pid']}  "
              f"(was owned by {prev_owner})")
        print(f"  worker stays loaded for {lease['idle_ttl_seconds']}s; "
              f"re-acquire inside that window avoids a model reload")
        print(f"  terminates at ~{iso(lease['idle_since'] + lease['idle_ttl_seconds'])}")
    return 0


def _cancel_permitted(cfg, args):
    """An owned lease (a Qwen-B job) is only ever cancelled deliberately."""
    lease = read_lease(cfg)
    return (not lease.get("owner")) or bool(getattr(args, "force", False)), lease


def cmd_terminate(cfg, args):
    # Stop wins over start. A start that is loading holds the lock for its
    # whole load; without this marker terminate would queue behind it (up to
    # the start timeout). The marker makes that start abort within a poll.
    # It is deliberately left in place afterwards: a start or acquire that was
    # requested BEFORE this terminate but has not got the lock yet must see it
    # too. Any later start clears it as stale.
    permitted, _ = _cancel_permitted(cfg, args)
    if permitted:
        request_cancel(cfg, by=getattr(args, "by", None) or "terminate")
    with Lock(cfg, timeout=args.lock_timeout):
        lease, o, _ = reconcile(cfg, orphan_policy=args.orphan_policy)
        if lease["state"] == BUSY and not args.force:
            sys.stderr.write(
                f"qwenctl: refusing to terminate — {cfg['instance']} is BUSY "
                f"(owner '{lease['owner']}'). Use --force to override.\n")
            sys.exit(3)
        if lease["state"] == STOPPED and o["observed_state"] == OBS_DOWN:
            print(f"terminate: {cfg['instance']} already STOPPED (idempotent)")
            return 0

        before = {"pid": o["pid"], "rss_gib": o["rss_gib"],
                  "socket_listening": o["socket"]["listening"]}
        previous_state = lease["state"]
        lease["state"] = STOPPING
        lease["stop_deadline"] = now() + 60
        write_lease(cfg, lease)
        log(cfg, "terminate.begin", pid=o["pid"], forced=bool(args.force),
            previous_state=previous_state)

        t0 = now()
        ok = _do_stop(cfg, o)
        after = observe(cfg)
        lease["state"] = STOPPED if ok else STOPPING
        lease["owner"] = None
        lease["pid"] = None
        lease["idle_since"] = None
        lease["stop_deadline"] = None
        lease["acquired_at"] = None
        if not ok:
            lease["notes"] = [f"terminate did not fully stop the worker by "
                              f"{iso(now())}"]
        write_lease(cfg, lease)
        log(cfg, "terminate.end", ok=ok, stop_s=round(now() - t0, 2))

        res = {"ok": ok, "state": lease["state"],
               "stop_seconds": round(now() - t0, 2),
               "before": before,
               "after": {"pid": after["pid"],
                         "processes": after["processes"],
                         "socket_listening": after["socket"]["listening"],
                         "health_ok": after["health"]["ok"]}}
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"{'TERMINATED' if ok else 'TERMINATE INCOMPLETE'} "
                  f"{cfg['instance']}  state={lease['state']}  "
                  f"stop={res['stop_seconds']}s")
            print(f"  before  pid={before['pid']} listening={before['socket_listening']}")
            print(f"  after   pid={after['pid']} "
                  f"listening={after['socket']['listening']} "
                  f"processes={len(after['processes'])}")
        return 0 if ok else 1


def cmd_reap(cfg, args):
    """Enforce the idle TTL. Safe to run on a timer; a no-op unless expired."""
    if cfg["idle_ttl"] <= 0:
        # The idle TTL is disabled for this instance (production semantics:
        # it runs until explicitly stopped). Reap must not even reconcile —
        # it is a pure no-op so a misconfigured timer can never kill it.
        if args.verbose:
            print(f"reap: idle TTL disabled for {cfg['instance']} — nothing "
                  f"to do")
        return 0
    with Lock(cfg, timeout=args.lock_timeout):
        lease, o, corr = reconcile(cfg, orphan_policy=args.orphan_policy)
        if lease["state"] != IDLE:
            if args.verbose:
                print(f"reap: state={lease['state']} — nothing to do")
            return 0
        since = lease.get("idle_since") or now()
        elapsed = now() - since
        if elapsed < lease["idle_ttl_seconds"]:
            if args.verbose:
                print(f"reap: IDLE {elapsed:.0f}s of "
                      f"{lease['idle_ttl_seconds']}s TTL — keeping worker")
            return 0
        log(cfg, "reap.expired", pid=lease["pid"], idle_s=round(elapsed, 1),
            ttl_s=lease["idle_ttl_seconds"])
        print(f"reap: idle TTL expired ({elapsed:.0f}s >= "
              f"{lease['idle_ttl_seconds']}s) — terminating {cfg['instance']} "
              f"pid {lease['pid']}")
        lease["state"] = STOPPING
        write_lease(cfg, lease)
        ok = _do_stop(cfg, o)
        after = observe(cfg)
        lease["state"] = STOPPED if ok else STOPPING
        lease["owner"] = None
        lease["pid"] = None
        lease["idle_since"] = None
        lease["acquired_at"] = None
        lease["notes"] = [f"terminated by idle-TTL reaper at {iso(now())} after "
                          f"{elapsed:.0f}s idle"]
        write_lease(cfg, lease)
        log(cfg, "reap.terminated", ok=ok,
            socket_listening=after["socket"]["listening"])
        print(f"reap: state={lease['state']} listening="
              f"{after['socket']['listening']} processes={len(after['processes'])}")
        return 0 if ok else 1


def cmd_heartbeat(cfg, args):
    with Lock(cfg, timeout=args.lock_timeout):
        lease, o, _ = reconcile(cfg, orphan_policy=args.orphan_policy)
        if lease["state"] != BUSY or lease["owner"] != args.owner:
            sys.stderr.write(f"qwenctl: no BUSY lease held by '{args.owner}' "
                             f"(state={lease['state']}, owner={lease['owner']})\n")
            sys.exit(3)
        lease["last_activity"] = now()
        write_lease(cfg, lease)
        print(f"heartbeat ok  owner={args.owner} pid={lease['pid']}")
    return 0


def cmd_cancel(cfg, args):
    """Ask a start/acquire in progress (or still waiting for the lock) on this
    slot to abort. Lock-free on purpose and returns at once; the start that
    sees the marker stops its worker and exits 9. `--wait N` then waits up to
    N seconds for the lock to come free and reports the resulting state."""
    permitted, lease = _cancel_permitted(cfg, args)
    if not permitted:
        sys.stderr.write(
            f"qwenctl: refusing to cancel — {cfg['instance']} is "
            f"{lease['state']} for owner '{lease['owner']}'. Use --force to "
            f"override deliberately.\n")
        sys.exit(3)
    rec = request_cancel(cfg, by=args.by or "cancel")
    in_progress = lease["state"] == STARTING
    settled = None
    if args.wait and args.wait > 0:
        deadline = now() + args.wait
        settled = False
        while now() < deadline:
            with TryLock(cfg) as held:
                if held:
                    settled = True
                    break
            time.sleep(0.25)
        lease = read_lease(cfg)
    res = {"ok": True, "worker": cfg["instance"],
           "cancel_requested_at": rec["requested_at_iso"], "by": rec["by"],
           "start_in_progress": in_progress, "state": lease["state"],
           "settled": settled,
           "note": ("the start in progress will abort and stop its worker"
                    if in_progress else
                    "no start is in progress; any start already waiting for "
                    "the lock will be refused, later starts are unaffected")}
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"CANCEL requested for {cfg['instance']}  state={res['state']}  "
              f"by={res['by']}")
        print(f"  {res['note']}")
        if settled is not None:
            print(f"  settled={settled}")
    return 0


def cmd_reconcile(cfg, args):
    # A mutation (it may adopt or, with --orphan-policy terminate, kill), so it
    # serialises with start/stop like every other writer.
    with Lock(cfg, timeout=args.lock_timeout):
        return _reconcile_locked(cfg, args)


def _reconcile_locked(cfg, args):
    before = read_lease(cfg)
    lease, o, corr = reconcile(cfg, orphan_policy=args.orphan_policy)
    res = {"recorded_before": before["state"],
           "recorded_before_owner": before["owner"],
           "observed": o["observed_state"],
           "state_after": lease["state"],
           "owner_after": lease["owner"],
           "pid": lease["pid"],
           "duplicate_processes": o["duplicate_processes"],
           "processes": o["processes"],
           "launchd": o["launchd"],
           "socket": o["socket"],
           "health_ok": o["health"]["ok"],
           "orphan_policy": args.orphan_policy,
           "corrections": corr,
           "notes": lease.get("notes") or []}
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(f"reconcile  recorded={res['recorded_before']} "
              f"observed={res['observed']} -> {res['state_after']}")
        print(f"  policy={args.orphan_policy}  pid={res['pid']}  "
              f"duplicates={res['duplicate_processes']}")
        if corr:
            for c in corr:
                print(f"  CORRECTION {json.dumps(c)}")
        else:
            print("  no corrections needed — recorded state already matched reality")
        for n in res["notes"]:
            print(f"  note {n}")
    return 0


def cmd_logs(cfg, args):
    n = str(args.lines)
    for name in ("controller.log", "stderr.log", "stdout.log"):
        p = os.path.join(cfg["logdir"], name)
        print(f"\n=== {p} (last {n}) ===")
        if os.path.exists(p):
            rc, out, _ = sh(["tail", "-n", n, p])
            print(out.rstrip() or "  (empty)")
        else:
            print("  (missing)")
    return 0


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(
        prog="qwenctl",
        description="Deterministic lifecycle controller for the local Qwen "
                    "instances (default config: Qwen-B; pass --config "
                    "config/qwen-a-controller.env for the production Qwen-A).")
    ap.add_argument("--config", default=CONFIG)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, owner=False):
        p.add_argument("--json", action="store_true")
        p.add_argument("--orphan-policy", choices=["adopt", "terminate", "report"],
                       default="adopt",
                       help="what to do with a running worker that has no lease "
                            "(default: adopt — never silently kills work)")
        p.add_argument("--lock-timeout", type=int, default=300)
        if owner:
            p.add_argument("--owner", required=True,
                           help="job/lease identifier of the caller")
        return p

    common(sub.add_parser("status", help="reconciled lifecycle state"))
    common(sub.add_parser("health", help="probe worker health/identity"))
    st = common(sub.add_parser(
        "start", help="ensure the instance is running, ownerless (production "
                      "verb; refuses while a peer instance is active)"))
    st.add_argument("--timeout", type=int, default=None,
                    help="seconds to wait for readiness (default from config)")
    st.add_argument("--allow-peer", action="store_true",
                    help="deliberately start alongside another Qwen instance "
                         "(dual-instance experiments only)")
    rs = common(sub.add_parser(
        "restart", help="stop, confirm termination, then start — one lock, "
                        "never two overlapping instances"))
    rs.add_argument("--timeout", type=int, default=None)
    rs.add_argument("--allow-peer", action="store_true")
    a = common(sub.add_parser("acquire", help="ensure READY worker, take lease"),
               owner=True)
    a.add_argument("--timeout", type=int, default=None,
                   help="seconds to wait for readiness (default from config)")
    a.add_argument("--steal", action="store_true",
                   help="deliberately take a lease held by another owner")
    r = common(sub.add_parser("release", help="give the lease back -> IDLE"),
               owner=True)
    r.add_argument("--force", action="store_true")
    t = common(sub.add_parser("terminate", help="stop the worker now -> STOPPED "
                                               "(cancels a start in progress)"))
    t.add_argument("--force", action="store_true",
                   help="terminate even if the worker is BUSY")
    t.add_argument("--by", default=None,
                   help="who asked (recorded in the cancel marker and log)")
    c = common(sub.add_parser("cancel", help="abort a start/acquire in progress "
                                            "(lock-free, returns at once)"))
    c.add_argument("--force", action="store_true",
                   help="also cancel an acquire that names a lease owner")
    c.add_argument("--by", default=None,
                   help="who asked (recorded in the cancel marker and log)")
    c.add_argument("--wait", type=float, default=0,
                   help="seconds to wait for the aborted start to release the "
                        "lock (default 0: return immediately)")
    rp = common(sub.add_parser("reap", help="terminate if the idle TTL expired"))
    rp.add_argument("--verbose", action="store_true")
    common(sub.add_parser("heartbeat", help="refresh a held lease"), owner=True)
    common(sub.add_parser("reconcile", help="force reconciliation with reality"))
    lg = common(sub.add_parser("logs", help="tail worker + controller logs"))
    lg.add_argument("-n", "--lines", type=int, default=40)

    args = ap.parse_args()
    cfg = load_config(args.config)
    fn = {"status": cmd_status, "health": cmd_health, "start": cmd_start,
          "restart": cmd_restart, "acquire": cmd_acquire,
          "release": cmd_release, "terminate": cmd_terminate,
          "cancel": cmd_cancel, "reap": cmd_reap,
          "heartbeat": cmd_heartbeat, "reconcile": cmd_reconcile,
          "logs": cmd_logs}[args.cmd]
    sys.exit(fn(cfg, args) or 0)


if __name__ == "__main__":
    main()
