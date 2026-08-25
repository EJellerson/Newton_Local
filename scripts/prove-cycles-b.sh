#!/bin/bash
# Newton_Local — repeated Qwen-B lifecycle cycles.
#
# STOPPED -> acquire -> BUSY -> infer -> release -> IDLE -> terminate -> STOPPED
#
# Proves across N cycles that nothing accumulates: no zombie processes, no
# duplicate launchd jobs, no port leakage, and that memory returns each time.
# Qwen-A is checked every cycle.
set -uo pipefail
cd /Users/ericjellerson/Newton_Local
Q=./scripts/qwen-b
N="${1:-5}"
UID_N=$(id -u)
OUT=benchmarks/cycles-b.json

bcount(){ ps -Ao pid,command | awk '$2 ~ /llama-server$/ && /--alias qwen-b/' | wc -l | tr -d ' '; }
zcount(){ ps -Ao state,command | awk '$1 ~ /^Z/ && /llama-server/' | wc -l | tr -d ' '; }
jcount(){ launchctl list 2>/dev/null | grep -c "com.newton.qwen38server.b$"; }
bport(){ lsof -nP -iTCP:8081 -sTCP:LISTEN >/dev/null 2>&1 && echo OPEN || echo CLOSED; }
gpu(){ python3 scripts/snapshot-machine.py c | python3 -c "import sys,json;d=json.load(sys.stdin);print(f\"{d['gpu']['alloc_system_memory_gib']}|{d['memory']['free_gib']}|{d['memory']['reclaimable_headroom_gib']}|{d['memory']['compressor_occupied_gib']}|{d['memory']['swapouts']}|{d['pressure']['vm_pressure_level']}\")"; }

echo "=============================================================="
echo " QWEN-B REPEATED LIFECYCLE CYCLES  (N=$N)   $(date '+%H:%M:%S')"
echo "=============================================================="
$Q reconcile --orphan-policy terminate >/dev/null 2>&1
$Q terminate >/dev/null 2>&1
printf "\n%-3s %-9s %-7s %-7s %-6s %-6s %-6s %-6s %-10s %-9s %-8s %-6s\n" \
  "#" "pid" "spawn" "ready" "procs" "zomb" "jobs" "port" "gpu_alloc" "free" "swapout" "A"
echo "----------------------------------------------------------------------------------------------"
echo "[" > $OUT
for i in $(seq 1 "$N"); do
    PRE=$(gpu)
    OWNER="cycle-$i"
    R=$($Q acquire --owner "$OWNER" --json 2>/dev/null)
    PID=$(echo "$R" | python3 -c "import sys,json;print(json.load(sys.stdin)['pid'])" 2>/dev/null)
    SPAWN=$(echo "$R" | python3 -c "import sys,json;d=json.load(sys.stdin);print((d.get('last_start') or {}).get('spawn_latency_s'))" 2>/dev/null)
    READY=$(echo "$R" | python3 -c "import sys,json;d=json.load(sys.stdin);print((d.get('last_start') or {}).get('load_to_ready_s'))" 2>/dev/null)
    NCTX=$(echo "$R" | python3 -c "import sys,json;d=json.load(sys.stdin);print((d.get('runtime') or {}).get('n_ctx'))" 2>/dev/null)
    ANS=$(curl -sS --max-time 120 http://127.0.0.1:8081/v1/chat/completions -H 'Content-Type: application/json' \
      -d "{\"model\":\"qwen-b\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: CYCLE-$i-OK\"}],\"max_tokens\":384,\"temperature\":0}" \
      | python3 -c "import sys,json;print((json.load(sys.stdin)['choices'][0]['message'].get('content') or '').strip())" 2>/dev/null)
    UP=$(gpu)
    $Q release --owner "$OWNER" >/dev/null
    $Q terminate >/dev/null
    sleep 2
    POST=$(gpu)
    A=$(curl -sS --max-time 8 http://127.0.0.1:8080/health 2>/dev/null | grep -o ok || echo BAD)
    printf "%-3s %-9s %-7s %-7s %-6s %-6s %-6s %-6s %-10s %-9s %-8s %-6s\n" \
      "$i" "$PID" "$SPAWN" "$READY" "$(bcount)" "$(zcount)" "$(jcount)" "$(bport)" \
      "$(echo $POST|cut -d'|' -f1)" "$(echo $POST|cut -d'|' -f2)" "$(echo $POST|cut -d'|' -f5)" "$A"
    python3 - "$i" "$PID" "$SPAWN" "$READY" "$NCTX" "$ANS" "$PRE" "$UP" "$POST" "$(bcount)" "$(zcount)" "$(jcount)" "$(bport)" "$A" >> $OUT <<'PY'
import sys, json
i,pid,spawn,ready,nctx,ans,pre,up,post,procs,zomb,jobs,port,a = sys.argv[1:15]
f=lambda s:dict(zip(["gpu_alloc_gib","free_gib","headroom_gib","compressor_gib","swapouts","pressure"],s.split("|")))
print(json.dumps({"cycle":int(i),"pid":pid,"spawn_latency_s":spawn,"load_to_ready_s":ready,
 "n_ctx":nctx,"answer":ans,"mem_before":f(pre),"mem_worker_up":f(up),"mem_after_stop":f(post),
 "qwen_b_processes_after":int(procs),"zombies":int(zomb),"launchd_jobs":int(jobs),
 "port_8081_after":port,"qwen_a_health":a},indent=1)+",")
PY
done
echo "null]" >> $OUT
echo "----------------------------------------------------------------------------------------------"
echo
echo "== FINAL INVARIANTS =="
echo "  qwen-b processes : $(bcount)   (must be 0)"
echo "  zombie processes : $(zcount)   (must be 0)"
echo "  launchd b jobs   : $(jcount)   (must be 1 — registered, not running)"
echo "  port 8081        : $(bport)   (must be CLOSED)"
echo "  controller state : $($Q status | head -1)"
echo "  qwen-a           : $(curl -sS --max-time 8 http://127.0.0.1:8080/health 2>/dev/null)"
echo "  launchd b detail : $(launchctl print gui/$UID_N/com.newton.qwen38server.b 2>/dev/null | grep -E '^\s*(state|runs)' | tr -d '\t' | tr '\n' ' ')"
echo "  wrote $OUT"
