#!/bin/bash
cd /Users/ericjellerson/Newton_Local
Q=./scripts/qwen-b; P=0; F=0
ck(){ if [ "$2" = "$3" ]; then echo "  PASS  $1  ($2)"; P=$((P+1)); else echo "  FAIL  $1  expected='$3' got='$2'"; F=$((F+1)); fi; }
ckc(){ if echo "$2" | grep -q "$3"; then echo "  PASS  $1"; P=$((P+1)); else echo "  FAIL  $1  ('$2' lacks '$3')"; F=$((F+1)); fi; }

echo "== 1-2. both services identity"
ckc "A healthy on 8080" "$(curl -sS -m 8 http://127.0.0.1:8080/health)" "ok"
ck  "A socket loopback-only" "$(lsof -nP -iTCP:8080 -sTCP:LISTEN | awk 'NR==2{print $9}')" "127.0.0.1:8080"
ck  "B launchd registered in gui/$(id -u)" "$(launchctl print gui/$(id -u)/com.newton.qwen38server.b >/dev/null 2>&1 && echo yes || echo no)" "yes"

echo "== 6-7. B normally stopped, no autoload, memory reclaimed"
ck  "B controller state" "$($Q status --json | python3 -c 'import sys,json;print(json.load(sys.stdin)["state"])')" "STOPPED"
ck  "B process count" "$(ps -Ao pid,command | awk '$2 ~ /llama-server$/ && /--alias qwen-b/' | wc -l | tr -d ' ')" "0"
ck  "port 8081 closed" "$(lsof -nP -iTCP:8081 -sTCP:LISTEN >/dev/null 2>&1 && echo OPEN || echo CLOSED)" "CLOSED"
ck  "B RunAtLoad" "$(plutil -extract RunAtLoad raw -o - config/com.newton.qwen38server.b.plist)" "false"
ck  "B KeepAlive" "$(plutil -extract KeepAlive raw -o - config/com.newton.qwen38server.b.plist)" "false"
ck  "GPU alloc back to A-only (~46 GiB)" "$(python3 scripts/snapshot-machine.py f | python3 -c 'import sys,json;g=json.load(sys.stdin)["gpu"]["alloc_system_memory_gib"];print("yes" if g<55 else "no ("+str(g)+")")')" "yes"

echo "== 3. shared canonical artifacts, no duplicate model"
ck  "GGUF copies on disk" "$(find ~ -name 'Qwen3.8-27B-Q8_0.gguf' -not -path '*/.Trash/*' 2>/dev/null | wc -l | tr -d ' ')" "1"
ck  "mmproj copies on disk" "$(find ~ -name 'mmproj-Qwen3.8-27B-f16.gguf' -not -path '*/.Trash/*' 2>/dev/null | wc -l | tr -d ' ')" "1"

echo "== 8. deterministic start -> READY"
R=$($Q acquire --owner final-acceptance --json)
ck  "acquire state" "$(echo "$R" | python3 -c 'import sys,json;print(json.load(sys.stdin)["state"])')" "BUSY"
echo "      start: spawn=$(echo "$R"|python3 -c 'import sys,json;print(json.load(sys.stdin)["last_start"]["spawn_latency_s"])')s ready=$(echo "$R"|python3 -c 'import sys,json;print(json.load(sys.stdin)["last_start"]["load_to_ready_s"])')s"

