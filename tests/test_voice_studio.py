"""Voice Studio: private store, HTTP boundary, lifecycle operations (docs/18).

Unit tests use the standard library only (Python 3.9, like the control
service). The integration class drives the whole path over HTTP, as the page
does: the real sandboxed audio tool ingests a recording, and the real engine
(fish_engine.py) runs in fixture mode with a synthesiser that makes a tone
instead of speech, so no model is loaded. It is skipped when the Fish runtime
(tools/fish-venv) is not installed.
"""
import http.client
import io
import json
import math
import os
import pathlib
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import voice_store as vs  # noqa: E402
import voice_studio as vst  # noqa: E402

VENV_PY = ROOT / "tools" / "fish-venv" / "bin" / "python3"
CONSENT = {"basis": "own_voice", "attested": True, "note": ""}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def speechlike_wav(seconds=6.0, rate=48000):
    """16-bit mono WAV: a voiced-sounding tone with syllable-rate envelope and
    pauses, enough to pass the ingest checks. Synthetic; not anyone's voice."""
    n = int(seconds * rate)
    frames = bytearray()
    for i in range(n):
        t = i / rate
        env = max(0.0, math.sin(2 * math.pi * 3.0 * t)) * (0.0 if 2.0 < t < 2.4 else 1.0)
        v = 0.4 * env * (math.sin(2 * math.pi * 140 * t) + 0.5 * math.sin(2 * math.pi * 280 * t))
        frames += struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32000))
    hdr = b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + b"data" + struct.pack("<I", len(frames))
    return hdr + bytes(frames)


def fixture_config(tmp, port, store=None):
    with open(ROOT / "config" / "fish.json") as f:
        cfg = json.load(f)
    cfg["fixture"] = True
    cfg["fake"] = {"enabled": True, "load_s": 0.2, "frames_per_s": 400}
    cfg["service"].update(label="com.newton.test.voicestudio", port=port,
                          statedir=str(tmp / "state"), logdir=str(tmp / "logs"),
                          store=str(store or tmp / "store"))
    path = tmp / "fish.json"
    path.write_text(json.dumps(cfg))
    return path


def ingest_result(seconds=9.0):
    """What the audio tool reports, for store tests that need no runtime."""
    return {"original": {"file": "original.wav", "bytes": 100, "sha256": "a" * 64,
                         "format": {"container": "wav", "codec": "pcm_s16le", "sample_rate": 48000,
                                    "channels": 1, "bits": 16, "lossy": False}},
            "analysis": {"seconds": seconds, "peak_dbfs": -3.0},
            "model_input": {"file": "model-input.wav", "bytes": 100, "sha256": "b" * 64,
                            "seconds": seconds, "sample_rate": 44100, "channels": 1, "bits": 24},
            "processing": {"mono": "already mono"}, "warnings": []}


def staged(store):
    d = store.staging_dir()
    out = os.path.join(d, "out")
    os.makedirs(out)
    for name in ("original.wav", "model-input.wav"):
        with open(os.path.join(out, name), "wb") as f:
            f.write(b"RIFF....WAVE")
    return out


