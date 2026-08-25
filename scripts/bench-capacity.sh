#!/bin/bash
# Newton_Local — A-alone vs A+B capacity characterisation.
#
# Runs the three phases the commission's comparison table requires, in order,
# with IDENTICAL parameters. No tuning happens between phases.
#
#     1. a-alone            B stopped; only Qwen-A exists
#     2. ab-concurrent      B acquired; A and B generate simultaneously
#     3. a-after-b-stopped  B terminated; A alone again
#
# --require-quiet waits for the unrelated ML training sweep to be absent
# before starting, and every phase records for itself whether unrelated heavy
# load was observed while it ran.
set -uo pipefail
cd /Users/ericjellerson/Newton_Local
Q=./scripts/qwen-b
TOKENS="${TOKENS:-500}"
PAD="${PAD:-3000}"
TAG="${TAG:-clean}"
REQUIRE_QUIET=0
QUIET_WAIT="${QUIET_WAIT:-3600}"
[ "${1:-}" = "--require-quiet" ] && REQUIRE_QUIET=1

mlbusy(){ ps -Ao command= | grep -q "ML_Model/SP6/tools/run_e" && echo yes || echo no; }
hot(){ ps -Ao %cpu,command -r 2>/dev/null | awk 'NR>1 && $1>150 && $0 !~ /llama-server/ {print}' | head -3; }

if [ "$REQUIRE_QUIET" = "1" ]; then
    echo "== waiting for a quiet window (no ML_Model/SP6 run_e* process)"
    DEADLINE=$(( $(date +%s) + QUIET_WAIT ))
    while [ "$(date +%s)" -lt "$DEADLINE" ]; do
        if [ "$(mlbusy)" = "no" ]; then
            sleep 8
            [ "$(mlbusy)" = "no" ] && { echo "   quiet at $(date '+%H:%M:%S')"; break; }
        fi
        sleep 5
    done
    [ "$(mlbusy)" = "yes" ] && echo "   WARNING: no quiet window within ${QUIET_WAIT}s; proceeding CONTAMINATED"
fi

NONCE="$TAG-$(date +%s)"
echo
echo "=============================================================="
echo " CAPACITY BENCHMARK  tag=$TAG tokens=$TOKENS pad_words=$PAD"
echo " started $(date '+%H:%M:%S')   ml_busy=$(mlbusy)"
echo "=============================================================="

echo; echo "######## PHASE 1: A ALONE (B stopped) ########"
$Q reconcile --orphan-policy terminate >/dev/null 2>&1
$Q terminate >/dev/null 2>&1
echo "  qwen-b state: $($Q status | head -1)"
python3 scripts/bench-dual.py --phase "a-alone-$TAG" --targets a --tokens "$TOKENS" \
    --prompt-pad-words "$PAD" --nonce "$NONCE-p1" --out "benchmarks/bench-a-alone-$TAG.json"

echo; echo "######## PHASE 2: A + B CONCURRENT ########"
$Q acquire --owner "capacity-bench-$TAG" --json | python3 -c "import sys,json;d=json.load(sys.stdin);print('  acquired pid',d['pid'],'ready_s',(d.get('last_start') or {}).get('load_to_ready_s'))"
python3 scripts/bench-dual.py --phase "ab-concurrent-$TAG" --targets a,b --tokens "$TOKENS" \
    --prompt-pad-words "$PAD" --nonce "$NONCE-p2" --out "benchmarks/bench-ab-$TAG.json"

echo; echo "######## PHASE 3: A AFTER B STOPPED ########"
$Q release --owner "capacity-bench-$TAG" >/dev/null
$Q terminate | tail -3
sleep 5
python3 scripts/bench-dual.py --phase "a-after-b-$TAG" --targets a --tokens "$TOKENS" \
    --prompt-pad-words "$PAD" --nonce "$NONCE-p3" --out "benchmarks/bench-a-after-b-$TAG.json"

echo; echo "== ml_busy at finish: $(mlbusy)"; hot
echo "== done $(date '+%H:%M:%S')"
