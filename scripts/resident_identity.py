#!/usr/bin/env python3
"""Read-only identity of the production slot. Never reconciles or writes leases."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "integrations"))
import newton_models as nm
import qwenctl
from newton_runtime.identity import (IdentityError, argument, canonical, http_json,
                                     manifest_hash, process_snapshot, unavailable, verify)


def resident_identity():
    try:
        reg = nm.load_registry()
        cfg = qwenctl.load_config(os.path.join(ROOT, "config", "qwen-a-controller.env"))
        job = qwenctl.obs_launchd(cfg)
        socket = qwenctl.obs_socket(cfg)
        if not socket["listening"]:
            return unavailable("No production model is resident", "stopped")
        pid = socket["pid"]
        if pid != job.get("pid"):
            raise IdentityError("socket is not owned by the production LaunchAgent")
        process = process_snapshot(pid)
        artifact = canonical(argument(process["argv"], "--model"))
        matches = []
        for mid, row in reg["models"].items():
            plan = nm.launch_plan(reg, "A", mid)
            if canonical(argument(plan["argv"], "--model")) == artifact:
                matches.append((mid, row, plan))
        if len(matches) != 1:
            raise IdentityError("resident artifact is not uniquely registered")
        mid, row, plan = matches[0]
        spec = {"id": mid, "display_name": row["display_name"], "artifact_path": artifact,
                "command": [plan["executable"]], "port": plan["port"],
                "backend": row["runtime"], "runtime_version": reg["runtimes"][row["runtime"]].get("pinned_version"),
                "manifest_sha256": manifest_hash(nm.abspath(row["manifest"])),
                "capabilities": {"vision": plan["vision"], "tools": True}}
        base = plan["endpoint"]
        metadata = {"health": http_json(base + "/health"), "props": http_json(base + "/props")}
        if row["runtime"] == "mlx-serve":
            metadata["models"] = http_json(base + "/v1/models")
        result = verify(spec, process, pid, metadata)
        if process_snapshot(pid) != process or qwenctl.obs_socket(cfg)["pid"] != pid:
            raise IdentityError("resident changed during verification")
        return result
    except Exception as exc:
        return unavailable(exc)


if __name__ == "__main__":
    print(json.dumps(resident_identity(), indent=2))
