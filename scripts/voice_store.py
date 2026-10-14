#!/usr/bin/env python3
"""Newton_Local — Voice Studio's private store (docs/18 §5).

Voice profiles, reference recordings with their transcripts, and generated
takes, kept outside the repository (config/fish.json service.store; by
default ~/Library/Application Support/Newton_Local/voice-studio):

    store.json                       next take number
    profiles/<id>.json               name, consent attestation, references
    references/<id>/reference.json   label, transcript, formats, analysis
    references/<id>/original.<ext>   the upload or recording, byte for byte
    references/<id>/model-input.wav  the derived 44.1 kHz mono model input
    takes/<id>/take.json             text, settings, seed, model, reference
    takes/<id>/audio.wav             the generated take (immutable)
    tmp/                             staging for uploads, emptied at start

Rules: directories 0700 and files 0600; Spotlight indexing disabled; every
id is server-generated (16 hex digits) and validated before it touches a
path, and every resolved path must stay inside the store; filenames from
uploads are display text only. Mutations serialise on one flock. A take is
never overwritten: each generation gets a new id. Standard library only;
Python 3.9-compatible. Nothing here logs transcripts, text or audio.
"""
import fcntl
import json
import os
import re
import secrets
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fish_tts.protocol import RequestError, check_text  # noqa: E402  (stdlib only)

ID_RE = re.compile(r"^[0-9a-f]{16}$")
CONSENT_BASES = ("own_voice", "documented_permission")
MAX_NAME = 60
MAX_LABEL = 80
MAX_NOTE = 500
MAX_TRANSCRIPT = 1000


class StoreError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def new_id():
    return secrets.token_hex(8)


def iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts or time.time()))


def clean_text(value, what, max_len, allow_empty=False):
    if not isinstance(value, str):
        raise StoreError(f"{what} must be text")
    v = value.strip()
    if not v and not allow_empty:
        raise StoreError(f"{what} must not be empty")
    if len(v) > max_len:
        raise StoreError(f"{what} is longer than {max_len} characters")
    if any(ord(c) < 32 and c not in "\n\t" for c in v):
        raise StoreError(f"{what} contains control characters")
    return v


