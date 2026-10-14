"""Start supervision — the 2026-10-12 stuck-start incident.

On 2026-10-12 a Flash-Next start sat in "Starting" for the full 600 s start
timeout although mlx-serve had refused the load within a second for lack of
free memory; Stop could not run until the start gave up; and a status poll
overwrote the controller record with a stale copy. These tests pin the fixes:

  1. fail fast when the engine exits during a start (launchd is watched);
  2. the engine's own reason is extracted and shown in plain words;
  3. Stop cancels a start that is in progress (cancel marker);
  4. unlocked status polls persist only by compare-and-swap;
  5. the advisory memory fit mirrors mlx-serve's own admission formula.

No test starts, kicks or kills any process. Controller observations, launchd,
the clock and qwenctl itself (for the panel) are faked.
"""
import argparse
import contextlib
import fcntl
import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "integrations"))
import qwenctl  # noqa: E402
import newton_models as nm  # noqa: E402

# Verbatim from logs/qwen-a/stderr.log, 2026-10-12 15:35 (mlx-serve v26.9.1).
INCIDENT = (
    "[preflight] weights ~70.13 GB, available 68.98 GB\n"
    "Insufficient memory to load model: needs ~77.1 GB free (70.1 GB of weights "
    "plus headroom for warmup buffers and a baseline KV cache) but only 69.0 GB is "
    "available. Close other models/apps (or wait for a prior mlx-serve to fully "
    "exit) and retry; pass --skip-mem-preflight to override.\n"
    "[scheduler] model load failed: InsufficientMemory\n"
    "error: LoadFailed\n")
INCIDENT_2 = INCIDENT.replace("68.98", "72.20").replace("only 69.0", "only 72.2")

GIB = 1 << 30


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make_cfg(tmp, instance="qwen-a", slot="A"):
    return {"instance": instance, "slot": slot, "alias": "local-model",
            "host": "127.0.0.1", "port": 18999, "ctx": 4096,
            "label": "com.newton.test.unit", "logdir": os.path.join(tmp, "logs"),
            "lease": os.path.join(tmp, "lease.json"),
            "lock": os.path.join(tmp, "slot.lock"), "idle_ttl": 0,
            "start_timeout": 30, "lease_max_age": 1800, "uid": os.getuid(),
            "llama_bin": None, "model": None, "mmproj": None}


def ld(state="not running", pid=None, runs=5, last_exit="1", stderr_path=None):
    return {"bootstrapped": True, "state": state, "pid": pid, "runs": runs,
            "last_exit": last_exit, "stderr_path": stderr_path}


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name
        self.cfg = make_cfg(self.tmp)
        self.clock = FakeClock()
        for p in (mock.patch.object(qwenctl, "now", self.clock.now),
                  mock.patch.object(qwenctl.time, "sleep", self.clock.sleep)):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self._td.cleanup()

    def controller_events(self):
        p = os.path.join(self.cfg["logdir"], "controller.log")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(l)["event"] for l in f if l.strip()]


def scripted(steps):
    """Fake (obs_processes, obs_health, obs_launchd): one step per poll."""
    st = {"i": -1}

    def procs(cfg):
        st["i"] = min(st["i"] + 1, len(steps) - 1)
        return steps[st["i"]].get("procs", [])

    def health(cfg, timeout=5):
        return {"ok": steps[st["i"]].get("health", False), "status": None,
                "props": None}

    def launchd(cfg):
        return dict(steps[st["i"]].get("ld") or ld())
    return procs, health, launchd


@contextlib.contextmanager
def observing(steps):
    procs, health, launchd = scripted(steps)
    with mock.patch.object(qwenctl, "obs_processes", procs), \
            mock.patch.object(qwenctl, "obs_health", health), \
            mock.patch.object(qwenctl, "obs_launchd", launchd):
        yield


PROC = [{"pid": 4242, "rss_gib": 1.0, "port": 18999, "alias": "x",
         "executable": "/x/engine"}]


