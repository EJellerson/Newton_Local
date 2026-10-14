#!/usr/bin/env python3
"""Newton_Local — Fish speech engine: the local TTS service (docs/18).

Launched by the com.newton.fish LaunchAgent through `fishctl.py exec-engine`
(which gates and then execs this under the network sandbox). Serves an
internal API on 127.0.0.1 only:

    GET  /health                      no auth; loading | ready | failed | stopping
    GET  /v1/identity                 the loaded model, runtime and process
    GET  /v1/status                   state, memory, queue, timings
    PUT  /v1/references/<sha256>      44.1 kHz mono WAV -> encoded once, cached
    GET  /v1/references/<sha256>      200 if cached, else 404
    POST /v1/jobs                     queue a take (202), 409 if a reference is
                                      not cached, 429 if the queue is full
    GET  /v1/jobs/<id>                state, progress, timings, flags
    GET  /v1/jobs/<id>/audio          the finished take (WAV)
    GET  /v1/jobs/<id>/pcm?from=N     progressive PCM16 after sample N
    POST /v1/jobs/<id>/cancel

Every /v1 request needs the bearer token that fishctl writes (0600) before
each start, a loopback Host header and no Origin header, so a browser page
can never drive it. Requests never carry filesystem paths. Logs record
sizes, counts and timings, never text, transcripts or audio.
"""
import argparse
import hmac
import json
import os
import re
import resource
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import fish_config as fc  # noqa: E402
from fish_tts import jobs as J  # noqa: E402
from fish_tts.protocol import RequestError, SHA256_RE, validate_job  # noqa: E402

MAX_REFERENCE_BODY = 16 << 20
MAX_JSON_BODY = 64 << 10
REFERENCE_CACHE = 32
STATE = {"state": "starting", "error": None, "started_at": time.time()}
GIB = 1 << 30


def log(event, **fields):
    rec = {"ts": round(time.time(), 3), "event": "engine." + event}
    rec.update(fields)
    sys.stdout.write(json.dumps(rec) + "\n")
    sys.stdout.flush()