class VoiceStore:
    def __init__(self, root):
        self.root = os.path.realpath(os.path.expanduser(root))

    # ---------------------------------------------------------- layout
    def path(self, *parts):
        """Absolute path inside the store; refuses anything that escapes it."""
        for p in parts:
            if not isinstance(p, str) or p in ("", ".", "..") or "/" in p or "\x00" in p:
                raise StoreError("invalid path component", 400)
        full = os.path.realpath(os.path.join(self.root, *parts))
        if not (full == self.root or full.startswith(self.root + os.sep)):
            raise StoreError("path escapes the store", 400)
        return full

    def init(self):
        old = os.umask(0o077)
        try:
            for d in ("", "profiles", "references", "takes", "tmp"):
                p = os.path.join(self.root, d) if d else self.root
                os.makedirs(p, mode=0o700, exist_ok=True)
                os.chmod(p, 0o700)
            marker = os.path.join(self.root, ".metadata_never_index")
            if not os.path.exists(marker):
                open(marker, "w").close()
            for name in os.listdir(self.path("tmp")):          # stale staging
                shutil.rmtree(os.path.join(self.root, "tmp", name), ignore_errors=True)
            if not os.path.exists(os.path.join(self.root, "store.json")):
                self._write(os.path.join(self.root, "store.json"),
                            {"schema": 1, "next_take_number": 1, "created_at": iso()})
        finally:
            os.umask(old)
        return self

    def lock(self):
        return _Flock(os.path.join(self.root, "store.lock"))

    @staticmethod
    def _write(path, obj):
        tmp = f"{path}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)

    @staticmethod
    def _read(path):
        try:
            with open(path) as f:
                v = json.load(f)
            return v if isinstance(v, dict) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def check_id(value, what="id"):
        if not isinstance(value, str) or not ID_RE.match(value):
            raise StoreError(f"invalid {what}", 404)
        return value

    def staging_dir(self):
        d = self.path("tmp", new_id())
        os.makedirs(d, mode=0o700)
        return d

    # --------------------------------------------------------- profiles
    def profile_path(self, pid):
        return self.path("profiles", self.check_id(pid, "profile id") + ".json")

    def get_profile(self, pid):
        rec = self._read(self.profile_path(pid))
        if rec is None:
            raise StoreError("no such voice profile", 404)
        return rec

    def list_profiles(self):
        out = []
        for name in sorted(os.listdir(self.path("profiles"))):
            if name.endswith(".json") and ID_RE.match(name[:-5]):
                rec = self._read(self.path("profiles", name))
                if rec:
                    out.append(rec)
        return sorted(out, key=lambda r: r.get("created_at") or "")

    def create_profile(self, name, consent):
        name = clean_text(name, "profile name", MAX_NAME)
        if not isinstance(consent, dict) or consent.get("attested") is not True:
            raise StoreError("confirm that this is your own voice, or that you have the "
                             "speaker's documented permission")
        basis = consent.get("basis")
        if basis not in CONSENT_BASES:
            raise StoreError("consent basis must be own_voice or documented_permission")
        note = clean_text(consent.get("note") or "", "consent note", MAX_NOTE, allow_empty=True)
        if basis == "documented_permission" and not note:
            raise StoreError("describe the documented permission (who granted it, and where "
                             "the record is kept)")
        with self.lock():
            if any(p["name"].lower() == name.lower() for p in self.list_profiles()):
                raise StoreError("a voice profile with that name already exists", 409)
            rec = {"schema": 1, "id": new_id(), "name": name, "created_at": iso(),
                   "updated_at": iso(), "active_reference_id": None, "reference_ids": [],
                   "consent": {"basis": basis, "attested": True, "note": note,
                               "attested_at": iso()}}
            self._write(self.profile_path(rec["id"]), rec)
        return rec

    def update_profile(self, pid, name=None, active_reference_id=None):
        with self.lock():
            rec = self.get_profile(pid)
            if name is not None:
                name = clean_text(name, "profile name", MAX_NAME)
                if any(p["id"] != pid and p["name"].lower() == name.lower()
                       for p in self.list_profiles()):
                    raise StoreError("a voice profile with that name already exists", 409)
                rec["name"] = name
            if active_reference_id is not None:
                self.check_id(active_reference_id, "reference id")
                if active_reference_id not in rec["reference_ids"]:
                    raise StoreError("that recording does not belong to this profile")
                rec["active_reference_id"] = active_reference_id
            rec["updated_at"] = iso()
            self._write(self.profile_path(pid), rec)
        return rec

    def delete_profile(self, pid):
        """The profile and its recordings. Takes stay (they carry snapshots)."""
        with self.lock():
            rec = self.get_profile(pid)
            for rid in rec["reference_ids"]:
                shutil.rmtree(self.path("references", self.check_id(rid)), ignore_errors=True)
            os.remove(self.profile_path(pid))
        return {"deleted": pid, "references_deleted": len(rec["reference_ids"])}

    # ------------------------------------------------------- references
    def ref_dir(self, rid):
        return self.path("references", self.check_id(rid, "reference id"))

    def get_reference(self, rid):
        rec = self._read(os.path.join(self.ref_dir(rid), "reference.json"))
        if rec is None:
            raise StoreError("no such recording", 404)
        return rec

    def references_of(self, pid):
        out = []
        for rid in self.get_profile(pid)["reference_ids"]:
            try:
                out.append(self.get_reference(rid))
            except StoreError:
                continue
        return out

    def add_reference(self, pid, staging, ingest, source, label, filename=None,
                      replaces=None):
        """Adopt a staging directory the audio tool filled. The audio files
        are never modified afterwards; a replacement is a new reference."""
        if source not in ("recording", "upload"):
            raise StoreError("source must be recording or upload")
        label = clean_text(label or "", "label", MAX_LABEL, allow_empty=True)
        if filename is not None:
            filename = clean_text(re.sub(r"[\x00-\x1f/\\]", "_", filename)[-120:],
                                  "file name", 120, allow_empty=True)
        with self.lock():
            prof = self.get_profile(pid)
            old = None
            if replaces is not None:
                old = self.get_reference(replaces)
                if old["profile_id"] != pid:
                    raise StoreError("that recording does not belong to this profile")
            if os.path.dirname(os.path.dirname(os.path.realpath(staging))) != self.path("tmp"):
                raise StoreError("staging directory is not inside the store", 500)
            rid = new_id()
            dest = self.path("references", rid)
            os.rename(staging, dest)                     # same filesystem: atomic
            os.chmod(dest, 0o700)
            n = len(prof["reference_ids"]) + 1
            rec = {"schema": 1, "id": rid, "profile_id": pid,
                   "label": label or (old["label"] if old else f"Recording {n}"),
                   "transcript": old["transcript"] if old else "",
                   "transcript_needs_review": bool(old),
                   "source": source, "filename": filename or None,
                   "original": ingest["original"], "model_input": ingest["model_input"],
                   "analysis": ingest["analysis"], "processing": ingest["processing"],
                   "warnings": ingest.get("warnings") or [], "created_at": iso(),
                   "updated_at": iso(), "replaces": replaces, "replaced_by": None}
            self._write(os.path.join(dest, "reference.json"), rec)
            prof["reference_ids"].append(rid)
            if old is not None:
                old["replaced_by"] = rid
                old["updated_at"] = iso()
                self._write(os.path.join(self.ref_dir(replaces), "reference.json"), old)
                if prof.get("active_reference_id") in (None, replaces):
                    prof["active_reference_id"] = rid
            elif not prof.get("active_reference_id"):
                prof["active_reference_id"] = rid
            prof["updated_at"] = iso()
            self._write(self.profile_path(pid), prof)
        return rec

    def update_reference(self, rid, label=None, transcript=None):
        with self.lock():
            rec = self.get_reference(rid)
            if label is not None:
                rec["label"] = clean_text(label, "label", MAX_LABEL)
            if transcript is not None:
                t = transcript.strip() if isinstance(transcript, str) else transcript
                if t:
                    try:
                        check_text(t, "transcript", MAX_TRANSCRIPT)
                    except RequestError as e:
                        raise StoreError(str(e))
                rec["transcript"] = t or ""
                rec["transcript_needs_review"] = False
                rec["transcript_updated_at"] = iso()
            rec["updated_at"] = iso()
            self._write(os.path.join(self.ref_dir(rid), "reference.json"), rec)
        return rec

    def delete_reference(self, rid):
        with self.lock():
            rec = self.get_reference(rid)
            prof = self.get_profile(rec["profile_id"])
            prof["reference_ids"] = [r for r in prof["reference_ids"] if r != rid]
            if prof.get("active_reference_id") == rid:
                live = [r for r in prof["reference_ids"]]
                prof["active_reference_id"] = live[-1] if live else None
            prof["updated_at"] = iso()
            self._write(self.profile_path(prof["id"]), prof)
            shutil.rmtree(self.ref_dir(rid), ignore_errors=True)
        return {"deleted": rid}

    def reference_file(self, rid, kind):
        rec = self.get_reference(rid)
        name = rec["model_input"]["file"] if kind == "model" else rec["original"]["file"]
        if not re.match(r"^(model-input\.wav|original\.[a-z0-9]{2,5})$", name):
            raise StoreError("unexpected file name in the record", 500)
        return os.path.join(self.ref_dir(rid), name), rec

    # ------------------------------------------------------------- takes
    def take_dir(self, tid):
        return self.path("takes", self.check_id(tid, "take id"))

    def save_take(self, meta, wav):
        """A new, immutable take. Returns its record."""
        with self.lock():
            st = self._read(os.path.join(self.root, "store.json")) or {"next_take_number": 1}
            number = int(st.get("next_take_number") or 1)
            st["next_take_number"] = number + 1
            self._write(os.path.join(self.root, "store.json"), st)
            tid = new_id()
            d = self.path("takes", tid)
            os.makedirs(d, mode=0o700)
            fd = os.open(os.path.join(d, "audio.wav"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(wav)
            import hashlib
            rec = dict(meta)
            rec.update({"schema": 1, "id": tid, "number": number, "created_at": iso(),
                        "audio": {"file": "audio.wav", "bytes": len(wav),
                                  "sha256": hashlib.sha256(wav).hexdigest(),
                                  "format": "WAV, 16-bit PCM, 44,100 Hz, mono"}})
            self._write(os.path.join(d, "take.json"), rec)
        return rec

    def get_take(self, tid):
        rec = self._read(os.path.join(self.take_dir(tid), "take.json"))
        if rec is None:
            raise StoreError("no such take", 404)
        return rec

    def list_takes(self, limit=200):
        out = []
        for name in os.listdir(self.path("takes")):
            if ID_RE.match(name):
                rec = self._read(os.path.join(self.root, "takes", name, "take.json"))
                if rec:
                    out.append(rec)
        out.sort(key=lambda r: r.get("number") or 0, reverse=True)
        return out[:limit]

    def update_take(self, tid, note=None, starred=None):
        """The operator's own annotations; the audio and generation record
        are never changed."""
        with self.lock():
            rec = self.get_take(tid)
            if note is not None:
                rec["note"] = clean_text(note, "note", MAX_NOTE, allow_empty=True)
            if starred is not None:
                if not isinstance(starred, bool):
                    raise StoreError("starred must be true or false")
                rec["starred"] = starred
            rec["annotated_at"] = iso()
            self._write(os.path.join(self.take_dir(tid), "take.json"), rec)
        return rec

    def take_file(self, tid):
        self.get_take(tid)
        return os.path.join(self.take_dir(tid), "audio.wav")

    def delete_take(self, tid):
        with self.lock():
            self.get_take(tid)
            shutil.rmtree(self.take_dir(tid))
        return {"deleted": tid}

    def usage(self):
        total = 0
        for dirpath, _, files in os.walk(self.root):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
        return total


class _Flock:
    def __init__(self, path, timeout=30):
        self.path = path
        self.timeout = timeout
        self.fh = None

    def __enter__(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self.fh = os.fdopen(fd, "r+")
        deadline = time.time() + self.timeout
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.time() > deadline:
                    self.fh.close()
                    raise StoreError("the voice store is busy; try again", 503)
                time.sleep(0.02)

    def __exit__(self, *a):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()
