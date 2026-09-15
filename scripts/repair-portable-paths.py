#!/usr/bin/env python3
"""Repair copied virtualenv launch paths without installing or loading models.

Only generated environment files and launcher symlinks inside a portable
Newton_Local tree are touched. Model artifacts, secrets and histories are not.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re

OLD_ROOT = re.compile(r"(?:/Users/[^/\s'\"]+|/Volumes/[^/\s'\"]+)/(?:Local_Fable|Newton_Local)(?=/)")
POLYGLOT = '#!/bin/sh\n\'\'\'exec\' "$(dirname "$0")/python3" "$0" "$@"\n\' \'\'\'\n'


def repair(root, check=False):
    root = Path(root).resolve()
    if not (root / "config/newton-local.yaml").is_file():
        raise ValueError("not a portable Newton_Local installation")
    changed, unavailable = [], []

    def write(path, content):
        if os.path.commonpath([str(root), str(path.resolve())]) != str(root):
            raise ValueError("generated file escapes the portable root: " + str(path))
        if path.read_bytes().decode() != content:
            changed.append(str(path.relative_to(root)))
            if not check:
                path.write_text(content)

    def link(path, target):
        relative = os.path.relpath(target, path.parent)
        if not path.is_symlink() or os.readlink(path) != relative:
            changed.append(str(path.relative_to(root)))
            if not check:
                if path.exists() or path.is_symlink():
                    path.unlink()
                path.symlink_to(relative)

    # uv's minor-version aliases can themselves still point at the staging
    # machine. Repair them before resolving any virtualenv interpreter.
    for alias in (root / "tools/uv-python").glob("cpython-*"):
        if alias.is_symlink():
            old = os.readlink(alias)
            new = OLD_ROOT.sub(str(root), old)
            if new != old:
                link(alias, Path(new))

    envs = [root / p for p in ("services/.venv", "openwebui/venv", "tools/mlx-venv",
                               "hermes/runtime/hermes-agent/venv", "hermes/runtime/hermes-agent/.venv")]
    envs += list((root / "hermes/home/uv-tools").glob("*"))
    for env in envs:
        cfg = env / "pyvenv.cfg"
        if not cfg.is_file():
            continue
        before = cfg.read_text()
        version = re.search(r"version_info\s*=\s*(\d+\.\d+)", before)
        if not version:
            unavailable.append(str(env.relative_to(root)))
            continue
        version = version.group(1)
        candidates = list((root / "tools/uv-python").glob("cpython-" + version + "*/bin/python" + version))
        interpreters = sorted({p.resolve() for p in candidates if p.is_file()})
        if len(interpreters) != 1 or not interpreters[0].is_file():
            unavailable.append(str(env.relative_to(root)))
            continue
        interpreter = interpreters[0]
        for name in ("python", "python3", "python" + version):
            link(env / "bin" / name, interpreter)
        write(cfg, re.sub(r"(?m)^home\s*=.*$", "home = " + str(interpreter.parent), before))
        for path in (env / "bin").iterdir():
            if path.is_symlink() or not path.is_file():
                continue
            raw = path.read_bytes()
            if b"\0" in raw[:4096]:
                continue
            try:
                text = raw.decode()
            except UnicodeDecodeError:
                continue
            if text.startswith("#!") and "python" in text.splitlines()[0].lower():
                text = POLYGLOT + text.split("\n", 1)[1]
            # Activation scripts and previously shell-wrapped entrypoints
            # are generated for this location, and refreshed on the next move.
            text = OLD_ROOT.sub(str(root), text)
            write(path, text)
        for pattern in ("*.pth", "*.egg-link", "__editable__*.py"):
            for path in env.glob("lib/python*/site-packages/" + pattern):
                text = OLD_ROOT.sub(str(root), path.read_text())
                if path.suffix in (".pth", ".egg-link"):
                    lines = []
                    for line in text.splitlines():
                        if line.startswith(str(root) + "/"):
                            line = os.path.relpath(line, path.parent)
                        lines.append(line)
                    text = "\n".join(lines) + "\n"
                write(path, text)
    for directory in (root / "hermes/home/bin", root / "tools/bin"):
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.is_symlink():
                old = os.readlink(path)
                new = OLD_ROOT.sub(str(root), old)
                if new != old:
                    link(path, Path(new))
    return changed, unavailable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    changed, unavailable = repair(args.root, args.check)
    print(("Pending" if args.check else "Repaired") + " generated path entries: " + str(len(changed)))
    if unavailable:
        print("Bootstrap must recreate environments lacking bundled interpreters: " + ", ".join(unavailable))
    if args.check and (changed or unavailable):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
