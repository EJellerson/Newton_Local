#!/usr/bin/env python3
"""Unit tests for the pure decision logic behind Commission #4 — runnable at
any time with ZERO model involvement: no launchctl mutation, no processes
started, no sockets opened.

Covers:
  * classify_llama_processes — the singleton/peer detector
  * load_config              — generic CTL_* keys + legacy QWEN_B_* keys
  * cmd_reap with a disabled idle TTL — hard no-op, touches nothing
  * effective_desired        — reboot always lands on Qwen=OFF
  * map_ui_state             — operator-facing state machine

    python3 scripts/test-qwenctl-logic.py
"""
import json
import os
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

import qwenctl               # noqa: E402
import newton_control        # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


LB = "/Users/x/Newton_Local/tools/llama.cpp/build/bin/llama-server"
CFG_A = {"llama_bin": LB, "port": 8080, "alias": "qwen-a", "instance": "qwen-a"}
CFG_B = {"llama_bin": LB, "port": 8081, "alias": "qwen-b", "instance": "qwen-b"}


def ps_line(pid, cmd):
    return f"  {pid} 48000000 {cmd}"


PS_A = ps_line(100, f"{LB} --model m.gguf --alias qwen-a --host 127.0.0.1 --port 8080 --ctx-size 262144")
PS_B = ps_line(200, f"{LB} --model m.gguf --alias qwen-b --host 127.0.0.1 --port 8081 --ctx-size 262144")
PS_OTHER = ps_line(300, "/usr/bin/python3 some-unrelated-thing --port 8080")
PS_MANUAL = ps_line(400, f"{LB} --model m.gguf --alias qwen-a --host 127.0.0.1 --port 9090")


SIGS = [LB]   # runtime signatures (the registry supplies these in production)


def t_classify():
    print("classify_model_processes (singleton/peer detection)")
    ours, peers = qwenctl.classify_model_processes(CFG_A, [PS_A, PS_OTHER], SIGS)
    check("A sees itself as OURS", [p["pid"] for p in ours] == [100])
    check("unrelated non-runtime process ignored", peers == [])

    ours, peers = qwenctl.classify_model_processes(CFG_A, [PS_A, PS_B], SIGS)
    check("A sees B as PEER", [p["pid"] for p in peers] == [200])
    check("peer carries identity", peers[0]["alias"] == "qwen-b"
          and peers[0]["port"] == 8081)

    ours, peers = qwenctl.classify_model_processes(CFG_B, [PS_A, PS_B], SIGS)
    check("B sees A as PEER (symmetry)", [p["pid"] for p in peers] == [100])

    ours, peers = qwenctl.classify_model_processes(CFG_A, [PS_B], SIGS)
    check("start-blocking case: no OURS, one PEER",
          ours == [] and len(peers) == 1)

    ours, peers = qwenctl.classify_model_processes(CFG_A, [PS_MANUAL], SIGS)
    check("manually mis-launched qwen-a (wrong port) counts as PEER",
          [p["pid"] for p in peers] == [400])

    ours, peers = qwenctl.classify_model_processes(CFG_A, [], SIGS)
    check("empty machine: nothing anywhere", ours == [] and peers == [])

    probes = [ps_line(500, f"{LB} --version"), ps_line(501, f"{LB} --help")]
    ours, peers = qwenctl.classify_model_processes(CFG_A, probes, SIGS)
    check("--version/--help probes are not model servers", ours == [] and peers == [])


def t_load_config():
    print("load_config (generic CTL_* + legacy QWEN_B_*)")
    a = qwenctl.load_config(os.path.join(ROOT, "config", "qwen-a-controller.env"))
    check("A instance name", a["instance"] == "qwen-a")
    check("A label", a["label"] == "com.newton.qwen38server.a")
    check("A port 8080 inherited from qwen-a.env", a["port"] == 8080)
    check("A idle TTL disabled", a["idle_ttl"] == 0)
    check("A model path inherited", a["model"].endswith("Q8_0.gguf"))

    b = qwenctl.load_config(os.path.join(ROOT, "config", "qwen-b.env"))
    check("B legacy keys still resolve", b["label"] == "com.newton.qwen38server.b")
    check("B port unchanged", b["port"] == 8081)
    check("B idle TTL unchanged", b["idle_ttl"] == 180)
    check("B instance defaults to alias", b["instance"] == "qwen-b")


def t_reap_disabled():
    print("cmd_reap with idle TTL disabled (production instance)")
    with tempfile.TemporaryDirectory() as td:
        lease = os.path.join(td, "lease.json")
        with open(lease, "w") as f:
            json.dump({"state": "IDLE", "idle_since": 0}, f)
        before = os.path.getmtime(lease)
        cfg = dict(CFG_A, idle_ttl=0, lease=lease,
                   lock=os.path.join(td, "l.lock"), logdir=td,
                   host="127.0.0.1", ctx=1, label="x", start_timeout=1,
                   lease_max_age=1, uid=os.getuid(),
                   model="m", mmproj="m")

        class Args:
            verbose = True
            lock_timeout = 5
            orphan_policy = "adopt"
            json = False

        rc = qwenctl.cmd_reap(cfg, Args())
        check("returns 0", rc == 0)
        check("does not touch the lease file",
              os.path.getmtime(lease) == before)
        check("takes no lock", not os.path.exists(cfg["lock"]))


