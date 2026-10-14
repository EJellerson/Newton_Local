"""Strict WAV reading and writing with numpy (Fish runtime only).

Reads RIFF/WAVE with PCM 8/16/24/32-bit integer or IEEE float 32/64 samples,
plain or WAVE_FORMAT_EXTENSIBLE, by parsing the chunks themselves: the file
extension is never trusted, malformed headers are refused, and the declared
data size must be present. Writes 16- or 24-bit PCM.
"""
import io
import struct

import numpy as np

PCM, IEEE_FLOAT, EXTENSIBLE = 0x0001, 0x0003, 0xFFFE
_SUBFORMAT_TAIL = b"\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"


class WavError(ValueError):
    pass


def is_wav(data):
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def read_wav(data, max_samples=None):
    """bytes -> (float32 array [frames, channels] in [-1, 1], sample_rate, info)."""
    if not is_wav(data):
        raise WavError("not a RIFF/WAVE file")
    pos, fmt, payload = 12, None, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            if size < 16:
                raise WavError("fmt chunk too short")
            tag, ch, rate, _, align, bits = struct.unpack("<HHIIHH", body[:16])
            if tag == EXTENSIBLE:
                if size < 40 or body[26:40] != _SUBFORMAT_TAIL:
                    raise WavError("unsupported WAVE_FORMAT_EXTENSIBLE subformat")
                tag = struct.unpack("<H", body[24:26])[0]
            fmt = (tag, ch, rate, align, bits)
        elif cid == b"data":
            if len(body) < size:
                raise WavError(f"data chunk truncated ({len(body)} of {size} bytes)")
            payload = body
            break
        pos += 8 + size + (size & 1)
    if fmt is None or payload is None:
        raise WavError("missing fmt or data chunk")
    tag, ch, rate, align, bits = fmt
    if not 1 <= ch <= 8:
        raise WavError(f"unsupported channel count {ch}")
    if not 8000 <= rate <= 192000:
        raise WavError(f"unsupported sample rate {rate}")
    width = bits // 8
    if bits % 8 or align != width * ch:
        raise WavError("inconsistent block alignment")
    n = len(payload) // align
    if max_samples is not None and n > max_samples:
        raise WavError(f"audio is longer than {max_samples / rate:.0f} s")
    raw = payload[:n * align]
    if tag == PCM and bits == 8:
        x = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128.0) / 128.0
    elif tag == PCM and bits == 16:
        x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    elif tag == PCM and bits == 24:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v & 0x800000, v - 0x1000000, v)
        x = v.astype(np.float32) / 8388608.0
    elif tag == PCM and bits == 32:
        x = (np.frombuffer(raw, "<i4").astype(np.float64) / 2147483648.0).astype(np.float32)
    elif tag == IEEE_FLOAT and bits == 32:
        x = np.frombuffer(raw, "<f4").astype(np.float32)
    elif tag == IEEE_FLOAT and bits == 64:
        x = np.frombuffer(raw, "<f8").astype(np.float32)
    else:
        raise WavError(f"unsupported WAV encoding (format {tag}, {bits}-bit)")
    if not np.isfinite(x).all():
        raise WavError("audio contains NaN or infinite samples")
    x = x.reshape(-1, ch)
    info = {"format": "float" if tag == IEEE_FLOAT else "pcm", "bits": bits,
            "channels": ch, "sample_rate": rate, "frames": int(x.shape[0])}
    return x, rate, info


def write_wav(samples, sample_rate, bits=16):
    """float array [frames] or [frames, channels] in [-1, 1] -> WAV bytes."""
    x = np.asarray(samples, dtype=np.float32)
    if x.ndim == 1:
        x = x[:, None]
    x = np.clip(np.nan_to_num(x), -1.0, 1.0)
    if bits == 16:
        raw = np.round(x * 32767.0).astype("<i2").tobytes()
    elif bits == 24:
        v = np.round(x * 8388607.0).astype(np.int32).reshape(-1)
        b = np.empty((v.size, 3), np.uint8)
        b[:, 0], b[:, 1], b[:, 2] = v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF
        raw = b.tobytes()
    else:
        raise ValueError("bits must be 16 or 24")
    ch = x.shape[1]
    width = bits // 8
    out = io.BytesIO()
    out.write(b"RIFF" + struct.pack("<I", 36 + len(raw)) + b"WAVE")
    out.write(b"fmt " + struct.pack("<IHHIIHH", 16, PCM, ch, sample_rate,
                                    sample_rate * ch * width, ch * width, bits))
    out.write(b"data" + struct.pack("<I", len(raw)) + raw)
    return out.getvalue()


def pcm16_bytes(samples):
    x = np.clip(np.nan_to_num(np.asarray(samples, dtype=np.float32).reshape(-1)), -1.0, 1.0)
    return np.round(x * 32767.0).astype("<i2").tobytes()
