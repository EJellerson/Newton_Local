"""Shared resource policy: Fish speech service vs the Qwen slots (docs/18 §4).

The operator's matrix:
    Fish OFF + Qwen 27B      allowed           (no new condition at all)
    Fish OFF + Flash-Next    allowed           (no new condition at all)
    Fish ON  + no LLM        allowed           (memory admission)
    Fish ON  + Qwen 27B      conditional       (memory admission)
    Fish ON  + Flash-Next    prohibited        (both directions, any memory)

Decisions are pure functions over observed facts, so every combination is
tested here without starting anything. Observation is tested with faked
primitives; the admission lock and the stale-reservation probe with real
flocks. Live races with real launchd jobs are in scripts/prove-fish-policy.py.
"""
import contextlib
import fcntl
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import resource_policy as rp  # noqa: E402
import newton_models as nm  # noqa: E402

GIB = 1 << 30
COMPAT = {"qwen27b": "conditional", "flash-next": "prohibited"}
ADM = {"reserve_gib": 4.0, "gpu_working_set_margin_gib": 4.0, "require_pressure": "normal"}
REG = {"models": {
    "qwen27b": {"display_name": "Qwen 3.8 27B", "runtime": "llama.cpp",
                "memory": {"admission": {"rule": "fixed", "required_gib": 52}}},
    "flash-next": {"display_name": "Qwen 3.8 Flash-Next", "runtime": "mlx-serve",
                   "memory": {"admission": {"rule": "mlx-serve-preflight"}}}}}


def mem(avail_gib, pressure="normal", gpu_gib=3.5, limit_gib=107.5, mmap_gib=0.0):
    return {"total_bytes": 128 * GIB, "engine_available_bytes": int((avail_gib + mmap_gib) * GIB),
            "mmap_resident_bytes": int(mmap_gib * GIB), "available_bytes": int(avail_gib * GIB),
            "pressure": pressure, "gpu_alloc_bytes": int(gpu_gib * GIB),
            "gpu_limit_bytes": int(limit_gib * GIB)}


def slot(name, model, phase="up", source="process"):
    return {"slot": name, "model": model, "phase": phase, "present": True, "source": source,
            "lease_state": "READY", "observed": phase != "recovering", "pid": 1}


DOWN = [{"slot": "A", "model": None, "phase": "down", "present": False, "source": None},
        {"slot": "B", "model": None, "phase": "down", "present": False, "source": None}]
FISH_DOWN = {"present": False, "phase": "down"}


def fish(phase):
    return {"present": True, "phase": phase, "state": "READY", "pids": [9]}