def t_effective_desired():
    print("effective_desired (reboot always lands on Qwen=OFF)")
    ed = newton_control.effective_desired
    check("no record -> STOPPED", ed(None, 1000) == "STOPPED")
    check("malformed -> STOPPED", ed("junk", 1000) == "STOPPED")
    check("RUNNING this boot -> RUNNING",
          ed({"desired": "RUNNING", "boot_sec": 1000}, 1000) == "RUNNING")
    check("RUNNING from a PREVIOUS boot -> STOPPED (reboot policy)",
          ed({"desired": "RUNNING", "boot_sec": 999}, 1000) == "STOPPED")
    check("explicit STOPPED -> STOPPED",
          ed({"desired": "STOPPED", "boot_sec": 1000}, 1000) == "STOPPED")


def t_start_guards():
    """Execute the REAL _start_locked refusal branches with canned
    observations and a stubbed kickstart: proves start/restart cannot launch
    anything while a peer instance exists or the port is foreign-owned."""
    print("start/restart guards (real branch execution, kickstart stubbed)")

    def canned_observe(peers=(), listening=False):
        return {"observed_state": qwenctl.OBS_DOWN, "pid": None,
                "launchd": {"bootstrapped": True, "state": "not running",
                            "pid": None, "runs": 0, "last_exit": "0"},
                "processes": [], "peer_processes": list(peers),
                "duplicate_processes": False, "launchd_owned": False,
                "socket": {"listening": listening, "pid": None, "name": None},
                "health": {"ok": False, "status": None, "props": None},
                "rss_gib": None}

    class Args:
        json = True
        orphan_policy = "adopt"
        lock_timeout = 5
        timeout = 1
        allow_peer = False

    kicked = []
    orig = (qwenctl.observe, qwenctl.obs_socket, qwenctl._kickstart)
    with tempfile.TemporaryDirectory() as td:
        cfg = dict(qwenctl.load_config(
            os.path.join(ROOT, "config", "qwen-a-controller.env")),
            lease=os.path.join(td, "lease.json"),
            lock=os.path.join(td, "l.lock"), logdir=td)
        try:
            qwenctl._kickstart = lambda c: (kicked.append(1) or (1, "STUB"))

            # peer active -> refuse with exit 5, kickstart never reached
            peer = [{"pid": 999, "rss_gib": 40.0, "alias": "qwen-b",
                     "port": 8081}]
            qwenctl.observe = lambda c, health_timeout=5: canned_observe(peer)
            try:
                qwenctl.cmd_start(cfg, Args())
                check("start refused while peer active", False, "no exit")
            except SystemExit as e:
                check("start refused while peer active (exit 5)", e.code == 5)
            check("peer refusal never kickstarts", kicked == [])

            try:
                qwenctl.cmd_restart(cfg, Args())
                check("restart refused while peer active", False, "no exit")
            except SystemExit as e:
                check("restart refused while peer active (exit 5)", e.code == 5)
            check("restart refusal never kickstarts", kicked == [])

            # foreign port owner -> refuse with exit 5, kickstart never reached
            qwenctl.observe = lambda c, health_timeout=5: canned_observe()
            qwenctl.obs_socket = lambda c: {"listening": True, "pid": 1234,
                                            "name": "127.0.0.1:8080"}
            try:
                qwenctl.cmd_start(cfg, Args())
                check("start refused on foreign port owner", False, "no exit")
            except SystemExit as e:
                check("start refused on foreign port owner (exit 5)",
                      e.code == 5)
            check("port refusal never kickstarts", kicked == [])
        finally:
            qwenctl.observe, qwenctl.obs_socket, qwenctl._kickstart = orig


def t_map_ui_state():
    print("map_ui_state (operator-facing states)")
    m = newton_control.map_ui_state
    check("stopped, no desire -> Stopped",
          m("STOPPED", False, "STOPPED", None, False) == "Stopped")
    check("running healthy -> Running",
          m("READY", True, "RUNNING", None, False) == "Running")
    check("IDLE healthy still Running (ownerless production)",
          m("IDLE", True, "RUNNING", None, False) == "Running")
    check("loading -> Starting",
          m("STARTING", False, "RUNNING", None, False) == "Starting")
    check("op start in flight -> Starting",
          m("STOPPED", False, "RUNNING", "start", False) == "Starting")
    check("op stop in flight -> Stopping",
          m("READY", True, "STOPPED", "stop", False) == "Stopping")
    check("crashed while desired RUNNING -> Recovering",
          m("STOPPED", False, "RUNNING", None, False) == "Recovering")
    check("recovery exhausted -> Unhealthy",
          m("STOPPED", False, "RUNNING", None, True) == "Unhealthy")
    check("process up but sick -> Unhealthy",
          m("READY", False, "RUNNING", None, False) == "Unhealthy")
    check("controller UNKNOWN -> Unhealthy",
          m("UNKNOWN", False, "STOPPED", None, False) == "Unhealthy")


def main():
    for t in (t_classify, t_load_config, t_reap_disabled, t_start_guards,
              t_effective_desired, t_map_ui_state):
        t()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
