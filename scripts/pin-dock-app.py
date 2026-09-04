#!/usr/bin/env python3
"""Pin an application to the macOS Dock, idempotently (Commission #4).

    python3 scripts/pin-dock-app.py "/Applications/Newton AI Control.app"

Reads the user's Dock preferences, appends ONE persistent-app tile for the
given bundle if (and only if) no tile for it exists, and restarts the Dock.
Never removes or reorders anything else.
"""
import os
import plistlib
import subprocess
import sys
import urllib.parse

DEFAULT_APP = "/Applications/Newton AI Control.app"


def tile_path(tile):
    """POSIX path a Dock tile points at, or '' if the tile is not an app."""
    try:
        url = tile["tile-data"]["file-data"]["_CFURLString"]
    except (KeyError, TypeError):
        return ""
    if url.startswith("file://"):
        return urllib.parse.unquote(url[len("file://"):]).rstrip("/")
    return ""


def main():
    app = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_APP)
    if not os.path.isdir(app):
        sys.exit(f"pin-dock-app: no app bundle at {app}")

    raw = subprocess.run(["defaults", "export", "com.apple.dock", "-"],
                         capture_output=True, check=True).stdout
    dock = plistlib.loads(raw)
    apps = dock.get("persistent-apps", [])

    if any(tile_path(t) == app.rstrip("/") for t in apps):
        print(f"already pinned: {app}")
        return

    apps.append({
        "tile-data": {
            "file-data": {
                "_CFURLString": "file://" + urllib.parse.quote(app) + "/",
                "_CFURLStringType": 15,
            }
        }
    })
    dock["persistent-apps"] = apps
    subprocess.run(["defaults", "import", "com.apple.dock", "-"],
                   input=plistlib.dumps(dock), check=True)
    subprocess.run(["killall", "Dock"], check=False)
    print(f"pinned: {app}")


if __name__ == "__main__":
    main()
