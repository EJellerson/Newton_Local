#!/usr/bin/env python3
"""Newton_Local — prove the Fish/Qwen resource policy and the Fish lifecycle
against REAL launchd jobs and controllers, with FAKE engines (docs/18 §4).

No model is ever loaded. The Qwen slots run the compiled fake engine
(tests/fixtures/fake_engine.c) registered as "qwen27b" and "flash-next"; the
Fish service runs the real fish_engine.py in fixture mode (a synthesiser that
makes a tone, allowed only with a configuration marked "fixture"). Everything
lives in a throwaway root — copies of the scripts, a fixture registry, a
fixture config/fish.json, its own admission lock, leases, desired-state
files, logs and voice store — under fixture launchd labels and ports:

    slot A   com.newton.test.fishpolicy.a      127.0.0.1:18082
    slot B   com.newton.test.fishpolicy.b      127.0.0.1:18083
    Fish     com.newton.test.fishpolicy.fish   127.0.0.1:18771
    panel    scripts/newton_control.py         127.0.0.1:18766

Production slots, the production Fish service, their records and the
operator's selections are never written. A guard samples the process table
throughout and FAILS the run if a real llama-server, mlx-serve or a Fish
engine outside the fixture root appears. Before starting, the prover refuses
to run while any real model runtime or Fish engine is up (the policy would,
correctly, count it).

USAGE
    scripts/prove-fish-policy.py [--evidence FILE] [--rounds N] [--keep]
"""
import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UID = os.getuid()
PY = "/usr/bin/python3"
LABEL_A = "com.newton.test.fishpolicy.a"
LABEL_B = "com.newton.test.fishpolicy.b"
LABEL_FISH = "com.newton.test.fishpolicy.fish"
PORT_A, PORT_B, PORT_FISH, PANEL_PORT = 18082, 18083, 18771, 18766
LOAD_S = 2.0
COPY_SCRIPTS = ["qwenctl.py", "newton_models.py", "newton_control.py", "control-panel.html",
                "resident_identity.py", "resource_policy.py", "fish_config.py", "fishctl.py",
                "fish_engine.py", "fish_audio.py", "voice_studio.py", "voice_store.py"]
COPY_DIRS = ["fish_tts", "voice-studio"]
REAL_RUNTIMES = [os.path.join(REPO, "tools/llama.cpp/build/bin/llama-server"),
                 os.path.join(REPO, "tools/mlx-serve/current/mlx-serve")]
REAL_RUNTIMES += [os.path.realpath(p) for p in REAL_RUNTIMES]
VENV_PY = os.path.realpath(os.path.join(REPO, "tools/fish-venv/bin/python3"))

RESULTS = []


def now():
    return time.time()


def sh(cmd, timeout=180, **kw):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"


def check(name, ok, detail=""):
    RESULTS.append({"check": name, "ok": bool(ok), "detail": str(detail)[:400]})
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return ok


def as_json(text):
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def boot_sec():
    _, out, _ = sh(["sysctl", "-n", "kern.boottime"])
    m = re.search(r"sec\s*=\s*(\d+)", out)
    return int(m.group(1)) if m else 0


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def wait_for(pred, timeout, step=0.1):
    dl = now() + timeout
    while now() < dl:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


# ------------------------------------------------------------- processes
def ps_lines():
    _, out, _ = sh(["ps", "-Ao", "pid=,command="], timeout=20)
    return [l.strip() for l in out.splitlines() if l.strip()]


def procs(root):
    """{'fish': engine pids, 'fish_shim': launch-shim pids, 'qwen27b': pids,
    'flash-next': pids} for THIS root. The shim (fishctl exec-engine) is the
    gatekeeper that decides whether to exec the engine; it is not a model."""
    out = {"fish": [], "fish_shim": [], "qwen27b": [], "flash-next": []}
    eng = os.path.join(root, "scripts/fish_engine.py")
    shim = os.path.join(root, "scripts/fishctl.py")
    fake = os.path.join(root, "tools/fake/fake-engine")
    for line in ps_lines():
        pid, _, cmd = line.partition(" ")
        if eng + " --config" in cmd:
            out["fish"].append(int(pid))
        elif shim in cmd and "exec-engine" in cmd:
            out["fish_shim"].append(int(pid))
        elif cmd.startswith(fake + " ") and "--version" not in cmd:
            if "/models/fake-flash" in cmd:
                out["flash-next"].append(int(pid))
            elif "/models/fake-27b" in cmd:
                out["qwen27b"].append(int(pid))
    return out