class FishStartDecisions(unittest.TestCase):
    def decide(self, slots, m):
        return rp.decide_fish_start(32.0, COMPAT, ADM, slots, m, REG, fish_name="Fish")

    def test_fish_alone_with_room_is_admitted(self):
        d = self.decide(DOWN, mem(79.8))
        self.assertTrue(d["ok"], d["message"])
        self.assertEqual(d["code"], "ok")

    def test_fish_alone_without_room_is_refused_with_figures(self):
        d = self.decide(DOWN, mem(30.0))
        self.assertFalse(d["ok"])
        self.assertEqual(d["code"], "memory")
        self.assertIn("36.0 GiB", d["message"])          # 32 + 4 headroom
        self.assertIn("30.0 GiB is free", d["message"])

    def test_flash_next_running_is_absolute_whatever_memory_says(self):
        for where in ("A", "B"):
            for phase in ("up", "starting", "stopping", "recovering"):
                d = self.decide([slot(where, "flash-next", phase)], mem(500.0))
                self.assertFalse(d["ok"], (where, phase))
                self.assertEqual(d["code"], "prohibited", (where, phase))
                self.assertIn("never allowed", d["message"])

    def test_an_unidentified_model_process_is_prohibited(self):
        d = self.decide([slot(None, None, source="unmanaged process")], mem(500.0))
        self.assertEqual(d["code"], "prohibited")

    def test_27b_running_with_room_is_admitted(self):
        d = self.decide([slot("A", "qwen27b")], mem(40.0))
        self.assertTrue(d["ok"], d["message"])
        self.assertEqual(d["details"]["alongside"], ["qwen27b"])

    def test_27b_running_without_room_is_refused(self):
        d = self.decide([slot("A", "qwen27b")], mem(35.0))
        self.assertEqual((d["ok"], d["code"]), (False, "memory"))

    def test_27b_still_starting_is_refused_one_load_at_a_time(self):
        d = self.decide([slot("A", "qwen27b", "starting")], mem(200.0))
        self.assertEqual((d["ok"], d["code"]), (False, "transition"))

    def test_pressure_must_be_normal(self):
        d = self.decide(DOWN, mem(90.0, pressure="warning"))
        self.assertEqual((d["ok"], d["code"]), (False, "pressure"))

    def test_gpu_working_set_limit(self):
        d = self.decide([slot("A", "qwen27b")], mem(60.0, gpu_gib=75.0, limit_gib=107.5))
        self.assertEqual((d["ok"], d["code"]), (False, "gpu"))
        d = self.decide([slot("A", "qwen27b")], mem(60.0, gpu_gib=48.0, limit_gib=107.5))
        self.assertTrue(d["ok"], d["message"])

    def test_unmeasurable_memory_refuses(self):
        d = self.decide(DOWN, None)
        self.assertEqual((d["ok"], d["code"]), (False, "memory"))

    def test_27b_recovering_does_not_block_and_is_not_counted_as_resident(self):
        d = self.decide([slot("A", "qwen27b", "recovering", "desired")], mem(40.0))
        self.assertTrue(d["ok"])
        self.assertEqual(d["details"]["alongside"], [])


class QwenStartDecisions(unittest.TestCase):
    def setUp(self):
        self.calls = 0

    def mem_fn(self, m):
        def f():
            self.calls += 1
            return m
        return f

    def decide(self, model, f, m=None):
        return rp.decide_qwen_start(model, COMPAT, ADM, f, self.mem_fn(m), REG, fish_name="Fish")

    def test_fish_absent_means_no_new_condition_for_any_model(self):
        for model in ("qwen27b", "flash-next", "anything-else"):
            d = self.decide(model, FISH_DOWN, None)
            self.assertTrue(d["ok"], model)
        self.assertEqual(self.calls, 0, "Qwen behaviour without Fish must not even measure")

    def test_flash_next_refused_in_every_fish_phase(self):
        for phase in ("up", "starting", "stopping", "recovering"):
            d = self.decide("flash-next", fish(phase), mem(500.0))
            self.assertEqual((d["ok"], d["code"]), (False, "prohibited"), phase)
            self.assertIn("Stop Fish in Voice Studio first", d["message"])
        self.assertEqual(self.calls, 0, "the absolute rule never consults memory")

    def test_unknown_model_refused_beside_fish(self):
        d = self.decide("some-new-model", fish("up"), mem(500.0))
        self.assertEqual(d["code"], "prohibited")

    def test_27b_beside_running_fish_needs_memory(self):
        d = self.decide("qwen27b", fish("up"), mem(60.0))
        self.assertTrue(d["ok"], d["message"])
        d = self.decide("qwen27b", fish("up"), mem(50.0))
        self.assertEqual((d["ok"], d["code"]), (False, "memory"))
        self.assertIn("while Fish is running", d["message"])

    def test_27b_while_fish_loads_is_refused(self):
        d = self.decide("qwen27b", fish("starting"), mem(500.0))
        self.assertEqual((d["ok"], d["code"]), (False, "transition"))

    def test_27b_beside_recovering_fish_is_admitted_without_measuring(self):
        d = self.decide("qwen27b", fish("recovering"), None)
        self.assertTrue(d["ok"])
        self.assertEqual(self.calls, 0)


