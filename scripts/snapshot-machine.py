#!/usr/bin/env python3
"""Newton_Local — deterministic machine memory/GPU/throughput snapshot.

Emits one JSON object describing the state of the machine at a point in
time. Used as the common measurement primitive for every Commission #2
capacity claim so that A-alone / A+B / A-after-B numbers are produced by
identical code rather than by ad-hoc shell one-liners.

Usage:  snapshot-machine.py [label]
"""
import json
import re
import subprocess
import sys
import time

PAGE = 16384


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True,
                              text=True, timeout=30).stdout
    except Exception:
        return ""


def vm_stat():
    out = sh("vm_stat")
    d = {}
    for line in out.splitlines():
        m = re.match(r'^(.*?):\s+(\d+)\.?$', line.strip())
        if m:
            d[m.group(1)] = int(m.group(2))
    g = lambda k: d.get(k, 0)
    return {
        "page_size_bytes": PAGE,
        "free_gib": round(g("Pages free") * PAGE / 2**30, 2),
        "active_gib": round(g("Pages active") * PAGE / 2**30, 2),
        "inactive_gib": round(g("Pages inactive") * PAGE / 2**30, 2),
        "speculative_gib": round(g("Pages speculative") * PAGE / 2**30, 2),
        "wired_gib": round(g("Pages wired down") * PAGE / 2**30, 2),
        "compressor_occupied_gib": round(g("Pages occupied by compressor") * PAGE / 2**30, 3),
        "pages_stored_in_compressor": g("Pages stored in compressor"),
        "swapins": g("Swapins"),
        "swapouts": g("Swapouts"),
        # "available" in the Activity-Monitor sense: free + inactive + speculative
        "available_gib": round((g("Pages free") + g("Pages inactive")
                                + g("Pages speculative")) * PAGE / 2**30, 2),
        # Activity-Monitor decomposition. Cached files are RECLAIMABLE headroom
        # and must not be read as "memory exhausted"; capacity judgements are
        # made from pressure / compression / swap / allocation success instead.
        "app_memory_gib": round(g("Anonymous pages") * PAGE / 2**30, 2),
        "cached_files_gib": round(g("File-backed pages") * PAGE / 2**30, 2),
        "reclaimable_headroom_gib": round((g("Pages free") + g("File-backed pages")
                                           + g("Pages purgeable")) * PAGE / 2**30, 2),
    }


def swap():
    out = sh("sysctl -n vm.swapusage")
    m = re.search(r'total = ([\d.]+)M\s+used = ([\d.]+)M\s+free = ([\d.]+)M', out)
    if not m:
        return {"raw": out.strip()}
    return {"total_mib": float(m.group(1)), "used_mib": float(m.group(2)),
            "free_mib": float(m.group(3))}


def pressure():
    out = sh("memory_pressure")
    m = re.search(r'System-wide memory free percentage:\s+(\d+)%', out)
    lvl = sh("sysctl -n kern.memorystatus_vm_pressure_level").strip()
    return {"free_percentage": int(m.group(1)) if m else None,
            "vm_pressure_level": lvl or None}


def gpu():
    """Metal/GPU allocation as reported by the AGX accelerator IOKit node."""
    out = sh("ioreg -r -d 1 -w 0 -c AGXAcceleratorG14X 2>/dev/null || ioreg -l -w 0")
    m = re.search(r'"PerformanceStatistics" = \{([^}]*)\}', out)
    if not m:
        return {"available": False}
    body = m.group(1)
    kv = dict(re.findall(r'"([^"]+)"=(\d+)', body))
    num = lambda k: int(kv[k]) if k in kv else None
    alloc = num("Alloc system memory")
    inuse = num("In use system memory")
    return {
        "available": True,
        "alloc_system_memory_gib": round(alloc / 2**30, 2) if alloc is not None else None,
        "in_use_system_memory_gib": round(inuse / 2**30, 2) if inuse is not None else None,
        "device_utilization_pct": num("Device Utilization %"),
        "renderer_utilization_pct": num("Renderer Utilization %"),
    }


def llama_procs():
    out = sh("ps -Ao pid,rss,%cpu,etime,command")
    procs = []
    for line in out.splitlines()[1:]:
        if "/tools/llama.cpp/build/bin/llama-server" not in line:
            continue
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid, rss_kb, cpu, etime, cmd = parts
        port = None
        alias = None
        mp = re.search(r'--port\s+(\d+)', cmd)
        ma = re.search(r'--alias\s+(\S+)', cmd)
        if mp:
            port = int(mp.group(1))
        if ma:
            alias = ma.group(1)
        procs.append({"pid": int(pid), "rss_gib": round(int(rss_kb) / 2**20, 2),
                      "cpu_pct": float(cpu), "etime": etime,
                      "port": port, "alias": alias})
    return procs


def busy_processes(threshold=50.0):
    """Non-llama processes burning serious CPU — contamination detector."""
    out = sh("ps -Ao pid,%cpu,rss,command -r")
    hot = []
    for line in out.splitlines()[1:12]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, cpu, rss_kb, comm = parts
        if "/tools/llama.cpp/build/bin/llama-server" in comm:
            continue
        if float(cpu) >= threshold:
            hot.append({"pid": int(pid), "cpu_pct": float(cpu),
                        "rss_gib": round(int(rss_kb) / 2**20, 2),
                        "comm": comm.strip()[:160]})
    return hot


def listeners():
    out = sh("lsof -nP -iTCP:8080,8081 -sTCP:LISTEN")
    res = []
    for line in out.splitlines()[1:]:
        f = line.split()
        if len(f) >= 9:
            res.append({"command": f[0], "pid": int(f[1]), "name": f[-2] if f[-1] == "(LISTEN)" else f[-1]})
    return res


def health(port):
    out = sh(f"curl -s -o /dev/null -w '%{{http_code}}' -m 5 http://127.0.0.1:{port}/health")
    return out.strip() == "200"


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    snap = {
        "label": label,
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "memory": vm_stat(),
        "swap": swap(),
        "pressure": pressure(),
        "gpu": gpu(),
        "llama_servers": llama_procs(),
        "unrelated_hot_processes": busy_processes(),
        "listeners_8080_8081": listeners(),
        "health": {"a_8080": health(8080), "b_8081": health(8081)},
    }
    print(json.dumps(snap, indent=2))


if __name__ == "__main__":
    main()