def network_sandbox_selftest():
    """'enforced' when an outbound connect is refused by the sandbox (EPERM),
    without sending a packet anywhere useful (TEST-NET-1, RFC 5737)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(("192.0.2.1", 9))
        return "NOT enforced (connected)"
    except PermissionError:
        return "enforced"
    except OSError as e:
        return f"NOT enforced ({type(e).__name__})"
    finally:
        s.close()


class Engine:
    def __init__(self, cfg, model_id, token, generation, fake=False):
        self.cfg = cfg
        self.model_id = model_id
        self.model = fc.model(cfg, model_id)
        self.token = token
        self.generation = generation
        self.fake = fake
        self.synth = None
        self.refs = {}                   # sha256 -> {"codes", "seconds", "frames", "used"}
        self.refs_lock = threading.Lock()
        self.limits = dict(self.model["limits"])
        self.jobs = J.JobManager(self._run_job)
        self.identity = None
        self.sandbox = network_sandbox_selftest()

    # ------------------------------------------------------------- loading
    def load(self):
        STATE["state"] = "loading"
        t0 = time.time()
        if self.fake:
            from fish_tts.fake_synth import FakeSynthesizer
            self.synth = FakeSynthesizer(self.cfg, self.model_id)
        else:
            from fish_tts.adapter_fish_omni import FishOmniSynthesizer
            self.synth = FishOmniSynthesizer(fc.abspath(self.model["path"]))
        self.jobs.call(self.synth.load)
        self.jobs.call(self.synth.warmup)
        rt = fc.runtime_of(self.cfg, self.model_id)
        rec = fc.read_install_record(self.cfg, self.model["runtime"]) or {}
        pk = rec.get("packages") or {}
        self.identity = {
            "service": "newton-fish", "model_id": self.model_id,
            "display_name": self.model["display_name"], "variant": self.model["variant"],
            "path": os.path.realpath(fc.abspath(self.model["path"])),
            "manifest_sha256": fc.manifest_sha256(self.cfg, self.model_id),
            "hf_repo": self.model["source"]["hf_repo"],
            "hf_revision": self.model["source"]["hf_revision"],
            "adapter": rt["adapter"], "runtime_commit": rec.get("mlx_audio_commit"),
            "mlx": pk.get("mlx"), "mlx_audio": pk.get("mlx-audio"),
            "python": sys.version.split()[0], "pid": os.getpid(),
            "generation": self.generation, "started_at": STATE["started_at"],
            "sample_rate": self.synth.sample_rate, "fake": self.fake,
            "network_sandbox": self.sandbox, "license": self.model["license"]["name"],
            "attribution": self.model["license"]["attribution"],
        }
        info = dict(self.synth.load_info)
        info["ready_s"] = round(time.time() - t0, 2)
        STATE["load"] = info
        STATE["state"] = "ready"
        log("ready", model=self.model_id, generation=self.generation, **info)

    # ---------------------------------------------------------- references
    def put_reference(self, sha, body):
        import hashlib
        if hashlib.sha256(body).hexdigest() != sha:
            raise RequestError("body does not match its sha256")
        from fish_tts.wavio import WavError, read_wav
        sr = self.synth.sample_rate
        lim = self.limits
        try:
            x, rate, info = read_wav(body, max_samples=int((lim["reference_max_s"] + 1) * sr))
        except WavError as e:
            raise RequestError(f"reference audio: {e}")
        if rate != sr or info["channels"] != 1:
            raise RequestError(f"reference audio must be {sr} Hz mono (got {rate} Hz, "
                               f"{info['channels']} channels); Voice Studio prepares it")
        seconds = x.shape[0] / sr
        if not lim["reference_min_s"] <= seconds <= lim["reference_max_s"]:
            raise RequestError(f"reference is {seconds:.1f} s; it must be "
                               f"{lim['reference_min_s']:.0f}-{lim['reference_max_s']:.0f} s")
        t = time.perf_counter()
        codes = self.jobs.call(lambda: self.synth.encode_reference(x[:, 0]), timeout=120)
        rec = {"codes": codes, "seconds": round(seconds, 3), "frames": int(codes.shape[1]),
               "encode_s": round(time.perf_counter() - t, 3), "used": time.time()}
        with self.refs_lock:
            self.refs[sha] = rec
            while len(self.refs) > REFERENCE_CACHE:
                oldest = min(self.refs, key=lambda k: self.refs[k]["used"])
                del self.refs[oldest]
        log("reference.encoded", seconds=rec["seconds"], frames=rec["frames"],
            encode_s=rec["encode_s"])
        return {k: v for k, v in rec.items() if k not in ("codes", "used")}

    def has_reference(self, sha):
        with self.refs_lock:
            return sha in self.refs

    # ---------------------------------------------------------------- jobs
    def submit(self, body):
        req = validate_job(body, self.limits, self.model["sampling_defaults"])
        missing = [r["sha256"] for r in req["references"] if not self.has_reference(r["sha256"])]
        if missing:
            return None, missing
        job = self.jobs.submit(req)
        log("job.queued", job=job.id, text_chars=len(req["text"]),
            references=len(req["references"]), progressive=bool(req["progressive"]))
        return job, []

    def _run_job(self, job, should_cancel, on_progress, on_segment):
        from fish_tts.wavio import pcm16_bytes, write_wav
        codes = []
        with self.refs_lock:
            for r in job.request["references"]:
                rec = self.refs.get(r["sha256"])
                if rec is None:
                    raise RuntimeError("a reference was evicted from the cache; resubmit")
                rec["used"] = time.time()
                codes.append(rec["codes"])

        def seg(audio):
            on_segment(pcm16_bytes(audio), int(len(audio)))
        log("job.start", job=job.id)
        meta, audio = self.synth.synthesize(job.request, codes, should_cancel, on_progress,
                                            on_segment=seg if job.request["progressive"] else None)
        t = time.perf_counter()
        wav = write_wav(audio, self.synth.sample_rate, bits=16)
        meta["wav_encode_s"] = round(time.perf_counter() - t, 4)
        meta["flags"] = flags_for(job.request, meta)
        meta["model_id"] = self.model_id
        meta["generation"] = self.generation
        log("job.done", job=job.id, audio_s=meta["audio_seconds"], frames=meta["frames"],
            synthesis_s=meta["synthesis_s"], first_frame_s=meta["first_frame_s"],
            first_audio_s=meta["first_audio_s"], flags=meta["flags"])
        return meta, wav

    def status(self):
        mem = self.synth.memory() if self.synth and hasattr(self.synth, "memory") else {}
        mem["process_peak_rss_gib"] = round(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / GIB, 2)
        cur = self.jobs.current
        return {"state": STATE["state"], "busy": self.jobs.busy(),
                "current_job": cur.public() if cur else None,
                "jobs": self.jobs.counts(), "memory": mem, "load": STATE.get("load"),
                "references_cached": len(self.refs), "error": STATE.get("error"),
                "uptime_s": round(time.time() - STATE["started_at"], 1),
                "model_id": self.model_id, "generation": self.generation}


def flags_for(request, meta):
    """Operator-facing warnings about a take. A chunk that never produced EOS
    is a definite failure to terminate; the rate checks are heuristics."""
    flags = []
    for c in meta["chunks"]:
        if c.get("empty"):
            flags.append({"kind": "empty_chunk", "chunk": c["index"],
                          "message": "the model ended a chunk before producing audio"})
        elif not c["eos"]:
            flags.append({"kind": "no_eos", "chunk": c["index"],
                          "message": f"chunk {c['index'] + 1} did not end by itself and was cut "
                                     f"at {c['frames']} frames: it may run on or be truncated"})
    words = max(1, len(re.findall(r"[^\s\[\]<>|]+", re.sub(r"\[[^\]]*\]|<\|[^|]*\|>", " ",
                                                             request["text"]))))
    dur = meta["audio_seconds"]
    if dur > 0:
        wps = words / dur
        if wps < 0.8:
            flags.append({"kind": "slow", "message": f"{wps:.1f} words/s is unusually slow for "
                          f"this text: it may have run on past the script (listen)"})
        elif wps > 5.5:
            flags.append({"kind": "fast", "message": f"{wps:.1f} words/s is unusually fast: "
                          f"words may be missing (listen)"})
    return flags


# ----------------------------------------------------------------- HTTP
ENGINE = None
PORT = None
JOB_PATH = re.compile(r"^/v1/jobs/([0-9a-f]{16})(/audio|/pcm|/cancel)?$")
REF_PATH = re.compile(r"^/v1/references/([0-9a-f]{64})$")


class Handler(BaseHTTPRequestHandler):
    server_version = "NewtonFish/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(data)

    def _gate(self, auth=True):
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            self._send(403, {"error": "bad host header"})
            return False
        if self.headers.get("Origin") is not None:
            self._send(403, {"error": "browser requests are not accepted"})
            return False
        if auth:
            got = self.headers.get("Authorization", "")
            want = f"Bearer {ENGINE.token}" if ENGINE else ""
            if not ENGINE or not hmac.compare_digest(got.encode(), want.encode()):
                self._send(401, {"error": "unauthorized"})
                return False
            if STATE["state"] != "ready":
                self._send(503, {"error": f"engine is {STATE['state']}", "state": STATE["state"]})
                return False
        return True

    def _body(self, limit):
        if self.headers.get("Transfer-Encoding"):
            raise RequestError("chunked bodies are not accepted")
        try:
            n = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            n = -1
        if n < 0:
            raise RequestError("Content-Length required")
        if n > limit:
            raise RequestError(f"body is {n} bytes; the limit is {limit}")
        return self.rfile.read(n)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/health":
            if not self._gate(auth=False):
                return
            busy = bool(ENGINE and ENGINE.jobs.busy())
            ok = STATE["state"] == "ready"
            return self._send(200 if ok else 503, {
                "ok": ok, "state": STATE["state"], "busy": busy,
                "model_id": ENGINE.model_id if ENGINE else None,
                "generation": ENGINE.generation if ENGINE else None,
                "error": STATE.get("error")})
        if not self._gate():
            return
        if path == "/v1/identity":
            return self._send(200, ENGINE.identity)
        if path == "/v1/status":
            return self._send(200, ENGINE.status())
        m = REF_PATH.match(path)
        if m:
            return self._send(200 if ENGINE.has_reference(m.group(1)) else 404,
                              {"cached": ENGINE.has_reference(m.group(1))})
        m = JOB_PATH.match(path)
        if m:
            job = ENGINE.jobs.get(m.group(1))
            if job is None:
                return self._send(404, {"error": "no such job"})
            if m.group(2) is None:
                return self._send(200, job.public())
            if m.group(2) == "/audio":
                if job.audio_wav is None:
                    return self._send(409, {"error": f"job is {job.state}", "state": job.state})
                return self._send(200, job.audio_wav, "audio/wav")
            if m.group(2) == "/pcm":
                mm = re.match(r"^from=(\d{1,12})$", query or "from=0")
                if not mm:
                    return self._send(400, {"error": "from must be a sample offset"})
                got = ENGINE.jobs.pcm_since(job.id, int(mm.group(1)))
                return self._send(200, got["pcm"], "application/octet-stream", {
                    "X-Job-State": got["state"], "X-Samples-Available": got["samples_available"],
                    "X-Sample-Rate": ENGINE.synth.sample_rate})
        return self._send(404, {"error": "not found"})

    def do_PUT(self):
        if not self._gate():
            return
        m = REF_PATH.match(self.path)
        if not m:
            return self._send(404, {"error": "not found"})
        try:
            body = self._body(MAX_REFERENCE_BODY)
            return self._send(200, ENGINE.put_reference(m.group(1), body))
        except RequestError as e:
            return self._send(400, {"error": str(e)})
        except TimeoutError as e:
            return self._send(503, {"error": str(e)})

    def do_POST(self):
        if not self._gate():
            return
        if self.path == "/v1/jobs":
            try:
                body = json.loads(self._body(MAX_JSON_BODY).decode("utf-8") or "{}")
                job, missing = ENGINE.submit(body)
            except (RequestError, ValueError, UnicodeDecodeError) as e:
                return self._send(400, {"error": str(e)})
            except J.QueueFull as e:
                return self._send(429, {"error": str(e)})
            if missing:
                return self._send(409, {"error": "references not cached; PUT them first",
                                        "missing_references": missing})
            return self._send(202, job.public())
        m = JOB_PATH.match(self.path)
        if m and m.group(2) == "/cancel":
            job = ENGINE.jobs.cancel(m.group(1))
            if job is None:
                return self._send(404, {"error": "no such job"})
            log("job.cancel", job=job.id)
            return self._send(200, job.public())
        return self._send(404, {"error": "not found"})


def read_token(path):
    st = os.stat(path)
    if st.st_mode & 0o077:
        raise SystemExit(f"refusing to start: {path} must not be readable by others")
    with open(path) as f:
        rec = json.load(f)
    tok = rec.get("token", "")
    if not re.match(r"^[0-9a-f]{64}$", tok):
        raise SystemExit(f"refusing to start: {path} holds no valid token")
    return tok, rec.get("generation"), rec.get("model_id")


def main():
    global ENGINE, PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--fake-model", action="store_true",
                    help="fixture proofs only: no model, synthetic audio")
    a = ap.parse_args()
    cfg = fc.load_config(a.config)
    if a.fake_model and not cfg.get("fixture"):
        raise SystemExit("refusing to start: --fake-model needs a fixture configuration")
    p = fc.paths(cfg)
    model_id, source = fc.read_selection(cfg)
    token, generation, token_model = read_token(p["token"])
    if token_model and token_model != model_id:
        raise SystemExit(f"refusing to start: the controller prepared {token_model} "
                         f"but the selection is {model_id}")
    PORT = int(cfg["service"]["port"])
    host = cfg["service"]["host"]
    try:
        srv = ThreadingHTTPServer((host, PORT), Handler)
    except OSError as e:
        raise SystemExit(f"error: could not bind {host}:{PORT}: {e}")
    srv.daemon_threads = True
    ENGINE = Engine(cfg, model_id, token, generation, fake=a.fake_model)
    log("start", pid=os.getpid(), model=model_id, selection=source, generation=generation,
        port=PORT, network_sandbox=ENGINE.sandbox)

    def on_term(signum, frame):
        STATE["state"] = "stopping"
        log("stop", signal=signum)
        ENGINE.jobs.stop()
        sys.stdout.flush()
        os._exit(0)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    threading.Thread(target=srv.serve_forever, name="http", daemon=True).start()
    try:
        ENGINE.load()
    except Exception as e:
        STATE["state"] = "failed"
        STATE["error"] = f"{type(e).__name__}: {e}"[:500]
        sys.stderr.write(f"error: model load failed: {STATE['error']}\n")
        sys.stderr.flush()
        log("load_failed", error=STATE["error"])
        os._exit(70)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
