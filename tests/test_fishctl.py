"""fishctl: desired state and stop intent (docs/18 §3).

The watchdog in Newton AI Control restarts Fish only while fishctl's desired
state reads RUNNING for this boot. These tests pin the rules that make an
intentional stop impossible to undo, whichever entry point stopped it.
Standard library only; nothing is started.
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fishctl  # noqa: E402
import qwenctl as q  # noqa: E402
import resource_policy as rp  # noqa: E402


class DesiredStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        with open(ROOT / "config" / "fish.json") as f:
            cfg = json.load(f)
        cfg["service"].update(label="com.newton.test.fishctl", port=18799,
                              statedir=str(self.tmp / "state"), logdir=str(self.tmp / "logs"),
                              store=str(self.tmp / "store"))
        path = self.tmp / "fish.json"
        path.write_text(json.dumps(cfg))
        self.cfg, self.ctl = fishctl.load(str(path))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def desired(self):
        return fishctl.read_desired(self.cfg)

    def test_default_is_stopped_and_records_are_boot_scoped(self):
        self.assertEqual(self.desired()["desired"], "STOPPED")
        fishctl.set_desired(self.cfg, "RUNNING", "test")
        self.assertEqual(self.desired()["desired"], "RUNNING")
        with mock.patch.object(rp, "boot_time_sec", lambda: 1):     # a reboot
            self.assertEqual(self.desired()["desired"], "STOPPED")
        mode = os.stat(fishctl.fc.paths(self.cfg)["desired"]).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_stop_intent_records_stopped_and_cancels(self):
        fishctl.set_desired(self.cfg, "RUNNING", "test")
        t0 = time.time()
        fishctl.stop_intent(self.cfg, self.ctl, "cli")
        self.assertEqual(self.desired()["desired"], "STOPPED")
        self.assertEqual(self.desired()["record"]["set_by"], "cli")
        self.assertTrue(q.cancel_requested(self.ctl, t0))

    def test_a_start_finishing_after_a_stop_does_not_record_running(self):
        t0 = time.time()
        fishctl.stop_intent(self.cfg, self.ctl, "panel")
        self.assertFalse(fishctl.desire_running(self.cfg, self.ctl, "panel", t0))
        self.assertEqual(self.desired()["desired"], "STOPPED")
        # a later start (t0 after the marker) does record RUNNING
        time.sleep(0.01)
        self.assertTrue(fishctl.desire_running(self.cfg, self.ctl, "panel", time.time()))
        self.assertEqual(self.desired()["desired"], "RUNNING")

    def test_race_between_finishing_start_and_stop_always_ends_stopped(self):
        for _ in range(100):
            fishctl.set_desired(self.cfg, "STOPPED", "reset")
            t0 = time.time()
            go = threading.Barrier(2)

            def start():
                go.wait()
                fishctl.desire_running(self.cfg, self.ctl, "panel", t0)

            def stop():
                go.wait()
                fishctl.stop_intent(self.cfg, self.ctl, "cli")
            a, b = threading.Thread(target=start), threading.Thread(target=stop)
            a.start(); b.start(); a.join(); b.join()
            self.assertEqual(self.desired()["desired"], "STOPPED")

    def test_failed_start_records_stopped_unless_recovery(self):
        fishctl.set_desired(self.cfg, "RUNNING", "watchdog")
        args = types.SimpleNamespace(recovery=True, by="watchdog")
        fishctl.start_failed_intent(self.cfg, self.ctl, args, 8)
        self.assertEqual(self.desired()["desired"], "RUNNING")
        args = types.SimpleNamespace(recovery=False, by="panel")
        fishctl.start_failed_intent(self.cfg, self.ctl, args, 0)       # success: untouched
        self.assertEqual(self.desired()["desired"], "RUNNING")
        fishctl.start_failed_intent(self.cfg, self.ctl, args, 10)
        self.assertEqual(self.desired()["desired"], "STOPPED")
        self.assertEqual(self.desired()["record"]["set_by"], "panel:start-failed")

    def test_cmd_start_failure_path_records_stopped(self):
        fishctl.set_desired(self.cfg, "RUNNING", "earlier")
        args = types.SimpleNamespace(lock_timeout=5, recovery=False, by="cli", json=False,
                                     timeout=None)

        def refuse(cfg, ctl, a, t0):
            fishctl.die("refused by policy", rp.EXIT_POLICY)
        with mock.patch.object(fishctl, "start_locked", refuse), \
                mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as cm:
                fishctl.cmd_start(self.cfg, self.ctl, args)
        self.assertEqual(cm.exception.code, rp.EXIT_POLICY)
        self.assertEqual(self.desired()["desired"], "STOPPED")

    def test_cmd_start_success_records_running(self):
        args = types.SimpleNamespace(lock_timeout=5, recovery=False, by="panel", json=False,
                                     timeout=None)
        with mock.patch.object(fishctl, "start_locked", lambda c, ctl, a, t0: 0):
            self.assertEqual(fishctl.cmd_start(self.cfg, self.ctl, args), 0)
        self.assertEqual(self.desired()["desired"], "RUNNING")
        self.assertEqual(self.desired()["record"]["set_by"], "panel")


if __name__ == "__main__":
    unittest.main()
