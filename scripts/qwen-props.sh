#!/bin/bash
# Newton_Local — report the RUNTIME configuration of an instance as the
# server itself sees it (never inferred from launch arguments).
# Usage: qwen-props.sh <port>
PORT="${1:-8080}"
curl -sS --max-time 10 "http://127.0.0.1:${PORT}/props" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('  endpoint    = http://127.0.0.1:${PORT}')
print('  alias       =', d.get('model_alias'))
print('  model_path  =', d.get('model_path'))
print('  model_ftype =', d.get('model_ftype'))
print('  n_ctx       =', d['default_generation_settings']['n_ctx'])
print('  total_slots =', d.get('total_slots'))
print('  modalities  =', d.get('modalities'))
print('  build_info  =', d.get('build_info'))
"
