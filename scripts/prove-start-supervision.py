#!/usr/bin/env python3
"""Newton_Local — prove start supervision against REAL launchd, FAKE engine.

The 2026-10-12 incident: mlx-serve refused a Flash-Next load within a second
(not enough free memory) but the panel sat in "Starting" for the full 600 s
start timeout, Stop could not run until then, and a status poll overwrote the
controller record. This prover exercises the fixes end to end — the real
controller, the real exec-slot shim, real launchd jobs, the real panel
service — with a compiled fake engine (tests/fixtures/fake_engine.c) that
contains no model code, on fixture ports, under fixture launchd labels:

    slot A  com.newton.test.supervision.a  127.0.0.1:18080
    panel   scripts/newton_control.py      127.0.0.1:18765

Everything lives in a throwaway root (a copy of the scripts plus a fixture
registry, selection, leases and logs). The production slots, their leases,
the operator's model selection and desired state are never read for writing
or touched. A guard samples the process table throughout and FAILS the run if
any real llama-server/mlx-serve process appears that was not already there.

SCENARIOS
    fast_fail        engine refuses for memory  -> exit 8 in seconds, reason
    terminate        terminate during a hung load -> start exits 9 at once
    cancel_verb      lock-free cancel           -> start exits 9
    queued_start     a stop that lands while a start waits for the lock
                     wins: the start is refused and nothing is spawned
    healthy_cas      a healthy start is not mistaken for an exit, and status
                     polls during it never overwrite STARTING
    panel            the panel shows the engine's reason; Stop during a start
                     cancels it; the memory hint warns before Start

USAGE
    scripts/prove-start-supervision.py [--keep-panel] [--evidence FILE]
    scripts/prove-start-supervision.py --cleanup ROOT
"""
import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UID = os.getuid()
LABEL_A = "com.newton.test.supervision.a"
PORT_A, PORT_B, PANEL_PORT = 18080, 18081, 18765
PY = "/usr/bin/python3"
# qwenctl consults the shared resource policy before every start (docs/18 §4)
# and refuses when it cannot load it, so the fixture carries it too.
COPY_SCRIPTS = ["qwenctl.py", "newton_models.py", "newton_control.py",
                "control-panel.html", "resident_identity.py",
                "resource_policy.py", "fish_config.py"]
REAL_RUNTIMES = [os.path.join(REPO, "tools/llama.cpp/build/bin/llama-server"),
                 os.path.join(REPO, "tools/mlx-serve/current/mlx-serve")]
REAL_RUNTIMES += [os.path.realpath(p) for p in REAL_RUNTIMES]

RESULTS = []


def now():
    return time.time()