# --------------------------------------------------------------- 1 fail fast
class WaitReadyTests(TmpCase):
    def test_ready_when_health_turns_ok(self):
        with observing([{"ld": ld("running", 4242, 6), "procs": PROC},
                        {"ld": ld("running", 4242, 6), "procs": PROC},
                        {"ld": ld("running", 4242, 6), "procs": PROC,
                         "health": True}]):
            ok, t_spawn, t_ready, failure = qwenctl.wait_ready(
                self.cfg, self.clock.now() + 60, runs_before=5)
        self.assertTrue(ok)
        self.assertIsNone(failure)
        self.assertIsNotNone(t_spawn)
        self.assertIsNotNone(t_ready)

    def test_engine_exit_reported_within_two_polls_not_the_timeout(self):
        t0 = self.clock.now()
        with observing([{"ld": ld("running", 4242, 6), "procs": PROC},
                        {"ld": ld("not running", None, 6, "1", "/l/stderr.log")}]):
            ok, _, _, failure = qwenctl.wait_ready(
                self.cfg, t0 + 600, runs_before=5)
        self.assertFalse(ok)
        self.assertEqual(failure["kind"], "exited")
        self.assertEqual(failure["last_exit"], "1")
        self.assertEqual(failure["stderr_path"], "/l/stderr.log")
        self.assertLessEqual(self.clock.now() - t0, 1.5)   # not 600 s

    def test_exit_before_first_observation_detected_by_run_counter(self):
        t0 = self.clock.now()
        with observing([{"ld": ld("not running", None, 6, "1")}]):
            ok, _, _, failure = qwenctl.wait_ready(self.cfg, t0 + 600,
                                                   runs_before=5)
        self.assertEqual(failure["kind"], "exited")
        self.assertLessEqual(self.clock.now() - t0, 1.0)

    def test_spawn_not_yet_registered_is_not_mistaken_for_exit(self):
        steps = [{"ld": ld("not running", None, 5)}] * 4 + [
            {"ld": ld("xpcproxy", 4242, 6)},
            {"ld": ld("running", 4242, 6), "procs": PROC},
            {"ld": ld("running", 4242, 6), "procs": PROC, "health": True}]
        with observing(steps):
            ok, _, _, failure = qwenctl.wait_ready(
                self.cfg, self.clock.now() + 60, runs_before=5)
        self.assertTrue(ok, failure)

    def test_a_single_not_running_blip_does_not_fail_the_start(self):
        steps = [{"ld": ld("running", 4242, 6), "procs": PROC},
                 {"ld": ld("not running", None, 6)},
                 {"ld": ld("running", 4242, 6), "procs": PROC},
                 {"ld": ld("running", 4242, 6), "procs": PROC, "health": True}]
        with observing(steps):
            ok, _, _, failure = qwenctl.wait_ready(
                self.cfg, self.clock.now() + 60, runs_before=5)
        self.assertTrue(ok, failure)

    def test_cancel_newer_than_the_start_aborts_at_the_next_poll(self):
        t0 = self.clock.now()
        self.clock.t += 1
        qwenctl.request_cancel(self.cfg, by="test")
        with observing([{"ld": ld("running", 4242, 6), "procs": PROC}]):
            ok, _, _, failure = qwenctl.wait_ready(
                self.cfg, t0 + 600, runs_before=5, cancel_since=t0)
        self.assertFalse(ok)
        self.assertEqual(failure["kind"], "cancelled")
        self.assertEqual(failure["cancel"]["by"], "test")

    def test_cancel_older_than_the_start_is_ignored(self):
        qwenctl.request_cancel(self.cfg, by="old")
        self.clock.t += 5
        t0 = self.clock.now()
        with observing([{"ld": ld("running", 4242, 6), "procs": PROC,
                         "health": True}]):
            ok, _, _, failure = qwenctl.wait_ready(
                self.cfg, t0 + 60, runs_before=5, cancel_since=t0)
        self.assertTrue(ok, failure)

    def test_timeout_still_ends_a_hung_load(self):
        t0 = self.clock.now()
        with observing([{"ld": ld("running", 4242, 6), "procs": PROC}]):
            ok, _, _, failure = qwenctl.wait_ready(self.cfg, t0 + 5,
                                                   runs_before=5)
        self.assertFalse(ok)
        self.assertEqual(failure["kind"], "timeout")


# ----------------------------------------------------------- 2 the reason
class DiagnosisTests(unittest.TestCase):
    def test_incident_refusal_is_parsed_in_the_engines_own_figures(self):
        r = qwenctl.diagnose_start_failure(INCIDENT)
        self.assertEqual(r["kind"], "insufficient_memory")
        self.assertEqual((r["required_gib"], r["weights_gib"], r["available_gib"]),
                         (77.1, 70.1, 69.0))
        self.assertEqual(r["preflight_available_gib"], 68.98)
        self.assertEqual(r["short_by_gib"], 8.1)
        self.assertEqual(r["summary"], "not enough free memory: it needs 77.1 GiB "
                                       "free and only 69.0 GiB was available")
        self.assertTrue(r["line"].startswith("Insufficient memory to load model"))

    def test_the_most_recent_attempt_wins_in_a_shared_log(self):
        r = qwenctl.diagnose_start_failure(INCIDENT + "noise\n" + INCIDENT_2)
        self.assertEqual(r["available_gib"], 72.2)
        self.assertEqual(r["preflight_available_gib"], 72.2)

    def test_shim_refusal_outranks_generic_error_lines(self):
        text = ("error: something generic\n"
                "newton-model exec-slot A: refusing to launch flash-next: missing: "
                "model-00003.safetensors\n")
        r = qwenctl.diagnose_start_failure(text)
        self.assertEqual(r["kind"], "engine_error")
        self.assertIn("refusing to launch flash-next", r["summary"])

    def test_scheduler_line_is_preferred_to_the_bare_error_code(self):
        r = qwenctl.diagnose_start_failure(
            "[scheduler] model load failed: UnsupportedArch\nerror: LoadFailed\n")
        self.assertEqual(r["summary"], "[scheduler] model load failed: UnsupportedArch")

    def test_llama_cpp_load_and_bind_failures(self):
        r = qwenctl.diagnose_start_failure(
            "llama_model_load: error loading model: tensor 'blk.0' is corrupt\n"
            "main: exiting due to model loading error\n")
        self.assertIn("error loading model", r["summary"])
        r = qwenctl.diagnose_start_failure(
            "srv    init: couldn't bind HTTP server socket, hostname: 127.0.0.1, "
            "port: 8080\n")
        self.assertIn("couldn't bind", r["summary"])
        r = qwenctl.diagnose_start_failure("info: listening\nerror: AddressInUse\n")
        self.assertEqual(r["summary"], "error: AddressInUse")

    def test_traceback_and_unknown_and_empty(self):
        r = qwenctl.diagnose_start_failure(
            "Traceback (most recent call last):\n  File \"x\", line 1\n"
            "ValueError: bad registry\n")
        self.assertEqual(r["summary"], "ValueError: bad registry")
        r = qwenctl.diagnose_start_failure("loading tensors 40%\nloading tensors 41%\n")
        self.assertEqual((r["kind"], r["summary"]), ("unknown", "loading tensors 41%"))
        self.assertIsNone(qwenctl.diagnose_start_failure("\n  \n"))
        self.assertIsNone(qwenctl.diagnose_start_failure(None))


class LogReadingTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._td.name, "stderr.log")
        with open(self.path, "w") as f:
            f.write("A" * 40 + "B" * 60)

    def tearDown(self):
        self._td.cleanup()

    def test_reads_only_what_was_written_since_the_offset(self):
        self.assertEqual(qwenctl.read_log_since(self.path, 40), "B" * 60)
        self.assertEqual(qwenctl.log_size(self.path), 100)

    def test_a_truncated_log_is_read_from_its_start(self):
        with open(self.path, "w") as f:
            f.write("fresh")
        self.assertEqual(qwenctl.read_log_since(self.path, 40), "fresh")

    def test_tail_mode_and_limit(self):
        self.assertEqual(qwenctl.read_log_since(self.path, None, tail=10), "B" * 10)
        self.assertEqual(qwenctl.read_log_since(self.path, 0, limit=25), "B" * 25)
        self.assertEqual(qwenctl.read_log_since("/nonexistent/x.log", 0), "")
        self.assertEqual(qwenctl.log_size(None), 0)


# ------------------------------------------------------------ 3 cancellation
class CancelMarkerTests(TmpCase):
    def test_request_read_clear(self):
        self.assertIsNone(qwenctl.read_cancel(self.cfg))
        rec = qwenctl.request_cancel(self.cfg, by="panel")
        self.assertEqual(qwenctl.cancel_path(self.cfg),
                         os.path.join(self.tmp, "slot.cancel"))
        self.assertEqual(qwenctl.read_cancel(self.cfg)["by"], "panel")
        self.assertTrue(qwenctl.cancel_requested(self.cfg, rec["requested_at"]))
        self.assertFalse(qwenctl.cancel_requested(self.cfg, rec["requested_at"] + 1))
        qwenctl.clear_cancel(self.cfg)
        qwenctl.clear_cancel(self.cfg)                      # idempotent
        self.assertIsNone(qwenctl.read_cancel(self.cfg))

    def test_a_corrupt_marker_is_stale_and_cannot_wedge_the_slot(self):
        with open(qwenctl.cancel_path(self.cfg), "w") as f:
            f.write("{not json")
        self.assertFalse(qwenctl.cancel_requested(self.cfg, self.clock.now()))
        self.assertIsNone(qwenctl._consume_stale_cancel(self.cfg, self.clock.now()))
        self.assertFalse(os.path.exists(qwenctl.cancel_path(self.cfg)))

    def test_consume_keeps_a_fresh_marker_and_clears_a_stale_one(self):
        t0 = self.clock.now()
        self.clock.t += 1
        qwenctl.request_cancel(self.cfg, by="fresh")
        self.assertEqual(qwenctl._consume_stale_cancel(self.cfg, t0)["by"], "fresh")
        self.assertTrue(os.path.exists(qwenctl.cancel_path(self.cfg)))
        self.clock.t += 10
        self.assertIsNone(qwenctl._consume_stale_cancel(self.cfg, self.clock.now()))
        self.assertFalse(os.path.exists(qwenctl.cancel_path(self.cfg)))
        self.assertIn("cancel.stale_cleared", self.controller_events())


def start_args(**kw):
    a = dict(json=True, orphan_policy="adopt", timeout=None, allow_peer=False,
             lock_timeout=5, owner="job-1", steal=False, force=False, by=None,
             wait=0)
    a.update(kw)
    return argparse.Namespace(**a)


PLAN = {"model": "flash-next", "runtime": "mlx-serve",
        "display_name": "Qwen 3.8 Flash-Next", "start_timeout_s": 600}


