#!/usr/bin/env python3
"""No-load proving suite for the dual-model registry / global selection
(Commission #5). Runs entirely against a FIXTURE: a temporary root with its
own registry, fake model directories (kilobyte files with real manifests) and
fake runtime executables that only answer `--version` and otherwise record
that they were invoked and exit 99. Nothing here can load a model; the last
check proves no real model process or slot port appeared.

    python3 scripts/test-model-registry.py
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REAL_ROOT = os.path.dirname(SCRIPT_DIR)

FIX = tempfile.mkdtemp(prefix="newton-fixture-")
os.environ["NEWTON_ROOT"] = FIX
os.environ["NEWTON_REGISTRY_PATH"] = os.path.join(FIX, "config", "models.json")
os.environ["NEWTON_SELECTION_PATH"] = os.path.join(FIX, "state", "model-selection.json")
sys.path.insert(0, SCRIPT_DIR)
import newton_models as nm  # noqa: E402  (bound to the fixture via env)
import qwenctl               # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok    {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


FAKE_RUNTIME = """#!/bin/sh
# FAKE runtime for the no-load proving suite. It cannot load any model:
# it answers --version and otherwise records the invocation and exits 99.
case "$1" in --version) echo "fake-$(basename "$0") 0.0"; exit 0;; esac
echo "INVOKED: $*" >> "$(dirname "$0")/INVOKED"
exit 99
"""


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode) as f:
        f.write(data)


def make_pack(rel, files):
    """Create fake weight files and a manifest with real sha256 / blob hashes."""
    entries = []
    for name, content in files.items():
        p = os.path.join(FIX, rel, name)
        write(p, content)
        sha = hashlib.sha256(content).hexdigest()
        entries.append({"name": name, "size": len(content), "sha256": sha,
                        "git_blob_sha1": None})
    write(os.path.join(FIX, rel, "MANIFEST.json"),
          json.dumps({"schema": 1, "files": entries}))


def build_fixture():
    with open(os.path.join(REAL_ROOT, "config", "models.json")) as f:
        reg = json.load(f)
    # runtimes -> fake executables; no host-OS gate in the fixture
    for rid, r in reg["runtimes"].items():
        exe = os.path.join("tools", "fake", rid.replace(".", "-"))
        write(os.path.join(FIX, exe), FAKE_RUNTIME)
        os.chmod(os.path.join(FIX, exe), 0o755)
        r["executable"] = exe
        r.pop("requires_macos", None)
    # symlinked "current" runtime path, to prove symlink-target recognition
    cur = os.path.join(FIX, "tools", "fake", "mlx-current")
    os.symlink(os.path.join(FIX, "tools", "fake", "mlx-serve"), cur)
    reg["runtimes"]["mlx-serve"]["executable"] = "tools/fake/mlx-current"
    reg["models"]["qwen27b"]["path"] = "models/fake-27b"
    reg["models"]["qwen27b"]["manifest"] = "models/fake-27b/MANIFEST.json"
    reg["models"]["flash-next"]["path"] = "models/fake-flash"
    reg["models"]["flash-next"]["manifest"] = "models/fake-flash/MANIFEST.json"
    write(os.path.join(FIX, "config", "models.json"), json.dumps(reg))
    make_pack("models/fake-27b", {"Qwen3.8-27B-Q8_0.gguf": b"GGUFfake27b",
                                  "mmproj-Qwen3.8-27B-f16.gguf": b"GGUFmmproj"})
    make_pack("models/fake-flash", {"config.json": b'{"model_type":"qwen4_exp"}',
                                    "model-00001.safetensors": b"\x00" * 4096,
                                    "ngram_table.bin": b"\x01" * 2048})
    return reg


def free_occ(reg):
    return {"occupied": False, "reasons": [], "slots": {}}


def busy_occ(reason):
    return lambda reg: {"occupied": True, "reasons": [reason], "slots": {}}


class NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


no_locks = lambda: [NoLock(), NoLock()]  # noqa: E731


def selection_on_disk():
    try:
        with open(os.environ["NEWTON_SELECTION_PATH"]) as f:
            return json.load(f).get("selected")
    except (OSError, ValueError):
        return None


def invoked_markers():
    return [p for p in (os.path.join(FIX, "tools", "fake", "INVOKED"),)
            if os.path.exists(p)]


def main():
    build_fixture()
    reg = nm.load_registry()

    print("1-2. registry + default")
    check("registry loads both models", set(nm.model_ids(reg)) == {"qwen27b", "flash-next"})
    check("both runtimes present", set(reg["runtimes"]) == {"llama.cpp", "mlx-serve"})
    sel, src = nm.read_selection(reg)
    check("default selection is qwen27b (no file)", sel == "qwen27b" and "default" in src)
    check("qwen27b is the registry default", reg["default"] == "qwen27b")

    print("3. persistence")
    nm.write_selection(reg, "flash-next", by="test")
    check("selection written atomically and read back", nm.read_selection(reg)[0] == "flash-next")
    check("no .tmp left behind",
          not [f for f in os.listdir(os.path.join(FIX, "state")) if ".tmp" in f])
    # "restart": a fresh process reads the same file
    out = subprocess.run([sys.executable, os.path.join(SCRIPT_DIR, "newton_models.py"),
                          "status", "--json"], capture_output=True, text=True,
                         env=dict(os.environ))
    st = json.loads(out.stdout or "{}")
    check("fresh process (CLI) sees the persisted selection", st.get("selected") == "flash-next")
    nm.write_selection(reg, "qwen27b", by="test")

    print("4. invalid selection rejected")
    for bad in ("nope", "", "QWEN27B", "../x"):
        try:
            nm.write_selection(reg, bad, by="test")
            check(f"reject {bad!r}", False, "no error")
        except nm.SelectionError as e:
            check(f"reject {bad!r} (exit {e.code})", e.code == nm.EXIT_INVALID)
    try:
        nm.select_model(reg, "nope", occupancy_fn=free_occ, lock_fn=no_locks)
        check("select_model rejects unknown id", False)
    except nm.SelectionError as e:
        check("select_model rejects unknown id", e.code == nm.EXIT_INVALID)
    check("selection unchanged after rejects", selection_on_disk() == "qwen27b")

    print("5-6. availability / integrity")
    c = nm.check_model(reg, "flash-next", deep=True)
    check("complete fake Flash pack: available (deep hashes ok)", c["available"], c["reasons"])
    c = nm.check_model(reg, "qwen27b", deep=True)
    check("complete fake 27B pack: available", c["available"], c["reasons"])
    # corrupt one shard: size mismatch -> unavailable
    shard = os.path.join(FIX, "models", "fake-flash", "model-00001.safetensors")
    with open(shard, "ab") as f:
        f.write(b"x")
    c = nm.check_model(reg, "flash-next")
    check("truncated/altered shard -> unavailable with reason",
          not c["available"] and any("size mismatch" in r for r in c["reasons"]))
    try:
        nm.select_model(reg, "flash-next", occupancy_fn=free_occ, lock_fn=no_locks)
        check("cannot select an unavailable model", False)
    except nm.SelectionError as e:
        check("cannot select an unavailable model (exit 4)", e.code == nm.EXIT_UNAVAILABLE)
    with open(shard, "wb") as f:
        f.write(b"\x00" * 4096)
    # same content, wrong hash in manifest -> deep catches it, shallow does not
    mpath = os.path.join(FIX, "models", "fake-flash", "MANIFEST.json")
    man = json.load(open(mpath))
    good = man["files"][1]["sha256"]
    man["files"][1]["sha256"] = "0" * 64
    json.dump(man, open(mpath, "w"))
    check("shallow check passes on size alone", nm.check_model(reg, "flash-next")["available"])
    check("deep check catches a hash mismatch",
          not nm.check_model(reg, "flash-next", deep=True)["available"])
    man["files"][1]["sha256"] = good
    json.dump(man, open(mpath, "w"))
    os.remove(os.path.join(FIX, "models", "fake-flash", "ngram_table.bin"))
    c = nm.check_model(reg, "flash-next")
    check("missing file -> unavailable ('missing:')",
          not c["available"] and any(r.startswith("missing:") for r in c["reasons"]))
    write(os.path.join(FIX, "models", "fake-flash", "ngram_table.bin"), b"\x01" * 2048)
    # runtime gone -> unavailable even with intact weights
    exe = os.path.join(FIX, "tools", "fake", "llama-cpp")
    os.chmod(exe, 0o644)
    c = nm.check_model(reg, "qwen27b")
    check("non-executable runtime -> unavailable", not c["available"])
    os.chmod(exe, 0o755)
    reg2 = json.load(open(os.environ["NEWTON_REGISTRY_PATH"]))
    reg2["runtimes"]["mlx-serve"]["requires_macos"] = "99.0"
    c = nm.check_model(reg2, "flash-next")
    check("host-OS requirement unmet -> unavailable with reason",
          not c["available"] and any("requires macOS" in r for r in c["reasons"]))

    print("7-8. selection locked while a slot is occupied")
    for reason in ("slot A (qwen-a) is READY/UP", "slot B (qwen-b) is BUSY/UP owner=job-1",
                   "slot A (qwen-a) is STARTING/LOADING", "model runtime process(es) present: [4242]"):
        try:
            nm.select_model(reg, "flash-next", occupancy_fn=busy_occ(reason), lock_fn=no_locks)
            check(f"refused while: {reason}", False, "no error")
        except nm.SelectionError as e:
            check(f"refused while: {reason} (exit 3)", e.code == nm.EXIT_LOCKED)
    check("selection untouched by refused attempts", selection_on_disk() == "qwen27b")
    rec = nm.select_model(reg, "flash-next", occupancy_fn=free_occ, lock_fn=no_locks)
    check("allowed while both slots free", rec["selected"] == "flash-next"
          and selection_on_disk() == "flash-next")
    nm.select_model(reg, "qwen27b", occupancy_fn=free_occ, lock_fn=no_locks)

    print("9-13. launch plans resolve per slot from the ONE global selection")
    pa27 = nm.launch_plan(reg, "A", "qwen27b")
    pb27 = nm.launch_plan(reg, "B", "qwen27b")
    paf = nm.launch_plan(reg, "A", "flash-next")
    pbf = nm.launch_plan(reg, "B", "flash-next")
    check("A/qwen27b -> llama.cpp", pa27["runtime"] == "llama.cpp" and pa27["argv"][0].endswith("llama-cpp"))
    check("A/flash-next -> mlx-serve", paf["runtime"] == "mlx-serve" and "mlx" in paf["argv"][0])
    check("B/qwen27b -> llama.cpp", pb27["runtime"] == "llama.cpp")
    check("B/flash-next -> mlx-serve", pbf["runtime"] == "mlx-serve")
    check("A endpoint stays :8080 for both models",
          pa27["endpoint"] == paf["endpoint"] == "http://127.0.0.1:8080")
    check("B endpoint stays :8081 for both models",
          pb27["endpoint"] == pbf["endpoint"] == "http://127.0.0.1:8081")
    nm.write_selection(reg, "flash-next", by="test")
    check("plans follow the global selection (A)", nm.launch_plan(reg, "A")["model"] == "flash-next")
    check("plans follow the global selection (B)", nm.launch_plan(reg, "B")["model"] == "flash-next")
    check("A and B can never disagree on the model",
          nm.launch_plan(reg, "A")["model"] == nm.launch_plan(reg, "B")["model"])
    nm.write_selection(reg, "qwen27b", by="test")

    print("14. llama.cpp parameters for 27B are byte-identical to the frozen definitions (REAL registry)")
    real = subprocess.run([sys.executable, os.path.join(SCRIPT_DIR, "newton_models.py"),
                           "plan", "A", "--model", "qwen27b", "--argv"], capture_output=True,
                          text=True, env={k: v for k, v in os.environ.items()
                                          if not k.startswith("NEWTON_")}).stdout.split("\n")
    real = [t for t in real if t]
    daemon = json.loads(subprocess.run(
        ["plutil", "-extract", "ProgramArguments", "json", "-o", "-",
         os.path.join(REAL_ROOT, "config", "com.newton.qwen38server.a.plist")],
        capture_output=True, text=True).stdout)
    check("real plan A/qwen27b == frozen Commission-#1 daemon argv", real == daemon,
          f"\n{real}\n{daemon}")
    realb = subprocess.run([sys.executable, os.path.join(SCRIPT_DIR, "newton_models.py"),
                            "plan", "B", "--model", "qwen27b", "--argv"], capture_output=True,
                           text=True, env={k: v for k, v in os.environ.items()
                                           if not k.startswith("NEWTON_")}).stdout.split("\n")
    argvb = subprocess.run([os.path.join(SCRIPT_DIR, "qwen-b-argv.sh")],
                           capture_output=True, text=True).stdout.split("\n")
    check("real plan B/qwen27b == scripts/qwen-b-argv.sh",
          [t for t in realb if t] == [t for t in argvb if t])

    print("15. mlx-serve parameters for Flash-Next")
    exp = [pbf["executable"], "--model", os.path.join(FIX, "models", "fake-flash"), "--serve",
           "--host", "127.0.0.1", "--port", "8081", "--ctx-size", "262144",
           "--max-concurrent", "1", "--log-level", "info",
           "--log-file", os.path.join(FIX, "logs", "qwen-b", "mlx-serve.log")]
    check("B/flash-next argv exactly as designed", pbf["argv"] == exp, f"\n{pbf['argv']}\n{exp}")
    check("loopback bind explicit (mlx-serve defaults to 0.0.0.0)",
          pbf["argv"][pbf["argv"].index("--host") + 1] == "127.0.0.1")
    check("Flash health semantics: /health + /v1/models",
          pbf["health"]["ready"] == "http_200_and_models_listed")
    check("27B health semantics unchanged: /health json status",
          pa27["health"]["ready"] == "json_status_ok")

    print("16. heterogeneous-runtime singleton detection")
    sigs = nm.runtime_signatures(reg)
    llama = os.path.join(FIX, "tools", "fake", "llama-cpp")
    mlx_link = os.path.join(FIX, "tools", "fake", "mlx-current")
    mlx_real = os.path.realpath(mlx_link)
    check("signatures include the symlink AND its target", mlx_link in sigs and mlx_real in sigs)
    cfg_a = {"port": 8080, "alias": "qwen-a"}
    cfg_b = {"port": 8081, "alias": "qwen-b"}
    lines = [
        f" 11 100 {llama} --model x.gguf --alias qwen-a --port 8080",
        f" 22 200 {mlx_real} --model /m --serve --host 127.0.0.1 --port 8081 --ctx-size 262144",
        f" 33 300 /usr/bin/python3 something --port 8080",
        f" 44 400 {mlx_link} --model /m --serve --port 9000",
    ]
    ours, peers = qwenctl.classify_model_processes(cfg_a, lines, sigs)
    check("A: llama on 8080 is OURS", [p["pid"] for p in ours] == [11])
    check("A: mlx-serve on 8081 (peer slot) is a PEER",
          22 in [p["pid"] for p in peers])
    check("A: mlx-serve via symlink on a foreign port is a PEER", 44 in [p["pid"] for p in peers])
    check("unrelated python on 8080 ignored", 33 not in [p["pid"] for p in ours + peers])
    ours, peers = qwenctl.classify_model_processes(cfg_b, lines, sigs)
    check("B: mlx-serve on 8081 is OURS", [p["pid"] for p in ours] == [22])
    check("B: llama on 8080 is a PEER", 11 in [p["pid"] for p in peers])
    ours, peers = qwenctl.classify_model_processes(cfg_a, [lines[1]], sigs)
    check("A with only an mlx-serve peer running: no OURS, one PEER -> start must refuse",
          ours == [] and len(peers) == 1)

    print("17. UI and CLI manipulate the same state")
    nm.select_model(reg, "flash-next", by="panel", occupancy_fn=free_occ, lock_fn=no_locks)
    out = subprocess.run([sys.executable, os.path.join(SCRIPT_DIR, "newton_models.py"), "list"],
                         capture_output=True, text=True, env=dict(os.environ)).stdout
    check("CLI `list` shows the panel's selection", "● flash-next" in out)
    nm.write_selection(reg, "qwen27b", by="cli")
    check("panel-side read sees the CLI write", nm.read_selection(reg)[0] == "qwen27b")
    rec = nm.read_selection_record()
    check("record carries who set it", rec.get("set_by") == "cli")

    print("18. changing the model never starts anything")
    for m in ("flash-next", "qwen27b"):
        nm.select_model(reg, m, occupancy_fn=free_occ, lock_fn=no_locks)
    check("fake runtimes were never invoked by select", invoked_markers() == [])
    out = subprocess.run([sys.executable, os.path.join(SCRIPT_DIR, "newton_models.py"),
                          "exec-slot", "A", "--dry-run"], capture_output=True, text=True,
                         env=dict(os.environ))
    check("exec-slot --dry-run resolves without launching",
          out.returncode == 0 and "would_exec" in out.stdout and invoked_markers() == [])
    os.remove(os.path.join(FIX, "models", "fake-27b", "mmproj-Qwen3.8-27B-f16.gguf"))
    out = subprocess.run([sys.executable, os.path.join(SCRIPT_DIR, "newton_models.py"),
                          "exec-slot", "A", "--dry-run"], capture_output=True, text=True,
                         env=dict(os.environ))
    check("exec-slot refuses an unavailable selection (exit 78)", out.returncode == 78)
    write(os.path.join(FIX, "models", "fake-27b", "mmproj-Qwen3.8-27B-f16.gguf"), b"GGUFmmproj")

    print("19. restart cannot silently change the model")
    cfg = dict(qwenctl.load_config(os.path.join(REAL_ROOT, "config", "qwen-a-controller.env")),
               lease=os.path.join(FIX, "state", "a-lease.json"),
               lock=os.path.join(FIX, "state", "a.lock"), logdir=os.path.join(FIX, "logs"))
    qwenctl._REGISTRY = reg
    lease = qwenctl.default_lease(cfg)
    lease.update(state="READY", pid=777, model="flash-next", runtime="mlx-serve")
    qwenctl.write_lease(cfg, lease)
    up = {"observed_state": qwenctl.OBS_UP, "pid": 777,
          "launchd": {"bootstrapped": True, "state": "running", "pid": 777, "runs": 1, "last_exit": "0"},
          "processes": [{"pid": 777, "rss_gib": 1.0, "port": 8080, "alias": None}],
          "peer_processes": [], "duplicate_processes": False, "launchd_owned": True,
          "socket": {"listening": True, "pid": 777, "name": "127.0.0.1:8080"},
          "health": {"ok": True, "status": 200, "props": None}, "rss_gib": 1.0}
    stopped = []
    orig = (qwenctl.observe, qwenctl._do_stop, qwenctl._kickstart)
    try:
        qwenctl.observe = lambda c, health_timeout=5: up
        qwenctl._do_stop = lambda c, o=None, grace=45: stopped.append(1) or True
        qwenctl._kickstart = lambda c: (stopped.append("KICK") or (1, "STUB"))

        class Args:
            json = True
            orphan_policy = "adopt"
            lock_timeout = 5
            timeout = 1
            allow_peer = False
        try:
            qwenctl.cmd_restart(cfg, Args())
            check("restart refused when selection != running model", False, "no exit")
        except SystemExit as e:
            check("restart refused when selection != running model (exit 7)", e.code == 7)
        check("refusal happens before any stop or kickstart", stopped == [])
    finally:
        qwenctl.observe, qwenctl._do_stop, qwenctl._kickstart = orig

    print("20. corrupted selection state fails safely")
    write(os.environ["NEWTON_SELECTION_PATH"], "{not json")
    sel, src = nm.read_selection(reg)
    check("corrupt file -> default qwen27b, reason reported", sel == "qwen27b" and "corrupt" in src)
    write(os.environ["NEWTON_SELECTION_PATH"], json.dumps({"selected": "ghost-model"}))
    sel, src = nm.read_selection(reg)
    check("unknown id in file -> default qwen27b, reason reported",
          sel == "qwen27b" and "not a registered model" in src)
    st = nm.status(reg, occupancy=False)
    check("status still renders with corrupt state", st["selected"] == "qwen27b")
    nm.write_selection(reg, "qwen27b", by="test")
    check("a valid write repairs the file", nm.read_selection(reg)[1] == "recorded")

    print("21. no real model process or slot port during this suite")
    ps = subprocess.run(["ps", "-Ao", "command="], capture_output=True, text=True).stdout
    real_sigs = nm.runtime_signatures(nm.load_registry(os.path.join(REAL_ROOT, "config", "models.json")))
    # signatures from the REAL registry, resolved under the real root
    real_sigs = [s.replace(FIX, REAL_ROOT) for s in real_sigs]
    hits = [l for l in ps.splitlines() if any(l.startswith(s) for s in real_sigs)]
    check("no llama-server / mlx-serve process exists", hits == [], str(hits))
    for port in (8080, 8081):
        lo = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                            capture_output=True, text=True).stdout.strip()
        check(f"port {port} closed", lo == "")
    check("fake runtimes never invoked at all", invoked_markers() == [])

    shutil.rmtree(FIX, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
