"""Speech jobs: one worker thread, bounded queue, cancellation (docs/18).

Standard library only. Every model call (load, reference encoding, generation)
runs on the single worker thread, as MLX work should. The synthesiser is
injected, so this module is unit-tested with a fake one.

A job's life: queued -> running -> done | failed | cancelled. Cancellation is
cooperative: the synthesiser asks should_cancel() between frames, so a cancel
takes effect within one frame (~40 ms). The same check enforces a wall-clock
limit per job, so nothing runs unbounded. Finished jobs keep their audio for a
retention period and are then dropped; the caller (Voice Studio) stores takes.
"""
import math
import queue
import secrets
import threading
import time

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
FINISHED = (DONE, FAILED, CANCELLED)


class QueueFull(Exception):
    pass


class JobCancelled(Exception):
    """Raised by a synthesiser when should_cancel() said stop."""


def job_time_limit(request, frame_floor_fps=8.0, base_s=45.0):
    """Wall-clock ceiling for one job: generous, but finite. Assumes a frame
    rate far below anything measured (8 frames/s) for every chunk the text
    can produce, plus a fixed allowance for prefill and decoding."""
    s = request["settings"]
    nbytes = len(request["text"].encode("utf-8"))
    chunks = 1 if s.get("single_chunk") else max(1, math.ceil(nbytes / s["chunk_bytes"]) + 1)
    return base_s + chunks * (s["max_frames_per_chunk"] / frame_floor_fps + 10.0)


class Job:
    def __init__(self, request):
        self.id = secrets.token_hex(8)
        self.request = request
        self.state = QUEUED
        self.created_at = time.time()
        self.started_at = None
        self.finished_at = None
        self.progress = {"chunk": 0, "chunks": None, "frames": 0, "audio_seconds": 0.0}
        self.result = None           # metadata from the synthesiser
        self.audio_wav = None        # final take, WAV bytes
        self.segments = []           # progressive PCM16LE segments
        self.samples_available = 0
        self.error = None
        self.cancel_requested = False
        self.cancel_reason = None
        self.deadline = None
        self.lock = threading.Lock()

    def public(self):
        with self.lock:
            return {"id": self.id, "state": self.state, "created_at": self.created_at,
                    "started_at": self.started_at, "finished_at": self.finished_at,
                    "progress": dict(self.progress), "result": self.result,
                    "error": self.error, "cancel_reason": self.cancel_reason,
                    "samples_available": self.samples_available,
                    "progressive": bool(self.request.get("progressive")),
                    "client_ref": self.request.get("client_ref"),
                    "has_audio": self.audio_wav is not None}


class JobManager:
    """run_job(job, should_cancel, on_progress, on_segment) -> (result, wav)."""

    def __init__(self, run_job, max_queue=4, keep_finished=16, retention_s=900.0,
                 time_limit=job_time_limit, clock=time.time):
        self.run_job = run_job
        self.max_queue = max_queue
        self.keep_finished = keep_finished
        self.retention_s = retention_s
        self.time_limit = time_limit
        self.clock = clock
        self.jobs = {}
        self.current = None
        self._tasks = queue.Queue()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._worker, name="fish-worker", daemon=True)
        self._thread.start()

    # ---------------------------------------------------------- queueing
    def submit(self, request):
        with self._lock:
            self._prune()
            pending = sum(1 for j in self.jobs.values() if j.state in (QUEUED, RUNNING))
            if pending >= self.max_queue:
                raise QueueFull(f"{pending} jobs are already queued or running")
            job = Job(request)
            self.jobs[job.id] = job
        self._tasks.put(("job", job))
        return job

    def call(self, fn, timeout=None):
        """Run fn() on the worker thread (between jobs) and return its result."""
        done = queue.Queue()
        self._tasks.put(("call", (fn, done)))
        try:
            ok, value = done.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("the speech worker did not answer in time")
        if not ok:
            raise value
        return value

    def get(self, job_id):
        with self._lock:
            return self.jobs.get(job_id)

    def cancel(self, job_id, reason="cancelled by request"):
        job = self.get(job_id)
        if job is None:
            return None
        with job.lock:
            if job.state in FINISHED:
                return job
            job.cancel_requested = True
            job.cancel_reason = reason
            if job.state == QUEUED:
                job.state = CANCELLED
                job.finished_at = self.clock()
        return job

    def cancel_all(self, reason):
        for job_id in list(self.jobs):
            self.cancel(job_id, reason)

    def busy(self):
        return self.current is not None

    def counts(self):
        with self._lock:
            out = {}
            for j in self.jobs.values():
                out[j.state] = out.get(j.state, 0) + 1
            return out

    def stop(self):
        self._stop.set()
        self.cancel_all("engine stopping")
        self._tasks.put(("stop", None))

    # ------------------------------------------------------------ worker
    def _prune(self):
        now = self.clock()
        finished = sorted((j for j in self.jobs.values() if j.state in FINISHED),
                          key=lambda j: j.finished_at or 0)
        for j in finished:
            if now - (j.finished_at or now) > self.retention_s:
                del self.jobs[j.id]
        finished = [j for j in finished if j.id in self.jobs]
        for j in finished[:max(0, len(finished) - self.keep_finished)]:
            del self.jobs[j.id]

    def _worker(self):
        while not self._stop.is_set():
            kind, item = self._tasks.get()
            if kind == "stop":
                return
            if kind == "call":
                fn, done = item
                try:
                    done.put((True, fn()))
                except Exception as e:      # surfaced to the caller
                    done.put((False, e))
                continue
            self._run(item)

    def _run(self, job):
        with job.lock:
            if job.state != QUEUED:          # cancelled while queued
                return
            job.state = RUNNING
            job.started_at = self.clock()
            job.deadline = job.started_at + self.time_limit(job.request)
        self.current = job

        def should_cancel():
            if job.cancel_requested:
                return True
            if self.clock() > job.deadline:
                job.cancel_requested = True
                job.cancel_reason = (f"time limit of {job.deadline - job.started_at:.0f} s "
                                     f"exceeded")
                return True
            return False

        def on_progress(**fields):
            with job.lock:
                job.progress.update(fields)

        def on_segment(pcm16_bytes, nsamples):
            with job.lock:
                job.segments.append(pcm16_bytes)
                job.samples_available += nsamples

        try:
            result, wav = self.run_job(job, should_cancel, on_progress, on_segment)
            with job.lock:
                job.result = result
                job.audio_wav = wav
                job.state = DONE
        except JobCancelled:
            with job.lock:
                job.state = CANCELLED
                timed_out = (job.cancel_reason or "").startswith("time limit")
                if timed_out:
                    job.state = FAILED
                    job.error = {"kind": "timeout", "message": job.cancel_reason}
        except Exception as e:               # a failed job never kills the worker
            with job.lock:
                job.state = FAILED
                job.error = {"kind": type(e).__name__, "message": str(e)[:500]}
        finally:
            with job.lock:
                job.finished_at = self.clock()
            self.current = None

    # ---------------------------------------------------------- audio out
    def pcm_since(self, job_id, from_sample):
        """Progressive PCM16LE after sample offset from_sample, plus state."""
        job = self.get(job_id)
        if job is None:
            return None
        with job.lock:
            data = b"".join(job.segments)
            return {"state": job.state, "samples_available": job.samples_available,
                    "pcm": data[from_sample * 2:]}
