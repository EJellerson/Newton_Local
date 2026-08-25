#!/bin/bash
# Newton_Local — Qwen-B stop / memory-reclamation evidence.
# Runs from the DUAL-RUNNING state and records before/after for every signal
# the commission asks about.
set -uo pipefail
cd /Users/ericjellerson/Newton_Local
Q=./scripts/qwen-b
snap(){ python3 scripts/snapshot-machine.py "$1"; }
show(){ python3 -c "
import sys,json;d=json.load(open(sys.argv[1]));m=d['memory']
print(f\"  gpu_alloc            {d['gpu']['alloc_system_memory_gib']:>8.2f} GiB\")
print(f\"  free                 {m['free_gib']:>8.2f} GiB\")
print(f\"  app_memory           {m['app_memory_gib']:>8.2f} GiB\")
print(f\"  cached_files         {m['cached_files_gib']:>8.2f} GiB\")
print(f\"  reclaimable_headroom {m['reclaimable_headroom_gib']:>8.2f} GiB\")
print(f\"  wired                {m['wired_gib']:>8.2f} GiB\")
print(f\"  compressor           {m['compressor_occupied_gib']:>8.3f} GiB\")
print(f\"  swapouts             {m['swapouts']:>8}\")
print(f\"  swap_used            {d['swap']['used_mib']:>8.1f} MiB\")
print(f\"  pressure_level       {d['pressure']['vm_pressure_level']:>8}\")
print(f\"  llama_servers        {[(s['alias'],s['pid'],s['rss_gib']) for s in d['llama_servers']]}\")
print(f\"  listeners            {[l['name'] for l in d['listeners_8080_8081']]}\")
print(f\"  health A/B           {d['health']}\")
" "$1"; }

echo "=============================================================="
echo " QWEN-B STOP / RECLAMATION EVIDENCE   $(date '+%H:%M:%S')"
echo "=============================================================="
echo
echo "######## BEFORE B STOP (dual-running) ########"
snap "reclaim-before" > benchmarks/snap-reclaim-before.json
echo "  qwen-b controller: $($Q status | head -1)"
echo "  qwen-b process   : $(ps -Ao pid,command | awk '$2 ~ /llama-server$/ && /--alias qwen-b/ {print "pid="$1}')"
echo "  port 8081        : $(lsof -nP -iTCP:8081 -sTCP:LISTEN >/dev/null 2>&1 && echo OPEN || echo CLOSED)"
show benchmarks/snap-reclaim-before.json

echo
echo "######## STOP VIA THE CONTROLLER ########"
OWNER=$(python3 -c "import json;print(json.load(open('state/qwen-b-lease.json'))['owner'])" 2>/dev/null)
[ "$OWNER" != "None" ] && [ -n "$OWNER" ] && $Q release --owner "$OWNER" | head -1
$Q terminate

echo
echo "######## AFTER B STOP ########"
sleep 6
snap "reclaim-after" > benchmarks/snap-reclaim-after.json
echo "  qwen-b controller: $($Q status | head -1)"
echo "  qwen-b process   : $(ps -Ao pid,command | awk '$2 ~ /llama-server$/ && /--alias qwen-b/ {print "pid="$1}' | grep . || echo NONE)"
echo "  port 8081        : $(lsof -nP -iTCP:8081 -sTCP:LISTEN >/dev/null 2>&1 && echo OPEN || echo CLOSED)"
show benchmarks/snap-reclaim-after.json

echo
echo "######## DELTA ########"
python3 -c "
import json
a=json.load(open('benchmarks/snap-reclaim-before.json')); b=json.load(open('benchmarks/snap-reclaim-after.json'))
def r(n,x,y,u='GiB'):
    print(f'  {n:<24}{x:>9.2f} -> {y:>9.2f}   ({y-x:+.2f} {u})')
r('gpu_alloc', a['gpu']['alloc_system_memory_gib'], b['gpu']['alloc_system_memory_gib'])
for k,l in [('free_gib','free'),('app_memory_gib','app_memory'),('cached_files_gib','cached_files'),
            ('reclaimable_headroom_gib','reclaimable_headroom'),('wired_gib','wired'),
            ('compressor_occupied_gib','compressor')]:
    r(l, a['memory'][k], b['memory'][k])
print(f\"  {'swapouts':<24}{a['memory']['swapouts']:>9} -> {b['memory']['swapouts']:>9}\")
print(f\"  {'pressure_level':<24}{a['pressure']['vm_pressure_level']:>9} -> {b['pressure']['vm_pressure_level']:>9}\")
"

echo
echo "######## QWEN-A AFTER B STOP ########"
./scripts/qwen-props.sh 8080
./scripts/qwen-a-infer.sh "Reply with exactly: A-HEALTHY-AFTER-B-STOP" 2>&1 | tail -2
echo
echo "=============================================================="
