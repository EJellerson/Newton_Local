"""Reference audio ingestion for Voice Studio (Fish runtime only; docs/18 §5).

    fish_audio.py ingest --input FILE --outdir DIR [--ext-hint wav]

Validates an uploaded or recorded file by its content, never its name;
decodes it; measures it; keeps the original byte for byte; and derives the
model input. Prints one JSON object. Runs under the network sandbox.

DERIVATION (documented in docs/18 §5, recorded per reference)
  input        any of WAV (PCM or float), FLAC, MP3, Ogg Vorbis/Opus, M4A/AAC,
               WebM/Opus, CAF, AIFF; WAV is parsed here, the rest decoded by
               ffmpeg at the file's native rate (no resampling by ffmpeg)
  channels     averaged to mono
  DC offset    removed (mean subtracted)
  silence      leading/trailing silence trimmed, keeping 0.25 s at each end;
               nothing inside the recording is removed
  resampling   to 44,100 Hz with mlx_audio.resample.resample_audio_array, the
               polyphase FIR the pinned model itself uses for references
               (its upstream found linear upsampling made generations ramble)
  level        linear gain to a -1 dBFS peak, at most +20 dB, never upward
               when the original is clipped; no compression or limiting
  output       model-input.wav: 24-bit PCM, 44.1 kHz, mono
"""
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys

import numpy as np

from .wavio import WavError, is_wav, read_wav, write_wav

TARGET_RATE = 44100
MAX_RAW_SECONDS = 120.0
MAX_BYTES = 32 << 20
PAD_S = 0.25
TARGET_PEAK_DB = -1.0
MAX_GAIN_DB = 20.0
SILENT_PEAK_DB = -45.0
FFMPEG_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


class IngestError(Exception):
    pass


def sniff(data):
    """Container from magic bytes. The extension is never trusted."""
    if is_wav(data):
        return "wav"
    if data[:4] == b"fLaC":
        return "flac"
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "mp3"
    if data[:4] == b"OggS":
        return "ogg"
    if data[4:8] == b"ftyp":
        return "m4a"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if data[:4] == b"caff":
        return "caf"
    if data[:4] == b"FORM" and data[8:12] in (b"AIFF", b"AIFC"):
        return "aiff"
    raise IngestError("this is not a supported audio file (WAV, FLAC, MP3, Ogg, M4A/AAC, "
                      "WebM, CAF or AIFF)")


def tool(name):
    for d in FFMPEG_DIRS:
        p = os.path.join(d, name)
        if os.access(p, os.X_OK):
            return p
    raise IngestError(f"{name} is needed to read this format but was not found; "
                      f"upload WAV, or install ffmpeg (brew install ffmpeg)")