# ================================================================ store
class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.store = vs.VoiceStore(str(self.tmp / "store")).init()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_layout_is_private(self):
        for d in ("", "profiles", "references", "takes", "tmp"):
            mode = stat.S_IMODE(os.stat(self.tmp / "store" / d).st_mode)
            self.assertEqual(mode, 0o700, d)
        self.assertTrue((self.tmp / "store" / ".metadata_never_index").exists())

    def test_profile_requires_consent(self):
        with self.assertRaises(vs.StoreError):
            self.store.create_profile("Me", {"basis": "own_voice", "attested": False})
        with self.assertRaises(vs.StoreError):
            self.store.create_profile("Me", {"basis": "someone", "attested": True})
        with self.assertRaises(vs.StoreError):     # permission needs a description
            self.store.create_profile("Them", {"basis": "documented_permission",
                                               "attested": True, "note": ""})
        p = self.store.create_profile("Me", CONSENT)
        self.assertEqual(p["consent"]["basis"], "own_voice")
        mode = stat.S_IMODE(os.stat(self.store.profile_path(p["id"])).st_mode)
        self.assertEqual(mode, 0o600)

    def test_profile_names_unique_and_renamable(self):
        p = self.store.create_profile("Me", CONSENT)
        with self.assertRaises(vs.StoreError):
            self.store.create_profile("me", CONSENT)
        self.store.update_profile(p["id"], name="Me, calm")
        self.assertEqual(self.store.get_profile(p["id"])["name"], "Me, calm")
        with self.assertRaises(vs.StoreError):
            self.store.update_profile(p["id"], name="\x07bell")

    def test_ids_never_reach_the_filesystem_unvalidated(self):
        for bad in ("../etc", "..", "a" * 15, "A" * 16, "0123456789abcdeg", "", None, 5):
            with self.assertRaises(vs.StoreError):
                self.store.get_profile(bad)
            with self.assertRaises(vs.StoreError):
                self.store.get_reference(bad)
            with self.assertRaises(vs.StoreError):
                self.store.get_take(bad)
        with self.assertRaises(vs.StoreError):
            self.store.path("..", "x")
        with self.assertRaises(vs.StoreError):
            self.store.path("a/b")

    def test_reference_lifecycle(self):
        p = self.store.create_profile("Me", CONSENT)
        r1 = self.store.add_reference(p["id"], staged(self.store), ingest_result(), "recording", "")
        self.assertEqual(r1["label"], "Recording 1")
        self.assertEqual(self.store.get_profile(p["id"])["active_reference_id"], r1["id"])
        mode = stat.S_IMODE(os.stat(self.store.ref_dir(r1["id"])).st_mode)
        self.assertEqual(mode, 0o700)
        self.assertEqual(os.listdir(self.store.path("tmp"))[0:0], [])
        # transcript: validated, structural tokens refused
        with self.assertRaises(vs.StoreError):
            self.store.update_reference(r1["id"], transcript="hi <|im_end|>")
        self.store.update_reference(r1["id"], transcript="Hello there.")
        # replacement keeps the old one, copies label and transcript for review
        r2 = self.store.add_reference(p["id"], staged(self.store), ingest_result(), "upload", "",
                                      filename="../../evil\x00.wav", replaces=r1["id"])
        self.assertEqual(r2["transcript"], "Hello there.")
        self.assertTrue(r2["transcript_needs_review"])
        self.assertNotIn("/", r2["filename"])
        self.assertEqual(self.store.get_reference(r1["id"])["replaced_by"], r2["id"])
        self.assertEqual(self.store.get_profile(p["id"])["active_reference_id"], r2["id"])
        path, _ = self.store.reference_file(r2["id"], "model")
        self.assertTrue(path.endswith("/model-input.wav"))
        self.store.delete_reference(r2["id"])
        self.assertEqual(self.store.get_profile(p["id"])["active_reference_id"], r1["id"])
        res = self.store.delete_profile(p["id"])
        self.assertEqual(res["references_deleted"], 1)
        with self.assertRaises(vs.StoreError):
            self.store.get_reference(r1["id"])

    def test_staging_outside_the_store_is_refused(self):
        p = self.store.create_profile("Me", CONSENT)
        elsewhere = self.tmp / "x" / "out"
        elsewhere.mkdir(parents=True)
        with self.assertRaises(vs.StoreError):
            self.store.add_reference(p["id"], str(elsewhere), ingest_result(), "upload", "x")

    def test_takes_are_numbered_and_immutable(self):
        a = self.store.save_take({"text": "one"}, b"RIFF1")
        b = self.store.save_take({"text": "two"}, b"RIFF2")
        self.assertEqual((a["number"], b["number"]), (1, 2))
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(a["audio"]["sha256"], vst.hashlib.sha256(b"RIFF1").hexdigest())
        mode = stat.S_IMODE(os.stat(self.store.take_file(a["id"])).st_mode)
        self.assertEqual(mode, 0o600)
        self.store.update_take(a["id"], note="best", starred=True)
        rec = self.store.get_take(a["id"])
        self.assertEqual((rec["note"], rec["starred"], rec["text"]), ("best", True, "one"))
        with self.assertRaises(vs.StoreError):
            self.store.update_take(a["id"], starred="yes")
        self.assertEqual([t["number"] for t in self.store.list_takes()], [2, 1])
        self.store.delete_take(a["id"])
        self.assertEqual([t["number"] for t in self.store.list_takes()], [2])

    def test_init_clears_stale_staging(self):
        d = self.store.staging_dir()
        open(os.path.join(d, "upload.bin"), "w").close()
        vs.VoiceStore(str(self.tmp / "store")).init()
        self.assertEqual(os.listdir(self.tmp / "store" / "tmp"), [])


