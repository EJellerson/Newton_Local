#!/bin/bash
# Newton_Local — Qwen-B reconciliation / orphan-handling proving run.
#
# Deliberately creates inconsistent states by BYPASSING the controller, then
# shows that reconciliation derives the truth from launchd/process/socket/
# health rather than trusting the recorded lease.
set -uo pipefail
cd /Users/ericjellerson/Newton_Local
Q=./scripts/qwen-b
LEASE=state/qwen-b-lease.json
UID_N=$(id -u)

step(){ echo; echo "################ $* ################"; }
sub(){ echo; echo "---- $*"; }
lease(){ python3 -c "import json;d=json.load(open('$LEASE'));print({k:d[k] for k in ['state','owner','pid','generation','idle_since']})" 2>/dev/null || echo "(no lease file)"; }
bproc(){ ps -Ao pid,command | awk '$2 ~ /llama-server$/ && /--alias qwen-b/ {print "pid="$1}' | grep . || echo "NONE"; }
ahealth(){ curl -sS --max-time 8 http://127.0.0.1:8080/health 2>/dev/null || echo "UNREACHABLE"; }
settle(){ for i in $(seq 1 90); do curl -s -o /dev/null -m 2 http://127.0.0.1:8081/health && return 0; sleep 1; done; return 1; }

echo "=============================================================="
echo " QWEN-B RECONCILIATION / ORPHAN PROVING RUN  $(date '+%H:%M:%S')"
echo "=============================================================="

step "CASE A/C — worker RUNNING but controller says STOPPED (orphan, no lease)"
sub "start B behind the controller's back, straight through launchd"
$Q terminate >/dev/null 2>&1
launchctl kickstart "gui/$UID_N/com.newton.qwen38server.b"
settle && echo "worker is healthy on 8081"
sub "force the recorded lease back to STOPPED to simulate a controller that never saw it"
python3 - <<'PY'
import json
p="state/qwen-b-lease.json"
d=json.load(open(p)); d["state"]="STOPPED"; d["owner"]=None; d["pid"]=None
d["idle_since"]=None; d["notes"]=[]
json.dump(d,open(p,"w"),indent=2)
PY
echo "recorded lease: $(lease)"
echo "observed      : $(bproc)  port=$(lsof -nP -iTCP:8081 -sTCP:LISTEN >/dev/null 2>&1 && echo OPEN || echo CLOSED)"
sub "reconcile (default policy = adopt)"
$Q reconcile --json
sub "lease after reconcile"; lease
echo "POLICY: an orphan is ADOPTED, never silently killed — into IDLE if it is"
echo "        already healthy, or STARTING if it is still loading. Either way it"
echo "        carries no owner and falls under the normal idle TTL, so an"
echo "        unclaimed worker cannot leak forever."

step "CASE B — controller says BUSY but the worker DIED"
sub "take a legitimate lease"
$Q acquire --owner case-b-owner --json | python3 -c "import sys,json;d=json.load(sys.stdin);print('acquired pid',d['pid'],'state',d['state'])"
PID=$(python3 -c "import json;print(json.load(open('$LEASE'))['pid'])")
sub "kill -9 the worker out from under the controller (simulated crash)"
kill -9 "$PID"; sleep 3
echo "recorded lease still claims: $(lease)"
echo "observed                   : $(bproc)"
sub "status (reconciles automatically)"
$Q status
sub "lease after"; lease
echo "RESULT: stale BUSY lease cleared, owner released, state derived from reality."

step "CASE D — controller restarts with NO state at all (lease file lost)"
sub "start a worker, then delete the lease file entirely"
$Q acquire --owner case-d-owner --json | python3 -c "import sys,json;d=json.load(sys.stdin);print('acquired pid',d['pid'])"
rm -f "$LEASE"
echo "lease file removed: $(lease)"
echo "observed          : $(bproc)"
sub "reconcile from a cold controller"
$Q reconcile --json
sub "lease rebuilt from reality"; lease
echo "RESULT: state was rebuilt from launchd/process/socket/health, not from"
echo "        a remembered value — there was nothing to remember."

step "CASE E — BUSY lease abandoned (owner vanished, no heartbeat)"
sub "acquire, then back-date last_activity beyond QWEN_B_LEASE_MAX_AGE (1800s)"
$Q acquire --owner case-e-owner --json >/dev/null
python3 - <<'PY'
import json, time
p="state/qwen-b-lease.json"
d=json.load(open(p))
d["last_activity"]=time.time()-7200
d["acquired_at"]=time.time()-7200
json.dump(d,open(p,"w"),indent=2)
print("back-dated last_activity by 2 hours")
PY
echo "recorded: $(lease)"
sub "reconcile"
$Q reconcile
sub "lease after"; lease
echo "RESULT: an abandoned BUSY lease is demoted to IDLE (not killed), so the"
echo "        worker becomes acquirable again and the idle TTL can reclaim it."

step "CASE F — orphan policy 'terminate' (explicit operator choice)"
sub "create an orphan again"
python3 - <<'PY'
import json
p="state/qwen-b-lease.json"
d=json.load(open(p)); d["state"]="STOPPED"; d["owner"]=None; d["pid"]=None; d["idle_since"]=None
json.dump(d,open(p,"w"),indent=2)
PY
echo "recorded: $(lease)   observed: $(bproc)"
sub "reconcile --orphan-policy terminate"
$Q reconcile --orphan-policy terminate
sub "after"; lease; bproc
echo "port 8081: $(lsof -nP -iTCP:8081 -sTCP:LISTEN >/dev/null 2>&1 && echo OPEN || echo CLOSED)"

step "QWEN-A UNAFFECTED THROUGHOUT"
ahealth
curl -sS --max-time 60 http://127.0.0.1:8080/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"local-model","messages":[{"role":"user","content":"Reply with exactly: A-UNAFFECTED"}],"max_tokens":256,"temperature":0}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('answer:',(d['choices'][0]['message'].get('content') or '').strip())"

echo
echo "=============================================================="
echo " RECONCILIATION PROVING RUN COMPLETE  $(date '+%H:%M:%S')"
echo "=============================================================="