def ffprobe(path):
    p = subprocess.run([tool("ffprobe"), "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", "-protocol_whitelist", "file",
                        path], capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        raise IngestError("the file could not be decoded (it may be corrupt or truncated)")
    info = json.loads(p.stdout or "{}")
    audio = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio:
        raise IngestError("the file contains no audio stream")
    return audio[0], info.get("format") or {}


def ffmpeg_decode(path, channels, rate):
    p = subprocess.run([tool("ffmpeg"), "-nostdin", "-v", "error", "-protocol_whitelist",
                        "file", "-i", path, "-map", "0:a:0", "-t", str(MAX_RAW_SECONDS + 1),
                        "-f", "f32le", "-acodec", "pcm_f32le", "-"],
                       capture_output=True, timeout=60)
    if p.returncode != 0:
        raise IngestError("the audio could not be decoded (it may be corrupt or truncated)")
    x = np.frombuffer(p.stdout, dtype="<f4")
    if channels < 1 or x.size % channels:
        raise IngestError("decoded audio has an inconsistent channel layout")
    return x.reshape(-1, channels).astype(np.float32), rate


def decode(path, data, kind):
    if kind == "wav":
        try:
            x, rate, info = read_wav(data, max_samples=int(192000 * (MAX_RAW_SECONDS + 1)))
            codec = f"pcm_{'f' if info['format'] == 'float' else 's'}{info['bits']}le"
            return x, rate, {"container": "wav", "codec": codec,
                             "sample_rate": rate, "channels": info["channels"],
                             "bits": info["bits"], "lossy": False}
        except WavError as e:
            if "unsupported WAV encoding" not in str(e):
                raise IngestError(f"invalid WAV file: {e}")
    stream, fmt = ffprobe(path)
    try:
        rate, ch = int(stream["sample_rate"]), int(stream["channels"])
    except (KeyError, TypeError, ValueError):
        raise IngestError("the audio stream has no usable sample rate or channel count")
    if not 8000 <= rate <= 192000 or not 1 <= ch <= 8:
        raise IngestError(f"unsupported audio layout ({rate} Hz, {ch} channels)")
    x, rate = ffmpeg_decode(path, ch, rate)
    codec = stream.get("codec_name") or "unknown"
    return x, rate, {"container": fmt.get("format_name", kind), "codec": codec,
                     "sample_rate": rate, "channels": ch,
                     "bits": stream.get("bits_per_raw_sample") or stream.get("bits_per_sample"),
                     "lossy": codec not in ("pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le",
                                            "pcm_s16be", "pcm_s24be", "pcm_f32be", "flac", "alac")}


def db(v):
    return round(20 * math.log10(max(float(v), 1e-9)), 1)


def frame_rms(x, rate, ms=20):
    n = max(1, int(rate * ms / 1000))
    k = len(x) // n
    if k == 0:
        return np.array([float(np.sqrt(np.mean(x ** 2)))]) if len(x) else np.zeros(1), n
    return np.sqrt(np.mean(x[:k * n].reshape(k, n) ** 2, axis=1)), n


def analyse(x, rate):
    mono = x.mean(axis=1)
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    rms = float(np.sqrt(np.mean(mono ** 2))) if mono.size else 0.0
    fr, _ = frame_rms(mono, rate)
    voiced = fr[fr > 1e-5]
    noise = float(np.percentile(voiced, 10)) if voiced.size else 0.0
    speech = float(np.percentile(voiced, 90)) if voiced.size else 0.0
    corr = None
    if x.shape[1] == 2 and x.shape[0] > 1:
        a, b = x[:, 0], x[:, 1]
        den = float(np.sqrt(np.sum(a * a) * np.sum(b * b)))
        corr = round(float(np.sum(a * b)) / den, 3) if den > 0 else None
    return {"seconds": round(x.shape[0] / rate, 3), "peak_dbfs": db(peak), "rms_dbfs": db(rms),
            "clipped_fraction": round(float(np.mean(np.abs(x) >= 0.999)), 5),
            "dc_offset": round(float(mono.mean()), 5) if mono.size else 0.0,
            "noise_floor_dbfs": db(noise), "speech_level_dbfs": db(speech),
            "snr_db": round(db(speech) - db(noise), 1), "stereo_correlation": corr}


def trim_bounds(mono, rate):
    fr, n = frame_rms(mono, rate)
    if fr.size == 0 or float(fr.max()) <= 0:
        return 0, len(mono)
    noise = float(np.percentile(fr[fr > 0], 10)) if np.any(fr > 0) else 0.0
    thresh = max(noise * 2.0, float(fr.max()) * 0.01)          # +6 dB over noise, -40 dB of peak
    loud = np.nonzero(fr > thresh)[0]
    if loud.size == 0:
        return 0, len(mono)
    pad = int(PAD_S * rate)
    return max(0, loud[0] * n - pad), min(len(mono), (loud[-1] + 1) * n + pad)


def warnings_for(fmt, a, model_seconds, limits):
    w = []
    if a["clipped_fraction"] > 0.001:
        w.append("Clipping detected: the recording was too loud. Re-record a little further "
                 "from the microphone or with lower input gain.")
    if a["rms_dbfs"] < -40:
        w.append("The recording is very quiet; the derived copy was amplified, which also "
                 "raises background noise.")
    if a["snr_db"] < 20:
        w.append(f"Noticeable background noise (about {a['snr_db']:.0f} dB between speech and "
                 f"noise). A quieter room usually clones better.")
    if fmt["sample_rate"] < 16000:
        w.append(f"Low sample rate ({fmt['sample_rate']} Hz): high frequencies are missing, "
                 f"so the cloned voice may sound muffled.")
    if fmt.get("lossy"):
        w.append(f"Lossy source ({fmt['codec']}). A WAV or in-browser recording keeps more "
                 f"detail.")
    if a["stereo_correlation"] is not None and a["stereo_correlation"] < 0.3:
        w.append("The two channels differ a lot; they were averaged to mono. Record in mono "
                 "if you can.")
    if model_seconds < 6:
        w.append("Short reference: 8 to 15 seconds of natural speech usually works best.")
    elif model_seconds > 20:
        w.append("Long reference: 8 to 15 seconds usually works as well and keeps prompts short.")
    return w


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def ingest(path, outdir, limits):
    size = os.path.getsize(path)
    if size == 0:
        raise IngestError("the file is empty")
    if size > MAX_BYTES:
        raise IngestError(f"the file is {size / 2**20:.0f} MiB; the limit is "
                          f"{MAX_BYTES / 2**20:.0f} MiB")
    with open(path, "rb") as f:
        data = f.read()
    kind = sniff(data)
    x, rate, fmt = decode(path, data, kind)
    raw_s = x.shape[0] / rate
    if raw_s > MAX_RAW_SECONDS:
        raise IngestError(f"the recording is {raw_s:.0f} s long; references must be at most "
                          f"{limits['reference_max_s']:.0f} s — trim it first")
    if raw_s < 0.5:
        raise IngestError("the recording is too short to contain speech")
    if not np.isfinite(x).all():
        raise IngestError("the audio contains invalid samples")
    a = analyse(x, rate)
    if a["peak_dbfs"] < SILENT_PEAK_DB:
        raise IngestError(f"the recording is almost silent (peak {a['peak_dbfs']:.0f} dBFS); "
                          f"check that the right microphone is selected and not muted")

    mono = x.mean(axis=1).astype(np.float32)
    mono = mono - np.float32(mono.mean())
    start, end = trim_bounds(mono, rate)
    mono = mono[start:end]
    from mlx_audio.resample import resample_audio_array
    y = np.asarray(resample_audio_array(mono, rate, TARGET_RATE), dtype=np.float32)
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    if peak <= 1e-6:
        raise IngestError("the recording is silent")
    gain_db = TARGET_PEAK_DB - db(peak)
    gain_db = min(gain_db, MAX_GAIN_DB)
    if a["clipped_fraction"] > 0.001:
        gain_db = min(gain_db, 0.0)
    y = np.clip(y * np.float32(10 ** (gain_db / 20)), -1.0, 1.0)
    seconds = len(y) / TARGET_RATE
    lo, hi = float(limits["reference_min_s"]), float(limits["reference_max_s"])
    if seconds < lo:
        raise IngestError(f"after trimming silence the speech is {seconds:.1f} s; a reference "
                          f"needs at least {lo:.0f} s (8 to 15 s works best)")
    if seconds > hi:
        raise IngestError(f"after trimming silence the speech is {seconds:.1f} s; references "
                          f"must be at most {hi:.0f} s — trim it, or record a shorter clip")
    wav = write_wav(y, TARGET_RATE, bits=24)

    os.makedirs(outdir, exist_ok=True)
    orig_name = f"original.{kind}"
    for name, blob in ((orig_name, data), ("model-input.wav", wav)):
        fd = os.open(os.path.join(outdir, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
    return {
        "original": {"file": orig_name, "bytes": size, "sha256": sha256(data), "format": fmt},
        "analysis": a,
        "model_input": {"file": "model-input.wav", "bytes": len(wav), "sha256": sha256(wav),
                        "seconds": round(seconds, 3), "sample_rate": TARGET_RATE,
                        "channels": 1, "bits": 24},
        "processing": {"mono": "average of channels" if fmt["channels"] > 1 else "already mono",
                       "dc_offset_removed": True,
                       "trimmed_start_s": round(start / rate, 3),
                       "trimmed_end_s": round((x.shape[0] - end) / rate, 3),
                       "resampler": f"mlx_audio.resample.resample_audio_array "
                                    f"(scipy resample_poly, polyphase FIR) {rate} -> {TARGET_RATE} Hz"
                       if rate != TARGET_RATE else "none (already 44,100 Hz)",
                       "gain_db": round(gain_db, 2), "target_peak_dbfs": TARGET_PEAK_DB,
                       "output": "WAV, 24-bit PCM, 44,100 Hz, mono"},
        "warnings": warnings_for(fmt, a, seconds, limits),
    }


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="fish_audio")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest")
    p.add_argument("--input", required=True)
    p.add_argument("--outdir", required=True)
    p.add_argument("--limits", required=True, help="JSON: reference_min_s, reference_max_s")
    a = ap.parse_args(argv)
    try:
        out = {"ok": True, **ingest(a.input, a.outdir, json.loads(a.limits))}
    except IngestError as e:
        out = {"ok": False, "error": str(e)}
        if os.path.isdir(a.outdir):
            shutil.rmtree(a.outdir, ignore_errors=True)
    print(json.dumps(out))
    return 0 if out["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