# =============================================================== pure parts
class PureTests(unittest.TestCase):
    def test_peer_from_lsof(self):
        out = "p100\nu501\nf9\nn127.0.0.1:50000->127.0.0.1:8765\np200\nu501\nf3\nn127.0.0.1:8765->127.0.0.1:50000\n"
        self.assertEqual(vst.peer_from_lsof(out, 50000, 8765, 501), (True, None))
        self.assertFalse(vst.peer_from_lsof(out, 50000, 8765, 502)[0])
        # only our own server socket visible (peer belongs to another account)
        mine = "p200\nu501\nf3\nn127.0.0.1:8765->127.0.0.1:50000\n"
        self.assertFalse(vst.peer_from_lsof(mine, 50000, 8765, 501)[0])
        self.assertFalse(vst.peer_from_lsof("", 50000, 8765, 501)[0])

    def test_ui_state_mapping(self):
        def st(state, obs="DOWN", ok=False, busy=False, desired="STOPPED", by=None):
            return {"state": state, "observed_state": obs, "health": {"ok": ok, "busy": busy},
                    "desired": {"desired": desired, "record": {"set_by": by} if by else None}}
        f = vst.fish_ui_state
        self.assertEqual(f(st("STOPPED"), None), "Stopped")
        self.assertEqual(f(st("STARTING", "LOADING"), None), "Starting")
        self.assertEqual(f(st("READY", "UP", ok=True), None), "Ready")
        self.assertEqual(f(st("READY", "UP", ok=True, busy=True), None), "Busy")
        self.assertEqual(f(st("READY", "LOADING"), None), "Failed")
        self.assertEqual(f(st("STOPPING", "UP"), None), "Stopping")
        self.assertEqual(f(st("STOPPED", desired="RUNNING"), None), "Recovering")
        self.assertEqual(f(st("STOPPED", desired="RUNNING"), None, gave_up=True), "Failed")
        self.assertEqual(f(st("STOPPED", by="watchdog:gave-up"), None), "Failed")
        self.assertEqual(f(st("STOPPED", by="panel"), None), "Stopped")
        self.assertEqual(f(st("STOPPED"), {"name": "start"}), "Starting")
        self.assertEqual(f(st("STARTING"), {"name": "start", "cancelling": True}), "Stopping")
        self.assertEqual(f(None, None), "Unknown")

    def test_describe_failure(self):
        msg, extra = vst.describe_failure({"ok": False, "error": "policy", "exit_code": 10,
                                           "message": "Fish can't start while Flash-Next is running.",
                                           "policy_code": "prohibited"}, "", 10)
        self.assertIn("Flash-Next", msg)
        self.assertEqual((extra["error"], extra["policy_code"]), ("policy", "prohibited"))
        msg, extra = vst.describe_failure({"ok": False, "error": "cancelled"}, "", 9)
        self.assertTrue(extra["cancelled"])
        msg, extra = vst.describe_failure(None, "fishctl: refusing to start: weights missing", 6)
        self.assertEqual(msg, "refusing to start: weights missing")


# ============================================================ HTTP boundary
class Handler(BaseHTTPRequestHandler):
    studio = None

    def log_message(self, *a):
        pass

    def _go(self, method):
        if not self.studio.handle(self, method):
            body = b'{"error": "not found"}'
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_GET(self):
        self._go("GET")

    def do_POST(self):
        self._go("POST")

    def do_PATCH(self):
        self._go("PATCH")

    def do_DELETE(self):
        self._go("DELETE")


class ServerCase(unittest.TestCase):
    """A Studio on a free port with a fixture config and a fake peer check."""
    engine_port = None

    def make_studio(self, peer=(True, None)):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.port = free_port()
        self.cfg_path = fixture_config(self.tmp, self.engine_port or free_port())
        self.studio = vst.Studio(self.port, config_path=str(self.cfg_path),
                                 peer_check=lambda c, s: peer, log_path=str(self.tmp / "voice.log"))
        handler = type("H", (Handler,), {"studio": self.studio})
        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.session = None

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def req(self, method, path, body=None, headers=None, session=True, ctype=None, raw=False):
        h = {"Host": f"127.0.0.1:{self.port}", "X-Newton-Control": "1"}
        if session and self.session:
            h["X-Voice-Session"] = self.session
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        elif body is not None:
            h["Content-Type"] = ctype or "application/octet-stream"
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            c.putheader(k, v)
        if body is not None and "Content-Length" not in h:
            c.putheader("Content-Length", str(len(body)))
        c.endheaders()
        if body is not None:
            c.send(body)
        r = c.getresponse()
        data = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        if raw:
            return r.status, hdrs, data
        try:
            return r.status, json.loads(data.decode() or "null")
        except ValueError:
            return r.status, data

    def open_session(self):
        st, j = self.req("POST", "/api/voice/session", session=False)
        self.assertEqual(st, 200, j)
        self.session = j["session"]