def sh(cmd, timeout=120, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    return r.returncode, r.stdout, r.stderr


def check(name, ok, detail=""):
    RESULTS.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return ok


# ------------------------------------------------------------ real-model guard
class RealModelGuard(threading.Thread):
    """Samples ps every 0.5 s for any process exec'd from a REAL runtime."""

    def __init__(self):
        super().__init__(daemon=True)
        self.baseline = self.sample()
        self.new = {}
        self.samples = 0
        self.stop_flag = threading.Event()

    @staticmethod
    def sample():
        rc, out, _ = sh(["ps", "-Ao", "pid=,command="], timeout=20)
        found = {}
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and any(parts[1] == s or parts[1].startswith(s + " ")
                                       for s in REAL_RUNTIMES):
                if " --version" not in parts[1]:
                    found[int(parts[0])] = parts[1][:160]
        return found

    def run(self):
        while not self.stop_flag.is_set():
            for pid, cmd in self.sample().items():
                if pid not in self.baseline:
                    self.new[pid] = cmd
            self.samples += 1
            self.stop_flag.wait(0.5)


# ----------------------------------------------------------------- fixture
def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def build_fixture(root):
    for d in ("scripts", "config", "state", "logs/qwen-a", "logs/qwen-b",
              "logs/control", "tools/fake", "models/fake"):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    for name in COPY_SCRIPTS:
        shutil.copy2(os.path.join(REPO, "scripts", name),
                     os.path.join(root, "scripts", name))
    shutil.copytree(os.path.join(REPO, "integrations", "newton_runtime"),
                    os.path.join(root, "integrations", "newton_runtime"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    exe = os.path.join(root, "tools", "fake", "fake-engine")
    rc, out, err = sh(["cc", "-O2", "-Wall", "-o", exe,
                       os.path.join(REPO, "tests", "fixtures", "fake_engine.c")])
    if rc != 0:
        sys.exit(f"cannot compile the fake engine: {err.strip()}")
    write(os.path.join(root, "models/fake/weights.bin"), "fixture-not-a-model\n")
    write(os.path.join(root, "models/fake/MANIFEST.json"), json.dumps(
        {"files": [{"name": "weights.bin", "size": 20}]}))

    def model(name, mode, required_gib, extra=()):
        return {"display_name": name, "runtime": "fake-engine",
                "path": "models/fake", "manifest": "models/fake/MANIFEST.json",
                "context_length": 4096, "start_timeout_s": 60,
                "memory": {"admission": {"rule": "fixed",
                                         "required_gib": required_gib,
                                         "basis": "fixture"}},
                "args": ["--mode", mode, *extra, "--host", "{host}",
                         "--port", "{port}", "--alias", "{alias}"]}
    reg = {
        "_comment": ["prove-start-supervision fixture registry — fake engine only"],
        "schema": 1, "default": "fake-ok",
        "slots": {
            "A": {"label": LABEL_A, "host": "127.0.0.1", "port": PORT_A,
                  "alias": "fixture-a", "display_name": "Fixture A",
                  "logdir": "logs/qwen-a"},
            "B": {"label": "com.newton.test.supervision.b", "host": "127.0.0.1",
                  "port": PORT_B, "alias": "fixture-b", "logdir": "logs/qwen-b"}},
        "runtimes": {"fake-engine": {
            "display_name": "fake engine (test fixture, no model code)",
            "executable": "tools/fake/fake-engine", "version_args": ["--version"],
            "health": {"path": "/health", "ready": "json_status_ok",
                       "identity_path": "/props"}}},
        "models": {
            "fake-oom": model("Fake OOM", "oom", 100000),
            "fake-hang": model("Fake Hang", "hang", 0.001),
            "fake-ok": model("Fake OK", "ok", 0.001, ("--load-seconds", "3")),
        }}
    write(os.path.join(root, "config/models.json"), json.dumps(reg, indent=2))
    for slot, port, ttl in (("A", PORT_A, 0), ("B", PORT_B, 180)):
        lower = slot.lower()
        write(os.path.join(root, f"config/{'qwen-a-controller' if slot == 'A' else 'qwen-b'}.env"),
              "\n".join([
                  f"ALIAS=fixture-{lower}", "HOST=127.0.0.1", f"PORT={port}",
                  "CTX=4096", f"CTL_INSTANCE=fixture-{lower}",
                  f"CTL_LABEL=com.newton.test.supervision.{lower}",
                  f"CTL_LOGDIR={root}/logs/qwen-{lower}",
                  f"CTL_LEASE={root}/state/qwen-{lower}-lease.json",
                  f"CTL_LOCK={root}/state/qwen-{lower}.lock",
                  f"CTL_IDLE_TTL={ttl}", "CTL_START_TIMEOUT=60",
                  "CTL_LEASE_MAX_AGE=1800", f"CTL_SLOT={slot}", ""]))
    plist = {
        "Label": LABEL_A,
        "ProgramArguments": [PY, os.path.join(root, "scripts/newton_models.py"),
                             "exec-slot", "A"],
        "RunAtLoad": False, "KeepAlive": False, "WorkingDirectory": root,
        "StandardOutPath": os.path.join(root, "logs/qwen-a/stdout.log"),
        "StandardErrorPath": os.path.join(root, "logs/qwen-a/stderr.log"),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        "ExitTimeOut": 30}
    ppath = os.path.join(root, f"{LABEL_A}.plist")
    with open(ppath, "wb") as f:
        plistlib.dump(plist, f)
    rc, out, err = sh(["launchctl", "bootstrap", f"gui/{UID}", ppath])
    if rc != 0:
        sys.exit(f"cannot bootstrap the fixture job: {(out + err).strip()}")
    return root


def teardown(root, panel=None):
    if panel and panel.poll() is None:
        panel.terminate()
        try:
            panel.wait(10)
        except subprocess.TimeoutExpired:
            panel.kill()
    if os.path.isdir(root):
        ctl(root, "terminate", "--force", "--by", "prover-teardown", timeout=90)
    sh(["launchctl", "bootout", f"gui/{UID}/{LABEL_A}"])
    rc, out, _ = sh(["pgrep", "-f", os.path.join(root, "tools/fake/fake-engine")])
    for pid in out.split():
        sh(["kill", "-KILL", pid])


# ------------------------------------------------------------------- drivers
def ctl_cmd(root, *args):
    return [PY, os.path.join(root, "scripts/qwenctl.py"), "--config",
            os.path.join(root, "config/qwen-a-controller.env"), *args]


def ctl(root, *args, timeout=120):
    return sh(ctl_cmd(root, *args), timeout=timeout)


def select(root, model_id):
    rc, out, err = sh([PY, os.path.join(root, "scripts/newton_models.py"),
                       "select", model_id, "--by", "prover"])
    if rc != 0:
        sys.exit(f"fixture select {model_id} failed: {(out + err).strip()}")


def lease(root):
    try:
        with open(os.path.join(root, "state/qwen-a-lease.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def launchd_runs():
    rc, out, _ = sh(["launchctl", "print", f"gui/{UID}/{LABEL_A}"])
    for line in out.splitlines():
        if line.strip().startswith("runs ="):
            return int(line.split("=")[1])
    return None


def engine_pids(root):
    rc, out, _ = sh(["pgrep", "-f", os.path.join(root, "tools/fake/fake-engine") + " --mode"])
    return [int(p) for p in out.split()]


def wait_for(pred, timeout, step=0.1):
    dl = now() + timeout
    while now() < dl:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def as_json(text):
    try:
        return json.loads(text)
    except ValueError:
        return {}


# ----------------------------------------------------------------- scenarios
def scenario_fast_fail(root):
    print("fast_fail — the engine refuses for lack of memory")
    select(root, "fake-oom")
    runs0 = launchd_runs()
    t0 = now()
    rc, out, err = ctl(root, "start", "--json", timeout=120)
    wall = now() - t0
    res = as_json(out)
    reason = res.get("reason") or {}
    check("start exits 8 (engine exited while loading)", rc == 8, f"rc={rc}")
    check("failure reported within 10 s (was the full start timeout)", wall < 10,
          f"{wall:.1f}s")
    check("engine's own reason extracted",
          reason.get("kind") == "insufficient_memory"
          and (reason.get("required_gib"), reason.get("available_gib")) == (77.1, 69.0),
          reason.get("summary", ""))
    check("launchd ran the job exactly once", launchd_runs() == (runs0 or 0) + 1,
          f"runs {runs0} -> {launchd_runs()}")
    check("record is STOPPED with the reason in its notes",
          lease(root).get("state") == "STOPPED"
          and "not enough free memory" in " ".join(lease(root).get("notes") or []))
    check("no engine process left", not engine_pids(root))
    return {"wall_s": round(wall, 2), "rc": rc}


def hung_start(root):
    select(root, "fake-hang")
    p = subprocess.Popen(ctl_cmd(root, "start", "--json"), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    up = wait_for(lambda: lease(root).get("state") == "STARTING" and engine_pids(root), 15)
    return p, bool(up)


def scenario_terminate(root):
    print("terminate — Stop during a load that never finishes")
    p, up = hung_start(root)
    check("hung engine is loading under launchd", up)
    t0 = now()
    rc, out, err = ctl(root, "terminate", "--json", "--by", "prover", timeout=120)
    t_term = now() - t0
    try:
        out_s, _ = p.communicate(timeout=30)
        rc_s = p.returncode
    except subprocess.TimeoutExpired:
        p.kill()
        out_s, rc_s = "", None
    check("terminate succeeds", rc == 0, f"rc={rc}")
    check("terminate returns within 10 s (no longer queued behind the load)",
          t_term < 10, f"{t_term:.1f}s")
    check("the start exits 9 (cancelled)", rc_s == 9, f"rc={rc_s}")
    check("record is STOPPED, no engine process left",
          lease(root).get("state") == "STOPPED" and not engine_pids(root))
    return {"terminate_wall_s": round(t_term, 2)}


def scenario_cancel_verb(root):
    print("cancel_verb — the lock-free cancel the panel uses")
    p, up = hung_start(root)
    check("hung engine is loading under launchd", up)
    t0 = now()
    rc, out, err = ctl(root, "cancel", "--json", "--by", "prover", timeout=30)
    t_cancel = now() - t0
    try:
        p.communicate(timeout=30)
        rc_s = p.returncode
    except subprocess.TimeoutExpired:
        p.kill()
        rc_s = None
    t_done = now() - t0
    check("cancel returns at once", rc == 0 and t_cancel < 3, f"rc={rc} {t_cancel:.1f}s")
    check("it reported a start in progress", as_json(out).get("start_in_progress") is True)
    check("the start exits 9 within 5 s", rc_s == 9 and t_done < 5,
          f"rc={rc_s} {t_done:.1f}s")
    check("record is STOPPED, no engine process left",
          wait_for(lambda: lease(root).get("state") == "STOPPED"
                   and not engine_pids(root), 10) is not None)
    return {"cancel_to_stopped_s": round(t_done, 2)}


def scenario_queued_start(root):
    print("queued_start — a stop that lands while a start waits for the lock")
    select(root, "fake-ok")
    runs0 = launchd_runs()
    holder = subprocess.Popen([PY, "-c", (
        "import fcntl,sys,time; f=open(sys.argv[1],'a+'); "
        "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(4)"),
        os.path.join(root, "state/qwen-a.lock")], stdout=subprocess.PIPE, text=True)
    holder.stdout.readline()
    start = subprocess.Popen(ctl_cmd(root, "start", "--json"), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    time.sleep(0.6)
    term = subprocess.Popen(ctl_cmd(root, "terminate", "--json", "--by", "prover"),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    start.communicate(timeout=60)
    term.communicate(timeout=60)
    holder.wait(10)
    check("the queued start is refused with exit 9", start.returncode == 9,
          f"rc={start.returncode}")
    check("the terminate succeeds", term.returncode == 0, f"rc={term.returncode}")
    check("nothing was spawned", launchd_runs() == runs0,
          f"runs {runs0} -> {launchd_runs()}")
    return {}


def scenario_healthy_cas(root):
    print("healthy_cas — a healthy start, with status polls racing it")
    select(root, "fake-ok")
    gen0 = int(lease(root).get("generation") or 0)
    p = subprocess.Popen(ctl_cmd(root, "start", "--json"), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    polls, regressions, seen_starting = 0, [], False
    while p.poll() is None:
        sh(ctl_cmd(root, "status", "--json"), timeout=60)
        polls += 1
        le = lease(root)
        if le.get("state") == "STARTING":
            seen_starting = True
        if seen_starting and (int(le.get("generation") or 0) != gen0 + 1
                              or le.get("state") == "STOPPED"):
            regressions.append((le.get("state"), le.get("generation")))
        time.sleep(0.2)
    out, err = p.communicate()
    le = lease(root)
    check("healthy start succeeds (no false 'exited')", p.returncode == 0,
          f"rc={p.returncode} {err.strip()[:160]}")
    check("status polls ran during the start", polls >= 3 and seen_starting,
          f"{polls} polls")
    check("no poll overwrote the start's record", not regressions, str(regressions))
    check("record is READY, generation advanced by one",
          le.get("state") == "READY" and int(le.get("generation") or 0) == gen0 + 1,
          f"{le.get('state')} gen {le.get('generation')}")
    rc, out, _ = ctl(root, "terminate", "--json", "--by", "prover")
    check("then stops cleanly", rc == 0 and not engine_pids(root))
    return {"status_polls": polls}


# --------------------------------------------------------------------- panel
def panel_req(method, path, body=None):
    url = f"http://127.0.0.1:{PANEL_PORT}{path}"
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(url, data=data, method=method)
    if method == "POST":
        req.add_header("X-Newton-Control", "1")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def panel_status():
    return panel_req("GET", "/api/status")[1]


def start_panel(root):
    log = open(os.path.join(root, "logs/control/panel.out"), "w")
    p = subprocess.Popen([PY, os.path.join(root, "scripts/newton_control.py"),
                          "--port", str(PANEL_PORT)], stdout=log, stderr=log,
                         cwd=root)
    ok = wait_for(lambda: _reachable(), 20, 0.3)
    if not ok:
        sys.exit("fixture panel did not come up")
    return p


def _reachable():
    try:
        return panel_status().get("model") is not None
    except Exception:
        return False


def wait_op_done(timeout):
    return wait_for(lambda: (lambda s: s if not s.get("operation") else None)(panel_status()),
                    timeout, 0.3)


def scenario_panel(root):
    print("panel — the operator's view")
    code, res = panel_req("POST", "/api/model/select", {"model": "fake-oom"})
    check("select Fake OOM while stopped", code == 200, f"HTTP {code}")
    s = panel_status()
    fit = (s["models"]["fake-oom"] or {}).get("memory_fit") or {}
    check("memory hint warns before Start", fit.get("verdict") == "wont_fit",
          fit.get("summary", ""))
    check("memory row is in the engine's terms", s["memory"].get("models_free_gib") is not None,
          f"{s['memory'].get('models_free_gib')} GiB free for models")
    t0 = now()
    code, res = panel_req("POST", "/api/model/start")
    check("Start accepted", code == 202, f"HTTP {code}")
    s = wait_op_done(30)
    wall = now() - t0
    lo = (s or {}).get("last_operation") or {}
    check("refused start finishes within 15 s", s is not None and wall < 15, f"{wall:.1f}s")
    check("panel shows the engine's reason in plain words",
          lo.get("ok") is False and (lo.get("message") or "").startswith(
              "Not enough free memory to load Fake OOM: it needs 77.1 GiB free and "
              "only 69.0 GiB was available."), lo.get("message", "")[:120])
    # A fresh snapshot: the one that first shows the operation finished may
    # carry a controller state observed a moment before it finished.
    s2 = panel_status()
    check("model is Stopped and desired STOPPED",
          s2["model"]["state"] == "Stopped" and s2["model"]["desired"] == "STOPPED",
          f"{s2['model']['state']} / {s2['model']['desired']}")

    code, _ = panel_req("POST", "/api/model/select", {"model": "fake-hang"})
    code, _ = panel_req("POST", "/api/model/start")
    starting = wait_for(lambda: (lambda s: s if s["model"]["state"] == "Starting"
                                 and lease(root).get("state") == "STARTING"
                                 and engine_pids(root) else None)(panel_status()), 20, 0.3)
    check("hung start shows Starting", starting is not None)
    op = (starting or {}).get("operation") or {}
    check("Stop is offered as a cancel during the start",
          op.get("name") == "start" and op.get("cancelling") is False)
    t0 = now()
    code, res = panel_req("POST", "/api/model/stop")
    check("Stop during Starting is accepted as a cancel",
          code == 202 and res.get("operation") == "cancel", f"HTTP {code} {res}")
    s1 = panel_status()
    check("panel shows Stopping while cancelling", s1["model"]["state"] in ("Stopping", "Stopped"),
          s1["model"]["state"])
    s = wait_op_done(30)
    wall = now() - t0
    lo = (s or {}).get("last_operation") or {}
    check("cancel completes within 10 s", s is not None and wall < 10, f"{wall:.1f}s")
    check("reported as a cancel, not a failure",
          lo.get("ok") is True and lo.get("cancelled") is True
          and lo.get("message") == "Start cancelled; the model is stopped.",
          lo.get("message", ""))
    check("model Stopped, no engine process left",
          s["model"]["state"] == "Stopped" and not engine_pids(root))
    return {"cancel_wall_s": round(wall, 2)}


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--keep-panel", action="store_true",
                    help="leave the fixture panel on :18765 (and the fixture) for a "
                         "manual look; remove later with --cleanup ROOT")
    ap.add_argument("--evidence", help="write the results as JSON here")
    ap.add_argument("--cleanup", metavar="ROOT", help="tear down a kept fixture")
    args = ap.parse_args()

    if args.cleanup:
        root = os.path.realpath(args.cleanup)
        if not os.path.isfile(os.path.join(root, f"{LABEL_A}.plist")):
            sys.exit(f"{root} is not a prove-start-supervision fixture")
        rc, out, _ = sh(["pgrep", "-f", os.path.join(root, "scripts/newton_control.py")])
        for pid in out.split():
            sh(["kill", "-TERM", pid])
        teardown(root)
        shutil.rmtree(root, ignore_errors=True)
        print(f"removed fixture {root}")
        return 0

    for port in (PORT_A, PORT_B, PANEL_PORT):
        rc, out, _ = sh(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"])
        if out.strip():
            sys.exit(f"port {port} is in use; refusing to run")
    rc, _, _ = sh(["launchctl", "print", f"gui/{UID}/{LABEL_A}"])
    if rc == 0:
        sys.exit(f"{LABEL_A} is already loaded (a kept fixture?); run --cleanup first")

    root = os.path.realpath(tempfile.mkdtemp(prefix="newton-supervision-"))
    guard = RealModelGuard()
    real_8080 = bool(sh(["lsof", "-nP", "-iTCP:8080", "-sTCP:LISTEN"])[1].strip())
    print(f"fixture root {root}")
    build_fixture(root)
    guard.start()
    panel = None
    summary = {}
    try:
        summary["fast_fail"] = scenario_fast_fail(root)
        summary["terminate"] = scenario_terminate(root)
        summary["cancel_verb"] = scenario_cancel_verb(root)
        summary["queued_start"] = scenario_queued_start(root)
        summary["healthy_cas"] = scenario_healthy_cas(root)
        panel = start_panel(root)
        summary["panel"] = scenario_panel(root)
    finally:
        guard.stop_flag.set()
        guard.join(5)
        if not args.keep_panel:
            teardown(root, panel)
    print("guard — real models")
    check("no real llama-server/mlx-serve process appeared during the run",
          not guard.new, json.dumps(guard.new) if guard.new else
          f"{guard.samples} samples")
    check("production port 8080 unchanged",
          bool(sh(["lsof", "-nP", "-iTCP:8080", "-sTCP:LISTEN"])[1].strip()) == real_8080)

    failed = [r for r in RESULTS if not r["ok"]]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    if args.evidence:
        with open(args.evidence, "w") as f:
            json.dump({"when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                       "fixture_root": root, "summary": summary,
                       "results": RESULTS,
                       "real_model_processes_new": guard.new}, f, indent=2)
    if args.keep_panel:
        print(f"fixture panel kept at http://127.0.0.1:{PANEL_PORT}/ — "
              f"remove with: {sys.argv[0]} --cleanup {root}")
    else:
        shutil.rmtree(root, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