echo "== 4-5. B runtime + loopback"
PR=$(curl -sS -m 10 http://127.0.0.1:8081/props)
ck  "B ftype" "$(echo "$PR"|python3 -c 'import sys,json;print(json.load(sys.stdin)["model_ftype"])')" "Q8_0"
ck  "B n_ctx" "$(echo "$PR"|python3 -c 'import sys,json;print(json.load(sys.stdin)["default_generation_settings"]["n_ctx"])')" "262144"
ck  "B vision" "$(echo "$PR"|python3 -c 'import sys,json;print(json.load(sys.stdin)["modalities"]["vision"])')" "True"
ck  "B model == A model" "$(echo "$PR"|python3 -c 'import sys,json;print(json.load(sys.stdin)["model_path"])')" "$(curl -sS -m 10 http://127.0.0.1:8080/props|python3 -c 'import sys,json;print(json.load(sys.stdin)["model_path"])')"
ck  "B socket loopback-only" "$(lsof -nP -iTCP:8081 -sTCP:LISTEN | awk 'NR==2{print $9}')" "127.0.0.1:8081"
ck  "B unreachable on LAN IP" "$(curl -s -m 4 -o /dev/null -w '%{http_code}' http://10.0.0.204:8081/health)" "000"

echo "== 9. A usable while B is up"
ckc "A inference with B running" "$(./scripts/qwen-a-infer.sh 'Reply with exactly: FINAL-A-OK' 2>&1 | grep answer)" "FINAL-A-OK"

echo "== 18. duplicate acquire prevented"
$Q acquire --owner final-intruder >/dev/null 2>&1; ck "conflicting acquire exit code" "$?" "3"
ck  "still exactly one B process" "$(ps -Ao pid,command | awk '$2 ~ /llama-server$/ && /--alias qwen-b/' | wc -l | tr -d ' ')" "1"

echo "== 19-20. release -> IDLE, reacquire reuses"
$Q release --owner final-acceptance >/dev/null
ck  "state after release" "$($Q status --json | python3 -c 'import sys,json;print(json.load(sys.stdin)["state"])')" "IDLE"
PID1=$(python3 -c 'import json;print(json.load(open("state/qwen-b-lease.json"))["pid"])')
R2=$($Q acquire --owner final-acceptance-2 --json)
ck  "reacquire reused worker" "$(echo "$R2"|python3 -c 'import sys,json;print(json.load(sys.stdin)["reused_loaded_worker"])')" "True"
ck  "reacquire did not reload" "$(echo "$R2"|python3 -c 'import sys,json;print(json.load(sys.stdin)["model_reloaded"])')" "False"
ck  "same pid across reacquire" "$(echo "$R2"|python3 -c 'import sys,json;print(json.load(sys.stdin)["pid"])')" "$PID1"

echo "== 14-15-21. terminate, reclaim, A unaffected"
$Q release --owner final-acceptance-2 >/dev/null
G1=$(python3 scripts/snapshot-machine.py f|python3 -c 'import sys,json;print(json.load(sys.stdin)["gpu"]["alloc_system_memory_gib"])')
$Q terminate >/dev/null
sleep 4
G2=$(python3 scripts/snapshot-machine.py f|python3 -c 'import sys,json;print(json.load(sys.stdin)["gpu"]["alloc_system_memory_gib"])')
echo "      GPU alloc ${G1} -> ${G2} GiB"
ck  "GPU reclaimed >40 GiB" "$(python3 -c "print('yes' if $G1-$G2>40 else 'no')")" "yes"
ck  "B process gone" "$(ps -Ao pid,command | awk '$2 ~ /llama-server$/ && /--alias qwen-b/' | wc -l | tr -d ' ')" "0"
ck  "port 8081 closed" "$(lsof -nP -iTCP:8081 -sTCP:LISTEN >/dev/null 2>&1 && echo OPEN || echo CLOSED)" "CLOSED"
ckc "A healthy after B stop" "$(curl -sS -m 8 http://127.0.0.1:8080/health)" "ok"
ck  "final B state" "$($Q status --json | python3 -c 'import sys,json;print(json.load(sys.stdin)["state"])')" "STOPPED"

echo "== 27-29. scope exclusions"
ck  "no Hermes" "$(ls ~/Newton_Local 2>/dev/null | grep -ci hermes)" "0"
ck  "no Foreman impl" "$(find ~/Newton_Local -iname '*foreman*' -not -path '*/.git/*' 2>/dev/null | wc -l | tr -d ' ')" "0"
ck  "no third instance (8082+)" "$(lsof -nP -iTCP:8082 -sTCP:LISTEN 2>/dev/null | wc -l | tr -d ' ')" "0"
ck  "llama-server processes total" "$(ps -Ao pid,command | awk '$2 ~ /llama-server$/' | wc -l | tr -d ' ')" "1"

echo
echo "=============================================="
echo " ACCEPTANCE: $P passed, $F failed"
echo "=============================================="
exit $F