class MemoryArithmetic(unittest.TestCase):
    def test_boundaries(self):
        self.assertIsNone(rp.memory_check("X", 32.0, mem(36.0), ADM))
        self.assertEqual(rp.memory_check("X", 32.0, mem(35.99), ADM)[0], "memory")
        self.assertIsNone(rp.memory_check("X", 32.0, mem(36.0, gpu_gib=71.5), ADM))
        self.assertEqual(rp.memory_check("X", 32.0, mem(36.0, gpu_gib=71.6), ADM)[0], "gpu")

    def test_mmap_weights_of_a_resident_27b_are_not_counted_as_free(self):
        counts = {"page_size": 16384, "wired": 0, "compressor": 0, "internal": 0, "purgeable": 0}
        reg = nm.load_registry()
        with mock.patch.object(rp, "gpu_allocation_bytes", return_value=0):
            resident = rp.measure_memory(reg, [slot("A", "qwen27b")], None, counts=counts,
                                         total=128 * GIB, gpu=0, pressure="normal")
            recovering = rp.measure_memory(reg, [slot("A", "qwen27b", "recovering")], None,
                                           counts=counts, total=128 * GIB, gpu=0,
                                           pressure="normal")
            flash = rp.measure_memory(reg, [slot("A", "flash-next")], None, counts=counts,
                                      total=128 * GIB, gpu=0, pressure="normal")
        self.assertEqual(resident["mmap_resident_bytes"], 29978230400)   # the 27B manifest
        self.assertEqual(resident["available_bytes"], 128 * GIB - 29978230400)
        self.assertEqual(recovering["mmap_resident_bytes"], 0)
        self.assertEqual(flash["mmap_resident_bytes"], 0)


class ProcessClassification(unittest.TestCase):
    def setUp(self):
        self.reg = nm.load_registry()
        a27 = nm.launch_plan(self.reg, "A", "qwen27b")["argv"]
        bfn = nm.launch_plan(self.reg, "B", "flash-next")["argv"]
        self.lines = [
            "101 18000000 " + " ".join(a27),
            "102 70000000 " + " ".join(bfn),
            "103 1000 " + a27[0] + " --version",
            "104 2000 /opt/other/llama-server --model /tmp/unknown.gguf --port 9999",
            "105 24000000 /x/Python /repo/scripts/fish_engine.py --config /repo/config/fish.json",
            "106 20000 /usr/bin/python3 /repo/scripts/fishctl.py exec-engine",
            "107 20000 /usr/bin/python3 /repo/scripts/fishctl.py status --json",
            "108 1000 /bin/zsh -c echo llama-server",
        ]

    def test_classification(self):
        got = {p["pid"]: p for p in rp.classify_processes(self.lines, self.reg)}
        self.assertEqual(got[101]["model"], "qwen27b")
        self.assertEqual(got[101]["port"], 8080)
        self.assertEqual(got[102]["model"], "flash-next")
        self.assertNotIn(103, got, "a --version probe is not a model server")
        self.assertIsNone(got[104]["model"], "an unknown artifact stays unidentified")
        self.assertEqual((got[105]["kind"], got[105]["shim"]), ("fish", False))
        self.assertEqual((got[106]["kind"], got[106]["shim"]), ("fish", True))
        self.assertNotIn(107, got, "fishctl status is not an engine")
        self.assertNotIn(108, got)


