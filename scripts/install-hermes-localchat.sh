#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# install-hermes-localchat.sh — create/refresh the `localchat` Hermes profile
# from this repository's committed configuration.
#
#   Open WebUI :3000  ->  Hermes API :8642  ->  Qwen-A :8080
#
# Idempotent. No sudo. Does not touch Qwen-A or Qwen-B.
#
# Prerequisite (run once, by hand, so you can see what it does):
#   curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --skip-setup
# ---------------------------------------------------------------------------
set -euo pipefail

PROFILE=localchat
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES="$HOME/.local/bin/hermes"
PROFILE_HOME="$HOME/.hermes/profiles/$PROFILE"
TEMPLATE="$REPO/config/hermes/localchat.config.yaml"

die() { printf 'install-hermes-localchat: %s\n' "$*" >&2; exit 1; }
say() { printf '\033[1m%s\033[0m\n' "$*"; }

[ -x "$HERMES" ] || die "Hermes not installed. Run:
    curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --skip-setup"
[ -f "$TEMPLATE" ] || die "missing $TEMPLATE"

say "1. Profile"
if [ -d "$PROFILE_HOME" ]; then
  printf '   %s already exists — reusing it\n' "$PROFILE_HOME"
else
  "$HERMES" profile create "$PROFILE"
fi

say "2. Configuration"
# Preserve anything Hermes itself has written (onboarding flags, allowlists)
# by merging the template over the live file rather than clobbering it.
python3 - "$TEMPLATE" "$PROFILE_HOME/config.yaml" <<'PY'
import sys, pathlib
try:
    import yaml
except ImportError:
    sys.exit("PyYAML not available; copy the template by hand")

tpl, live = (pathlib.Path(p) for p in sys.argv[1:3])
want = yaml.safe_load(tpl.read_text()) or {}
have = yaml.safe_load(live.read_text()) if live.exists() else {}
have = have or {}

def merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            merge(dst[k], v)
        else:
            dst[k] = v
    return dst

live.write_text(yaml.safe_dump(merge(have, want), sort_keys=False))
print(f"   merged {tpl.name} into {live}")
PY

say "3. API-server credential"
ENVF="$PROFILE_HOME/.env"
touch "$ENVF"; chmod 600 "$ENVF"
if grep -q '^API_SERVER_KEY=.\{16,\}' "$ENVF" 2>/dev/null; then
  printf '   API_SERVER_KEY already present — left untouched\n'
else
  KEY="hermes-localchat-$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
  /usr/bin/sed -i '' '/^API_SERVER_/d' "$ENVF"
  {
    printf '\n# --- Hermes API server (loopback only) ---\n'
    printf 'API_SERVER_ENABLED=true\n'
    printf 'API_SERVER_KEY=%s\n' "$KEY"
    printf 'API_SERVER_HOST=127.0.0.1\n'
    printf 'API_SERVER_PORT=8642\n'
  } >> "$ENVF"
  printf '   generated a new API_SERVER_KEY (not printed)\n'
fi

say "4. Repository skills"
"$REPO/scripts/install-hermes-skill.sh"

say "5. Gateway service (hosts the API server)"
"$HERMES" -p "$PROFILE" gateway install 2>&1 | /usr/bin/sed 's/^/   /'

say "6. Waiting for http://127.0.0.1:8642 ..."
for _ in $(seq 1 45); do
  if /usr/bin/nc -z -G 1 127.0.0.1 8642 >/dev/null 2>&1; then
    printf '   API server is listening\n'
    break
  fi
  /bin/sleep 2
done

cat <<EOF

Done. Next:
  $REPO/scripts/install-openwebui.sh    # the ChatGPT-like frontend
  $REPO/scripts/hermes-localchat status
  $REPO/scripts/hermes-localchat chat
EOF
