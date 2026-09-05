#!/usr/bin/env python3
"""Write a machine-readable MANIFEST.json for a registered model (Commission #5).

The manifest is the availability/integrity contract newton_models.py checks
before a model may be selected or started: every expected file with its
byte size, and the upstream content hash (LFS sha256, or the git blob sha1
for small non-LFS files) taken from the Hugging Face API at the PINNED
revision — never from the local copy, so a corrupt download cannot certify
itself.

    make-model-manifest.py --model flash-next            # fetch metadata live
    make-model-manifest.py --model qwen27b --only Qwen3.8-27B-Q8_0.gguf \
                           --only mmproj-Qwen3.8-27B-f16.gguf
    make-model-manifest.py --model flash-next --hf-json /path/to/api.json

The repository and pinned revision come from the machine-local
config/model-sources.local.json (untracked; template beside it), whose
optional "files" map renames upstream files to the local names used here.

Then prove the local copy: `newton-model verify <id> [--deep]`.
This script reads metadata and file sizes only; it never opens a model for
inference.
"""
import argparse
import json
import os
import sys
import time
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import newton_models as nm  # noqa: E402

SOURCES = os.path.normpath(
    os.path.join(SCRIPT_DIR, "..", "config", "model-sources.local.json"))


def load_source(model_id):
    """This model's download source: hf_repo, hf_revision and an optional
    local -> upstream "files" map, from the untracked SOURCES file."""
    try:
        with open(SOURCES) as f:
            return json.load(f).get(model_id) or {}
    except FileNotFoundError:
        return {}


def fetch_api(repo, revision):
    url = f"https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true"
    req = urllib.request.Request(url, headers={"User-Agent": "newton-local-manifest"})
    tok = os.environ.get("HF_TOKEN")
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--hf-json", help="already-fetched HF API JSON (?blobs=true)")
    ap.add_argument("--only", action="append", default=[],
                    help="restrict to these repo files (repeatable)")
    a = ap.parse_args()

    reg = nm.load_registry()
    if a.model not in reg["models"]:
        sys.exit(f"unknown model {a.model}")
    m = reg["models"][a.model]
    src = load_source(a.model)
    repo, rev = src.get("hf_repo"), src.get("hf_revision")
    if not repo or not rev:
        sys.exit(f"no hf_repo/hf_revision for {a.model} in {SOURCES} "
                 "(template: config/model-sources.example.json)")
    local_name = {up: loc for loc, up in (src.get("files") or {}).items()}

    if a.hf_json:
        with open(a.hf_json) as f:
            api = json.load(f)
    else:
        api = fetch_api(repo, rev)
    if api.get("sha") and api["sha"] != rev:
        sys.exit(f"API returned sha {api['sha']} != pinned {rev}")

    files = []
    for s in api.get("siblings", []):
        name = local_name.get(s["rfilename"], s["rfilename"])
        if a.only and name not in a.only:
            continue
        lfs = s.get("lfs") or {}
        size = s.get("size") if s.get("size") is not None else lfs.get("size")
        if size is None:
            sys.exit(f"no size for {name} (fetch with ?blobs=true)")
        files.append({"name": name, "size": int(size),
                      "sha256": lfs.get("sha256") or None,
                      "git_blob_sha1": s.get("blobId") or None})
    if a.only and len(files) != len(a.only):
        sys.exit(f"--only asked for {a.only} but matched {[f['name'] for f in files]}")
    files.sort(key=lambda f: f["name"])

    manifest = {
        "schema": 1, "model_id": a.model,
        "hf_last_modified": api.get("lastModified"),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "file_count": len(files), "total_bytes": sum(f["size"] for f in files),
        "files": files,
    }
    out = nm.abspath(m["manifest"])
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=1)
        f.write("\n")
    os.replace(tmp, out)
    print(f"wrote {out}: {len(files)} files, {manifest['total_bytes']:,} bytes "
          f"({manifest['total_bytes']/2**30:.2f} GiB) @ {rev}")

    w = nm.check_weights(reg, a.model)
    print(f"local size check: {w['files_ok']}/{w['files_expected']} ok, "
          f"{w['bytes_present']:,} bytes present")
    for p in w["problems"][:10]:
        print("  !", p)
    sys.exit(0 if w["ok"] else 1)


if __name__ == "__main__":
    main()
