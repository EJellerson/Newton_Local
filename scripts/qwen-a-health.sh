#!/bin/bash
# Newton_Local — Qwen-A API health + model identity check.
set -e
echo "-- /health"; curl -sS http://127.0.0.1:8080/health; echo
echo "-- /v1/models"; curl -sS http://127.0.0.1:8080/v1/models | python3 -m json.tool
echo "-- /props (key fields)"
curl -sS http://127.0.0.1:8080/props | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('  model_path =', d.get('model_path'))
print('  model_ftype=', d.get('model_ftype'))
print('  alias      =', d.get('model_alias'))
print('  n_ctx      =', d['default_generation_settings']['n_ctx'])
print('  slots      =', d.get('total_slots'))
print('  modalities =', d.get('modalities'))
print('  build_info =', d.get('build_info'))
"