class Presence(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name
        self.addCleanup(self._td.cleanup)
        self.cfg = {"service": {"label": "com.newton.test.fishpolicy", "port": 18779,
                                "statedir": os.path.join(self.tmp, "fish"),
                                "logdir": os.path.join(self.tmp, "logs"),
                                "store": os.path.join(self.tmp, "store")},
                    "compatibility": COMPAT}
        os.makedirs(os.path.join(self.tmp, "fish"))
        self.state = os.path.join(self.tmp, "fish", "state.json")
        self.lock = os.path.join(self.tmp, "fish", "fish.lock")
        self.desired = os.path.join(self.tmp, "fish", "desired.json")
        for p in (mock.patch.object(rp, "_launchd", lambda label: {"state": "not running",
                                                                  "pid": None}),
                  mock.patch.object(rp, "_socket", lambda port: {"listening": False,
                                                                "pid": None})):
            p.start()
            self.addCleanup(p.stop)

    def write(self, path, obj):
        with open(path, "w") as f:
            json.dump(obj, f)

    def observe(self, procs=(), boot=1000):
        with mock.patch.object(rp.fc, "paths", lambda cfg: {
                "state": self.state, "lock": self.lock, "desired": self.desired}):
            return rp.observe_fish(self.cfg, list(procs), boot)

    def test_nothing_recorded_or_running_is_absent(self):
        self.assertFalse(self.observe()["present"])

    def test_an_engine_process_is_present(self):
        o = self.observe([{"kind": "fish", "pid": 9, "rss_gib": 24.0, "shim": False}])
        self.assertEqual((o["present"], o["phase"]), (True, "up"))

    def test_starting_record_with_its_controller_alive_is_present(self):
        self.write(self.state, {"state": "STARTING", "start_deadline": time.time() + 300})
        with open(self.lock, "a+") as fh:
            holder = subprocess.Popen([sys.executable, "-c",
                                       "import fcntl,sys,time;f=open(sys.argv[1],'a+');"
                                       "fcntl.flock(f,fcntl.LOCK_EX);print('held',flush=True);"
                                       "time.sleep(30)", self.lock],
                                      stdout=subprocess.PIPE, text=True)
            self.addCleanup(holder.kill)
            self.assertEqual(holder.stdout.readline().strip(), "held")
            o = self.observe()
        self.assertEqual((o["present"], o["phase"]), (True, "starting"))

    def test_starting_record_whose_controller_died_is_stale(self):
        self.write(self.state, {"state": "STARTING", "start_deadline": time.time() + 300})
        self.assertFalse(self.observe()["present"])

    def test_starting_record_past_its_deadline_is_stale_even_if_locked(self):
        self.write(self.state, {"state": "STARTING", "start_deadline": time.time() - 1})
        with mock.patch.object(rp, "lock_is_free", return_value=False):
            self.assertFalse(self.observe()["present"])

    def test_ready_record_with_nothing_running_is_stale(self):
        self.write(self.state, {"state": "READY"})
        self.assertFalse(self.observe()["present"])

    def test_desired_running_this_boot_is_recovering(self):
        self.write(self.desired, {"desired": "RUNNING", "boot_sec": 1000})
        o = self.observe(boot=1000)
        self.assertEqual((o["present"], o["phase"]), (True, "recovering"))
        self.assertFalse(self.observe(boot=2000)["present"], "a reboot lands on OFF")


class QwenPresence(unittest.TestCase):
    """observe_qwen with qwenctl's primitives faked."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.leases = {"A": {"state": "STOPPED", "model": None},
                       "B": {"state": "STOPPED", "model": None}}
        self.lock_free = {"A": True, "B": True}
        self.reg = nm.load_registry()
        import qwenctl as q
        self.q = q
        cfgs = {"A": {"port": 8080, "lock": "A.lock", "slot": "A"},
                "B": {"port": 8081, "lock": "B.lock", "slot": "B"}}
        patches = [
            mock.patch.object(nm, "SLOT_CONFIGS", {"A": "A", "B": "B"}),
            mock.patch.object(q, "load_config", lambda env: cfgs[env]),
            mock.patch.object(q, "read_lease", lambda cfg: dict(self.leases[cfg["slot"]])),
            mock.patch.object(q, "obs_launchd", lambda cfg: {"state": "not running", "pid": None}),
            mock.patch.object(q, "obs_socket", lambda cfg: {"listening": False, "pid": None}),
            mock.patch.object(rp, "lock_is_free", lambda path: self.lock_free[path[0]]),
            mock.patch.object(nm, "read_selection", lambda reg: ("flash-next", "recorded")),
            mock.patch.object(rp.fc, "read_json", lambda path: self.desired),
        ]
        self.desired = None
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def present(self, procs=(), boot=1000):
        return [s for s in rp.observe_qwen(self.reg, list(procs), boot) if s["present"]]

    def test_all_stopped_is_absent(self):
        self.assertEqual(self.present(), [])

    def test_live_starting_lease_is_present_with_its_model(self):
        self.leases["A"] = {"state": "STARTING", "model": "flash-next",
                            "start_deadline": time.time() + 600}
        self.lock_free["A"] = False
        s = self.present()
        self.assertEqual([(x["slot"], x["phase"], x["model"]) for x in s],
                         [("A", "starting", "flash-next")])

    def test_starting_lease_of_a_dead_start_is_stale(self):
        self.leases["A"] = {"state": "STARTING", "model": "flash-next",
                            "start_deadline": time.time() + 600}
        self.assertEqual(self.present(), [])

    def test_process_on_slot_b_is_identified(self):
        s = self.present([{"kind": "qwen", "pid": 5, "port": 8081, "model": "qwen27b"}])
        self.assertEqual([(x["slot"], x["model"]) for x in s], [("B", "qwen27b")])

    def test_unmanaged_runtime_process_counts(self):
        s = self.present([{"kind": "qwen", "pid": 5, "port": 9999, "model": None}])
        self.assertEqual([(x["slot"], x["model"], x["source"]) for x in s],
                         [(None, None, "unmanaged process")])

    def test_slot_a_desired_running_is_recovering_with_the_selection(self):
        self.desired = {"desired": "RUNNING", "boot_sec": 1000}
        s = self.present(boot=1000)
        self.assertEqual([(x["slot"], x["phase"], x["model"]) for x in s],
                         [("A", "recovering", "flash-next")])


class AdmissionLock(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.path = os.path.join(self._td.name, "admission.lock")

    def test_held_elsewhere_means_busy_not_a_hang(self):
        holder = subprocess.Popen([sys.executable, "-c",
                                   "import fcntl,sys,time;f=open(sys.argv[1],'a+');"
                                   "fcntl.flock(f,fcntl.LOCK_EX);print('held',flush=True);"
                                   "time.sleep(30)", self.path], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        t = time.time()
        with self.assertRaises(rp.AdmissionBusy):
            with rp.admission_lock(self.path, timeout=0.3):
                pass
        self.assertLess(time.time() - t, 2)
        self.assertFalse(rp.lock_is_free(self.path))
        holder.kill()
        holder.wait()
        self.assertTrue(rp.lock_is_free(self.path))
        with rp.admission_lock(self.path, timeout=1):
            pass

    def test_check_then_reserve_under_the_lock_never_admits_both(self):
        """The discipline the controllers follow: decide and write the
        reservation inside one admission_lock. A Fish start and a Flash-Next
        start race 200 times; at most one may ever pass."""
        res = {"fish": os.path.join(self._td.name, "fish"),
               "flash": os.path.join(self._td.name, "flash")}

        def start(me, other, barrier, out):
            barrier.wait()
            with rp.admission_lock(self.path, timeout=5):
                ok = not os.path.exists(res[other])
                time.sleep(0.0005)               # widen the window
                if ok:
                    open(res[me], "w").close()
            out[me] = ok

        for _ in range(200):
            for p in res.values():
                if os.path.exists(p):
                    os.remove(p)
            barrier, out = threading.Barrier(2), {}
            ts = [threading.Thread(target=start, args=("fish", "flash", barrier, out)),
                  threading.Thread(target=start, args=("flash", "fish", barrier, out))]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(sum(out.values()), 1, out)


class DesiredState(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(rp.effective_desired(None, 5), "STOPPED")
        self.assertEqual(rp.effective_desired({"desired": "RUNNING", "boot_sec": 5}, 5), "RUNNING")
        self.assertEqual(rp.effective_desired({"desired": "RUNNING", "boot_sec": 4}, 5), "STOPPED")
        self.assertEqual(rp.effective_desired({"desired": "STOPPED", "boot_sec": 5}, 5), "STOPPED")


class QwenctlHook(unittest.TestCase):
    """qwenctl._admission: a refusal exits 10 and writes nothing; an
    admission holds the shared lock exactly while STARTING is written."""

    def setUp(self):
        import qwenctl
        self.q = qwenctl
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.lockpath = os.path.join(self._td.name, "admission.lock")
        self.cfg = {"instance": "qwen-a", "logdir": self._td.name}
        p = mock.patch.object(rp.fc, "ADMISSION_LOCK", self.lockpath)
        p.start()
        self.addCleanup(p.stop)

    def args(self):
        return type("A", (), {"json": True})()

    def test_refusal_exits_10_with_a_record(self):
        refused = {"ok": False, "code": "prohibited", "message": "Flash-Next can't start beside Fish."}
        wrote = []
        out = __import__("io").StringIO()
        with mock.patch.object(rp, "check_qwen_start", return_value=refused), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(__import__("io").StringIO()):
            with self.assertRaises(SystemExit) as cm:
                with self.q._admission(self.cfg, self.args(), {"model": "flash-next"}, "start"):
                    wrote.append("STARTING")
        self.assertEqual(cm.exception.code, 10)
        self.assertEqual(wrote, [], "nothing may be written after a refusal")
        rec = json.loads(out.getvalue())
        self.assertEqual((rec["error"], rec["exit_code"], rec["policy_code"]),
                         ("policy", 10, "prohibited"))
        self.assertTrue(rp.lock_is_free(self.lockpath), "the lock is released")

    def test_admission_holds_the_lock_during_the_write(self):
        seen = []
        with mock.patch.object(rp, "check_qwen_start", return_value={"ok": True, "code": "ok"}):
            with self.q._admission(self.cfg, self.args(), {"model": "qwen27b"}, "start"):
                seen.append(rp.lock_is_free(self.lockpath))
        self.assertEqual(seen, [False])
        self.assertTrue(rp.lock_is_free(self.lockpath))

    def test_a_policy_crash_fails_closed(self):
        with mock.patch.object(rp, "check_qwen_start", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stdout(__import__("io").StringIO()), \
                contextlib.redirect_stderr(__import__("io").StringIO()):
            with self.assertRaises(SystemExit) as cm:
                with self.q._admission(self.cfg, self.args(), {"model": "qwen27b"}, "start"):
                    self.fail("must not admit")
        self.assertEqual(cm.exception.code, 10)


class ExecSlotBackstop(unittest.TestCase):
    def test_refusal_and_fail_closed(self):
        with mock.patch.object(rp, "backstop_qwen_launch",
                               return_value={"ok": False, "message": "Fish is running"}):
            self.assertEqual(nm.policy_backstop("flash-next"), "Fish is running")
        with mock.patch.object(rp, "backstop_qwen_launch", return_value={"ok": True}):
            self.assertIsNone(nm.policy_backstop("qwen27b"))
        with mock.patch.object(rp, "backstop_qwen_launch", side_effect=OSError("ps failed")):
            self.assertIn("ps failed", nm.policy_backstop("qwen27b"))

    def test_backstop_enforces_only_the_absolute_rule(self):
        with mock.patch.object(rp, "check_qwen_start",
                               return_value={"ok": False, "code": "memory", "message": "tight"}):
            self.assertTrue(rp.backstop_qwen_launch("qwen27b")["ok"])
        with mock.patch.object(rp, "check_qwen_start",
                               return_value={"ok": False, "code": "prohibited", "message": "no"}):
            self.assertFalse(rp.backstop_qwen_launch("flash-next")["ok"])


if __name__ == "__main__":
    unittest.main()
