#!/bin/bash
# Newton_Local — Commission #4 acceptance prover: the model-OFF control plane.
#
# Proves, WITHOUT EVER STARTING QWEN, that:
#   * no Qwen process exists before, during, or after this prover;
#   * Qwen-A is registered on-demand-only in the gui domain (no RunAtLoad,
#     no KeepAlive) and Qwen-B kept its on-demand semantics;
#   * the control service reports Stopped and desired=STOPPED;
#   * Stop-while-stopped is idempotent and spawns nothing;
#   * mutating endpoints refuse cross-site requests;
#   * Start Chat targets the local Open WebUI;
#   * every service binds loopback only;
#   * Hermes and Open WebUI are healthy and independent of the model.
#
# Deliberately NOT exercised here (deferred until the operator authorises a
# live model test): Start, Restart, crash recovery, live singleton conflict.
#
#   ~/Newton_Local/scripts/prove-control-panel.sh
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UID_N="$(id -u)"
CTRL=http://127.0.0.1:8765
PASS=0; FAIL=0; WARN=0

ok()   { PASS=$((PASS+1)); printf '  ok    %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$1"; }
note() { WARN=$((WARN+1)); printf '  warn  %s\n' "$1"; }
hdr()  { printf '\n%s\n' "$1"; }

no_qwen() {   # no llama-server AND no mlx-serve process (any registered runtime)
    ! ps -Ao command= | grep -qE "^$REPO/tools/(llama\.cpp/build/bin/llama-server|mlx-serve/)";
}

jget() {  # jget <json-file> <python-expr over d>
    python3 -c "import json,sys; d=json.load(open('$1')); print($2)" 2>/dev/null
}

hdr "0. Precondition: absolutely no Qwen process on this machine"
no_qwen && ok "no llama-server process" || bad "a llama-server process EXISTS"

hdr "1. Qwen-A launchd job: registered, on-demand only, gui domain"
AP="$HOME/Library/LaunchAgents/com.newton.qwen38server.a.plist"
[ -f "$AP" ] && ok "agent plist installed" || bad "agent plist missing at $AP"
[ "$(plutil -extract RunAtLoad raw -o - "$AP" 2>/dev/null)" = "false" ] \
    && ok "RunAtLoad=false (no start at login)" || bad "RunAtLoad is not false"
[ "$(plutil -extract KeepAlive raw -o - "$AP" 2>/dev/null)" = "false" ] \
    && ok "KeepAlive=false (stop stays stopped)" || bad "KeepAlive is not false"
launchctl print "gui/$UID_N/com.newton.qwen38server.a" >/dev/null 2>&1 \
    && ok "registered in gui/$UID_N" || bad "not bootstrapped in gui/$UID_N"
lsof -nP -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1 \
    && bad "something is listening on 8080" || ok "port 8080 closed"

hdr "2. Qwen-B kept its Commission-#2 on-demand semantics"
BP="$HOME/Library/LaunchAgents/com.newton.qwen38server.b.plist"
[ "$(plutil -extract RunAtLoad raw -o - "$BP" 2>/dev/null)" = "false" ] \
    && ok "B RunAtLoad=false" || bad "B RunAtLoad changed"
"$REPO/scripts/qwen-b" status | head -1 | grep -q "state=STOPPED" \
    && ok "B controller reports STOPPED" || note "B controller not STOPPED"

hdr "3. Legacy root LaunchDaemon (migration status, informational)"
if [ -f /Library/LaunchDaemons/com.newton.qwen38server.a.plist ]; then
    note "legacy daemon plist still installed — reboot would auto-start Qwen; run: sudo $REPO/scripts/uninstall-launchdaemon.sh"
    launchctl print system/com.newton.qwen38server.a >/dev/null 2>&1 \
        && bad "legacy daemon is LOADED in the system domain right now" \
        || ok "legacy daemon not currently loaded"
else
    ok "legacy daemon fully removed — boot policy clean"
fi

hdr "4. Controller: status and idempotent stop, model stays off"
S="$("$REPO/scripts/qwen-a" status 2>/dev/null | head -1)"
echo "$S" | grep -q "state=STOPPED" \
    && ok "qwen-a status: STOPPED" || bad "qwen-a status: $S"
T="$("$REPO/scripts/qwen-a" terminate 2>&1)"
echo "$T" | grep -q "already STOPPED (idempotent)" \
    && ok "terminate while stopped is a no-op" || bad "terminate said: $T"
no_qwen && ok "still no llama-server process" || bad "terminate spawned something?!"

hdr "5. Control service: loopback, truthful Stopped state"
J="$(mktemp)"
curl -sS -m 5 "$CTRL/api/status" -o "$J" \
    && ok "GET /api/status responds" || bad "control service unreachable"
[ "$(jget "$J" "d['model']['state']")" = "Stopped" ] \
    && ok "panel reports model: Stopped" || bad "panel state = $(jget "$J" "d['model']['state']")"
[ "$(jget "$J" "d['model']['desired']")" = "STOPPED" ] \
    && ok "desired state: STOPPED" || bad "desired = $(jget "$J" "d['model']['desired']")"
[ "$(jget "$J" "d['chat_url']")" = "http://127.0.0.1:3000" ] \
    && ok "chat url is the local Open WebUI" || bad "chat url wrong"

hdr "6. Stop button while already stopped: idempotent, spawns nothing (twice)"
for i in 1 2; do
    CODE=$(curl -sS -m 5 -o /dev/null -w '%{http_code}' -X POST \
           -H 'X-Newton-Control: 1' "$CTRL/api/model/stop")
    [ "$CODE" = "202" ] && ok "stop #$i accepted (202)" || bad "stop #$i http=$CODE"
    for _ in $(seq 1 30); do
        curl -sS -m 5 "$CTRL/api/status" -o "$J" 2>/dev/null
        [ "$(jget "$J" "d['operation'] is None")" = "True" ] && break
        /bin/sleep 0.5
    done
    [ "$(jget "$J" "d['model']['state']")" = "Stopped" ] \
        && ok "still Stopped after stop #$i" || bad "state after stop #$i: $(jget "$J" "d['model']['state']")"
done
LAST_OK="$(jget "$J" "d['last_operation']['ok']")"
[ "$LAST_OK" = "True" ] && ok "stop reported success (idempotent)" \
                        || bad "last stop op not ok"
no_qwen && ok "no llama-server after stop tests" || bad "a process appeared!"

hdr "7. Mutation guards (loopback CSRF/rebinding defence)"
CODE=$(curl -sS -m 5 -o /dev/null -w '%{http_code}' -X POST "$CTRL/api/model/stop")
[ "$CODE" = "403" ] && ok "POST without panel header refused (403)" \
                    || bad "unguarded POST got $CODE"
CODE=$(curl -sS -m 5 -o /dev/null -w '%{http_code}' -H 'Host: evil.example' \
       "$CTRL/api/status")
[ "$CODE" = "403" ] && ok "foreign Host header refused (403)" \
                    || bad "foreign Host got $CODE"
CODE=$(curl -sS -m 5 -o /dev/null -w '%{http_code}' -X POST \
       -H 'X-Newton-Control: 1' -H 'Origin: http://evil.example' \
       "$CTRL/api/model/stop")
[ "$CODE" = "403" ] && ok "cross-origin POST refused (403)" \
                    || bad "cross-origin POST got $CODE"

hdr "8. Start Chat: panel redirect reaches the local Open WebUI"
LOC=$(curl -sS -m 5 -o /dev/null -w '%{redirect_url}' "$CTRL/chat")
[ "$LOC" = "http://127.0.0.1:3000/" ] || [ "$LOC" = "http://127.0.0.1:3000" ] \
    && ok "/chat redirects to $LOC" || bad "/chat redirects to '$LOC'"

hdr "9. Hermes + Open WebUI healthy and independent of the (stopped) model"
KEY="$(/usr/bin/sed -n 's/^API_SERVER_KEY=//p' "$HOME/.hermes/profiles/localchat/.env" 2>/dev/null | head -1)"
CODE=$(curl -sS -m 5 -o /dev/null -w '%{http_code}' \
       -H "Authorization: Bearer $KEY" http://127.0.0.1:8642/health)
[ "$CODE" = "200" ] && ok "Hermes /health 200" || bad "Hermes /health $CODE"
CODE=$(curl -sS -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:3000/health)
[ "$CODE" = "200" ] && ok "Open WebUI /health 200" || bad "Open WebUI /health $CODE"

hdr "10. Loopback-only binds (control, Hermes, Open WebUI)"
for P in 8765 8642 3000; do
    BINDS="$(lsof -nP -iTCP:$P -sTCP:LISTEN 2>/dev/null | tail -n +2 | awk '{print $9}' | sort -u)"
    if [ -z "$BINDS" ]; then
        bad "nothing listening on $P"
    elif echo "$BINDS" | grep -qv '^127\.0\.0\.1:'; then
        bad "port $P binds beyond loopback: $BINDS"
    else
        ok "port $P loopback only ($BINDS)"
    fi
done

hdr "11. Auto-start roster: infra at login yes, Qwen no"
for L in ai.hermes.gateway-localchat com.newton.openwebui com.newton.control; do
    V="$(plutil -extract RunAtLoad raw -o - "$HOME/Library/LaunchAgents/$L.plist" 2>/dev/null)"
    [ "$V" = "true" ] && ok "$L RunAtLoad=true" || bad "$L RunAtLoad=$V"
done
for L in com.newton.qwen38server.a com.newton.qwen38server.b; do
    V="$(plutil -extract RunAtLoad raw -o - "$HOME/Library/LaunchAgents/$L.plist" 2>/dev/null)"
    [ "$V" = "false" ] && ok "$L RunAtLoad=false" || bad "$L RunAtLoad=$V"
done

hdr "12. Global model selection (Commission #5): shared state, locks, availability"
M="$(mktemp)"
curl -sS -m 10 "$CTRL/api/models" -o "$M" && ok "GET /api/models responds" || bad "GET /api/models failed"
[ "$(jget "$M" "sorted(d['models'].keys())")" = "['flash-next', 'qwen27b']" ] \
    && ok "registry exposes both models" || bad "models = $(jget "$M" "list(d['models'].keys())")"
SEL_BEFORE="$(jget "$M" "d['selected']")"
[ "$(jget "$M" "d['selection_locked']")" = "False" ] \
    && ok "selector unlocked while both slots are stopped" || bad "selector locked: $(jget "$M" "d['occupancy']['reasons']")"
FLASH_AVAIL="$(jget "$M" "d['models']['flash-next']['available']")"
CODE=$(curl -sS -m 10 -o /dev/null -w '%{http_code}' -X POST -H 'X-Newton-Control: 1' \
       -H 'Content-Type: application/json' -d '{"model":"no-such-model"}' "$CTRL/api/model/select")
[ "$CODE" = "400" ] && ok "unknown model id refused (400)" || bad "unknown id got $CODE"
CODE=$(curl -sS -m 10 -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
       -d '{"model":"qwen27b"}' "$CTRL/api/model/select")
[ "$CODE" = "403" ] && ok "select without panel header refused (403)" || bad "unguarded select got $CODE"
if [ "$FLASH_AVAIL" = "True" ]; then
    CODE=$(curl -sS -m 10 -o /dev/null -w '%{http_code}' -X POST -H 'X-Newton-Control: 1' \
           -H 'Content-Type: application/json' -d '{"model":"flash-next"}' "$CTRL/api/model/select")
    [ "$CODE" = "200" ] && ok "select flash-next accepted while stopped (200)" || bad "select flash-next got $CODE"
    [ "$("$REPO/scripts/newton-model" status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["selected"])')" = "flash-next" ] \
        && ok "CLI sees the panel's selection (shared state)" || bad "CLI disagrees with panel"
else
    CODE=$(curl -sS -m 10 -o /dev/null -w '%{http_code}' -X POST -H 'X-Newton-Control: 1' \
           -H 'Content-Type: application/json' -d '{"model":"flash-next"}' "$CTRL/api/model/select")
    [ "$CODE" = "422" ] && ok "select flash-next refused: unavailable on this host (422)" || bad "unavailable select got $CODE"
    note "flash-next unavailable here: $(jget "$M" "'; '.join(d['models']['flash-next']['reasons'])")"
fi
CODE=$(curl -sS -m 10 -o /dev/null -w '%{http_code}' -X POST -H 'X-Newton-Control: 1' \
       -H 'Content-Type: application/json' -d '{"model":"qwen27b"}' "$CTRL/api/model/select")
[ "$CODE" = "200" ] && ok "select qwen27b accepted (200) — restored default" || bad "select qwen27b got $CODE"
[ "$("$REPO/scripts/newton-model" status --json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["selected"], d["selection_source"])')" = "qwen27b recorded" ] \
    && ok "CLI: selected qwen27b (recorded) — same state file as the panel" || bad "CLI state mismatch"
[ -f "$REPO/state/model-selection.json" ] && ok "selection persisted at state/model-selection.json" || bad "selection file missing"
no_qwen && ok "selection operations started nothing" || bad "a model process appeared during selection tests"
rm -f "$M"

hdr "13. Final: Qwen was never started by this prover"
no_qwen && ok "no llama-server process at exit" || bad "llama-server exists at exit"
rm -f "$J"

printf '\n%d ok, %d warnings, %d failures\n' "$PASS" "$WARN" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