class Guard(threading.Thread):
    """Real-model guard plus the invariant sampler: a Fish engine and the
    fixture Flash-Next must never be alive at the same moment."""

    def __init__(self, root):
        super().__init__(daemon=True)
        self.root = root
        self.baseline = self.real()
        self.new_real = {}
        self.overlaps = []
        self.shim_checks = 0
        self.samples = 0
        self.stop_flag = threading.Event()

    @staticmethod
    def real(exclude_root=None):
        """Real model runtimes and Fish engines; with exclude_root, those of
        the fixture root are left out (matched on the full command line)."""
        found = {}
        for line in ps_lines():
            pid, _, cmd = line.partition(" ")
            if exclude_root and exclude_root in cmd:
                continue
            if any(cmd == s or cmd.startswith(s + " ") for s in REAL_RUNTIMES) and \
                    "--version" not in cmd:
                found[int(pid)] = cmd[:200]
            elif "/scripts/fish_engine.py --config" in cmd:
                found[int(pid)] = cmd[:200]
        return found

    def run(self):
        while not self.stop_flag.is_set():
            for pid, cmd in self.real(exclude_root=self.root).items():
                if pid not in self.baseline:
                    self.new_real[pid] = cmd
            p = procs(self.root)
            if p["fish"] and p["flash-next"]:                 # engines, not the gatekeeper shim
                self.overlaps.append({"at": round(now(), 2), **p})
            if p["fish_shim"] and p["flash-next"]:
                self.shim_checks += 1
            self.samples += 1
            self.stop_flag.wait(0.1)


# ----------------------------------------------------------------- fixture
def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def manifest(root, rel, name, text):
    write(os.path.join(root, rel, name), text)
    write(os.path.join(root, rel, "MANIFEST.json"), json.dumps(
        {"files": [{"name": name, "size": len(text.encode())}],
         "total_bytes": len(text.encode())}))


