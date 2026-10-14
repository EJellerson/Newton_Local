#!/usr/bin/env python3
"""Write MANIFEST.json for a Fish model pack (docs/18).

Same contract and schema as scripts/make-model-manifest.py (docs/10 §6): every
file of the repository at the PINNED revision, with its byte size and its
upstream content hash (LFS sha256, or the git blob sha1 of small files),
taken from the Hugging Face API — never from the local copy, so a corrupt
download cannot certify itself. Source and revision come from config/fish.json.

    make-fish-manifest.py [--model drama3-q4] [--hf-json api.json]

Then prove the local copy: `scripts/fish verify <id> --deep`. Reads metadata
and file sizes only; never opens the model.
"""
import argparse
import json
import os
import sys
import time
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import fish_config as fc  # noqa: E402


def fetch_api(repo, revision):
    url = f"https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true"
    req = urllib.request.Request(url, headers={"User-Agent": "newton-local-manifest"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def build_manifest(model_id, api, revision):
    if api.get("sha") and api["sha"] != revision:
        raise SystemExit(f"API returned sha {api['sha']} != pinned {revision}")
    files = []
    for s in api.get("siblings", []):
        lfs = s.get("lfs") or {}
        size = s.get("size") if s.get("size") is not None else lfs.get("size")
        if size is None:
            raise SystemExit(f"no size for {s['rfilename']} (fetch with ?blobs=true)")
        files.append({"name": s["rfilename"], "size": int(size),
                      "sha256": lfs.get("sha256") or None,
                      "git_blob_sha1": s.get("blobId") or None})
    files.sort(key=lambda f: f["name"])
    return {"schema": 1, "model_id": model_id, "hf_revision": revision,
            "hf_last_modified": api.get("lastModified"),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "file_count": len(files), "total_bytes": sum(f["size"] for f in files),
            "files": files}


def main():
    cfg = fc.load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=cfg["default_model"])
    ap.add_argument("--hf-json", help="already-fetched HF API JSON (?blobs=true)")
    a = ap.parse_args()
    m = fc.model(cfg, a.model)
    repo, rev = m["source"]["hf_repo"], m["source"]["hf_revision"]
    if a.hf_json:
        with open(a.hf_json) as f:
            api = json.load(f)
    else:
        api = fetch_api(repo, rev)
    man = build_manifest(a.model, api, rev)
    want = m["source"]
    if man["file_count"] != want["files"] or man["total_bytes"] != want["bytes"]:
        raise SystemExit(f"upstream has {man['file_count']} files / {man['total_bytes']:,} bytes "
                         f"at {rev}; config/fish.json expects {want['files']} / {want['bytes']:,}")
    out = fc.abspath(m["manifest"])
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(man, f, indent=1)
        f.write("\n")
    os.replace(tmp, out)
    print(f"wrote {out}: {man['file_count']} files, {man['total_bytes']:,} bytes "
          f"({man['total_bytes'] / 2**30:.2f} GiB) @ {repo} {rev}")
    w = fc.check_weights(cfg, a.model)
    print(f"local size check: {w['files_ok']}/{w['files_expected']} ok, "
          f"{w['bytes_present']:,} bytes present")
    for p in w["problems"][:10]:
        print("  !", p)
    sys.exit(0 if w["ok"] else 1)


if __name__ == "__main__":
    main()
