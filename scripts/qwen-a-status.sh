#!/bin/bash
# Newton_Local — Qwen-A service status.
LABEL="com.newton.qwen38server.a"
echo "== launchd =="
sudo launchctl print system/$LABEL 2>/dev/null | grep -E "^\s*(state|pid|program|last exit|runs) " || echo "  not loaded in system domain"
echo
echo "== process =="
pgrep -fl llama-server || echo "  no llama-server process"
echo
echo "== listening socket =="
lsof -nP -iTCP:8080 -sTCP:LISTEN 2>/dev/null || echo "  nothing listening on 8080"
echo
echo "== API =="
curl -sS --max-time 5 http://127.0.0.1:8080/health 2>/dev/null && echo || echo "  /health unreachable"
curl -sS --max-time 5 http://127.0.0.1:8080/props 2>/dev/null \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('  alias =',d.get('model_alias'),'| ftype =',d.get('model_ftype'),'| n_ctx =',d['default_generation_settings']['n_ctx'],'| vision =',d.get('modalities',{}).get('vision'))" 2>/dev/null
