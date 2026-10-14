#!/usr/bin/env python3
"""Newton_Local — Voice Studio, the browser side of the Fish speech service (docs/18).

Newton AI Control (scripts/newton_control.py) serves it at /voice and hands
it every request under /voice, /api/voice and /api/fish. This module only
orchestrates; each layer below it stays separate:

    voice_store.py     private profiles, recordings, transcripts and takes
    fish_audio.py      reference ingestion (Fish runtime, network sandbox)
    fishctl.py         the Fish lifecycle; this module never starts or stops
                       anything except through it
    fish_engine.py     inference, over its token-protected loopback API

ROUTES
    GET    /voice  /voice/app.js  /voice/style.css  /voice/recorder-worklet.js
    POST   /api/voice/session                      open a session (see SECURITY)
    GET    /api/voice/state                        profiles, recordings, takes
    POST   /api/voice/profiles                     {name, consent}
    PATCH  /api/voice/profiles/<id>                {name?, active_reference_id?}
    DELETE /api/voice/profiles/<id>
    POST   /api/voice/profiles/<id>/references     raw audio body; query:
                                                   source, label, filename, replaces
    PATCH  /api/voice/references/<id>              {label?, transcript?}
    DELETE /api/voice/references/<id>
    GET    /api/voice/references/<id>/audio?kind=original|model
    POST   /api/voice/generate                     {profile_id, reference_ids, text,
                                                    settings, count, progressive}
    GET    /api/voice/generations/<id>             progress; take ids when done
    GET    /api/voice/generations/<id>/pcm?from=N  progressive PCM16 (first take)
    POST   /api/voice/generations/<id>/cancel
    GET    /api/voice/takes/<id>/audio
    PATCH  /api/voice/takes/<id>                   {note?, starred?}
    DELETE /api/voice/takes/<id>
    GET    /api/fish/status
    POST   /api/fish/start | stop | restart        (stop during a start cancels it)

SECURITY
    Everything inherits the control service's loopback bind. Every /api route
    checks the Host header (DNS rebinding), and every route except GET
    /api/fish/status needs the X-Newton-Control header, a same-origin Origin
    and Sec-Fetch-Site, so no other web page can drive or read it (CSRF; a
    cross-origin request with that header needs a CORS preflight, which is
    never answered). Private data (recordings, transcripts, takes) needs a
    session as well: POST /api/voice/session issues one only after checking,
    with lsof, that the program on the other end of the TCP connection runs
    as this Mac's operator account, so other accounts and system services
    are refused even on 127.0.0.1. The session token lives in the page's
    memory and travels in a header (never a cookie, which every other
    127.0.0.1 port would also receive). Bodies need Content-Length and are
    bounded (JSON 64 KiB, audio 32 MiB); ids are server-generated and
    validated; no request names a filesystem path; error replies and logs
    never contain audio, transcripts, text or names.
"""
import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import fish_config as fc  # noqa: E402
import voice_store as vs  # noqa: E402
from fish_tts import protocol as P  # noqa: E402

UI_DIR = os.path.join(SCRIPT_DIR, "voice-studio")
STATIC = {
    "/voice": ("index.html", "text/html"),
    "/voice/": ("index.html", "text/html"),
    "/voice/app.js": ("app.js", "text/javascript"),
    "/voice/style.css": ("style.css", "text/css"),
    "/voice/recorder-worklet.js": ("recorder-worklet.js", "text/javascript"),
}
FISHCTL = os.path.join(SCRIPT_DIR, "fishctl.py")
AUDIO_TOOL = os.path.join(SCRIPT_DIR, "fish_audio.py")
LSOF = "/usr/sbin/lsof"

MAX_JSON = 64 << 10
MAX_AUDIO = 32 << 20
MAX_TAKES_PER_REQUEST = 4
SESSION_IDLE_S = 12 * 3600
MAX_SESSIONS = 16
GENERATIONS_KEPT = 32
POLL_S = 0.2
STATUS_TTL_S = 4.0
WATCHDOG_PERIOD_S = 15
RECOVERY_BACKOFF_S = [0, 60, 180]
RECOVERY_MAX_ATTEMPTS = 3
STOP_TIMEOUT_S = 150
CANCELLABLE_OPS = ("start", "restart")
PROGRESSIVE_SEGMENT_FRAMES = 11          # about half a second of audio per segment

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "media-src 'self' blob:; connect-src 'self'; worker-src 'self'; object-src 'none'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
AUDIO_TYPES = {"wav": "audio/wav", "flac": "audio/flac", "mp3": "audio/mpeg", "ogg": "audio/ogg",
               "m4a": "audio/mp4", "webm": "audio/webm", "caf": "audio/x-caf", "aiff": "audio/aiff"}
ID = r"([0-9a-f]{16})"
ROUTES = [
    ("POST", r"^/api/voice/session$", "session", "control"),
    ("GET", r"^/api/voice/state$", "state", "private"),
    ("POST", r"^/api/voice/profiles$", "profile_create", "private"),
    ("PATCH", r"^/api/voice/profiles/%s$" % ID, "profile_update", "private"),
    ("DELETE", r"^/api/voice/profiles/%s$" % ID, "profile_delete", "private"),
    ("POST", r"^/api/voice/profiles/%s/references$" % ID, "reference_add", "private"),
    ("PATCH", r"^/api/voice/references/%s$" % ID, "reference_update", "private"),
    ("DELETE", r"^/api/voice/references/%s$" % ID, "reference_delete", "private"),
    ("GET", r"^/api/voice/references/%s/audio$" % ID, "reference_audio", "private"),
    ("POST", r"^/api/voice/generate$", "generate", "private"),
    ("GET", r"^/api/voice/generations/%s$" % ID, "generation_get", "private"),
    ("GET", r"^/api/voice/generations/%s/pcm$" % ID, "generation_pcm", "private"),
    ("POST", r"^/api/voice/generations/%s/cancel$" % ID, "generation_cancel", "private"),
    ("GET", r"^/api/voice/takes/%s/audio$" % ID, "take_audio", "private"),
    ("PATCH", r"^/api/voice/takes/%s$" % ID, "take_update", "private"),
    ("DELETE", r"^/api/voice/takes/%s$" % ID, "take_delete", "private"),
    ("GET", r"^/api/fish/status$", "fish_status", "host"),
    ("POST", r"^/api/fish/(start|stop|restart)$", "fish_op", "control"),
]


class HttpError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


class EngineDown(Exception):
    """The Fish engine is not reachable (stopped, starting or crashed)."""


class EngineError(Exception):
    """The engine answered, but refused or failed."""


def now():
    return time.time()


def iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts or now()))


def parse_json(text):
    try:
        v = json.loads(text)
    except (ValueError, TypeError):
        return None
    return v if isinstance(v, dict) else None