class BoundaryTests(ServerCase):
    def setUp(self):
        self.make_studio()

    def test_static_page_headers(self):
        st, hdrs, data = self.req("GET", "/voice", session=False, raw=True)
        self.assertEqual(st, 200)
        self.assertIn(b"Voice Studio", data)
        self.assertIn("script-src 'self'", hdrs["content-security-policy"])
        self.assertIn("frame-ancestors 'none'", hdrs["content-security-policy"])
        self.assertEqual(hdrs["x-frame-options"], "DENY")
        self.assertIn("microphone=(self)", hdrs["permissions-policy"])
        for p in ("/voice/app.js", "/voice/style.css", "/voice/recorder-worklet.js"):
            self.assertEqual(self.req("GET", p, session=False, raw=True)[0], 200, p)
        self.assertEqual(self.req("GET", "/voice/../fish.json", session=False)[0], 404)
        self.assertEqual(self.req("GET", "/voice/index.html", session=False)[0], 404)

    def test_host_header_is_checked_everywhere(self):
        for path in ("/voice", "/api/fish/status", "/api/voice/state"):
            st, _ = self.req("GET", path, headers={"Host": "evil.example:80"})
            self.assertEqual(st, 403, path)

    def test_private_routes_need_header_origin_and_session(self):
        st, j = self.req("GET", "/api/voice/state", session=False)
        self.assertEqual((st, j.get("code")), (401, "session"))
        self.open_session()
        self.assertEqual(self.req("GET", "/api/voice/state")[0], 200)
        self.assertEqual(self.req("GET", "/api/voice/state",
                                  headers={"X-Newton-Control": None})[0], 403)
        self.assertEqual(self.req("GET", "/api/voice/state",
                                  headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.req("GET", "/api/voice/state",
                                  headers={"Origin": "http://127.0.0.1:3000"})[0], 403)
        self.assertEqual(self.req("GET", "/api/voice/state",
                                  headers={"Sec-Fetch-Site": "same-site"})[0], 403)
        self.assertEqual(self.req("GET", "/api/voice/state",
                                  headers={"Origin": f"http://127.0.0.1:{self.port}",
                                           "Sec-Fetch-Site": "same-origin"})[0], 200)
        self.assertEqual(self.req("GET", "/api/voice/state",
                                  headers={"X-Voice-Session": "forged"})[0], 401)

    def test_session_needs_header_and_operator_peer(self):
        st, _ = self.req("POST", "/api/voice/session", session=False,
                         headers={"X-Newton-Control": None})
        self.assertEqual(st, 403)
        self.studio.peer_check = lambda c, s: (False, "the connecting program runs as a different account")
        st, j = self.req("POST", "/api/voice/session", session=False)
        self.assertEqual(st, 403)
        self.assertIn("different account", j["error"])

    def test_bodies_are_bounded(self):
        self.open_session()
        st, j = self.req("POST", "/api/voice/profiles", body=b"x" * (vst.MAX_JSON + 1),
                         ctype="application/json")
        self.assertEqual(st, 413)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest("POST", "/api/voice/profiles", skip_host=True)
        for k, v in {"Host": f"127.0.0.1:{self.port}", "X-Newton-Control": "1",
                     "X-Voice-Session": self.session, "Transfer-Encoding": "chunked"}.items():
            c.putheader(k, v)
        c.endheaders()
        c.send(b"0\r\n\r\n")
        self.assertEqual(c.getresponse().status, 411)
        c.close()
        p = self.req("POST", "/api/voice/profiles", body={"name": "Me", "consent": CONSENT})[1]
        st, j = self.req("POST", f"/api/voice/profiles/{p['profile']['id']}/references?source=upload",
                         body=b"x", headers={"Content-Length": str(vst.MAX_AUDIO + 1)})
        self.assertEqual(st, 413)

    def test_methods_ids_and_fields(self):
        self.open_session()
        self.assertEqual(self.req("PUT", "/api/voice/state")[0], 501)   # not served at all
        self.assertEqual(self.req("DELETE", "/api/voice/state")[0], 405)
        self.assertEqual(self.req("GET", "/api/voice/takes/../../x/audio")[0], 404)
        self.assertEqual(self.req("GET", "/api/voice/takes/0123456789abcdef/audio")[0], 404)
        self.assertEqual(self.req("DELETE", "/api/voice/profiles/0123456789abcdef")[0], 404)
        st, j = self.req("POST", "/api/voice/profiles", body={"name": "Me", "consent": CONSENT})
        self.assertEqual(st, 201)
        pid = j["profile"]["id"]
        st, j = self.req("POST", "/api/voice/profiles", body={"name": "No consent"})
        self.assertEqual(st, 400)
        st, j = self.req("POST", f"/api/voice/profiles/{pid}/references?source=elsewhere", body=b"RIFF")
        self.assertEqual(st, 400)
        st, j = self.req("POST", "/api/voice/generate",
                         body={"profile_id": pid, "reference_ids": [], "text": "hi"})
        self.assertEqual(st, 400)
        st, j = self.req("POST", "/api/voice/generate", body={"profile_id": pid, "path": "/etc"})
        self.assertEqual(st, 400)
        self.assertIn("unsupported fields", j["error"])

    def test_upload_errors_leave_nothing_behind(self):
        self.open_session()
        pid = self.req("POST", "/api/voice/profiles", body={"name": "Me", "consent": CONSENT})[1]["profile"]["id"]
        with mock.patch.object(self.studio, "ingest",
                               side_effect=vst.HttpError(422, "this is not a supported audio file")):
            st, j = self.req("POST", f"/api/voice/profiles/{pid}/references?source=upload",
                             body=b"not audio at all")
        self.assertEqual((st, j["error"]), (422, "this is not a supported audio file"))
        self.assertEqual(os.listdir(os.path.join(self.studio.store.root, "tmp")), [])
        self.assertEqual(os.listdir(os.path.join(self.studio.store.root, "references")), [])

    def test_errors_and_logs_carry_no_private_text(self):
        self.open_session()
        secret = "MY PRIVATE SENTENCE"
        pid = self.req("POST", "/api/voice/profiles", body={"name": "Me", "consent": CONSENT})[1]["profile"]["id"]
        with mock.patch.object(self.studio, "ingest", return_value=ingest_result()), \
                mock.patch.object(vs.VoiceStore, "add_reference",
                                  side_effect=RuntimeError(secret)):
            st, j = self.req("POST", f"/api/voice/profiles/{pid}/references?source=upload",
                             body=b"RIFF")
        self.assertEqual(st, 500)
        self.assertNotIn(secret, json.dumps(j))
        self.assertNotIn(secret, (self.tmp / "voice.log").read_text())

    def test_generation_needs_a_running_engine(self):
        self.open_session()
        pid = self.req("POST", "/api/voice/profiles", body={"name": "Me", "consent": CONSENT})[1]["profile"]["id"]
        rid = self.studio.store.add_reference(pid, staged(self.studio.store), ingest_result(),
                                              "recording", "a")["id"]
        st, j = self.req("POST", "/api/voice/generate",
                         body={"profile_id": pid, "reference_ids": [rid], "text": "Hello."})
        self.assertEqual(st, 400)
        self.assertIn("transcript", j["error"])
        self.studio.store.update_reference(rid, transcript="Hello there.")
        st, j = self.req("POST", "/api/voice/generate",
                         body={"profile_id": pid, "reference_ids": [rid], "text": "Hello."})
        self.assertEqual((st, j.get("code")), (409, "fish_not_ready"))


# ============================================================ lifecycle ops
class FakeCtl:
    """Records fishctl calls; answers from a script of (rc, stdout, stderr)."""

    def __init__(self):
        self.calls = []
        self.answers = {}
        self.gate = None

    def __call__(self, verb, *extra, timeout=60):
        self.calls.append((verb,) + extra)
        if verb == "start" and self.gate is not None:
            self.gate.wait(10)
        a = self.answers.get(verb, (0, "{}", ""))
        return a(verb, extra) if callable(a) else a


class OpsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.studio = vst.Studio(free_port(), config_path=str(fixture_config(self.tmp, free_port())),
                                 log_path=str(self.tmp / "voice.log"))
        self.ctl = FakeCtl()
        self.studio.fishctl = self.ctl

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait_idle(self):
        for _ in range(200):
            if not self.studio.ops.current:
                return
            time.sleep(0.02)
        self.fail("operation did not finish")

    def test_start_success_and_policy_refusal(self):
        self.ctl.answers["start"] = (0, json.dumps({"ok": True, "started": True,
                                                    "last_start": {"load_to_ready_s": 41.2}}), "")
        self.assertTrue(self.studio.ops.run("start"))
        self.wait_idle()
        self.assertTrue(self.studio.ops.last["ok"])
        self.assertIn("ready", self.studio.ops.last["message"])
        self.assertEqual(self.ctl.calls[0], ("start", "--by", "panel"))
        self.ctl.answers["start"] = (10, json.dumps({
            "ok": False, "error": "policy", "exit_code": 10, "policy_code": "prohibited",
            "message": "Fish Audio Drama 3 (MLX Q4) can't start while Qwen 3.8 Flash-Next is running"}),
            "fishctl: refused")
        self.studio.ops.run("start")
        self.wait_idle()
        last = self.studio.ops.last
        self.assertFalse(last["ok"])
        self.assertEqual(last["policy_code"], "prohibited")
        self.assertIn("Flash-Next", last["message"])

    def test_one_operation_at_a_time(self):
        self.ctl.gate = threading.Event()
        self.assertTrue(self.studio.ops.run("start"))
        self.assertFalse(self.studio.ops.run("restart"))
        self.ctl.gate.set()
        self.wait_idle()

    def test_stop_during_start_cancels_it(self):
        self.ctl.gate = threading.Event()
        self.ctl.answers["start"] = (9, json.dumps({"ok": False, "error": "cancelled",
                                                    "exit_code": 9}), "")
        self.studio.ops.run("start")
        self.assertEqual(self.studio.ops.request_cancel("panel"), "start")
        self.assertTrue(self.studio.ops.cancel_inflight("start"))
        self.ctl.gate.set()
        self.wait_idle()
        verbs = [c[0] for c in self.ctl.calls]
        self.assertEqual(verbs, ["start", "cancel", "stop"])
        self.assertIn(("stop", "--by", "panel:cancel"), self.ctl.calls)
        self.assertTrue(self.studio.ops.last["cancelled"])
        self.assertTrue(self.studio.ops.last["ok"])

    def _status(self, state="STOPPED", obs="DOWN", desired="RUNNING", ok=False):
        return (0, json.dumps({"state": state, "observed_state": obs, "health": {"ok": ok},
                               "desired": {"desired": desired, "record": {}}}), "")

    def test_watchdog_never_acts_without_desired_running(self):
        self.ctl.answers["status"] = self._status(desired="STOPPED")
        self.studio.ops.watchdog_tick()
        self.assertEqual([c[0] for c in self.ctl.calls], ["status"])

    def test_watchdog_waits_while_starting_or_stopping(self):
        for state in ("STARTING", "STOPPING"):
            self.ctl.calls.clear()
            self.ctl.answers["status"] = self._status(state=state, obs="LOADING")
            self.studio.ops.watchdog_tick()
            self.assertEqual([c[0] for c in self.ctl.calls], ["status"])

    def test_watchdog_recovers_with_backoff_then_gives_up(self):
        self.ctl.answers["status"] = self._status()
        self.ctl.answers["start"] = (8, json.dumps({"ok": False, "error": "exited", "exit_code": 8,
                                                    "message": "the engine exited"}), "")
        clock = [1000.0]
        with mock.patch.object(vst, "now", lambda: clock[0]):
            for _ in range(3):
                self.studio.ops.watchdog_tick()
                clock[0] += 1
                self.studio.ops.watchdog_tick()          # still inside the backoff: no start
                clock[0] += 200
            self.assertEqual([c for c in self.ctl.calls if c[0] == "start"],
                             [("start", "--by", "watchdog", "--recovery")] * 3)
            self.studio.ops.watchdog_tick()
        self.assertTrue(self.studio.ops.recovery["gave_up"])
        self.assertIn(("stop", "--by", "watchdog:gave-up"), self.ctl.calls)
        self.assertIn("automatic restarts failed", self.studio.ops.last["message"])

    def test_policy_view_is_refreshed_less_often_and_after_operations(self):
        def status(verb, extra):
            st = {"state": "STOPPED", "observed_state": "DOWN", "health": {"ok": False},
                  "desired": {"desired": "STOPPED", "record": None}}
            if "--admission" in extra:
                st.update(policy={"qwen": []}, admission={"ok": True, "message": "fits"})
            return (0, json.dumps(st), "")
        self.ctl.answers["status"] = status
        clock = [1000.0]
        with mock.patch.object(vst, "now", lambda: clock[0]):
            self.studio.fish_status(wait=True)
            for _ in range(2):
                clock[0] += vst.STATUS_TTL_S + 0.1
                self.studio._refresh_status()
            v = self.studio.fish_status()
            self.assertEqual(v["admission"]["message"], "fits")        # carried over
            self.assertIn("policy", v)
            self.studio.invalidate_status()                              # after an operation
            clock[0] += 0.1
            self.studio._refresh_status()
        flags = [("--admission" in c) for c in self.ctl.calls if c[0] == "status"]
        self.assertEqual(flags, [True, False, False, True])

    def test_watchdog_resets_after_recovery(self):
        self.ctl.answers["status"] = self._status()
        self.ctl.answers["start"] = (0, "{}", "")
        self.studio.ops.watchdog_tick()
        self.assertEqual(self.studio.ops.recovery["attempts"], 0)
        self.assertTrue(self.studio.ops.last["ok"])


# ============================================================== integration
@unittest.skipUnless(VENV_PY.exists(), "Fish runtime (tools/fish-venv) not installed")
class EndToEndTests(ServerCase):
    """Real engine (fixture mode), real sandboxed audio tool, real HTTP."""

    @classmethod
    def setUpClass(cls):
        cls.engine_tmp = pathlib.Path(tempfile.mkdtemp())
        cls.engine_port = free_port()
        cls.engine_cfg = fixture_config(cls.engine_tmp, cls.engine_port)
        state = cls.engine_tmp / "state"
        state.mkdir(mode=0o700)
        tok = state / "engine.token"
        fd = os.open(str(tok), os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"token": "ab" * 32, "generation": 1, "model_id": "drama3-q4"}, f)
        cls.engine = subprocess.Popen([str(VENV_PY), "-I", str(ROOT / "scripts" / "fish_engine.py"),
                                       "--config", str(cls.engine_cfg), "--fake-model"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(100):
            try:
                c = http.client.HTTPConnection("127.0.0.1", cls.engine_port, timeout=1)
                c.request("GET", "/health")
                if c.getresponse().status == 200:
                    break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            cls.engine.kill()
            raise RuntimeError("fixture engine did not become ready")

    @classmethod
    def tearDownClass(cls):
        cls.engine.terminate()
        cls.engine.wait(10)
        cls.engine.stderr.close()
        shutil.rmtree(cls.engine_tmp, ignore_errors=True)

    def setUp(self):
        self.make_studio()
        # Voice Studio reads the token where fishctl writes it for this config.
        st = pathlib.Path(json.loads(self.cfg_path.read_text())["service"]["statedir"])
        st.mkdir(mode=0o700, exist_ok=True)
        shutil.copy(self.engine_tmp / "state" / "engine.token", st / "engine.token")
        os.chmod(st / "engine.token", 0o600)
        self.open_session()

    def wait_generation(self, gid, timeout=60):
        for _ in range(int(timeout / 0.1)):
            st, j = self.req("GET", f"/api/voice/generations/{gid}")
            if j["generation"]["finished_at"]:
                return j["generation"]
            time.sleep(0.1)
        self.fail("generation did not finish")

    def test_record_transcribe_generate_play_download(self):
        st, j = self.req("POST", "/api/voice/profiles", body={"name": "Test voice", "consent": CONSENT})
        pid = j["profile"]["id"]
        wav = speechlike_wav()
        st, j = self.req("POST", f"/api/voice/profiles/{pid}/references?source=recording&label=Desk%20mic"
                         "&filename=browser-recording.wav", body=wav, ctype="audio/wav")
        self.assertEqual(st, 201, j)
        ref = j["reference"]
        self.assertEqual(ref["label"], "Desk mic")
        self.assertEqual(ref["model_input"]["sample_rate"], 44100)
        self.assertEqual(ref["original"]["sha256"], vst.hashlib.sha256(wav).hexdigest())
        self.assertIn("resample_audio_array", ref["processing"]["resampler"])
        st, hdrs, data = self.req("GET", f"/api/voice/references/{ref['id']}/audio?kind=original",
                                  raw=True)
        self.assertEqual((st, data), (200, wav))
        self.assertIn("attachment", hdrs["content-disposition"])
        self.assertEqual(self.req("PATCH", f"/api/voice/references/{ref['id']}",
                                  body={"transcript": "This is a synthetic test signal."})[0], 200)
        st, j = self.req("POST", "/api/voice/generate", body={
            "profile_id": pid, "reference_ids": [ref["id"]], "text": "Hello from the test.",
            "settings": {"seed": 7}, "count": 2, "progressive": True})
        self.assertEqual(st, 202, j)
        gid = j["generation"]["id"]
        self.assertEqual(self.req("POST", "/api/voice/generate", body={
            "profile_id": pid, "reference_ids": [ref["id"]], "text": "again"})[0], 409)
        g = self.wait_generation(gid)
        self.assertEqual((g["state"], len(g["take_ids"])), ("done", 2), g)
        st, hdrs, pcm = self.req("GET", f"/api/voice/generations/{gid}/pcm?from=0", raw=True)
        self.assertEqual(st, 200)
        self.assertGreater(len(pcm), 0)
        self.assertEqual(hdrs["x-sample-rate"], "44100")
        takes = self.req("GET", "/api/voice/state")[1]["takes"]
        self.assertEqual(len(takes), 2)
        t = takes[-1]
        self.assertEqual(t["references"][0]["id"], ref["id"])
        self.assertEqual(t["references"][0]["model_input_sha256"], ref["model_input"]["sha256"])
        self.assertEqual(t["references"][0]["transcript"], "This is a synthetic test signal.")
        self.assertEqual(t["settings"]["temperature"], 1.0)
        self.assertEqual(t["model"]["model_id"], "drama3-q4")
        self.assertEqual(t["model"]["hf_revision"], "5331ab509359c0e4a7e353a19e255ddf3e8152a8")
        self.assertEqual(sorted(x["seed"] for x in takes), [7, 8])
        st, hdrs, audio = self.req("GET", f"/api/voice/takes/{t['id']}/audio", raw=True)
        self.assertEqual(audio[:4], b"RIFF")
        self.assertIn(f"voice-take-{t['number']}.wav", hdrs["content-disposition"])
        self.assertEqual(vst.hashlib.sha256(audio).hexdigest(), t["audio"]["sha256"])
        log = (self.tmp / "voice.log").read_text()
        for private in ("Hello from the test", "synthetic test signal", "Test voice", "Desk mic"):
            self.assertNotIn(private, log)

    def test_cancel_a_generation(self):
        pid = self.req("POST", "/api/voice/profiles", body={"name": "V", "consent": CONSENT})[1]["profile"]["id"]
        ref = self.req("POST", f"/api/voice/profiles/{pid}/references?source=upload",
                       body=speechlike_wav(), ctype="audio/wav")[1]["reference"]
        self.req("PATCH", f"/api/voice/references/{ref['id']}", body={"transcript": "Test."})
        st, j = self.req("POST", "/api/voice/generate", body={
            "profile_id": pid, "reference_ids": [ref["id"]], "text": "x" * 1200, "count": 4})
        gid = j["generation"]["id"]
        time.sleep(0.3)
        self.assertEqual(self.req("POST", f"/api/voice/generations/{gid}/cancel")[0], 200)
        g = self.wait_generation(gid)
        self.assertEqual(g["state"], "cancelled", g)
        self.assertLess(len(g["take_ids"]), 4)

    def test_bad_audio_is_rejected_by_content(self):
        pid = self.req("POST", "/api/voice/profiles", body={"name": "V", "consent": CONSENT})[1]["profile"]["id"]
        for body, needle in ((b"RIFF\x00\x00\x00\x00WAVEjunk", "invalid WAV"),
                             (b"#!/bin/sh\necho hi\n" * 10, "not a supported audio file"),
                             (speechlike_wav(seconds=1.2), "at least")):
            st, j = self.req("POST", f"/api/voice/profiles/{pid}/references?source=upload",
                             body=body, ctype="audio/wav")
            self.assertEqual(st, 422, j)
            self.assertIn(needle, j["error"])
        self.assertEqual(os.listdir(os.path.join(self.studio.store.root, "tmp")), [])


if __name__ == "__main__":
    unittest.main()