def run_exit(fn, *a, **kw):
    """Run fn, return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = fn(*a, **kw)
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


class FailStartTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.err = os.path.join(self.tmp, "stderr.log")
        with open(self.err, "w") as f:
            f.write("old run: Insufficient memory to load model: needs ~77.1 GB free "
                    "(70.1 GB of weights) but only 50.0 GB is available.\n")
        self.off = os.path.getsize(self.err)
        with open(self.err, "a") as f:
            f.write(INCIDENT)
        p = mock.patch.object(qwenctl, "_do_stop", return_value=True)
        self.do_stop = p.start()
        self.addCleanup(p.stop)

    def fail(self, failure, verb="start", **kw):
        lease = qwenctl.default_lease(self.cfg)
        lease.update(state="STARTING", owner="job-1" if verb == "acquire" else None)
        return run_exit(qwenctl._fail_start, self.cfg, start_args(), lease, PLAN,
                        failure, 600, self.err, self.off, verb, **kw)

    def test_engine_exit_gives_exit_8_with_the_reason_since_kickstart(self):
        rc, out, err = self.fail({"kind": "exited", "last_exit": "1"})
        self.assertEqual(rc, 8)
        res = json.loads(out)
        self.assertEqual(res["error"], "exited")
        self.assertEqual(res["reason"]["kind"], "insufficient_memory")
        self.assertEqual(res["reason"]["available_gib"], 69.0)   # not the old 50.0
        self.assertEqual(res["display_name"], "Qwen 3.8 Flash-Next")
        self.assertEqual(res["engine_exit_code"], "1")
        self.assertTrue(res["worker_stopped"])
        self.assertIn("not enough free memory", err)
        lease = qwenctl.read_lease(self.cfg)
        self.assertEqual(lease["state"], "STOPPED")
        self.assertIn("start exited", lease["notes"][0])
        self.assertIn("start.exited", self.controller_events())
        self.do_stop.assert_called_once()

    def test_exit_without_any_log_says_so(self):
        rc, out, err = run_exit(
            qwenctl._fail_start, self.cfg, start_args(),
            qwenctl.default_lease(self.cfg), PLAN, {"kind": "exited"}, 600,
            self.err, os.path.getsize(self.err), "start")
        self.assertEqual(rc, 8)
        self.assertIn("its log gave no reason", err)

    def test_cancel_gives_exit_9_and_reads_no_log(self):
        with mock.patch.object(qwenctl, "read_log_since") as rd:
            rc, out, err = self.fail({"kind": "cancelled"})
        self.assertEqual(rc, 9)
        rd.assert_not_called()
        self.assertEqual(json.loads(out)["error"], "cancelled")
        self.assertEqual(qwenctl.read_lease(self.cfg)["state"], "STOPPED")

    def test_timeout_keeps_exit_4_and_its_wording(self):
        rc, out, err = self.fail({"kind": "timeout"})
        self.assertEqual(rc, 4)
        self.assertIn("qwen-a did not become healthy within 600s; worker stopped", err)

    def test_acquire_failure_clears_the_lease_owner(self):
        rc, out, err = self.fail({"kind": "exited"}, verb="acquire",
                                 timeout_message="x")
        self.assertEqual(rc, 8)
        self.assertIsNone(qwenctl.read_lease(self.cfg)["owner"])
        self.assertIn("acquire.exited", self.controller_events())

    def test_unconfirmed_stop_is_never_reported_as_stopped(self):
        self.do_stop.return_value = False
        rc, out, err = self.fail({"kind": "exited"})
        self.assertEqual(qwenctl.read_lease(self.cfg)["state"], "STOPPING")
        self.assertIn("could not be confirmed stopped", err)
        self.assertFalse(json.loads(out)["worker_stopped"])


class StartPathTests(TmpCase):
    """_start_locked's cold path with every external effect faked."""

    def setUp(self):
        super().setUp()
        self.err = os.path.join(self.tmp, "stderr.log")
        with open(self.err, "w") as f:
            f.write("previous engine output\n")
        self.kicks = 0
        obs = {"observed_state": "DOWN", "pid": None, "processes": [],
               "peer_processes": [], "duplicate_processes": False,
               "launchd": ld("not running", None, 5, "1", self.err),
               "socket": {"listening": False, "pid": None, "name": None},
               "health": {"ok": False, "status": None, "props": None},
               "rss_gib": None}
        self.ld_calls = 0

        def fake_launchd(cfg):
            self.ld_calls += 1
            return ld("not running", None, 5 + self.kicks, "1", self.err)

        def fake_kick(cfg):
            self.kicks += 1
            with open(self.err, "a") as f:     # what mlx-serve printed
                f.write(INCIDENT)
            return 0, ""

        patches = [
            mock.patch.object(qwenctl, "reconcile",
                              lambda cfg, **kw: (qwenctl.read_lease(cfg), obs, [])),
            mock.patch.object(qwenctl, "_resolve_launch", lambda cfg: (PLAN, {})),
            mock.patch.object(qwenctl, "obs_socket",
                              lambda cfg: {"listening": False, "pid": None,
                                           "name": None}),
            mock.patch.object(qwenctl, "obs_processes", lambda cfg: []),
            mock.patch.object(qwenctl, "obs_health",
                              lambda cfg, timeout=5: {"ok": False, "status": None,
                                                      "props": None}),
            mock.patch.object(qwenctl, "obs_launchd", fake_launchd),
            mock.patch.object(qwenctl, "_kickstart", fake_kick),
            mock.patch.object(qwenctl, "_do_stop", return_value=True),
            # These tests are about start supervision. The shared resource
            # policy (docs/18) observes the real machine, so it is admitted
            # here; tests/test_resource_policy.py covers the policy itself.
            mock.patch.object(qwenctl, "_admission",
                              lambda cfg, args, plan, verb: contextlib.nullcontext()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_refused_load_fails_in_about_a_second_with_exit_8(self):
        t0 = self.clock.now()
        rc, out, err = run_exit(qwenctl._start_locked, self.cfg, start_args(), t0)
        self.assertEqual(rc, 8)
        self.assertEqual(self.kicks, 1)
        self.assertLess(self.clock.now() - t0, 3)          # was 600 s
        res = json.loads(out)
        self.assertEqual(res["reason"]["required_gib"], 77.1)
        self.assertEqual(res["log"], self.err)
        lease = qwenctl.read_lease(self.cfg)
        self.assertEqual((lease["state"], lease["generation"]), ("STOPPED", 1))
        self.assertEqual(self.controller_events()[-2:], ["start.begin", "start.exited"])

    def test_a_stop_requested_before_the_spawn_wins_and_nothing_is_spawned(self):
        t0 = self.clock.now()
        self.clock.t += 0.5
        qwenctl.request_cancel(self.cfg, by="panel")
        rc, out, err = run_exit(qwenctl._start_locked, self.cfg, start_args(), t0)
        self.assertEqual(rc, 9)
        self.assertEqual(self.kicks, 0)
        self.assertEqual(json.loads(out)["error"], "cancelled")
        self.assertEqual(qwenctl.read_lease(self.cfg)["state"], "STOPPED")

    def test_a_stale_marker_does_not_block_a_later_start(self):
        qwenctl.request_cancel(self.cfg, by="an earlier terminate")
        self.clock.t += 30
        rc, out, err = run_exit(qwenctl._start_locked, self.cfg, start_args(),
                                self.clock.now())
        self.assertEqual(rc, 8)               # it ran (and the fake engine refused)
        self.assertEqual(self.kicks, 1)
        self.assertFalse(os.path.exists(qwenctl.cancel_path(self.cfg)))


# -------------------------------------------- 4 status never clobbers a start
class ReconcileCasTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.lease = qwenctl.default_lease(self.cfg)
        self.lease.update(state="STOPPED", generation=41)
        qwenctl.write_lease(self.cfg, self.lease)

    def observation(self, state="DOWN"):
        return {"observed_state": state, "pid": 77 if state != "DOWN" else None,
                "processes": [], "peer_processes": [],
                "duplicate_processes": False, "launchd_owned": False,
                "launchd": ld(), "socket": {"listening": state != "DOWN"},
                "health": {"ok": state == "UP"}, "rss_gib": None}

    def concurrent_start(self, state="DOWN"):
        """observe() that lets a lock holder write STARTING/42 meanwhile —
        exactly the 2026-10-12 race window."""
        def obs(cfg, health_timeout=5):
            started = qwenctl.default_lease(cfg)
            started.update(state="STARTING", generation=42)
            qwenctl.write_lease(cfg, started)
            return self.observation(state)
        return obs

    def test_unchanged_observation_never_writes(self):
        before = pathlib.Path(self.cfg["lease"]).read_bytes()
        with mock.patch.object(qwenctl, "observe",
                               lambda cfg, health_timeout=5: self.observation()):
            qwenctl.reconcile(self.cfg, locked=False)
        self.assertEqual(pathlib.Path(self.cfg["lease"]).read_bytes(), before)

    def test_the_incident_race_no_longer_overwrites_starting(self):
        with mock.patch.object(qwenctl, "observe", self.concurrent_start()):
            qwenctl.reconcile(self.cfg, locked=False)
        lease = qwenctl.read_lease(self.cfg)
        self.assertEqual((lease["state"], lease["generation"]), ("STARTING", 42))

    def test_a_real_correction_still_loses_to_a_concurrent_writer(self):
        self.lease.update(state="READY", pid=77)
        qwenctl.write_lease(self.cfg, self.lease)
        with mock.patch.object(qwenctl, "observe", self.concurrent_start()):
            qwenctl.reconcile(self.cfg, locked=False)
        lease = qwenctl.read_lease(self.cfg)
        self.assertEqual((lease["state"], lease["generation"]), ("STARTING", 42))
        self.assertIn("reconcile.persist_skipped", self.controller_events())

    def test_a_real_correction_is_persisted_when_uncontended(self):
        self.lease.update(state="READY", pid=77)
        qwenctl.write_lease(self.cfg, self.lease)
        with mock.patch.object(qwenctl, "observe",
                               lambda cfg, health_timeout=5: self.observation()):
            lease, _, corr = qwenctl.reconcile(self.cfg, locked=False)
        self.assertEqual(corr[0]["reason"], "worker_vanished")
        self.assertEqual(qwenctl.read_lease(self.cfg)["state"], "STOPPED")

    def test_no_write_while_a_mutation_holds_the_lock(self):
        self.lease.update(state="READY", pid=77)
        qwenctl.write_lease(self.cfg, self.lease)
        with open(self.cfg["lock"], "a+") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(qwenctl, "observe",
                                   lambda cfg, health_timeout=5: self.observation()):
                qwenctl.reconcile(self.cfg, locked=False)
            fcntl.flock(holder, fcntl.LOCK_UN)
        self.assertEqual(qwenctl.read_lease(self.cfg)["state"], "READY")

    def test_an_observer_never_kills_an_orphan(self):
        with mock.patch.object(qwenctl, "observe",
                               lambda cfg, health_timeout=5: self.observation("UP")), \
                mock.patch.object(qwenctl, "_do_stop") as stop:
            _, _, corr = qwenctl.reconcile(self.cfg, orphan_policy="terminate",
                                           locked=False)
        stop.assert_not_called()
        self.assertEqual(corr[0]["action"], "orphan_reported_not_adopted")

    def test_unlocked_reconcile_must_read_the_lease_itself(self):
        with self.assertRaises(ValueError):
            qwenctl.reconcile(self.cfg, lease=self.lease, locked=False)

    def test_status_verb_is_an_unlocked_observer(self):
        seen = {}

        def fake(cfg, **kw):
            seen.update(kw)
            raise SystemExit(0)
        with mock.patch.object(qwenctl, "reconcile", fake):
            run_exit(qwenctl.cmd_status, self.cfg, start_args())
        self.assertIs(seen.get("locked"), False)


class TerminateCancelTests(TmpCase):
    def lock_spy(self):
        seen = {}

        @contextlib.contextmanager
        def spy(cfg, timeout=300):
            seen["marker_at_lock"] = os.path.exists(qwenctl.cancel_path(cfg))
            yield
        return seen, spy

    def stopped(self, cfg, **kw):
        return qwenctl.read_lease(cfg), {"observed_state": "DOWN"}, []

    def test_terminate_drops_the_marker_before_waiting_for_the_lock(self):
        seen, spy = self.lock_spy()
        with mock.patch.object(qwenctl, "Lock", spy), \
                mock.patch.object(qwenctl, "reconcile", self.stopped):
            rc, out, _ = run_exit(qwenctl.cmd_terminate, self.cfg,
                                  start_args(force=True, by="panel"))
        self.assertEqual(rc, 0)
        self.assertTrue(seen["marker_at_lock"])
        self.assertEqual(qwenctl.read_cancel(self.cfg)["by"], "panel")
        self.assertIn("already STOPPED", out)

    def test_an_owned_lease_is_not_cancelled_without_force(self):
        lease = qwenctl.default_lease(self.cfg)
        lease.update(state="BUSY", owner="bench")
        qwenctl.write_lease(self.cfg, lease)
        seen, spy = self.lock_spy()
        with mock.patch.object(qwenctl, "Lock", spy), \
                mock.patch.object(qwenctl, "reconcile",
                                  lambda cfg, **kw: (qwenctl.read_lease(cfg),
                                                     {"observed_state": "UP"}, [])):
            rc, _, err = run_exit(qwenctl.cmd_terminate, self.cfg, start_args())
        self.assertEqual(rc, 3)
        self.assertFalse(seen["marker_at_lock"])

    def test_cancel_verb(self):
        lease = qwenctl.default_lease(self.cfg)
        lease.update(state="STARTING", owner="bench")
        qwenctl.write_lease(self.cfg, lease)
        rc, _, err = run_exit(qwenctl.cmd_cancel, self.cfg, start_args())
        self.assertEqual(rc, 3)
        self.assertIsNone(qwenctl.read_cancel(self.cfg))
        rc, out, _ = run_exit(qwenctl.cmd_cancel, self.cfg,
                              start_args(force=True, by="cli"))
        self.assertEqual(rc, 0)
        res = json.loads(out)
        self.assertTrue(res["start_in_progress"])
        self.assertEqual(res["by"], "cli")
        self.assertEqual(qwenctl.read_cancel(self.cfg)["by"], "cli")


# ------------------------------------------------------------ 5 memory fit
class EngineFormulaTests(unittest.TestCase):
    """The vectors are mlx-serve v26.9.1's own (src/status.zig, src/scheduler.zig)."""
    PAGE = 16384
    PPG = GIB // PAGE

    def avail(self, total_gib, wired, comp, internal, purgeable):
        c = {"page_size": self.PAGE, "wired": wired * self.PPG,
             "compressor": comp * self.PPG, "internal": internal * self.PPG,
             "purgeable": purgeable * self.PPG}
        return nm.engine_available_bytes(total_gib * GIB, c)

    def test_compute_available_bytes_vectors(self):
        self.assertEqual(self.avail(16, 3, 1, 2, 0), 10 * GIB)
        self.assertEqual(self.avail(16, 3, 1, 9, 0), 3 * GIB)
        self.assertEqual(self.avail(16, 3, 1, 9, 2), 5 * GIB)
        self.assertEqual(self.avail(16, 3, 1, 1, 5), 12 * GIB)
        self.assertEqual(nm.engine_available_bytes(
            0, {"page_size": self.PAGE, "wired": 1, "compressor": 1,
                "internal": 1, "purgeable": 0}), 0)
        self.assertEqual(self.avail(8, 4, 0, 5, 0), 0)

    def test_load_requirement_vectors(self):
        req = nm.mlx_load_requirement_bytes
        peak = 42 * GIB
        insufficient = lambda w, a: a < req(w)          # memInsufficientForLoad
        self.assertFalse(insufficient(peak, req(peak)))
        self.assertTrue(insufficient(peak, req(peak) - 1))
        self.assertFalse(insufficient(42 * GIB, 86 * GIB))
        self.assertEqual(req(8 * GIB), 8 * GIB + GIB + GIB)       # w/8 under cap
        self.assertEqual(req(80 * GIB), 80 * GIB + 6 * GIB + GIB)  # 6 GiB cap

    def test_vm_stat_parsing(self):
        text = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
                "Pages free:                                5000.\n"
                "Pages wired down:                         249600.\n"
                "Pages purgeable:                           23500.\n"
                "Anonymous pages:                         2931000.\n"
                "Pages occupied by compressor:             111400.\n")
        c = nm.vm_counts(text)
        self.assertEqual(c, {"page_size": 16384, "wired": 249600,
                             "compressor": 111400, "internal": 2931000,
                             "purgeable": 23500})
        self.assertIsNone(nm.vm_counts("Pages free: 5.\n"))


class MemoryFitTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        base = pathlib.Path(self._td.name) / "pack"
        base.mkdir()
        (base / "model-00001.safetensors").write_bytes(b"x" * 3000)
        (base / "model-00002.safetensors").write_bytes(b"x" * 1000)
        (base / "stray.safetensors").write_bytes(b"x" * 99999)      # not indexed
        (base / "model.safetensors.index.json").write_text(json.dumps(
            {"weight_map": {"a": "model-00001.safetensors",
                            "b": "model-00002.safetensors"}}))
        self.reg = {"models": {
            "mlx": {"path": str(base), "memory": {"admission": {
                "rule": "mlx-serve-preflight"}}},
            "est": {"path": str(base), "memory": {"admission": {
                "rule": "fixed", "required_gib": 52}}},
            "none": {"path": str(base)}}}
        self.page = 16384

    def tearDown(self):
        self._td.cleanup()

    def counts_for(self, avail_bytes, total):
        used_pages = (total - avail_bytes) // self.page
        return {"page_size": self.page, "wired": used_pages, "compressor": 0,
                "internal": 0, "purgeable": 0}

    def test_weights_are_the_indexed_shards_only(self):
        self.assertEqual(nm.mlx_weights_bytes(self.reg, "mlx"), 4000)

    def test_verdict_bands(self):
        total = 128 * GIB
        cases = [(60 * GIB, "fits"), (53 * GIB, "tight"), (51 * GIB, "wont_fit")]
        for avail, verdict in cases:
            fit = nm.memory_fit(self.reg, "est", total_bytes=total,
                                counts=self.counts_for(avail, total))
            self.assertEqual(fit["verdict"], verdict, (avail / GIB, fit))
            self.assertFalse(fit["authoritative"])
            self.assertTrue(fit["summary"].endswith("(estimate)"))
        fit = nm.memory_fit(self.reg, "mlx", total_bytes=total,
                            counts=self.counts_for(4 * GIB, total))
        self.assertEqual(fit["verdict"], "fits")       # needs 4000 B + 1 GiB baseline
        fit = nm.memory_fit(self.reg, "mlx", total_bytes=total,
                            counts=self.counts_for(2 * GIB, total))
        self.assertEqual(fit["verdict"], "tight")      # under the 2 GiB margin
        self.assertTrue(fit["authoritative"])
        self.assertEqual(nm.memory_fit(self.reg, "none", total_bytes=total,
                                       counts=self.counts_for(1, total))["verdict"],
                         "unknown")
        self.assertEqual(nm.memory_fit(self.reg, "est", total_bytes=None,
                                       counts=None).get("rule"), "fixed")

    def test_registry_validates_admission_rules(self):
        reg = nm.load_registry()
        self.assertEqual(reg["models"]["flash-next"]["memory"]["admission"]["rule"],
                         "mlx-serve-preflight")
        self.assertEqual(reg["models"]["qwen27b"]["memory"]["admission"]["rule"],
                         "fixed")
        bad = json.loads(json.dumps(reg))
        bad["models"]["qwen27b"]["memory"]["admission"] = {"rule": "guess"}
        with self.assertRaises(nm.RegistryError):
            nm.validate_registry(bad)
        bad["models"]["qwen27b"]["memory"]["admission"] = {"rule": "fixed"}
        with self.assertRaises(nm.RegistryError):
            nm.validate_registry(bad)

    @unittest.skipUnless((ROOT / "models/qwen38-flash-next-mlx-4bit/"
                          "model.safetensors.index.json").exists(),
                         "Flash-Next pack not installed")
    def test_real_pack_matches_the_figures_the_engine_printed(self):
        reg = nm.load_registry()
        w = nm.mlx_weights_bytes(reg, "flash-next")
        self.assertEqual(f"{w / GIB:.2f}", "70.13")            # "[preflight] weights ~70.13 GB"
        self.assertEqual(f"{nm.mlx_load_requirement_bytes(w) / GIB:.1f}", "77.1")  # "needs ~77.1 GB"


# ------------------------------------------------------------- panel side
class PanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import newton_control
        cls.nc = newton_control

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        nc = self.nc
        for p in (mock.patch.object(nc, "DESIRED_PATH",
                                    os.path.join(self._td.name, "desired.json")),
                  mock.patch.object(nc, "LOGDIR", os.path.join(self._td.name, "logs")),
                  mock.patch.object(nc, "OPS", nc.Ops())):
            p.start()
            self.addCleanup(p.stop)
        self.calls = []

    def tearDown(self):
        self._td.cleanup()

    def stub(self, results):
        def fake(verb, *extra, timeout=30):
            self.calls.append((verb,) + extra)
            r = results.get(verb, (0, "{}", ""))
            return r() if callable(r) else r
        return mock.patch.object(self.nc, "qwenctl", fake)

    def oom_json(self):
        return json.dumps({"ok": False, "error": "exited", "exit_code": 8,
                           "display_name": "Qwen 3.8 Flash-Next",
                           "model_id": "flash-next", "worker_stopped": True,
                           "log": "/x/logs/qwen-a/stderr.log",
                           "reason": qwenctl.diagnose_start_failure(INCIDENT)})

    def test_ui_state_while_cancelling(self):
        m = self.nc.map_ui_state
        self.assertEqual(m("STARTING", False, "RUNNING", "start", False), "Starting")
        self.assertEqual(m("STARTING", False, "STOPPED", "start", False,
                           cancelling=True), "Stopping")
        self.assertEqual(m("STOPPING", False, "STOPPED", "restart", False,
                           cancelling=True), "Stopping")

    def test_failure_sentences(self):
        d = self.nc.describe_start_failure
        msg, extra = d(json.loads(self.oom_json()), "fallback")
        self.assertEqual(msg, "Not enough free memory to load Qwen 3.8 Flash-Next: "
                              "it needs 77.1 GiB free and only 69.0 GiB was "
                              "available. Close other memory-heavy apps and try "
                              "again, or choose a smaller model.")
        self.assertEqual((extra["error"], extra["exit_code"]), ("exited", 8))
        msg, _ = d({"ok": False, "error": "exited", "display_name": "M",
                    "reason": {"kind": "engine_error", "summary": "boom"}}, "f")
        self.assertEqual(msg, "M failed to start: boom")
        msg, extra = d({"ok": False, "error": "cancelled"}, "f")
        self.assertEqual(msg, "Start cancelled; the model is stopped.")
        self.assertTrue(extra["cancelled"])
        msg, _ = d({"ok": False, "error": "timeout", "display_name": "M"}, "f")
        self.assertEqual(msg, "M did not become ready in time and was stopped.")
        self.assertEqual(d(None, "plain stderr"), ("plain stderr", {}))

    def test_cancel_only_applies_to_starts(self):
        ops = self.nc.OPS
        self.assertIsNone(ops.request_cancel("panel"))
        ops.begin("stop")
        self.assertIsNone(ops.request_cancel("panel"))
        ops.finish("stop", True, "ok")
        ops.begin("restart")
        self.assertEqual(ops.request_cancel("panel"), "restart")
        self.assertTrue(ops.current["cancelling"])
        ops.finish("restart", True, "done")
        self.assertFalse(ops.cancel_requested())

    def test_refused_start_shows_the_engines_reason(self):
        nc = self.nc
        nc.OPS.begin("start")
        with self.stub({"start": (8, self.oom_json(), "qwenctl: ...")}):
            nc.op_start()
        last = nc.OPS.last
        self.assertFalse(last["ok"])
        self.assertTrue(last["message"].startswith(
            "Not enough free memory to load Qwen 3.8 Flash-Next"))
        self.assertEqual(last["log"], "/x/logs/qwen-a/stderr.log")
        self.assertEqual(nc.read_desired_record()["desired"], "STOPPED")
        self.assertEqual(nc.read_desired_record()["set_by"], "panel:start-failed")

    def test_stop_during_start_cancels_and_confirms(self):
        nc = self.nc
        nc.OPS.begin("start")
        nc.OPS.request_cancel("panel")
        cancelled = json.dumps({"ok": False, "error": "cancelled", "exit_code": 9})
        with self.stub({"start": (9, cancelled, ""),
                        "terminate": (0, '{"ok": true}', "")}):
            nc.op_start()
        self.assertEqual(self.calls[-1], ("terminate", "--force", "--by",
                                          "panel:cancel"))
        last = nc.OPS.last
        self.assertTrue(last["ok"])
        self.assertTrue(last["cancelled"])
        self.assertEqual(last["message"], "Start cancelled; the model is stopped.")
        self.assertIsNone(nc.OPS.current)

    def test_a_start_that_won_the_race_is_still_stopped(self):
        nc = self.nc
        nc.OPS.begin("start")
        nc.OPS.request_cancel("panel")
        with self.stub({"start": (0, '{"ok": true, "model_id": "qwen27b"}', ""),
                        "terminate": (0, '{"ok": true}', "")}):
            nc.op_start()
        self.assertEqual(self.calls[-1][0], "terminate")
        self.assertTrue(nc.OPS.last["cancelled"])

    def test_http_stop_while_starting_returns_202_cancel(self):
        nc = self.nc
        srv = ThreadingHTTPServer(("127.0.0.1", 0), nc.Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        nc.OPS.begin("start")
        with mock.patch.object(nc, "SERVER_PORT", port), \
                self.stub({"cancel": (0, '{"ok": true}', "")}):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/model/stop", method="POST",
                headers={"X-Newton-Control": "1"}, data=b"")
            with urllib.request.urlopen(req, timeout=10) as r:
                body = json.load(r)
                code = r.status
        self.assertEqual(code, 202)
        self.assertEqual((body["operation"], body["cancelling"]), ("cancel", "start"))
        self.assertIn(("cancel", "--by", "panel"), self.calls)
        self.assertTrue(nc.OPS.current["cancelling"])
        rec = nc.read_desired_record()
        self.assertEqual((rec["desired"], rec["set_by"]), ("STOPPED", "panel:cancel"))

    def test_memory_fit_is_only_attached_while_everything_is_stopped(self):
        nc = self.nc
        models = {"qwen27b": {}, "flash-next": {}}
        counts = {"page_size": 16384, "wired": 0, "compressor": 0, "internal": 0,
                  "purgeable": 0}
        nc.attach_memory_fit(models, ["AI Inference is Running"], counts, 128 * GIB)
        self.assertIsNone(models["qwen27b"]["memory_fit"])
        nc.attach_memory_fit(models, [], counts, 128 * GIB)
        self.assertEqual(models["qwen27b"]["memory_fit"]["verdict"], "fits")


if __name__ == "__main__":
    unittest.main()