def build_fixture(root):
    for d in ("scripts", "config", "state/fish", "logs/qwen-a", "logs/qwen-b", "logs/control",
              "logs/fish", "tools/fake", "store"):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    for name in COPY_SCRIPTS:
        shutil.copy2(os.path.join(REPO, "scripts", name), os.path.join(root, "scripts", name))
    for name in COPY_DIRS:
        shutil.copytree(os.path.join(REPO, "scripts", name), os.path.join(root, "scripts", name),
                        ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(os.path.join(REPO, "integrations", "newton_runtime"),
                    os.path.join(root, "integrations", "newton_runtime"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(os.path.join(REPO, "config", "fish-engine.sb"),
                 os.path.join(root, "config", "fish-engine.sb"))
    exe = os.path.join(root, "tools", "fake", "fake-engine")
    rc, out, err = sh(["cc", "-O2", "-Wall", "-o", exe,
                       os.path.join(REPO, "tests", "fixtures", "fake_engine.c")])
    if rc != 0:
        sys.exit(f"cannot compile the fake engine: {err.strip()}")
    manifest(root, "models/fake-27b", "weights.bin", "fixture-not-a-model-27b\n")
    manifest(root, "models/fake-flash", "weights.bin", "fixture-not-a-model-flash\n")
    manifest(root, "models/fish-fixture", "weights.bin", "fixture-not-a-model-fish\n")

    def model(name, path):
        return {"display_name": name, "runtime": "fake-engine", "path": path,
                "manifest": path + "/MANIFEST.json", "context_length": 4096,
                "start_timeout_s": 60,
                "memory": {"admission": {"rule": "fixed", "required_gib": 0.001,
                                         "basis": "fixture"}},
                "args": ["--mode", "ok", "--load-seconds", str(LOAD_S), "--model", "{path}",
                         "--host", "{host}", "--port", "{port}", "--alias", "{alias}"]}
    reg = {"_comment": ["prove-fish-policy fixture registry: fake engine only"],
           "schema": 1, "default": "qwen27b",
           "slots": {"A": {"label": LABEL_A, "host": "127.0.0.1", "port": PORT_A,
                           "alias": "fixture-a", "display_name": "Fixture A",
                           "logdir": "logs/qwen-a"},
                     "B": {"label": "com.newton.test.fishpolicy.b", "host": "127.0.0.1",
                           "port": PORT_B, "alias": "fixture-b", "logdir": "logs/qwen-b"}},
           "runtimes": {"fake-engine": {
               "display_name": "fake engine (test fixture, no model code)",
               "executable": "tools/fake/fake-engine", "version_args": ["--version"],
               "health": {"path": "/health", "ready": "json_status_ok",
                          "identity_path": "/props"}}},
           "models": {"qwen27b": model("Fixture 27B", "models/fake-27b"),
                      "flash-next": model("Fixture Flash-Next", "models/fake-flash")}}
    write(os.path.join(root, "config/models.json"), json.dumps(reg, indent=2))
    for slot, port, ttl in (("A", PORT_A, 0), ("B", PORT_B, 180)):
        lower = slot.lower()
        write(os.path.join(root, f"config/{'qwen-a-controller' if slot == 'A' else 'qwen-b'}.env"),
              "\n".join([f"ALIAS=fixture-{lower}", "HOST=127.0.0.1", f"PORT={port}", "CTX=4096",
                         f"CTL_INSTANCE=fixture-{lower}",
                         f"CTL_LABEL=com.newton.test.fishpolicy.{lower}",
                         f"CTL_LOGDIR={root}/logs/qwen-{lower}",
                         f"CTL_LEASE={root}/state/qwen-{lower}-lease.json",
                         f"CTL_LOCK={root}/state/qwen-{lower}.lock",
                         f"CTL_IDLE_TTL={ttl}", "CTL_START_TIMEOUT=60",
                         "CTL_LEASE_MAX_AGE=1800", f"CTL_SLOT={slot}", ""]))
    with open(os.path.join(REPO, "config", "fish.json")) as f:
        fish = json.load(f)
    fish["fixture"] = True
    fish["fake"] = {"enabled": True, "load_s": LOAD_S, "frames_per_s": 400}
    fish["service"].update(label=LABEL_FISH, port=PORT_FISH, logdir="logs/fish",
                           statedir="state/fish", store=os.path.join(root, "store"),
                           start_timeout_s=60, stop_grace_s=10)
    rt = fish["runtimes"]["mlx-audio-fish-omni"]
    rt.update(python=VENV_PY, install_record="state/fish-install-fixture.json")
    m = fish["models"]["drama3-q4"]
    m.update(path="models/fish-fixture", manifest="models/fish-fixture/MANIFEST.json")
    m["memory"] = {"required_gib": 1.0, "basis": "fixture"}
    write(os.path.join(root, "config/fish.json"), json.dumps(fish, indent=2))
    write_json(os.path.join(root, "state/fish-install-fixture.json"),
               {"mlx_audio_commit": rt["source"]["commit"],
                "packages": dict(rt["pinned_versions"]), "fixture": True})
    for label, args, logdir in (
            (LABEL_A, [PY, os.path.join(root, "scripts/newton_models.py"), "exec-slot", "A"],
             "logs/qwen-a"),
            (LABEL_B, [PY, os.path.join(root, "scripts/newton_models.py"), "exec-slot", "B"],
             "logs/qwen-b"),
            (LABEL_FISH, [PY, os.path.join(root, "scripts/fishctl.py"), "--config",
                          os.path.join(root, "config/fish.json"), "exec-engine"], "logs/fish")):
        plist = {"Label": label, "ProgramArguments": args, "RunAtLoad": False,
                 "KeepAlive": False, "WorkingDirectory": root,
                 "StandardOutPath": os.path.join(root, logdir, "stdout.log"),
                 "StandardErrorPath": os.path.join(root, logdir, "stderr.log"),
                 "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
                 "ExitTimeOut": 30, "ThrottleInterval": 1}
        ppath = os.path.join(root, f"{label}.plist")
        with open(ppath, "wb") as f:
            plistlib.dump(plist, f)
        sh(["launchctl", "bootout", f"gui/{UID}/{label}"])
        rc, out, err = sh(["launchctl", "bootstrap", f"gui/{UID}", ppath])
        if rc != 0:
            sys.exit(f"cannot bootstrap {label}: {(out + err).strip()}")
    return root


def set_fish(root, **fake):
    path = os.path.join(root, "config/fish.json")
    cfg = read_json(path)
    if "required_gib" in fake:
        cfg["models"]["drama3-q4"]["memory"]["required_gib"] = fake.pop("required_gib")
    cfg["fake"] = {"enabled": True, "load_s": LOAD_S, "frames_per_s": 400}
    cfg["fake"].update(fake)
    write_json(path, cfg)


# ------------------------------------------------------------------ drivers
def fish(root, *args, timeout=150):
    return sh([PY, os.path.join(root, "scripts/fishctl.py"), "--config",
               os.path.join(root, "config/fish.json"), *args], timeout=timeout)


def fish_cmd(root, *args):
    return [PY, os.path.join(root, "scripts/fishctl.py"), "--config",
            os.path.join(root, "config/fish.json"), *args]


def qwen_cmd(root, *args, slot="A"):
    env = "qwen-a-controller.env" if slot == "A" else "qwen-b.env"
    return [PY, os.path.join(root, "scripts/qwenctl.py"), "--config",
            os.path.join(root, "config", env), *args]


def qwen(root, *args, slot="A", timeout=150):
    return sh(qwen_cmd(root, *args, slot=slot), timeout=timeout)


def select(root, model_id):
    rc, out, err = sh([PY, os.path.join(root, "scripts/newton_models.py"), "select", model_id,
                       "--by", "prover"])
    if rc != 0:
        sys.exit(f"fixture select {model_id} failed: {(out + err).strip()}")


def launchd(label):
    _, out, _ = sh(["launchctl", "print", f"gui/{UID}/{label}"])
    runs = re.search(r"^\s*runs = (\d+)", out, re.M)
    code = re.search(r"last exit code = (\d+)", out)
    return {"runs": int(runs.group(1)) if runs else None,
            "last_exit": int(code.group(1)) if code else None}


def fish_state(root):
    return read_json(os.path.join(root, "state/fish/state.json")).get("state", "STOPPED")


def fish_desired(root):
    return read_json(os.path.join(root, "state/fish/desired.json"))


def lease(root, slot="A"):
    return read_json(os.path.join(root, f"state/qwen-{slot.lower()}-lease.json"))


def all_stopped(root):
    fish(root, "stop", "--json", "--by", "prover")
    qwen(root, "terminate", "--force", "--by", "prover")
    qwen(root, "terminate", "--force", "--by", "prover", slot="B")
    for name in ("state/qwen-a-desired.json",):
        write_json(os.path.join(root, name), {"desired": "STOPPED", "boot_sec": boot_sec(),
                                              "set_by": "prover"})
    ok = wait_for(lambda: not any(procs(root).values()), 30)
    return bool(ok)


def start_fish(root):
    rc, out, err = fish(root, "start", "--json", "--by", "prover")
    return rc, as_json(out), err


def start_qwen(root, model_id, slot="A"):
    select(root, model_id)
    rc, out, err = qwen(root, "start", "--json") if slot == "A" else \
        qwen(root, "acquire", "--json", "--owner", "prover", slot="B")
    return rc, as_json(out), err


# ---------------------------------------------------------------- scenarios
def s_matrix(root):
    print("matrix — each side alone, Fish + 27B admitted, Fish + Flash-Next refused both ways")
    rc, res, err = start_fish(root)
    check("Fish alone: start succeeds (identity verified)", rc == 0 and res.get("state") == "READY",
          f"rc={rc} {err.strip()[-200:]}")
    check("Fish alone: desired state RUNNING after a successful start",
          fish_desired(root).get("desired") == "RUNNING")
    rc, _, _ = fish(root, "stop", "--json", "--by", "prover")
    check("Fish alone: stop succeeds, engine gone, desired STOPPED",
          rc == 0 and not procs(root)["fish"] and fish_desired(root).get("desired") == "STOPPED")
    for mid in ("qwen27b", "flash-next"):
        rc, res, err = start_qwen(root, mid)
        check(f"{mid} alone: start succeeds (no new condition)", rc == 0, f"rc={rc} {err.strip()[-160:]}")
        qwen(root, "terminate", "--force", "--by", "prover")
        check(f"{mid} alone: stopped cleanly", wait_for(lambda: not procs(root)[mid], 20) is not None)

    rc, _, _ = start_fish(root)
    check("Fish READY for the pairing tests", rc == 0)
    runs = launchd(LABEL_A)["runs"]
    rc, res, err = start_qwen(root, "flash-next")
    check("Fish ON + Flash-Next start: refused, exit 10", rc == 10, f"rc={rc}")
    check("refusal says why, in plain words", "never allowed" in (res.get("message") or ""),
          res.get("message"))
    check("nothing was spawned for Flash-Next (launchd runs unchanged, no process)",
          launchd(LABEL_A)["runs"] == runs and not procs(root)["flash-next"])
    check("slot A record still STOPPED", lease(root).get("state") in (None, "STOPPED"))
    runs_b = launchd(LABEL_B)["runs"]
    rc, res, err = start_qwen(root, "flash-next", slot="B")
    check("Fish ON + Flash-Next acquire on slot B: refused, exit 10", rc == 10,
          f"rc={rc} {(res.get('message') or err.strip()[-160:])}")
    check("nothing was spawned on slot B", launchd(LABEL_B)["runs"] == runs_b
          and not procs(root)["flash-next"])
    rc, res, err = start_qwen(root, "qwen27b")
    check("Fish ON + 27B start: admitted (memory admission passed)", rc == 0,
          f"rc={rc} {(res.get('message') or err.strip()[-160:])}")
    check("Fish and 27B both running", procs(root)["fish"] and procs(root)["qwen27b"])
    all_stopped(root)

    rc, res, _ = start_qwen(root, "flash-next")
    check("Flash-Next READY for the reverse test", rc == 0)
    runs = launchd(LABEL_FISH)["runs"]
    rc, res, err = start_fish(root)
    check("Flash-Next ON + Fish start: refused, exit 10", rc == 10, f"rc={rc}")
    check("refusal names the conflict", "never allowed" in (res.get("message") or ""),
          res.get("message"))
    check("no Fish engine was spawned (launchd runs unchanged)",
          launchd(LABEL_FISH)["runs"] == runs and not procs(root)["fish"])
    check("a refused explicit start leaves Fish desired STOPPED",
          fish_desired(root).get("desired") == "STOPPED")
    all_stopped(root)


def s_memory_guard(root):
    print("memory_guard — Fish is refused when measured memory is not enough")
    set_fish(root, required_gib=4096)
    runs = launchd(LABEL_FISH)["runs"]
    rc, res, err = start_fish(root)
    check("start refused with exit 10", rc == 10, f"rc={rc}")
    check("reason is memory, in plain words", res.get("policy_code") in ("memory", "gpu") and
          "GiB" in (res.get("message") or ""), res.get("message"))
    check("nothing spawned", launchd(LABEL_FISH)["runs"] == runs and not procs(root)["fish"])
    set_fish(root, required_gib=1.0)


def race(root, a_cmd, b_cmd, rounds, label):
    outcomes = []
    for i in range(rounds):
        all_stopped(root)
        barrier = threading.Barrier(2)
        res = {}

        def run(name, cmd):
            barrier.wait()
            res[name] = sh(cmd, timeout=150)[0]
        ta = threading.Thread(target=run, args=("a", a_cmd))
        tb = threading.Thread(target=run, args=("b", b_cmd))
        ta.start(); tb.start(); ta.join(); tb.join()
        outcomes.append((res.get("a"), res.get("b")))
    print(f"    {label}: {outcomes}")
    return outcomes


def s_races(root, rounds):
    print(f"races — simultaneous starts ({rounds} rounds each); a sampler watches every 0.1 s")
    select(root, "flash-next")
    out = race(root, fish_cmd(root, "start", "--json", "--by", "prover"),
               qwen_cmd(root, "start", "--json"), rounds, "Fish start vs Flash-Next start (rc)")
    check("never both admitted", all(not (a == 0 and b == 0) for a, b in out), out)
    check("every round: one started, the other was refused by the policy (10)",
          all(sorted((a, b)) == [0, 10] for a, b in out), out)
    select(root, "flash-next")
    out = race(root, fish_cmd(root, "start", "--json", "--by", "prover"),
               qwen_cmd(root, "acquire", "--json", "--owner", "prover", slot="B"),
               max(2, rounds // 2), "Fish start vs slot-B Flash-Next acquire (rc)")
    check("Fish vs slot B: never both admitted", all(not (a == 0 and b == 0) for a, b in out), out)
    check("Fish vs slot B: every round one started and the other was refused (10)",
          all(sorted((a, b)) == [0, 10] for a, b in out), out)
    # restart vs a Flash-Next start: the restart's stop opens a window
    restart = []
    for _ in range(max(2, rounds // 2)):
        all_stopped(root)
        start_fish(root)
        select(root, "flash-next")
        barrier = threading.Barrier(2)
        res = {}

        def run(name, cmd):
            barrier.wait()
            res[name] = sh(cmd, timeout=150)[0]
        ta = threading.Thread(target=run, args=("restart", fish_cmd(root, "restart", "--json",
                                                                     "--by", "prover")))
        tb = threading.Thread(target=run, args=("flash", qwen_cmd(root, "start", "--json")))
        ta.start(); tb.start(); ta.join(); tb.join()
        restart.append((res["restart"], res["flash"]))
    print(f"    Fish restart vs Flash-Next start (rc): {restart}")
    check("restart vs Flash-Next: never both running at the end",
          all(not (a == 0 and b == 0) for a, b in restart), restart)
    select(root, "qwen27b")
    out = race(root, fish_cmd(root, "start", "--json", "--by", "prover"),
               qwen_cmd(root, "start", "--json"), max(2, rounds // 2),
               "Fish start vs 27B start (rc)")
    check("Fish vs 27B: no deadlock, every start finished with a defined outcome",
          all(a in (0, 10) and b in (0, 10) for a, b in out), out)
    all_stopped(root)


def s_recovery(root):
    print("recovery — a pending recovery counts as presence, in both directions")
    write_json(os.path.join(root, "state/fish/desired.json"),
               {"desired": "RUNNING", "boot_sec": boot_sec(), "set_by": "prover"})
    rc, res, _ = start_qwen(root, "flash-next")
    check("Fish recovering (desired RUNNING, engine down) blocks Flash-Next", rc == 10,
          res.get("message"))
    check("message says Fish is set to run", "recovering" in (res.get("message") or ""))
    write_json(os.path.join(root, "state/fish/desired.json"),
               {"desired": "RUNNING", "boot_sec": 1, "set_by": "prover (previous boot)"})
    rc, res, _ = start_qwen(root, "flash-next")
    check("a desired=RUNNING record from a previous boot is ignored (reboot = OFF)", rc == 0,
          res.get("message"))
    all_stopped(root)
    write_json(os.path.join(root, "state/fish/desired.json"),
               {"desired": "STOPPED", "boot_sec": boot_sec(), "set_by": "prover"})
    select(root, "flash-next")
    write_json(os.path.join(root, "state/qwen-a-desired.json"),
               {"desired": "RUNNING", "boot_sec": boot_sec(), "set_by": "prover"})
    rc, res, _ = start_fish(root)
    check("Qwen-A recovering with Flash-Next selected blocks Fish", rc == 10, res.get("message"))
    select(root, "qwen27b")
    rc, res, _ = start_fish(root)
    check("Qwen-A recovering with the 27B selected does not block Fish", rc == 0,
          res.get("message"))
    all_stopped(root)


def s_stale(root):
    print("stale — reservations left by dead controllers do not block anything")
    write_json(os.path.join(root, "state/qwen-a-lease.json"),
               {"state": "STARTING", "model": "flash-next", "controller_pid": 999999,
                "start_deadline": now() + 600, "generation": 1})
    rc, res, _ = start_fish(root)
    check("stale Qwen STARTING (lock free) does not block Fish", rc == 0, res.get("message"))
    all_stopped(root)
    rec = read_json(os.path.join(root, "state/fish/state.json"))
    rec.update(state="STARTING", start_deadline=now() + 600, controller_pid=999999, pid=None)
    write_json(os.path.join(root, "state/fish/state.json"), rec)
    rc, res, _ = start_qwen(root, "flash-next")
    check("stale Fish STARTING (lock free) does not block Flash-Next", rc == 0, res.get("message"))
    all_stopped(root)
    check("the stale Fish record was reconciled to STOPPED", fish_state(root) == "STOPPED")


def s_backstops(root):
    print("backstops — a direct launchctl kickstart cannot bypass the policy")
    start_fish(root)
    select(root, "flash-next")
    runs = launchd(LABEL_A)["runs"]
    sh(["launchctl", "kickstart", f"gui/{UID}/{LABEL_A}"])
    wait_for(lambda: launchd(LABEL_A)["runs"] != runs, 10)
    time.sleep(1.0)
    st = launchd(LABEL_A)
    check("exec-slot refused Flash-Next beside Fish (exit 78)", st["last_exit"] == 78, st)
    check("no Flash-Next process appeared", not procs(root)["flash-next"])
    all_stopped(root)
    rc, res, _ = start_qwen(root, "flash-next")
    tok = os.path.join(root, "state/fish/engine.token")
    if not os.path.exists(tok):
        fd = os.open(tok, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"token": "ab" * 32, "generation": 99, "model_id": "drama3-q4"}, f)
    runs = launchd(LABEL_FISH)["runs"]
    sh(["launchctl", "kickstart", f"gui/{UID}/{LABEL_FISH}"])
    wait_for(lambda: launchd(LABEL_FISH)["runs"] != runs, 10)
    time.sleep(1.0)
    st = launchd(LABEL_FISH)
    check("exec-engine refused Fish beside Flash-Next (exit 78)", st["last_exit"] == 78, st)
    check("no Fish engine appeared (the gatekeeper exited without exec)",
          not procs(root)["fish"] and not procs(root)["fish_shim"])
    log = open(os.path.join(root, "logs/fish/stderr.log")).read()
    check("its stderr names the policy", "never run beside" in log)
    all_stopped(root)


def s_lifecycle(root):
    print("lifecycle — stop during start, engine failures, duplicates, already running")
    set_fish(root, load_s=60)
    p = subprocess.Popen(fish_cmd(root, "start", "--json", "--by", "prover"),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    up = wait_for(lambda: fish_state(root) == "STARTING" and procs(root)["fish"], 20)
    check("a slow start is loading", bool(up))
    t0 = now()
    rc, out, _ = fish(root, "stop", "--json", "--by", "prover")
    t_stop = now() - t0
    try:
        p.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        p.kill()
    check("stop during start succeeds within 15 s", rc == 0 and t_stop < 15, f"{t_stop:.1f}s")
    check("the start exits 9 (cancelled)", p.returncode == 9, f"rc={p.returncode}")
    check("engine gone, record STOPPED, desired STOPPED",
          not procs(root)["fish"] and fish_state(root) == "STOPPED"
          and fish_desired(root).get("desired") == "STOPPED")

    set_fish(root, exit_on_load="error: simulated Metal device failure while loading")
    t0 = now()
    rc, res, _ = start_fish(root)
    check("engine exits while loading: start fails with exit 8 within 10 s",
          rc == 8 and now() - t0 < 10, f"rc={rc} {now() - t0:.1f}s")
    check("the engine's own reason is surfaced",
          "simulated Metal device failure" in json.dumps(res), (res.get("message") or "")[:200])
    set_fish(root, load_error="checkpoint is unreadable (simulated)")
    rc, res, _ = start_fish(root)
    check("model load error: exit 8 with the reason",
          rc == 8 and "unreadable" in json.dumps(res), (res.get("message") or "")[:200])
    set_fish(root)

    rc, res, _ = start_fish(root)
    runs = launchd(LABEL_FISH)["runs"]
    rc2, res2, _ = start_fish(root)
    check("a second start while READY starts nothing (idempotent)",
          rc2 == 0 and res2.get("started") is False and launchd(LABEL_FISH)["runs"] == runs
          and len(procs(root)["fish"]) == 1, res2.get("note"))
    decoy = subprocess.Popen(["/bin/bash", "-c", 'exec -a "$0" /bin/sleep 60',
                              os.path.join(root, "scripts/fish_engine.py") + " --config "
                              + os.path.join(root, "config/fish.json")])
    time.sleep(0.5)
    rc, out, _ = fish(root, "status", "--json")
    check("a duplicate engine process is detected", as_json(out).get("duplicate_processes") is True)
    rc, res, err = start_fish(root)
    check("start refuses while duplicates exist (exit 5)", rc == 5, err.strip()[-160:])
    decoy.kill()
    decoy.wait()
    all_stopped(root)


def s_panel(root):
    print("panel — the watchdogs in Newton AI Control honour the policy")
    env = dict(os.environ, PATH="/usr/bin:/bin:/usr/sbin:/sbin")
    panel = subprocess.Popen([PY, os.path.join(root, "scripts/newton_control.py"), "--port",
                              str(PANEL_PORT)], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=env, cwd=root)
    try:
        ok = wait_for(lambda: http_ok(f"http://127.0.0.1:{PANEL_PORT}/api/fish/status"), 20)
        check("fixture panel is up", bool(ok))
        start_fish(root)
        select(root, "flash-next")
        runs = launchd(LABEL_A)["runs"]
        write_json(os.path.join(root, "state/qwen-a-desired.json"),
                   {"desired": "RUNNING", "boot_sec": boot_sec(), "set_by": "prover"})
        hit = wait_for(lambda: log_has(root, "logs/control/control.log", "watchdog.recover_attempt"),
                       45, step=0.5)
        check("Qwen watchdog attempted a recovery", bool(hit))
        time.sleep(2.0)
        check("the recovery was refused: no Flash-Next process, no launchd run",
              not procs(root)["flash-next"] and launchd(LABEL_A)["runs"] == runs)
        check("refusal recorded as a policy failure",
              log_has(root, "logs/qwen-a/controller.log", "refused_policy"))
        write_json(os.path.join(root, "state/qwen-a-desired.json"),
                   {"desired": "STOPPED", "boot_sec": boot_sec(), "set_by": "prover"})
        all_stopped(root)

        rc, _, _ = start_qwen(root, "flash-next")
        runs = launchd(LABEL_FISH)["runs"]
        write_json(os.path.join(root, "state/fish/desired.json"),
                   {"desired": "RUNNING", "boot_sec": boot_sec(), "set_by": "prover"})
        hit = wait_for(lambda: log_has(root, "logs/control/voice.log",
                                       "fish.watchdog.recover_attempt"), 45, step=0.5)
        check("Fish watchdog attempted a recovery", bool(hit))
        done = wait_for(lambda: log_has(root, "logs/control/voice.log", '"policy_code"'), 30,
                        step=0.5)
        check("the Fish recovery was refused by the policy (exit 10)", bool(done))
        check("no Fish engine spawned", not procs(root)["fish"] and
              launchd(LABEL_FISH)["runs"] == runs)
        st = as_json(urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{PANEL_PORT}/api/fish/status",
            headers={"Host": f"127.0.0.1:{PANEL_PORT}"}), timeout=10).read().decode())
        lo = (st.get("fish") or {}).get("last_operation") or {}
        check("the panel shows the refusal in plain words", "never allowed" in (lo.get("message") or ""),
              lo.get("message"))
        fish(root, "stop", "--json", "--by", "prover")
        check("an intentional stop records STOPPED, so the watchdog stands down",
              fish_desired(root).get("desired") == "STOPPED")
    finally:
        panel.terminate()
        try:
            panel.wait(10)
        except subprocess.TimeoutExpired:
            panel.kill()
    all_stopped(root)


def http_ok(url):
    try:
        req = urllib.request.Request(url, headers={"Host": url.split("/")[2]})
        return urllib.request.urlopen(req, timeout=3).status == 200
    except Exception:
        return False


def log_has(root, rel, needle):
    try:
        with open(os.path.join(root, rel)) as f:
            return needle in f.read()
    except OSError:
        return False


# --------------------------------------------------------------------- main
def preflight():
    busy = Guard.real()
    blockers = {pid: cmd for pid, cmd in busy.items()}
    for line in ps_lines():
        pid, _, cmd = line.partition(" ")
        if os.path.basename(cmd.split(" ")[0]) in ("llama-server", "mlx-serve") and \
                "--version" not in cmd:
            blockers[int(pid)] = cmd[:160]
    if blockers:
        sys.exit("refusing to run: a real model runtime or Fish engine is up, and the policy "
                 "would (correctly) count it: " + json.dumps(blockers))
    for port in (PORT_A, PORT_B, PORT_FISH, PANEL_PORT):
        rc, out, _ = sh(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"])
        if out.strip():
            sys.exit(f"refusing to run: fixture port {port} is in use")


def teardown(root):
    try:
        all_stopped(root)
    finally:
        for label in (LABEL_A, LABEL_B, LABEL_FISH):
            sh(["launchctl", "bootout", f"gui/{UID}/{label}"])
        for kind, pids in procs(root).items():
            for pid in pids:
                sh(["kill", "-KILL", str(pid)])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", default=os.path.join(REPO, "benchmarks/fish/policy-proof.json"))
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--keep", action="store_true", help="keep the fixture root afterwards")
    a = ap.parse_args()
    if not os.access(VENV_PY, os.X_OK):
        sys.exit("the Fish runtime (tools/fish-venv) is needed for the fixture engine")
    preflight()
    root = tempfile.mkdtemp(prefix="newton-fishpolicy-")
    t0 = now()
    build_fixture(root)
    guard = Guard(root)
    guard.start()
    timings = {}
    try:
        for name, fn in (("matrix", s_matrix), ("memory_guard", s_memory_guard),
                         ("races", lambda r: s_races(r, a.rounds)), ("recovery", s_recovery),
                         ("stale", s_stale), ("backstops", s_backstops),
                         ("lifecycle", s_lifecycle), ("panel", s_panel)):
            t = now()
            fn(root)
            timings[name] = round(now() - t, 1)
    finally:
        guard.stop_flag.set()
        guard.join(5)
        teardown(root)
    check("invariant held at every sample: a Fish engine and Flash-Next never alive together",
          not guard.overlaps, f"{guard.samples} samples; overlaps: {guard.overlaps[:3]}; "
          f"samples with only the gatekeeper shim deciding beside Flash-Next: {guard.shim_checks}")
    check("no real model runtime or real Fish engine was started", not guard.new_real,
          json.dumps(guard.new_real)[:300])
    check("no fixture process left behind", not any(procs(root).values()))
    failed = [r for r in RESULTS if not r["ok"]]
    evidence = {"prover": "scripts/prove-fish-policy.py", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "wall_s": round(now() - t0, 1), "rounds": a.rounds, "scenario_s": timings,
                "samples": guard.samples, "checks": RESULTS, "passed": len(RESULTS) - len(failed),
                "failed": len(failed)}
    os.makedirs(os.path.dirname(a.evidence), exist_ok=True)
    with open(a.evidence, "w") as f:
        json.dump(evidence, f, indent=2)
        f.write("\n")
    if not a.keep:
        shutil.rmtree(root, ignore_errors=True)
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed "
          f"({evidence['wall_s']} s; evidence {a.evidence})")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
