#!/usr/bin/env python3
"""Install/check the project-managed patch against exactly bound Hermes sources.

No Git commands, network, environment installation, or service changes.
"""
import argparse
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=pathlib.Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "integrations/hermes-patch.json").read_text())
    changes = []
    for rel, hashes in manifest["files"].items():
        path = args.runtime / rel
        sha = digest(path)
        if sha == hashes["patched"]:
            continue
        if args.check or sha != hashes["original"]:
            sys.exit("Unrecognized/unpatched Hermes source: " + str(path))
        changes.append(rel)
    if changes and len(changes) != len(manifest["files"]):
        sys.exit("Partial installation; restore the bound original files before applying")
    if changes:
        patch = (ROOT / "integrations/hermes.patch").read_bytes()
        subprocess.run(["patch", "--dry-run", "-p1", "-d", str(args.runtime)], input=patch, check=True)
        subprocess.run(["patch", "-p1", "-d", str(args.runtime)], input=patch, check=True)
    for path in (ROOT / "integrations/newton_runtime").glob("*.py"):
        dest = args.runtime / "agent/newton_runtime" / path.name
        if args.check:
            if not dest.exists() or digest(path) != digest(dest):
                sys.exit("Integration module differs: " + str(dest))
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
    for rel, hashes in manifest["files"].items():
        if digest(args.runtime / rel) != hashes["patched"]:
            sys.exit("Patch verification failed: " + rel)
    print("Hermes integration verified; upstream " + manifest["upstream_commit"])


if __name__ == "__main__":
    main()
