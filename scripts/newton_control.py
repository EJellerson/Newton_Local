#!/usr/bin/env python3
"""Newton AI Control — loopback control-plane service (Commission #4).

One small HTTP service, bound to 127.0.0.1 only, that gives a nontechnical
operator the whole local-AI stack on one page:

    Start Chat            open the existing Open WebUI (:3000)
    Start the AI Model    exactly one production Qwen (A), via qwenctl
    Stop the AI Model     unload it and keep it unloaded
    Restart the AI Model  stop, confirm termination, start one replacement

DESIGN
    * All model lifecycle decisions are delegated to scripts/qwenctl.py with
      config/qwen-a-controller.env — the same deterministic controller that
      has driven Qwen-B since Commission #2. This service adds only:
      desired-state memory, crash recovery while desired=RUNNING, and HTTP.
    * DESIRED STATE lives in state/qwen-a-desired.json together with the
      kernel boot time that wrote it. A record from a previous boot is
      treated as STOPPED, so a reboot ALWAYS returns to Qwen=OFF no matter
      what was running at shutdown. A crash of this service does not.
    * CRASH RECOVERY: while desired=RUNNING (same boot), a watchdog restarts
      an unexpectedly dead model, with bounded backoff. Stop sets
      desired=STOPPED first, so the watchdog can never resurrect an
      intentionally stopped model. With the agent's KeepAlive=false, launchd
      never respawns it either.
    * SINGLETON: qwenctl `start`/`restart` refuse to run while any peer
      llama-server exists (Qwen-B included), hold the controller lock, and
      verify port ownership. This service additionally serialises its own
      operations (one at a time) and is idempotent per button.
    * The service holds no secrets. The Hermes bearer key is read from the
      profile .env only to probe /health and is never sent to the page.

SECURITY
    Loopback bind; Host-header allow-list (anti DNS-rebinding); mutating
    endpoints require the X-Newton-Control header and a same-origin Origin
    (anti CSRF); GET endpoints expose status only.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)
import newton_models as nm  # noqa: E402  (model registry + global selection)
from resident_identity import resident_identity

QWENCTL = os.path.join(SCRIPT_DIR, "qwenctl.py")
A_CONFIG = os.path.join(ROOT, "config", "qwen-a-controller.env")
B_LEASE = os.path.join(ROOT, "state", "qwen-b-lease.json")
DESIRED_PATH = os.path.join(ROOT, "state", "qwen-a-desired.json")
LOGDIR = os.path.join(ROOT, "logs", "control")
PANEL_HTML = os.path.join(SCRIPT_DIR, "control-panel.html")

LEGACY_DAEMON_PLIST = "/Library/LaunchDaemons/com.newton.qwen38server.a.plist"
MIGRATION_CMD = f"sudo {ROOT}/scripts/uninstall-launchdaemon.sh"

HERMES_ENV = os.path.expanduser("~/.hermes/profiles/localchat/.env")
HERMES_URL = "http://127.0.0.1:8642"
WEBUI_URL = "http://127.0.0.1:3000"

BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

RUNNING_STATES = ("READY", "IDLE", "BUSY")
RECOVERY_BACKOFF_S = [0, 60, 180]      # delay before recovery attempt N
RECOVERY_MAX_ATTEMPTS = 3
WATCHDOG_PERIOD_S = 15

START_TIMEOUT_S = 700                  # ceiling on a qwenctl start/restart call (Flash-Next cold load: registry 600 s)
STOP_TIMEOUT_S = 150


# ------------------------------------------------------------------ utilities
def now():
    return time.time()


def log_event(event, **fields):
    try:
        os.makedirs(LOGDIR, exist_ok=True)
        rec = {"ts": round(now(), 3),
               "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event}
        rec.update(fields)
        with open(os.path.join(LOGDIR, "control.log"), "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def sh(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def http_get(url, timeout=3, bearer=None):
    """Return (status_code, body) or (None, error string)."""
    try:
        req = urllib.request.Request(url)
        if bearer:
            req.add_header("Authorization", f"Bearer {bearer}")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return None, str(e)


def boot_time_sec():
    """Kernel boot time — the epoch that scopes a desired=RUNNING record."""
    rc, out, _ = sh(["sysctl", "-n", "kern.boottime"])
    m = re.search(r"sec\s*=\s*(\d+)", out)
    return int(m.group(1)) if m else 0


# -------------------------------------------------------------- desired state
def effective_desired(record, boot_sec):
    """Pure decision: what does the operator currently want?

    STOPPED unless a well-formed record says RUNNING *and* was written during
    THIS boot. A record from a previous boot is stale by policy: a reboot
    always lands on Qwen=OFF, regardless of the state at shutdown.
    """
    if not isinstance(record, dict):
        return "STOPPED"
    if record.get("desired") != "RUNNING":
        return "STOPPED"
    if record.get("boot_sec") != boot_sec:
        return "STOPPED"
    return "RUNNING"


def read_desired_record():
    try:
        with open(DESIRED_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_desired(desired, by):
    rec = {"desired": desired, "boot_sec": boot_time_sec(),
           "set_at": now(), "set_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "set_by": by}
    os.makedirs(os.path.dirname(DESIRED_PATH), exist_ok=True)
    tmp = DESIRED_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f, indent=2)
        f.write("\n")
    os.replace(tmp, DESIRED_PATH)
    log_event("desired", desired=desired, by=by)
    return rec


# ------------------------------------------------------------------- qwenctl
def qwenctl(verb, *extra, timeout=30):
    """Run one qwenctl verb against the production Qwen-A config."""
    cmd = [sys.executable, QWENCTL, "--config", A_CONFIG, verb,
           "--json", *extra]
    return sh(cmd, timeout=timeout)


def qwen_status():
    rc, out, err = qwenctl("status", timeout=45)
    if rc != 0 or not out.strip():
        return {"state": "UNKNOWN", "error": (err or out).strip()[:400]}
    try:
        return json.loads(out)
    except ValueError:
        return {"state": "UNKNOWN", "error": out.strip()[:400]}


# --------------------------------------------------------------- UI mapping
def map_ui_state(ctl_state, health_ok, desired, op_name, recovery_gave_up):
    """Pure mapping from controller truth to the operator-facing state."""
    if op_name == "stop":
        return "Stopping"
    if op_name in ("start", "restart"):
        return "Starting"
    if ctl_state == "STARTING":
        return "Starting"
    if ctl_state == "STOPPING":
        return "Stopping"
    if ctl_state in RUNNING_STATES:
        return "Running" if health_ok else "Unhealthy"
    if ctl_state == "STOPPED":
        if desired == "RUNNING":
            return "Unhealthy" if recovery_gave_up else "Recovering"
        return "Stopped"
    return "Unhealthy"          # UNKNOWN / anything else


# ------------------------------------------------------------------ probes
def hermes_probe():
    key = None
    try:
        with open(HERMES_ENV) as f:
            for line in f:
                if line.startswith("API_SERVER_KEY="):
                    key = line.split("=", 1)[1].strip()
                    break
    except OSError:
        return {"up": False, "detail": "hermes profile .env not readable"}
    st, _ = http_get(HERMES_URL + "/health", timeout=3, bearer=key)
    # Any HTTP answer (even 401) proves the listener is alive; 200 means healthy.
    return {"up": st == 200, "http": st,
            "detail": None if st == 200 else "no healthy response on :8642"}


def webui_probe():
    st, _ = http_get(WEBUI_URL + "/health", timeout=3)
    return {"up": st == 200, "http": st}


def memory_probe():
    rc, out, _ = sh(["vm_stat"])
    pages = {}
    for line in out.splitlines():
        m = re.match(r"^(.*?):\s+(\d+)\.?$", line.strip())
        if m:
            pages[m.group(1)] = int(m.group(2))
    page = 16384
    gib = lambda k: round(pages.get(k, 0) * page / 2**30, 1)
    rc, lvl, _ = sh(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"])
    level = {"1": "normal", "2": "warning", "4": "critical"}.get(
        lvl.strip(), lvl.strip() or "unknown")
    return {
        "free_gib": gib("Pages free"),
        "available_gib": round((pages.get("Pages free", 0)
                                + pages.get("Pages inactive", 0)
                                + pages.get("Pages speculative", 0))
                               * page / 2**30, 1),
        "app_memory_gib": gib("Anonymous pages"),
        "pressure": level,
    }


def qwen_b_probe():
    """Passive view of the maintenance worker: read its lease, never touch it."""
    try:
        with open(B_LEASE) as f:
            lease = json.load(f)
    except (OSError, ValueError):
        lease = {}
    state = lease.get("state", "STOPPED")
    rc, out, _ = sh(["lsof", "-nP", "-iTCP:8081", "-sTCP:LISTEN"])
    listening = bool(out.strip())
    return {"active": state != "STOPPED" or listening,
            "state": state, "listening": listening}


_legacy_cache = {"ts": 0.0, "value": None}


def legacy_daemon_probe():
    if now() - _legacy_cache["ts"] < 60 and _legacy_cache["value"] is not None:
        return _legacy_cache["value"]
    present = os.path.exists(LEGACY_DAEMON_PLIST)
    state = None
    if present:
        rc, out, _ = sh(["launchctl", "print-disabled", "system"])
        m = re.search(r'"com\.newton\.qwen38server\.a"\s*=>\s*(\w+)', out)
        state = m.group(1) if m else "unknown"
    value = {"installed": present, "launchd_state": state,
             "migration_cmd": MIGRATION_CMD if present else None}
    _legacy_cache.update(ts=now(), value=value)
    return value


# ------------------------------------------------------------------ operations
class Ops:
    """One model operation at a time; everything else gets a 409."""

    def __init__(self):
        self._lock = threading.Lock()
        self.current = None            # {"name", "started_at"}
        self.last = None               # {"name", "ok", "message", ...}
        self.recovery = {"attempts": 0, "next_at": 0.0, "gave_up": False}

    def begin(self, name):
        with self._lock:
            if self.current:
                return False
            self.current = {"name": name, "started_at": now()}
            return True

    def finish(self, name, ok, message):
        with self._lock:
            self.last = {"name": name, "ok": ok, "message": message[:500],
                         "finished_at": now(),
                         "finished_at_iso": time.strftime("%H:%M:%S")}
            self.current = None
        log_event("op.finish", name=name, ok=ok, message=message[:500])

    def reset_recovery(self):
        self.recovery = {"attempts": 0, "next_at": 0.0, "gave_up": False}


OPS = Ops()


def _tail(text, n=3):
    lines = [l for l in text.strip().splitlines() if l.strip()]
    return " | ".join(lines[-n:]) if lines else ""


def op_start(by="panel"):
    """Start exactly one production model — the globally selected one. Idempotent."""
    write_desired("RUNNING", by)
    OPS.reset_recovery()
    rc, out, err = qwenctl("start", timeout=START_TIMEOUT_S)
    if rc == 0:
        try:
            res = json.loads(out)
            note = res.get("note", "")
            mid = res.get("model_id")
        except ValueError:
            note, mid = "", None
        OPS.finish("start", True, f"{mid or 'model'} running. {note}".strip())
    else:
        # A failed explicit start must not leave the watchdog retrying a
        # start the controller just refused (peer active, timeout, ...).
        write_desired("STOPPED", f"{by}:start-failed")
        OPS.finish("start", False, _tail(err or out) or f"start failed rc={rc}")


def op_stop(by="panel"):
    """Stop the model and keep it stopped. Idempotent, safe when stopped."""
    write_desired("STOPPED", by)          # silences the watchdog FIRST
    OPS.reset_recovery()
    rc, out, err = qwenctl("terminate", "--force", timeout=STOP_TIMEOUT_S)
    if rc == 0:
        OPS.finish("stop", True, "model stopped; memory released."
                   if "TERMINATED" in out or '"ok": true' in out
                   else "model already stopped.")
    else:
        legacy = legacy_daemon_probe()
        msg = _tail(err or out) or f"stop failed rc={rc}"
        if legacy["installed"]:
            msg += (" — the legacy boot daemon may be resurrecting it; run: "
                    + MIGRATION_CMD)
        OPS.finish("stop", False, msg)


def op_restart(by="panel"):
    write_desired("RUNNING", by)
    OPS.reset_recovery()
    rc, out, err = qwenctl("restart", timeout=START_TIMEOUT_S + STOP_TIMEOUT_S)
    if rc == 0:
        OPS.finish("restart", True, "model restarted and healthy.")
    else:
        write_desired("STOPPED", f"{by}:restart-failed")
        OPS.finish("restart", False, _tail(err or out) or f"restart failed rc={rc}")


def run_op(name, fn):
    if not OPS.begin(name):
        return False
    log_event("op.begin", name=name)
    threading.Thread(target=fn, name=f"op-{name}", daemon=True).start()
    return True


# ------------------------------------------------------------------ watchdog
def watchdog_loop():
    """Crash recovery, desired-state aware.

    Only ever STARTS the model, and only while the operator's recorded desire
    is RUNNING in THIS boot. Never fights an operator: desired=STOPPED means
    do nothing, even if something started Qwen by hand.
    """
    while True:
        time.sleep(WATCHDOG_PERIOD_S)
        try:
            if OPS.current:
                continue
            desired = effective_desired(read_desired_record(), boot_time_sec())
            if desired != "RUNNING":
                continue
            st = qwen_status()
            state = st.get("state")
            if state in RUNNING_STATES and st.get("health_ok"):
                if OPS.recovery["attempts"]:
                    log_event("watchdog.recovered_confirmed")
                OPS.reset_recovery()
                continue
            if state not in ("STOPPED",):
                continue                    # starting/stopping — let it settle
            r = OPS.recovery
            if r["gave_up"] or now() < r["next_at"]:
                continue
            if r["attempts"] >= RECOVERY_MAX_ATTEMPTS:
                r["gave_up"] = True
                log_event("watchdog.gave_up", attempts=r["attempts"])
                continue
            attempt = r["attempts"] + 1
            log_event("watchdog.recover_attempt", attempt=attempt)
            if not OPS.begin("start"):
                continue
            rc, out, err = qwenctl("start", timeout=START_TIMEOUT_S)
            ok = rc == 0
            r["attempts"] = 0 if ok else attempt
            backoff = RECOVERY_BACKOFF_S[min(attempt,
                                             len(RECOVERY_BACKOFF_S) - 1)]
            r["next_at"] = now() + backoff
            OPS.finish("start", ok,
                       "model recovered after an unexpected exit." if ok
                       else f"recovery attempt {attempt} failed: "
                            f"{_tail(err or out)}")
        except Exception as e:              # the watchdog must never die
            log_event("watchdog.error", error=str(e)[:300])


# ------------------------------------------------------------ model selection
def selection_lock_reasons(ui_state, b, peers):
    """Pure: why the selector must be read-only right now (empty = free)."""
    reasons = []
    if ui_state != "Stopped":
        reasons.append(f"AI Inference is {ui_state}")
    if b.get("active"):
        reasons.append(f"Qwen-B is {b.get('state')}" + (" (listening)" if b.get("listening") else ""))
    if peers:
        reasons.append("a model runtime process is present")
    return reasons


def models_summary():
    """Registry + selection + availability (no slot observation; cheap)."""
    try:
        reg = nm.load_registry()
        st = nm.status(reg, probe_runtime=True, occupancy=False)
        sel = st["selected"]
        return {
            "selected": {"id": sel, "display_name": reg["models"][sel]["display_name"],
                         "runtime": reg["models"][sel]["runtime"],
                         "source": st["selection_source"]},
            "default": st["default"],
            "models": st["models"],
            "error": None,
        }
    except Exception as e:                      # registry trouble must be visible
        return {"selected": None, "default": None, "models": {}, "error": str(e)[:300]}


# ------------------------------------------------------------------ status
def assemble_status():
    st = qwen_status()
    desired = effective_desired(read_desired_record(), boot_time_sec())
    op = OPS.current
    ui_state = map_ui_state(st.get("state"), st.get("health_ok"),
                            desired, op["name"] if op else None,
                            OPS.recovery["gave_up"])
    b = qwen_b_probe()
    legacy = legacy_daemon_probe()
    ms = models_summary()
    identity = resident_identity()
    lock_reasons = selection_lock_reasons(ui_state, b, st.get("peer_processes"))
    return {
        "model": {
            "state": ui_state,
            "controller_state": st.get("state"),
            "desired": desired,
            "pid": st.get("pid"),
            "rss_gib": st.get("rss_gib"),
            "endpoint": st.get("endpoint"),
            "alias": identity["routing_alias"],
            "running_model": identity.get("registry_id"),
            "running_runtime": identity.get("backend"),
            "identity": identity,
            "health_ok": bool(st.get("health_ok")),
            "error": st.get("error"),
            "recovery": dict(OPS.recovery),
        },
        "selection": ms["selected"],
        "models": ms["models"],
        "registry_error": ms["error"],
        "selection_locked": bool(lock_reasons),
        "selection_lock_reasons": lock_reasons,
        "operation": op,
        "last_operation": OPS.last,
        "hermes": hermes_probe(),
        "openwebui": webui_probe(),
        "chat_url": WEBUI_URL,
        "qwen_b": b,
        "legacy_daemon": legacy,
        "memory": memory_probe(),
        "control": {"port": SERVER_PORT, "pid": os.getpid(),
                    "boot_sec": boot_time_sec()},
        "ts": now(),
    }


# ------------------------------------------------------------------ HTTP
SERVER_PORT = DEFAULT_PORT


class Handler(BaseHTTPRequestHandler):
    server_version = "NewtonControl/1.0"

    # ---- helpers
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, indent=2))

    def _host_ok(self):
        host = (self.headers.get("Host") or "").strip().lower()
        return host in (f"127.0.0.1:{SERVER_PORT}", f"localhost:{SERVER_PORT}")

    def _mutation_ok(self):
        if not self._host_ok():
            return False
        if self.headers.get("X-Newton-Control") != "1":
            return False
        origin = self.headers.get("Origin")
        if origin and origin.rstrip("/") not in (
                f"http://127.0.0.1:{SERVER_PORT}",
                f"http://localhost:{SERVER_PORT}"):
            return False
        return True

    def log_message(self, fmt, *args):   # quiet; JSONL log carries the signal
        pass

    # ---- routes
    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "bad host header"})
        path = self.path.split("?", 1)[0]
        if path == "/":
            try:
                with open(PANEL_HTML, "rb") as f:
                    return self._send(200, f.read(), "text/html")
            except OSError:
                return self._send(500, "control-panel.html missing", "text/plain")
        if path == "/api/status":
            return self._json(200, assemble_status())
        if path == "/api/identity":
            identity = resident_identity()
            return self._json(200, identity)
        if path == "/api/models":
            try:
                reg = nm.load_registry()
                return self._json(200, nm.status(reg))
            except nm.RegistryError as e:
                return self._json(500, {"error": f"registry invalid: {e}"})
        if path == "/chat":
            self.send_response(302)
            self.send_header("Location", WEBUI_URL)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._mutation_ok():
            return self._json(403, {"error": "forbidden (same-origin panel "
                                             "requests only)"})
        path = self.path.split("?", 1)[0]
        if path == "/api/model/select":
            return self._select()
        ops = {"/api/model/start": ("start", op_start),
               "/api/model/stop": ("stop", op_stop),
               "/api/model/restart": ("restart", op_restart)}
        if path not in ops:
            return self._json(404, {"error": "not found"})
        name, fn = ops[path]
        if run_op(name, fn):
            return self._json(202, {"ok": True, "operation": name})
        return self._json(409, {"ok": False,
                                "error": "another operation is in progress",
                                "operation": OPS.current})

    def _select(self):
        """Change the global model selection. Never starts anything.
        Refused (409) while either slot is occupied, (422) when the model is
        not installed/intact/runnable, (400) for an unknown id."""
        if OPS.current:
            return self._json(409, {"ok": False, "error": "an operation is in progress"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode() or "{}") if n else {}
            model_id = str(body.get("model", ""))
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"ok": False, "error": "malformed JSON body"})
        try:
            reg = nm.load_registry()
            rec = nm.select_model(reg, model_id, by="panel")
        except nm.RegistryError as e:
            return self._json(500, {"ok": False, "error": f"registry invalid: {e}"})
        except nm.SelectionError as e:
            http = {nm.EXIT_INVALID: 400, nm.EXIT_LOCKED: 409,
                    nm.EXIT_UNAVAILABLE: 422}.get(e.code, 400)
            log_event("select.refused", model=model_id, error=str(e)[:300])
            return self._json(http, {"ok": False, "error": str(e)})
        log_event("select", model=model_id, by="panel")
        return self._json(200, {"ok": True, "selected": rec["selected"],
                                "display_name": reg["models"][rec["selected"]]["display_name"],
                                "note": "selection saved; nothing was started"})


def main():
    global SERVER_PORT
    port = DEFAULT_PORT
    args = sys.argv[1:]
    if "--port" in args:
        port = int(args[args.index("--port") + 1])
    SERVER_PORT = port

    os.makedirs(LOGDIR, exist_ok=True)
    log_event("service.start", port=port, root=ROOT)

    threading.Thread(target=watchdog_loop, name="watchdog", daemon=True).start()

    srv = ThreadingHTTPServer((BIND_HOST, port), Handler)
    srv.daemon_threads = True
    print(f"Newton AI Control listening on http://{BIND_HOST}:{port}/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
