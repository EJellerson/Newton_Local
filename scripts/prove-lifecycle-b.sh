#!/bin/bash
# Newton_Local — Qwen-B elastic-worker lifecycle proving run.
#
# Exercises every lifecycle path the commission requires and prints the real
# command output for each step so a reader can distinguish evidence from
# assertion. Qwen-A is checked after every destructive step.
#
# Usage: prove-lifecycle-b.sh [ttl_wait_seconds]
set -uo pipefail
cd /Users/ericjellerson/Newton_Local
Q=./scripts/qwen-b
SNAP=./scripts/snapshot-machine.py
TTL=$(python3 -c "import json,subprocess;print(180)")
WAIT="${1:-210}"

step() { echo; echo "################ $* ################"; }
sub()  { echo; echo "---- $*"; }
bproc(){ ps -Ao pid,rss,command | awk '$3 ~ /llama-server$/ && /--alias qwen-b/ {printf "pid=%s rss=%.2fGiB\n",$1,$2/1048576}' | grep . || echo "NONE"; }
bport(){ lsof -nP -iTCP:8081 -sTCP:LISTEN >/dev/null 2>&1 && echo "OPEN" || echo "CLOSED"; }
ahealth(){ curl -sS --max-time 8 http://127.0.0.1:8080/health 2>/dev/null || echo "UNREACHABLE"; }
mem(){ python3 $SNAP "$1" | python3 -c "
import sys,json;d=json.load(sys.stdin);m=d['memory']
print(f\"  gpu_alloc={d['gpu']['alloc_system_memory_gib']}GiB app={m['app_memory_gib']}GiB cached={m['cached_files_gib']}GiB free={m['free_gib']}GiB headroom={m['reclaimable_headroom_gib']}GiB compressor={m['compressor_occupied_gib']}GiB swapouts={m['swapouts']} pressure={d['pressure']['vm_pressure_level']}\")
print('  llama:', [(s['alias'],s['pid'],s['rss_gib']) for s in d['llama_servers']])"; }

echo "=============================================================="
echo " QWEN-B ELASTIC WORKER — LIFECYCLE PROVING RUN"
echo " started $(date '+%Y-%m-%d %H:%M:%S %z')   idle TTL = ${TTL}s"
echo "=============================================================="

step "0. GROUND TRUTH BEFORE ANYTHING"
sub "controller"; $Q status
sub "process"; bproc
sub "port 8081"; bport
sub "qwen-a"; ahealth
sub "memory"; mem "lifecycle-t0"

step "1. RELEASE  (BUSY -> IDLE, idle TTL starts, worker STAYS LOADED)"
$Q release --owner commission2-start-proof
sub "state"; $Q status | head -3
sub "process still present?"; bproc

step "2. RE-ACQUIRE INSIDE THE IDLE TTL  (must NOT reload the model)"
PID_BEFORE=$(python3 -c "import json;print(json.load(open('state/qwen-b-lease.json'))['pid'])")
GEN_BEFORE=$(python3 -c "import json;print(json.load(open('state/qwen-b-lease.json'))['generation'])")
echo "pid before re-acquire = $PID_BEFORE   generation before = $GEN_BEFORE"
$Q acquire --owner commission2-reuse-proof --json
PID_AFTER=$(python3 -c "import json;print(json.load(open('state/qwen-b-lease.json'))['pid'])")
GEN_AFTER=$(python3 -c "import json;print(json.load(open('state/qwen-b-lease.json'))['generation'])")
echo "pid after  re-acquire = $PID_AFTER    generation after  = $GEN_AFTER"
if [ "$PID_BEFORE" = "$PID_AFTER" ] && [ "$GEN_BEFORE" = "$GEN_AFTER" ]; then
    echo "RESULT: SAME PID, SAME GENERATION -> model was NOT reloaded. PASS"
else
    echo "RESULT: pid/generation changed -> model WAS reloaded. FAIL"
fi

step "3. DUPLICATE-ACQUIRE PREVENTION  (second owner must be refused)"
sub "attempt by a different owner while BUSY"
$Q acquire --owner commission2-intruder --json; RC=$?
echo "exit code = $RC   (3 = lease conflict, as designed)"
sub "worker process count (must still be exactly 1)"; bproc
COUNT=$(ps -Ao pid,command | awk '$2 ~ /llama-server$/ && /--alias qwen-b/' | wc -l | tr -d ' ')
echo "qwen-b process count = $COUNT"

step "4. CONCURRENT ACQUIRE RACE  (two simultaneous cold acquirers)"
sub "release + terminate first, so both racers see STOPPED"
$Q release --owner commission2-reuse-proof >/dev/null
$Q terminate | tail -3
bproc; bport
sub "firing two acquires at once"
( $Q acquire --owner racer-1 --json > /tmp/racer1.json 2>/tmp/racer1.err; echo "racer-1 exit=$?" > /tmp/racer1.rc ) &
( $Q acquire --owner racer-2 --json > /tmp/racer2.json 2>/tmp/racer2.err; echo "racer-2 exit=$?" > /tmp/racer2.rc ) &
wait
cat /tmp/racer1.rc /tmp/racer2.rc
sub "racer-1 result"; head -c 700 /tmp/racer1.json; echo; cat /tmp/racer1.err
sub "racer-2 result"; head -c 700 /tmp/racer2.json; echo; cat /tmp/racer2.err
sub "process count after the race (must be exactly 1)"
COUNT=$(ps -Ao pid,command | awk '$2 ~ /llama-server$/ && /--alias qwen-b/' | wc -l | tr -d ' ')
echo "qwen-b process count = $COUNT"; bproc
sub "launchd job count (must be exactly 1)"
launchctl print "gui/$(id -u)/com.newton.qwen38server.b" 2>/dev/null | grep -E "^\s*(state|pid|runs)" 
sub "qwen-a"; ahealth

step "5. INFERENCE ON THE ACQUIRED WORKER"
OWNER=$(python3 -c "import json;print(json.load(open('state/qwen-b-lease.json'))['owner'])")
echo "lease owner = $OWNER"
curl -sS --max-time 120 http://127.0.0.1:8081/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen-b","messages":[{"role":"user","content":"Reply with exactly: B-LEASED-OK"}],"max_tokens":512,"temperature":0}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);m=d['choices'][0]['message'];print('answer:',(m.get('content') or '').strip());print('gen tok/s: %.2f'%d['timings']['predicted_per_second'])"

step "6. IDLE-TTL EXPIRY PATH  (release, then let the reaper terminate it)"
sub "memory BEFORE release"; mem "lifecycle-before-ttl"
$Q release --owner "$OWNER"
sub "state right after release"; $Q status | head -8
echo
echo "waiting ${WAIT}s for the ${TTL}s idle TTL to expire (reaper runs every 30s)..."
for i in $(seq 1 $((WAIT/30))); do
    sleep 30
    S=$($Q status --json 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['state'],d.get('idle_remaining_seconds'))" 2>/dev/null)
    echo "  t+$((i*30))s  state/idle_remaining = $S   proc=$(bproc | head -1)"
    case "$S" in STOPPED*) break;; esac
done
sub "state after TTL"; $Q status
sub "process"; bproc
sub "port 8081"; bport
sub "reaper log"; tail -6 logs/qwen-b/reaper.log 2>/dev/null || echo "(none)"
sub "memory AFTER ttl-terminate"; mem "lifecycle-after-ttl"
sub "qwen-a"; ahealth

echo
echo "=============================================================="
echo " LIFECYCLE PROVING RUN COMPLETE  $(date '+%H:%M:%S')"
echo "=============================================================="
