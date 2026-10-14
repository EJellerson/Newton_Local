"""Validation of speech requests (docs/18 §API). Standard library only.

Used twice: by Voice Studio before it sends anything, and by the engine
before it queues anything. Text rules mirror the pinned prompt builder
(mlx_audio/tts/models/fish_omni/prompt.py, validate_text): structural tokens
belong to the prompt builder, and speaker tags must be <|speaker:0|> to
<|speaker:15|>. Ranges are deliberately bounded; nothing unbounded reaches
the model.
"""
import re

STRUCTURAL_TOKENS = ("<|im_start|>", "<|im_end|>", "<|voice|>", "<|text|>",
                     "<|interleave|>", "<|semantic:")
SPEAKER_TAG = re.compile(r"<\|speaker:([0-9]+)\|>")
MAX_SPEAKER_ID = 15
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Unicode White_Space, as in the prompt builder (str.strip() differs).
WHITE_SPACE = "".join(map(chr, (*range(0x9, 0xE), 0x20, 0x85, 0xA0, 0x1680,
                                *range(0x2000, 0x200B), 0x2028, 0x2029, 0x202F,
                                0x205F, 0x3000)))
MAX_TRANSCRIPT_CHARS = 1000
MAX_SEED = 2**31 - 1

# name: (type, minimum, maximum, default). Defaults are the model developer's
# published settings (temperature 1.0, top_p 0.9, top_k 30) and the pinned
# implementation's own chunking limits; config/fish.json may lower maxima.
SETTINGS = {
    "temperature": (float, 0.05, 1.5, 1.0),
    "top_p": (float, 0.05, 1.0, 0.9),
    "top_k": (int, 1, 100, 30),
    "max_frames_per_chunk": (int, 20, 600, 600),
    "chunk_bytes": (int, 80, 600, 300),
}


class RequestError(ValueError):
    pass


def check_text(text, where, max_chars):
    if not isinstance(text, str):
        raise RequestError(f"{where} must be text")
    if not text.strip(WHITE_SPACE):
        raise RequestError(f"{where} must not be empty")
    if len(text) > max_chars:
        raise RequestError(f"{where} is {len(text)} characters; the limit is {max_chars}")
    if "\x00" in text:
        raise RequestError(f"{where} contains a NUL character")
    for token in STRUCTURAL_TOKENS:
        if token in text:
            raise RequestError(f"{where} may not contain {token}; the prompt builder writes it")
    for marker in re.finditer(r"<\|speaker", text):
        tag = SPEAKER_TAG.match(text, marker.start())
        digits = tag.group(1) if tag else ""
        if not digits or str(int(digits)) != digits or int(digits) > MAX_SPEAKER_ID:
            raise RequestError(f"{where}: speaker tags must be <|speaker:0|> to "
                               f"<|speaker:{MAX_SPEAKER_ID}|>")
    return text


def _number(name, value):
    kind, lo, hi, _ = SETTINGS[name]
    if isinstance(value, bool) or value is None:
        raise RequestError(f"{name} must be a number")
    try:
        v = kind(value)
    except (TypeError, ValueError):
        raise RequestError(f"{name} must be a number")
    if kind is int and float(value) != v:
        raise RequestError(f"{name} must be a whole number")
    if v != v or not lo <= v <= hi:          # v != v: NaN
        raise RequestError(f"{name} must be between {lo} and {hi}")
    return v


def settings_with_defaults(raw, limits=None, sampling_defaults=None):
    """Validated generation settings; unknown keys are refused."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise RequestError("settings must be an object")
    allowed = set(SETTINGS) | {"seed", "single_chunk"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise RequestError(f"unsupported settings: {', '.join(unknown)}")
    out = {}
    for name, (_, _, _, default) in SETTINGS.items():
        if name in (sampling_defaults or {}):
            default = sampling_defaults[name]
        out[name] = _number(name, raw[name]) if raw.get(name) is not None else default
    limits = limits or {}
    if out["max_frames_per_chunk"] > int(limits.get("max_frames_per_chunk", 600)):
        raise RequestError(f"max_frames_per_chunk may not exceed "
                           f"{limits.get('max_frames_per_chunk')}")
    seed = raw.get("seed")
    if seed in (None, ""):
        out["seed"] = None
    else:
        if isinstance(seed, bool):
            raise RequestError("seed must be a whole number")
        try:
            s = int(seed)
        except (TypeError, ValueError):
            raise RequestError("seed must be a whole number")
        if str(seed).strip() not in (str(s), f"{s}.0") or not 0 <= s <= MAX_SEED:
            raise RequestError(f"seed must be a whole number from 0 to {MAX_SEED}")
        out["seed"] = s
    single = raw.get("single_chunk", False)
    if not isinstance(single, bool):
        raise RequestError("single_chunk must be true or false")
    out["single_chunk"] = single
    return out


def validate_job(body, limits, sampling_defaults=None):
    """A complete engine job request -> normalised dict, or RequestError."""
    if not isinstance(body, dict):
        raise RequestError("request must be a JSON object")
    unknown = sorted(set(body) - {"text", "references", "settings", "progressive",
                                  "client_ref"})
    if unknown:
        raise RequestError(f"unsupported fields: {', '.join(unknown)}")
    text = check_text(body.get("text"), "text", int(limits.get("max_text_chars", 2000)))
    refs = body.get("references") or []
    if not isinstance(refs, list):
        raise RequestError("references must be a list")
    if len(refs) > int(limits.get("max_references", 3)):
        raise RequestError(f"at most {limits.get('max_references', 3)} references per take")
    clean = []
    for i, r in enumerate(refs, 1):
        if not isinstance(r, dict) or set(r) - {"sha256", "transcript"}:
            raise RequestError(f"reference {i} must have exactly sha256 and transcript")
        sha = r.get("sha256")
        if not isinstance(sha, str) or not SHA256_RE.match(sha):
            raise RequestError(f"reference {i} needs a sha256 of its audio")
        clean.append({"sha256": sha, "transcript": check_text(
            r.get("transcript"), f"reference {i} transcript", MAX_TRANSCRIPT_CHARS)})
    settings = settings_with_defaults(body.get("settings"), limits, sampling_defaults)
    prog = body.get("progressive")
    if prog not in (None, False):
        if not isinstance(prog, dict) or set(prog) - {"segment_frames"}:
            raise RequestError("progressive must be {\"segment_frames\": N}")
        seg = prog.get("segment_frames", 11)
        if isinstance(seg, bool) or not isinstance(seg, int) or not 1 <= seg <= 600:
            raise RequestError("segment_frames must be a whole number from 1 to 600")
        prog = {"segment_frames": seg}
    else:
        prog = None
    client_ref = body.get("client_ref")
    if client_ref is not None and (not isinstance(client_ref, str)
                                   or not re.match(r"^[A-Za-z0-9_.:-]{1,64}$", client_ref)):
        raise RequestError("client_ref must be a short identifier")
    return {"text": text, "references": clean, "settings": settings,
            "progressive": prog, "client_ref": client_ref}