def tail_line(text):
    lines = [l.strip() for l in (text or "").strip().splitlines() if l.strip()]
    line = lines[-1] if lines else ""
    return line[len("fishctl: "):] if line.startswith("fishctl: ") else line


# ------------------------------------------------------------- peer identity
def peer_is_operator(client_port, server_port, uid=None):
    """(ok, why): does the program at the other end of this loopback TCP
    connection run as this account? lsof run as this (non-root) account
    only sees this account's processes, so a peer owned by anyone else is
    simply not found, and the answer fails closed."""
    uid = os.getuid() if uid is None else uid
    try:
        p = subprocess.run([LSOF, "-nP", "-a", f"-iTCP@127.0.0.1:{int(client_port)}",
                            "-sTCP:ESTABLISHED", "-Fpun"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False, "the connecting program could not be identified"
    return peer_from_lsof(p.stdout, client_port, server_port, uid)


def peer_from_lsof(out, client_port, server_port, uid):
    """Pure: parse `lsof -Fpun` output for the client end of the connection."""
    want = f"127.0.0.1:{int(client_port)}->127.0.0.1:{int(server_port)}"
    owner = None
    for line in (out or "").splitlines():
        if line.startswith("p"):
            owner = None
        elif line.startswith("u"):
            owner = int(line[1:]) if line[1:].isdigit() else None
        elif line.startswith("n") and line[1:] == want:
            if owner == uid:
                return True, None
            return False, "the connecting program runs as a different account"
    return False, "the connecting program is not running as this Mac's operator account"


# ------------------------------------------------------------------ engine
class EngineClient:
    """The engine's internal API (fish_engine.py). Loopback, bearer token,
    no Origin header. Raises EngineDown when nothing answers."""

    def __init__(self, cfg):
        svc = cfg["service"]
        self.host, self.port = svc["host"], int(svc["port"])
        self.token_path = fc.paths(cfg)["token"]

    def token(self):
        rec = fc.read_json(self.token_path) or {}
        tok = rec.get("token")
        if not tok:
            raise EngineDown("Fish has no access token; start Fish first")
        return tok

    def call(self, method, path, body=None, ctype=None, timeout=15, auth=True):
        headers = {}
        if auth:
            headers["Authorization"] = "Bearer " + self.token()
        if body is not None:
            headers["Content-Type"] = ctype or "application/octet-stream"
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=headers)
            r = conn.getresponse()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
        except (OSError, http.client.HTTPException) as e:
            raise EngineDown(f"Fish is not answering ({type(e).__name__})")
        finally:
            conn.close()

    def json_call(self, method, path, obj=None, timeout=15, auth=True):
        body = json.dumps(obj).encode() if obj is not None else None
        st, _, data = self.call(method, path, body, "application/json" if body else None,
                                timeout=timeout, auth=auth)
        try:
            return st, json.loads(data.decode("utf-8") or "null")
        except (ValueError, UnicodeDecodeError):
            return st, None

    def health(self):
        try:
            st, body = self.json_call("GET", "/health", auth=False, timeout=3)
        except EngineDown:
            return {"ok": False, "state": "down"}
        body = body if isinstance(body, dict) else {}
        return {"ok": st == 200 and body.get("state") == "ready", "state": body.get("state"),
                "busy": bool(body.get("busy")), "generation": body.get("generation")}

    @staticmethod
    def _err(st, body, what):
        msg = body.get("error") if isinstance(body, dict) else None
        return EngineError(f"{what}: {msg or f'HTTP {st}'}")

    def identity(self):
        st, body = self.json_call("GET", "/v1/identity")
        if st != 200 or not isinstance(body, dict):
            raise self._err(st, body, "identity")
        return body

    def ensure_reference(self, sha, path):
        """Encode a reference once per engine start: PUT it only if the engine
        does not hold it already. The bytes are checked against their record
        first, so a damaged file is never used."""
        st, _ = self.json_call("GET", f"/v1/references/{sha}")
        if st == 200:
            return False
        if st != 404:
            raise EngineError(f"reference check answered HTTP {st}")
        with open(path, "rb") as f:
            data = f.read()
        if hashlib.sha256(data).hexdigest() != sha:
            raise HttpError(409, "a stored recording no longer matches its checksum; "
                                 "record or upload it again")
        st, _, body = self.call("PUT", f"/v1/references/{sha}", data, "audio/wav", timeout=180)
        if st != 200:
            raise self._err(st, parse_json(body.decode("utf-8", "replace")), "reference")
        return True

    def submit(self, job):
        return self.json_call("POST", "/v1/jobs", job)

    def job(self, jid):
        st, body = self.json_call("GET", f"/v1/jobs/{jid}")
        if st != 200 or not isinstance(body, dict):
            raise self._err(st, body, "job status")
        return body

    def audio(self, jid):
        st, _, data = self.call("GET", f"/v1/jobs/{jid}/audio", timeout=60)
        if st != 200 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            raise EngineError(f"the finished take could not be fetched (HTTP {st})")
        return data

    def pcm(self, jid, start):
        return self.call("GET", f"/v1/jobs/{jid}/pcm?from={int(start)}", timeout=10)

    def cancel(self, jid):
        try:
            self.json_call("POST", f"/v1/jobs/{jid}/cancel", timeout=5)
        except EngineDown:
            pass


# -------------------------------------------------------------- generations
class Generation:
    """One request from the page: up to MAX_TAKES_PER_REQUEST takes, made one
    after another with the same voice, text and settings."""

    def __init__(self, request, count, progressive):
        self.id = secrets.token_hex(8)
        self.request = request                 # validated; contains text: never logged
        self.count = count
        self.progressive = progressive
        self.state = "preparing"
        self.index = 0
        self.take_ids = []
        self.errors = []
        self.error = None
        self.engine_job = None
        self.first_engine_job = None
        self.progress = {}
        self.samples_available = 0
        self.cancel_requested = False
        self.cancel_reason = None
        self.created_at = now()
        self.finished_at = None
        self.lock = threading.Lock()

    def public(self):
        with self.lock:
            return {"id": self.id, "state": self.state, "count": self.count,
                    "index": self.index, "take_ids": list(self.take_ids),
                    "errors": list(self.errors), "error": self.error,
                    "progress": dict(self.progress), "progressive": self.progressive,
                    "samples_available": self.samples_available,
                    "created_at": self.created_at, "finished_at": self.finished_at,
                    "cancel_reason": self.cancel_reason,
                    "elapsed_s": round((self.finished_at or now()) - self.created_at, 2)}


class Generations:
    def __init__(self, studio):
        self.s = studio
        self.items = {}
        self.lock = threading.Lock()

    def active(self):
        with self.lock:
            for g in self.items.values():
                if g.finished_at is None:
                    return g
        return None

    def get(self, gid):
        with self.lock:
            g = self.items.get(gid)
        if g is None:
            raise HttpError(404, "no such generation")
        return g

    def start(self, request, count, progressive):
        with self.lock:
            if any(g.finished_at is None for g in self.items.values()):
                raise HttpError(409, "a take is already being generated; wait for it or "
                                     "cancel it")
            g = Generation(request, count, progressive)
            self.items[g.id] = g
            done = sorted((x for x in self.items.values() if x.finished_at),
                          key=lambda x: x.finished_at)
            for x in done[:max(0, len(self.items) - GENERATIONS_KEPT)]:
                del self.items[x.id]
        threading.Thread(target=self._run, args=(g,), name=f"voice-gen-{g.id}",
                         daemon=True).start()
        return g

    def cancel(self, g, reason="cancelled"):
        with g.lock:
            if g.finished_at is not None:
                return
            g.cancel_requested = True
            g.cancel_reason = reason
            jid = g.engine_job
        if jid:
            try:
                self.s.engine().cancel(jid)
            except EngineDown:
                pass

    def cancel_all(self, reason):
        with self.lock:
            live = [g for g in self.items.values() if g.finished_at is None]
        for g in live:
            self.cancel(g, reason)

    # ---------------------------------------------------------------- worker
    def _run(self, g):
        s = self.s
        log = s.log
        log("voice.generation.begin", generation=g.id, takes=g.count,
            references=len(g.request["references"]), text_chars=len(g.request["text"]))
        try:
            eng = s.engine()
            if not eng.health()["ok"]:
                raise HttpError(409, "Fish is not running; start it first")
            ident = eng.identity()
            t = now()
            for r in g.request["refs_full"]:
                eng.ensure_reference(r["model_input_sha256"], r["path"])
            ref_s = round(now() - t, 3)
            for i in range(g.count):
                with g.lock:
                    if g.cancel_requested:
                        break
                    g.index = i + 1
                    g.progress = {}
                    g.state = "submitting"
                self._one_take(g, eng, ident, i, ref_s if i == 0 else 0.0)
            with g.lock:
                if g.cancel_requested and not g.error:
                    g.state = "cancelled"
                elif g.take_ids and not g.errors:
                    g.state = "done"
                elif g.take_ids:
                    g.state = "partial"
                else:
                    g.state = "failed"
                    g.error = g.error or (g.errors[-1] if g.errors else "no take was produced")
        except HttpError as e:
            with g.lock:
                g.state, g.error = "failed", e.message
        except EngineDown:
            with g.lock:
                g.state = "cancelled" if g.cancel_requested else "failed"
                g.error = None if g.cancel_requested else \
                    "Fish stopped before the take was finished"
        except (EngineError, vs.StoreError, OSError) as e:
            with g.lock:
                g.state, g.error = "failed", str(e)[:300]
        except Exception as e:                       # never leave a generation hanging
            with g.lock:
                g.state = "failed"
                g.error = f"internal error ({type(e).__name__}); see logs/control/voice.log"
            log("voice.generation.crashed", generation=g.id, error=type(e).__name__)
        finally:
            with g.lock:
                g.finished_at = now()
                g.engine_job = None
            log("voice.generation.end", generation=g.id, state=g.state,
                takes=len(g.take_ids), elapsed_s=round(g.finished_at - g.created_at, 2))

    def _one_take(self, g, eng, ident, i, ref_s):
        req = g.request
        settings = dict(req["settings"])
        if settings.get("seed") is not None and i:
            settings["seed"] = (settings["seed"] + i) % (P.MAX_SEED + 1)
        body = {"text": req["text"],
                "references": [{"sha256": r["model_input_sha256"], "transcript": r["transcript"]}
                               for r in req["refs_full"]],
                "settings": settings, "client_ref": g.id,
                "progressive": {"segment_frames": PROGRESSIVE_SEGMENT_FRAMES}
                if (g.progressive and i == 0) else None}
        t_submit = now()
        st, job = eng.submit(body)
        if st == 409 and isinstance(job, dict) and job.get("missing_references"):
            for r in req["refs_full"]:                   # evicted meanwhile: once more
                eng.ensure_reference(r["model_input_sha256"], r["path"])
            st, job = eng.submit(body)
        if st != 202 or not isinstance(job, dict):
            msg = job.get("error") if isinstance(job, dict) else None
            raise EngineError(f"Fish refused the request: {msg or f'HTTP {st}'}")
        jid = job["id"]
        with g.lock:
            g.engine_job = jid
            if i == 0:
                g.first_engine_job = jid
            cancel_now = g.cancel_requested
        if cancel_now:
            eng.cancel(jid)
        while True:
            j = eng.job(jid)
            with g.lock:
                g.state = j["state"]
                g.progress = j.get("progress") or {}
                if i == 0:
                    g.samples_available = j.get("samples_available") or 0
            if j["state"] in ("done", "failed", "cancelled"):
                break
            time.sleep(POLL_S)
        if j["state"] == "cancelled":
            return
        if j["state"] == "failed":
            err = j.get("error") or {}
            msg = err.get("message") or "generation failed"
            if err.get("kind") == "timeout":
                msg = f"the take took too long and was stopped ({msg})"
            with g.lock:
                g.errors.append(f"take {i + 1}: {msg}"[:300])
            return
        wav = eng.audio(jid)
        res = j.get("result") or {}
        meta = take_metadata(req, settings, res, ident, j, t_submit, ref_s, g)
        rec = self.s.store.save_take(meta, wav)
        with g.lock:
            g.take_ids.append(rec["id"])
        self.s.log("voice.take.saved", generation=g.id, take=rec["id"], number=rec["number"],
                   audio_s=res.get("audio_seconds"), synthesis_s=res.get("synthesis_s"),
                   flags=[f.get("kind") for f in res.get("flags") or []])


def take_metadata(req, settings, res, ident, job, t_submit, ref_s, g):
    """Everything needed to understand, compare and reproduce a take."""
    started, finished = job.get("started_at"), job.get("finished_at")
    return {
        "text": req["text"],
        "profile": {"id": req["profile"]["id"], "name": req["profile"]["name"]},
        "references": [{k: r[k] for k in ("id", "label", "transcript", "model_input_sha256",
                                          "original_sha256", "seconds", "source")}
                       for r in req["refs_full"]],
        "settings": {k: v for k, v in settings.items() if k != "seed"},
        "seed": res.get("seed"),
        "seed_requested": settings.get("seed"),
        "model": {k: ident.get(k) for k in ("model_id", "display_name", "variant", "hf_repo",
                                            "hf_revision", "manifest_sha256", "adapter",
                                            "runtime_commit", "mlx", "mlx_audio", "generation",
                                            "license", "attribution")},
        "result": {k: res.get(k) for k in ("frames", "audio_seconds", "synthesis_s",
                                           "first_frame_s", "first_audio_s", "realtime_factor",
                                           "peak_gib", "progressive", "segment_frames",
                                           "wav_encode_s", "chunks", "flags")},
        "timing": {"submitted_at": iso(t_submit),
                   "queue_s": round(started - t_submit, 3) if started else None,
                   "engine_s": round(finished - started, 3) if started and finished else None,
                   "reference_upload_s": ref_s},
        "generation_id": g.id,
        "take_in_request": g.index,
        "regenerated_from": req.get("regenerated_from"),
        "note": "", "starred": False,
    }


# ------------------------------------------------------------- lifecycle
class FishOps:
    """One Fish lifecycle operation at a time, through fishctl, plus crash
    recovery that honours fishctl's desired state (docs/18 §3)."""

    def __init__(self, studio):
        self.s = studio
        self._lock = threading.Lock()
        self.current = None
        self.last = None
        self.cancel = None
        self.recovery = {"attempts": 0, "next_at": 0.0, "gave_up": False}

    def begin(self, name):
        with self._lock:
            if self.current:
                return False
            self.current = {"name": name, "started_at": now(), "cancelling": False}
            self.cancel = None
            return True

    def request_cancel(self, by):
        with self._lock:
            if not self.current or self.current["name"] not in CANCELLABLE_OPS:
                return None
            if self.cancel is None:
                self.cancel = {"requested_at": now(), "by": by}
                self.current["cancelling"] = True
            return self.current["name"]

    def cancel_requested(self):
        with self._lock:
            return self.cancel is not None

    def finish(self, name, ok, message, **extra):
        with self._lock:
            self.last = {"name": name, "ok": ok, "message": message[:600],
                         "finished_at": now(), "finished_at_iso": time.strftime("%H:%M:%S")}
            self.last.update(extra)
            self.current = None
            self.cancel = None
        self.s.invalidate_status()
        self.s.log("fish.op.finish", name=name, ok=ok,
                   **{k: v for k, v in extra.items() if k in ("error", "exit_code", "cancelled",
                                                             "policy_code")})

    def run(self, name):
        fn = {"start": self._start, "restart": self._restart, "stop": self._stop}[name]
        if not self.begin(name):
            return False
        self.s.log("fish.op.begin", name=name)
        threading.Thread(target=fn, name=f"fish-op-{name}", daemon=True).start()
        return True

    # ---------------------------------------------------------- operations
    def _start(self, verb="start"):
        try:
            rc, out, err = self.s.fishctl(verb, "--by", "panel", timeout=self.s.start_timeout())
            if self.cancel_requested():
                return self._finish_cancelled(verb)
            if rc == 0:
                res = parse_json(out) or {}
                ls = res.get("last_start") or {}
                took = f" in {ls['load_to_ready_s']:.0f} s" if ls.get("load_to_ready_s") else ""
                self.recovery = {"attempts": 0, "next_at": 0.0, "gave_up": False}
                self.finish(verb, True, ("Fish restarted and is ready" if verb == "restart"
                                         else "Fish is ready") + took + "."
                            if res.get("started", True) else "Fish was already running.")
            else:
                msg, extra = describe_failure(parse_json(out), err or out, rc)
                self.finish(verb, extra.get("cancelled", False), msg, **extra)
        except Exception as e:                      # the op thread must always finish
            self.finish(verb, False, f"internal error: {type(e).__name__}")

    def _restart(self):
        self.s.gens.cancel_all("Fish was restarted")
        self._start("restart")

    def _stop(self):
        try:
            self.s.gens.cancel_all("Fish was stopped")
            rc, out, err = self.s.fishctl("stop", "--by", "panel", timeout=STOP_TIMEOUT_S)
            res = parse_json(out) or {}
            if rc == 0:
                after = ((res.get("after") or {}).get("memory") or {}).get("free_for_models_gib")
                note = "Fish was already stopped." if res.get("note") == "already stopped" else \
                    "Fish stopped; its memory was released" + (
                        f" ({after:.0f} GiB free for models)." if after else ".")
                self.recovery = {"attempts": 0, "next_at": 0.0, "gave_up": False}
                self.finish("stop", True, note)
            else:
                self.finish("stop", False, "Fish could not be confirmed stopped: "
                            + (tail_line(err or out) or f"fishctl exit {rc}"), exit_code=rc)
        except Exception as e:
            self.finish("stop", False, f"internal error: {type(e).__name__}")

    def _finish_cancelled(self, name):
        rc, out, err = self.s.fishctl("stop", "--by", "panel:cancel", timeout=STOP_TIMEOUT_S)
        if rc == 0:
            self.finish(name, True, "Start cancelled; Fish is stopped.", cancelled=True)
        else:
            self.finish(name, False, "Start cancelled, but Fish could not be confirmed "
                        "stopped: " + (tail_line(err or out) or f"exit {rc}"), cancelled=True)

    def cancel_inflight(self, name):
        """Stop pressed during a start: fishctl records STOPPED and drops its
        cancel marker; the start aborts within a poll and stops its engine."""
        rc, out, err = self.s.fishctl("cancel", "--by", "panel", timeout=30)
        self.s.log("fish.op.cancel", name=name, rc=rc)
        return rc == 0

    # ------------------------------------------------------------ watchdog
    def watchdog_tick(self):
        """Restart Fish after an unexpected exit, only while fishctl's desired
        state is RUNNING for this boot (every stop, from any entry point,
        writes STOPPED first), with bounded backoff, through the same
        admission-checked start. Gives up after RECOVERY_MAX_ATTEMPTS and
        records STOPPED, so a failed recovery never blocks other models."""
        if self.current:
            return
        rc, out, _ = self.s.fishctl("status", timeout=45)
        st = parse_json(out) if rc == 0 else None
        if not st:
            return
        if (st.get("desired") or {}).get("desired") != "RUNNING":
            if self.recovery["attempts"] and not self.recovery["gave_up"]:
                self.recovery = {"attempts": 0, "next_at": 0.0, "gave_up": False}
            return
        if st["state"] == "READY" and (st.get("health") or {}).get("ok"):
            if self.recovery["attempts"]:
                self.s.log("fish.watchdog.recovered_confirmed")
            self.recovery = {"attempts": 0, "next_at": 0.0, "gave_up": False}
            return
        if st["state"] != "STOPPED" or st.get("observed_state") != "DOWN":
            return                                   # starting or stopping: let it settle
        r = self.recovery
        if r["gave_up"] or now() < r["next_at"]:
            return
        if r["attempts"] >= RECOVERY_MAX_ATTEMPTS:
            r["gave_up"] = True
            self.s.fishctl("stop", "--by", "watchdog:gave-up", timeout=STOP_TIMEOUT_S)
            with self._lock:
                self.last = {"name": "recovery", "ok": False, "finished_at": now(),
                             "finished_at_iso": time.strftime("%H:%M:%S"),
                             "message": f"Fish stopped unexpectedly and {r['attempts']} automatic "
                                        f"restarts failed; it is now stopped. "
                                        + ((self.last or {}).get("message") or "")}
            self.s.log("fish.watchdog.gave_up", attempts=r["attempts"])
            self.s.invalidate_status()
            return
        attempt = r["attempts"] + 1
        if not self.begin("start"):
            return
        self.s.log("fish.watchdog.recover_attempt", attempt=attempt)
        rc, out, err = self.s.fishctl("start", "--by", "watchdog", "--recovery",
                                      timeout=self.s.start_timeout())
        if self.cancel_requested():
            self.recovery = {"attempts": 0, "next_at": 0.0, "gave_up": False}
            return self._finish_cancelled("start")
        r["attempts"] = 0 if rc == 0 else attempt
        r["next_at"] = now() + RECOVERY_BACKOFF_S[min(attempt, len(RECOVERY_BACKOFF_S) - 1)]
        if rc == 0:
            self.finish("start", True, "Fish recovered after an unexpected exit.")
        else:
            msg, extra = describe_failure(parse_json(out), err or out, rc)
            self.finish("start", False, f"recovery attempt {attempt} failed: {msg}", **extra)

    def watchdog_loop(self, stop_event):
        while not stop_event.wait(WATCHDOG_PERIOD_S):
            try:
                self.watchdog_tick()
            except Exception as e:                   # the watchdog must never die
                self.s.log("fish.watchdog.error", error=type(e).__name__)


def describe_failure(res, text, rc):
    """(message, extra) for a fishctl start/restart that did not succeed."""
    if isinstance(res, dict) and res.get("ok") is False and res.get("error"):
        kind = res["error"]
        extra = {"error": kind, "exit_code": res.get("exit_code", rc)}
        if kind == "cancelled":
            extra["cancelled"] = True
            return "Start cancelled; Fish is stopped.", extra
        if kind == "policy":
            extra["policy_code"] = res.get("policy_code")
        if res.get("reason"):
            extra["reason"] = res["reason"]
        if res.get("log"):
            extra["log"] = res["log"]
        return res.get("message") or f"Fish did not start ({kind})", extra
    return tail_line(text) or f"Fish did not start (fishctl exit {rc})", {"exit_code": rc}


def fish_ui_state(st, op, gave_up=False):
    """Pure: controller truth -> one of Stopped, Starting, Ready, Busy,
    Stopping, Failed, Recovering, Unknown."""
    if op and (op.get("name") == "stop" or op.get("cancelling")):
        return "Stopping"
    if op and op.get("name") in CANCELLABLE_OPS:
        return "Starting"
    if not isinstance(st, dict) or "state" not in st:
        return "Unknown"
    ctl, obs = st["state"], st.get("observed_state")
    health = st.get("health") or {}
    if ctl == "STARTING":
        return "Starting"
    if ctl == "STOPPING":
        return "Stopping"
    if ctl == "READY":
        if health.get("ok"):
            return "Busy" if health.get("busy") else "Ready"
        return "Failed"
    if obs and obs != "DOWN":
        return "Starting"                          # an engine with no record: being adopted
    desired = st.get("desired") or {}
    if desired.get("desired") == "RUNNING":
        return "Failed" if gave_up else "Recovering"
    if ((desired.get("record") or {}).get("set_by") or "").startswith("watchdog:gave-up"):
        return "Failed"
    return "Stopped"


# ------------------------------------------------------------------ studio
class Studio:
    def __init__(self, port, config_path=None, peer_check=None, log_path=None):
        self.port = port
        self.config_path = config_path or fc.CONFIG_PATH
        self.peer_check = peer_check or peer_is_operator
        self.log_path = log_path or os.path.join(fc.ROOT, "logs", "control", "voice.log")
        self.cfg, self.error, self.store = None, None, None
        try:
            self.cfg = fc.load_config(self.config_path)
            self.store = vs.VoiceStore(fc.paths(self.cfg)["store"]).init()
        except (fc.ConfigError, OSError) as e:
            self.error = f"{type(e).__name__}: {e}"[:300]
        self.sessions = {}
        self.sessions_lock = threading.Lock()
        self.ingest_lock = threading.BoundedSemaphore(1)
        self.ops = FishOps(self)
        self.gens = Generations(self)
        self._status = {"ts": 0.0, "value": None, "busy": False}
        self._status_lock = threading.Lock()
        self._usage = {"ts": 0.0, "bytes": None}
        self._stop = threading.Event()

    # ------------------------------------------------------------- plumbing
    def log(self, event, **fields):
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            rec = {"ts": round(now(), 3), "ts_iso": iso(), "event": event}
            rec.update(fields)
            with open(self.log_path, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass

    def model_id(self):
        return fc.read_selection(self.cfg)[0]

    def model(self):
        return fc.model(self.cfg, self.model_id())

    def engine(self):
        return EngineClient(self.cfg)

    def start_timeout(self):
        return int(self.cfg["service"]["start_timeout_s"]) + 120

    def fishctl(self, verb, *extra, timeout=60):
        cmd = [sys.executable, FISHCTL, "--config", self.cfg["_config_path"], verb, "--json"]
        cmd += list(extra)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired:
            return 124, "", f"fishctl {verb} did not answer within {timeout} s"

    def start_background(self):
        if self.cfg is not None:
            threading.Thread(target=self.ops.watchdog_loop, args=(self._stop,),
                             name="fish-watchdog", daemon=True).start()

    def stop_background(self):
        self._stop.set()

    # --------------------------------------------------------------- status
    def invalidate_status(self):
        with self._status_lock:
            self._status["ts"] = 0.0

    def fish_status(self, wait=False):
        """fishctl status --admission, cached for a few seconds. A refresh runs
        in the background while callers get the previous answer, so polling
        pages stay fast."""
        with self._status_lock:
            fresh = now() - self._status["ts"] < STATUS_TTL_S
            have = self._status["value"] is not None
            if fresh and have:
                return self._status["value"]
            start = not self._status["busy"]
            if start:
                self._status["busy"] = True
        if start:
            if have and not wait:
                threading.Thread(target=self._refresh_status, daemon=True).start()
            else:
                self._refresh_status()
        with self._status_lock:
            return self._status["value"]

    def _refresh_status(self):
        try:
            rc, out, err = self.fishctl("status", "--admission", timeout=45)
            v = parse_json(out) if rc == 0 else None
            if v is None:
                v = {"error": tail_line(err or out) or f"fishctl status exit {rc}"}
        except Exception as e:
            v = {"error": type(e).__name__}
        with self._status_lock:
            self._status.update(ts=now(), value=v, busy=False)

    def summary(self):
        """The Fish service as Voice Studio and the main panel show it.
        No private data: no names, transcripts or text."""
        if self.cfg is None:
            return {"available": False, "state": "Unavailable",
                    "error": f"Voice Studio is not configured: {self.error}"}
        st = self.fish_status() or {}
        op = self.ops.current
        state = fish_ui_state(st, op, self.ops.recovery["gave_up"])
        m = self.model()
        eng = st.get("engine") or {}
        mem = eng.get("memory") or {}
        policy = st.get("policy") or {}
        qwen = [q for q in (policy.get("qwen") or []) if q.get("present")]
        compat = policy.get("compatibility") or {}
        blockers = [q for q in qwen if compat.get(q.get("model")) != "conditional"]
        cur = eng.get("current_job") if isinstance(eng, dict) else None
        out = {
            "available": True, "state": state, "controller_state": st.get("state"),
            "observed_state": st.get("observed_state"), "error": st.get("error"),
            "display_name": m["display_name"], "variant": m["variant"],
            "model_id": self.model_id(), "pid": st.get("pid"), "rss_gib": st.get("rss_gib"),
            "endpoint": st.get("endpoint"), "generation": st.get("generation"),
            "memory": {"mlx_active_gib": mem.get("active_gib"),
                       "mlx_peak_gib": mem.get("peak_gib"),
                       "mlx_cache_gib": mem.get("cache_gib"),
                       "process_peak_rss_gib": mem.get("process_peak_rss_gib"),
                       "machine": st.get("memory")},
            "busy": state == "Busy",
            "current_job": {"state": cur.get("state"), "progress": cur.get("progress")}
            if isinstance(cur, dict) else None,
            "load": (st.get("last_start") or {}).get("engine_load"),
            "last_start": st.get("last_start"), "last_failure": st.get("last_failure"),
            "last_stop": st.get("last_stop"), "desired": (st.get("desired") or {}).get("desired"),
            "operation": op, "last_operation": self.ops.last,
            "recovery": dict(self.ops.recovery),
            "qwen": [{"slot": q.get("slot"), "model": q.get("model"), "phase": q.get("phase"),
                      "rule": compat.get(q.get("model"), "prohibited")} for q in qwen],
            "blocked_by": [q.get("model") for q in blockers],
            "admission": st.get("admission"),
            "compatibility": compat,
            "model_names": self.qwen_names(),
            "license": {k: m["license"][k] for k in ("name", "notice", "attribution")},
            "url": "/voice",
        }
        return out

    def qwen_names(self):
        """Display names of the Qwen registry models (for compatibility lines)."""
        try:
            import newton_models as nm
            return {k: v.get("display_name", k) for k, v in nm.load_registry()["models"].items()}
        except Exception:
            return {}

    # ----------------------------------------------------------------- HTTP
    def handles(self, path):
        return (path == "/voice" or path.startswith("/voice/") or path.startswith("/api/voice/")
                or path.startswith("/api/fish/"))

    def handle(self, h, method):
        """Serve one request if it belongs to Voice Studio; True if handled."""
        path, _, query = h.path.partition("?")
        if not self.handles(path):
            return False
        h._vs_sent = False
        try:
            self._dispatch(h, method, path, query)
        except HttpError as e:
            self._reply(h, e.status, dict({"ok": False, "error": e.message}, **e.extra))
        except vs.StoreError as e:
            self._reply(h, e.status, {"ok": False, "error": str(e)})
        except Exception as e:
            self.log("voice.http.error", method=method, route=route_name(method, path),
                     error=type(e).__name__)
            self._reply(h, 500, {"ok": False, "error": "internal error in Voice Studio; see "
                                                       "logs/control/voice.log"})
        return True

    def _reply(self, h, code, obj):
        if not getattr(h, "_vs_sent", False):
            self.send(h, code, json.dumps(obj, indent=1).encode(), "application/json")

    def send(self, h, code, data, ctype, extra=None, html=False):
        h._vs_sent = True
        h.send_response(code)
        h.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/")
                                               or ctype == "application/json" else ""))
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Cache-Control", "no-store")
        h.send_header("X-Content-Type-Options", "nosniff")
        h.send_header("Cross-Origin-Resource-Policy", "same-origin")
        h.send_header("Referrer-Policy", "no-referrer")
        h.send_header("X-Frame-Options", "DENY")
        if html:
            h.send_header("Content-Security-Policy", CSP)
            h.send_header("Permissions-Policy", "microphone=(self), camera=(), geolocation=()")
        for k, v in (extra or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(data)

    def _host_ok(self, h):
        host = (h.headers.get("Host") or "").strip().lower()
        return host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")

    def _control_ok(self, h):
        """The panel's same-origin rule, plus Fetch Metadata."""
        if h.headers.get("X-Newton-Control") != "1":
            return "this request must come from the Newton AI Control page"
        origin = h.headers.get("Origin")
        if origin and origin.rstrip("/") not in (f"http://127.0.0.1:{self.port}",
                                                 f"http://localhost:{self.port}"):
            return "cross-origin requests are not accepted"
        site = h.headers.get("Sec-Fetch-Site")
        if site and site != "same-origin":
            return "cross-site requests are not accepted"
        return None

    def _session_ok(self, h):
        tok = h.headers.get("X-Voice-Session") or ""
        with self.sessions_lock:
            rec = self.sessions.get(tok)
            if rec is None or now() - rec["used"] > SESSION_IDLE_S:
                self.sessions.pop(tok, None)
                return False
            rec["used"] = now()
            return True

    def _dispatch(self, h, method, path, query):
        if not self._host_ok(h):
            raise HttpError(403, "bad host header")
        if path in STATIC:
            if method != "GET":
                raise HttpError(405, "method not allowed")
            name, ctype = STATIC[path]
            with open(os.path.join(UI_DIR, name), "rb") as f:
                return self.send(h, 200, f.read(), ctype, html=ctype == "text/html")
        for m, pattern, name, level in ROUTES:
            mm = re.match(pattern, path)
            if not mm or m != method:
                continue
            if level in ("control", "private"):
                why = self._control_ok(h)
                if why:
                    raise HttpError(403, why)
            if self.cfg is None and name not in ("fish_status",):
                raise HttpError(503, f"Voice Studio is not available: {self.error}")
            if level == "private" and not self._session_ok(h):
                raise HttpError(401, "your Voice Studio session has expired; reload the page",
                                code="session")
            return getattr(self, "r_" + name)(h, *mm.groups(), query=query)
        if any(re.match(p, path) for _, p, _, _ in ROUTES):
            raise HttpError(405, "method not allowed")
        raise HttpError(404, "not found")

    def body(self, h, limit, kind="JSON"):
        if h.headers.get("Transfer-Encoding"):
            raise HttpError(411, "chunked bodies are not accepted")
        raw = h.headers.get("Content-Length")
        if raw is None or not raw.strip().isdigit():
            raise HttpError(411, "Content-Length is required")
        n = int(raw)
        if n > limit:
            h.close_connection = True
            if kind == "audio":
                raise HttpError(413, f"the file is {n / 2**20:.1f} MiB; the limit is "
                                     f"{limit / 2**20:.0f} MiB")
            raise HttpError(413, f"the request body is too large ({n} bytes; limit {limit})")
        data = h.rfile.read(n) if n else b""
        if len(data) != n:
            raise HttpError(400, "the request body was cut short")
        return data

    def json_body(self, h):
        try:
            v = json.loads(self.body(h, MAX_JSON).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            raise HttpError(400, "malformed JSON body")
        if not isinstance(v, dict):
            raise HttpError(400, "the body must be a JSON object")
        return v

    def ok(self, h, obj=None, code=200):
        out = {"ok": True}
        out.update(obj or {})
        self._reply(h, code, out)

    # --------------------------------------------------------------- routes
    def r_session(self, h, query=""):
        ok, why = self.peer_check(h.client_address[1], self.port)
        if not ok:
            self.log("voice.session.refused", reason=why)
            raise HttpError(403, f"Voice Studio refused this connection: {why}")
        tok = secrets.token_urlsafe(32)
        with self.sessions_lock:
            t = now()
            for k in [k for k, v in self.sessions.items() if t - v["used"] > SESSION_IDLE_S]:
                del self.sessions[k]
            while len(self.sessions) >= MAX_SESSIONS:
                del self.sessions[min(self.sessions, key=lambda k: self.sessions[k]["used"])]
            self.sessions[tok] = {"created": t, "used": t}
        self.log("voice.session.opened")
        self.ok(h, {"session": tok, "idle_timeout_s": SESSION_IDLE_S})

    def r_state(self, h, query=""):
        m = self.model()
        limits = dict(m["limits"])
        profiles = []
        for p in self.store.list_profiles():
            refs = self.store.references_of(p["id"])
            profiles.append(dict(p, references=refs))
        if now() - self._usage["ts"] > 30:
            self._usage = {"ts": now(), "bytes": self.store.usage()}
        g = self.gens.active()
        self.ok(h, {
            "model": {"model_id": self.model_id(), "display_name": m["display_name"],
                      "variant": m["variant"], "sample_rate": m["sample_rate"],
                      "limits": limits, "sampling_defaults": m["sampling_defaults"],
                      "license": {k: m["license"][k] for k in ("name", "notice", "attribution")},
                      "hf_repo": m["source"]["hf_repo"], "hf_revision": m["source"]["hf_revision"]},
            "settings": {k: {"type": v[0].__name__, "min": v[1], "max": v[2],
                             "default": m["sampling_defaults"].get(k, v[3])}
                         for k, v in P.SETTINGS.items()},
            "max_seed": P.MAX_SEED, "max_takes_per_request": MAX_TAKES_PER_REQUEST,
            "max_transcript_chars": P.MAX_TRANSCRIPT_CHARS, "max_upload_bytes": MAX_AUDIO,
            "profiles": profiles, "takes": self.store.list_takes(),
            "generation": g.public() if g else None,
            "store": {"path": self.store.root, "bytes": self._usage["bytes"]},
        })

    def r_profile_create(self, h, query=""):
        b = self.json_body(h)
        rec = self.store.create_profile(b.get("name"), b.get("consent"))
        self.log("voice.profile.created", profile=rec["id"], basis=rec["consent"]["basis"])
        self.ok(h, {"profile": rec}, 201)

    def r_profile_update(self, h, pid, query=""):
        b = self.json_body(h)
        rec = self.store.update_profile(pid, name=b.get("name"),
                                        active_reference_id=b.get("active_reference_id"))
        self.ok(h, {"profile": rec})

    def r_profile_delete(self, h, pid, query=""):
        res = self.store.delete_profile(pid)
        self.log("voice.profile.deleted", profile=pid, references=res["references_deleted"])
        self.ok(h, res)

    def r_reference_add(self, h, pid, query=""):
        q = urllib.parse.parse_qs(query, keep_blank_values=True, max_num_fields=8)

        def one(name, default=None):
            v = q.get(name)
            return v[0] if v else default
        source = one("source")
        if source not in ("recording", "upload"):
            raise HttpError(400, "source must be recording or upload")
        replaces = one("replaces") or None
        self.store.get_profile(pid)
        if replaces:
            self.store.check_id(replaces, "recording id")
        data = self.body(h, MAX_AUDIO, kind="audio")
        if not data:
            raise HttpError(400, "the upload is empty")
        if not self.ingest_lock.acquire(timeout=30):
            raise HttpError(429, "another recording is still being processed; try again")
        try:
            staging = self.store.staging_dir()
            try:
                infile = os.path.join(staging, "upload.bin")
                fd = os.open(infile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                outdir = os.path.join(staging, "out")
                t = now()
                res = self.ingest(infile, outdir, staging)
                rec = self.store.add_reference(pid, outdir, res, source, one("label", ""),
                                               filename=one("filename"), replaces=replaces)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        finally:
            self.ingest_lock.release()
        self.log("voice.reference.added", profile=pid, reference=rec["id"], source=source,
                 bytes=len(data), seconds=rec["model_input"]["seconds"],
                 format=rec["original"]["format"].get("codec"), ingest_s=round(now() - t, 2),
                 warnings=len(rec["warnings"]), replaces=replaces)
        self.ok(h, {"reference": rec}, 201)

    def ingest(self, infile, outdir, staging):
        """Run the audio tool under the network sandbox with the Fish runtime."""
        m = self.model()
        limits = {k: m["limits"][k] for k in ("reference_min_s", "reference_max_s")}
        py = fc.abspath(fc.runtime_of(self.cfg, self.model_id())["python"])
        if not os.access(py, os.X_OK):
            raise HttpError(503, "the Fish runtime is not installed "
                                 "(scripts/install-fish-runtime.sh)")
        argv = ["/usr/bin/sandbox-exec", "-f", fc.SANDBOX_PROFILE, py, "-I", AUDIO_TOOL,
                "ingest", "--input", infile, "--outdir", outdir, "--limits", json.dumps(limits)]
        env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.environ.get("HOME", ""),
               "TMPDIR": staging, "LANG": "en_US.UTF-8", "PYTHONNOUSERSITE": "1",
               "HF_HUB_OFFLINE": "1"}
        try:
            p = subprocess.run(argv, capture_output=True, timeout=180, env=env, cwd=fc.ROOT)
        except subprocess.TimeoutExpired:
            raise HttpError(504, "processing the recording took too long")
        lines = p.stdout.decode("utf-8", "replace").strip().splitlines()
        out = parse_json(lines[-1]) if lines else None
        if p.returncode == 0 and out and out.get("ok"):
            return out
        if p.returncode == 2 and out and out.get("error"):
            raise HttpError(422, out["error"])
        err = p.stderr.decode("utf-8", "replace").strip().splitlines()
        self.log("voice.ingest.crashed", rc=p.returncode, detail=(err[-1] if err else "")[:200])
        raise HttpError(500, "the recording could not be processed (the audio tool failed; "
                             "see logs/control/voice.log)")

    def r_reference_update(self, h, rid, query=""):
        b = self.json_body(h)
        unknown = set(b) - {"label", "transcript"}
        if unknown:
            raise HttpError(400, "unsupported fields: " + ", ".join(sorted(unknown)))
        rec = self.store.update_reference(rid, label=b.get("label"),
                                          transcript=b.get("transcript"))
        self.log("voice.reference.updated", reference=rid, fields=sorted(b))
        self.ok(h, {"reference": rec})

    def r_reference_delete(self, h, rid, query=""):
        res = self.store.delete_reference(rid)
        self.log("voice.reference.deleted", reference=rid)
        self.ok(h, res)

    def r_reference_audio(self, h, rid, query=""):
        kind = (urllib.parse.parse_qs(query).get("kind") or ["original"])[0]
        if kind not in ("original", "model"):
            raise HttpError(400, "kind must be original or model")
        path, rec = self.store.reference_file(rid, kind)
        ext = path.rsplit(".", 1)[-1]
        with open(path, "rb") as f:
            data = f.read()
        self.send(h, 200, data, AUDIO_TYPES.get(ext, "application/octet-stream"), {
            "Content-Disposition": f'attachment; filename="recording-{rid}-{kind}.{ext}"'})

    def r_generate(self, h, query=""):
        b = self.json_body(h)
        allowed = {"profile_id", "reference_ids", "text", "settings", "count", "progressive",
                   "regenerated_from"}
        unknown = set(b) - allowed
        if unknown:
            raise HttpError(400, "unsupported fields: " + ", ".join(sorted(unknown)))
        m = self.model()
        prof = self.store.get_profile(self.store.check_id(b.get("profile_id"), "profile id"))
        ids = b.get("reference_ids")
        if not isinstance(ids, list) or not ids:
            raise HttpError(400, "choose at least one recording")
        if len(ids) > int(m["limits"]["max_references"]) or len(set(ids)) != len(ids):
            raise HttpError(400, f"choose at most {m['limits']['max_references']} different "
                                 f"recordings")
        refs = []
        for rid in ids:
            r = self.store.get_reference(self.store.check_id(rid, "recording id"))
            if r["profile_id"] != prof["id"]:
                raise HttpError(400, "that recording belongs to another voice profile")
            if not (r.get("transcript") or "").strip():
                raise HttpError(400, f"\"{r['label']}\" has no transcript yet; type exactly "
                                     f"what is said in it first")
            path, _ = self.store.reference_file(rid, "model")
            refs.append({"id": rid, "label": r["label"], "transcript": r["transcript"],
                         "model_input_sha256": r["model_input"]["sha256"],
                         "original_sha256": r["original"]["sha256"],
                         "seconds": r["model_input"]["seconds"], "source": r["source"],
                         "path": path})
        count = b.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or \
                not 1 <= count <= MAX_TAKES_PER_REQUEST:
            raise HttpError(400, f"count must be 1 to {MAX_TAKES_PER_REQUEST}")
        progressive = b.get("progressive", False)
        if not isinstance(progressive, bool):
            raise HttpError(400, "progressive must be true or false")
        regen = b.get("regenerated_from")
        if regen is not None:
            self.store.check_id(regen, "take id")
        try:
            job = P.validate_job({"text": b.get("text"), "settings": b.get("settings"),
                                  "references": [{"sha256": r["model_input_sha256"],
                                                  "transcript": r["transcript"]} for r in refs]},
                                 m["limits"], m["sampling_defaults"])
        except P.RequestError as e:
            raise HttpError(400, str(e))
        if not self.engine().health()["ok"]:
            raise HttpError(409, "Fish is not running. Start it at the top of this page, then "
                                 "try again.", code="fish_not_ready")
        request = {"text": job["text"], "settings": job["settings"], "references": ids,
                   "refs_full": refs, "profile": {"id": prof["id"], "name": prof["name"]},
                   "regenerated_from": regen}
        g = self.gens.start(request, count, progressive)
        self.ok(h, {"generation": g.public()}, 202)

    def r_generation_get(self, h, gid, query=""):
        self.ok(h, {"generation": self.gens.get(gid).public()})

    def r_generation_pcm(self, h, gid, query=""):
        g = self.gens.get(gid)
        mm = re.match(r"^from=(\d{1,12})$", query or "from=0")
        if not mm:
            raise HttpError(400, "from must be a sample offset")
        jid = g.first_engine_job
        if not jid:
            return self.send(h, 200, b"", "application/octet-stream",
                             {"X-Job-State": g.state, "X-Samples-Available": "0"})
        try:
            st, headers, data = self.engine().pcm(jid, int(mm.group(1)))
        except EngineDown:
            raise HttpError(409, "Fish is not running")
        if st != 200:
            raise HttpError(409, "progressive audio is no longer available for this take")
        self.send(h, 200, data, "application/octet-stream", {
            "X-Job-State": headers.get("x-job-state", ""),
            "X-Samples-Available": headers.get("x-samples-available", "0"),
            "X-Sample-Rate": headers.get("x-sample-rate", "")})

    def r_generation_cancel(self, h, gid, query=""):
        g = self.gens.get(gid)
        self.gens.cancel(g, "cancelled by the operator")
        self.log("voice.generation.cancel", generation=gid)
        self.ok(h, {"generation": g.public()})

    def r_take_audio(self, h, tid, query=""):
        rec = self.store.get_take(tid)
        with open(self.store.take_file(tid), "rb") as f:
            data = f.read()
        self.send(h, 200, data, "audio/wav", {
            "Content-Disposition": f'attachment; filename="voice-take-{int(rec["number"])}.wav"'})

    def r_take_update(self, h, tid, query=""):
        b = self.json_body(h)
        unknown = set(b) - {"note", "starred"}
        if unknown:
            raise HttpError(400, "unsupported fields: " + ", ".join(sorted(unknown)))
        self.ok(h, {"take": self.store.update_take(tid, note=b.get("note"),
                                                   starred=b.get("starred"))})

    def r_take_delete(self, h, tid, query=""):
        res = self.store.delete_take(tid)
        self.log("voice.take.deleted", take=tid)
        self.ok(h, res)

    def r_fish_status(self, h, query=""):
        self.ok(h, {"fish": self.summary()})

    def r_fish_op(self, h, verb, query=""):
        if verb == "stop":
            inflight = self.ops.request_cancel("panel")
            if inflight:
                ok = self.ops.cancel_inflight(inflight)
                return self.ok(h, {"operation": "cancel", "cancelling": inflight,
                                   "cancel_recorded": ok,
                                   "note": "the start is being cancelled; Fish will be stopped"},
                               202)
        if self.ops.run(verb):
            return self.ok(h, {"operation": verb}, 202)
        raise HttpError(409, "another Fish operation is in progress",
                        operation=self.ops.current)


def route_name(method, path):
    for m, pattern, name, _ in ROUTES:
        if m == method and re.match(pattern, path):
            return name
    return "static" if path in STATIC else "unknown"
