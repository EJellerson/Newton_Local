#!/bin/bash
# Newton_Local — side-by-side status of BOTH instances.
#   Qwen-A : persistent primary   : system LaunchDaemon : 127.0.0.1:8080
#   Qwen-B : elastic worker       : user LaunchAgent    : 127.0.0.1:8081
UID_N="$(id -u)"

echo "==================== QWEN-A (persistent primary) ===================="
echo "-- launchd (system domain)"
launchctl print system/com.newton.qwen38server.a 2>/dev/null \
  | grep -E "^\s*(state|pid|runs|last exit code|path|domain)\s*=" | sed 's/^/   /' \
  || echo "   not loaded in system domain"
echo "-- socket"
lsof -nP -iTCP:8080 -sTCP:LISTEN 2>/dev/null | tail -n +2 | sed 's/^/   /' \
  || echo "   nothing listening on 8080"
echo "-- runtime"
if curl -sS --max-time 5 http://127.0.0.1:8080/health >/dev/null 2>&1; then
    /Users/ericjellerson/Newton_Local/scripts/qwen-props.sh 8080
else
    echo "   /health unreachable"
fi
echo "-- process"
ps -Ao pid,rss,%cpu,etime,command 2>/dev/null \
  | awk '$5 ~ /^\/Users\/ericjellerson\/Newton_Local\/tools\/llama.cpp\/build\/bin\/llama-server$/ && /--alias qwen-a/ {printf "   pid=%s rss=%.2fGiB cpu=%s%% up=%s\n", $1, $2/1048576, $3, $4}' \
  | grep . || echo "   no qwen-a process"

echo
echo "==================== QWEN-B (elastic worker) ========================"
echo "-- launchd (gui/$UID_N domain)"
launchctl print "gui/$UID_N/com.newton.qwen38server.b" 2>/dev/null \
  | grep -E "^\s*(state|pid|runs|last exit code|path|domain)\s*=" | sed 's/^/   /' \
  || echo "   not bootstrapped in gui/$UID_N"
echo "-- socket"
lsof -nP -iTCP:8081 -sTCP:LISTEN 2>/dev/null | tail -n +2 | sed 's/^/   /' \
  || echo "   nothing listening on 8081 (expected when stopped)"
echo "-- controller"
/Users/ericjellerson/Newton_Local/scripts/qwen-b status | sed 's/^/   /'

echo
echo "==================== MACHINE ========================================"
python3 /Users/ericjellerson/Newton_Local/scripts/snapshot-machine.py "qwen-status" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin); m = d['memory']
print(f\"   app_memory={m['app_memory_gib']}GiB  cached_files={m['cached_files_gib']}GiB  wired={m['wired_gib']}GiB\")
print(f\"   free={m['free_gib']}GiB  reclaimable_headroom={m['reclaimable_headroom_gib']}GiB\")
print(f\"   swap_used={d['swap']['used_mib']}MiB  swapouts={m['swapouts']}  compressor={m['compressor_occupied_gib']}GiB\")
print(f\"   memory_pressure_level={d['pressure']['vm_pressure_level']}  free_pct={d['pressure']['free_percentage']}%\")
print(f\"   gpu_alloc={d['gpu']['alloc_system_memory_gib']}GiB  gpu_util={d['gpu']['device_utilization_pct']}%\")
hot = d['unrelated_hot_processes']
print('   unrelated_hot_processes: ' + (', '.join(f\"pid{h['pid']} {h['cpu_pct']}%CPU {h['rss_gib']}GiB\" for h in hot) if hot else 'none'))
"
