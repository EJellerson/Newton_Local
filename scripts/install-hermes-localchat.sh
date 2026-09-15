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
python3 "$REPO/scripts/install-hermes-integration.py" --runtime "$HOME/.hermes/hermes-agent"
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
if isinstance(have.get("model"), dict):
    have["model"].pop("supports_vision", None)  # now supplied by resident identity

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
python3 "$REPO/scripts/migrate-resident-memory.py" "$PROFILE_HOME"

say "5. Gateway service (hosts the API server, starts at login)"
GW_LABEL=ai.hermes.gateway-localchat
GW_PLIST="$HOME/Library/LaunchAgents/${GW_LABEL}.plist"
UID_="$(id -u)"

# Two upstream behaviours to work around, both observed on macOS 14.8.5 with
# Hermes v0.20.5:
#
#  1. `hermes gateway install` bakes the CALLER's $PATH into the plist on every
#     run. Invoked from a shell with session-scoped entries (a CI runner, an
#     editor terminal, an agent harness) it would pin paths that vanish with
#     that session into a service meant to outlive it. So it is given a stable
#     PATH to copy — and Hermes then still recognises the definition as current,
#     which a post-hoc plutil rewrite would break.
#
#  2. `--force` does bootout immediately followed by bootstrap. launchd is still
#     tearing the old job down and returns EIO (5); Hermes concludes "launchd
#     cannot manage the gateway on this macOS version" and silently falls back
#     to an unsupervised background process that does NOT survive logout or
#     crashes. So the plist is written by Hermes, and the load is done here with
#     a wait in between.
CLEAN_PATH="$HOME/.hermes/hermes-agent/venv/bin:$HOME/.hermes/hermes-agent/node_modules/.bin:$HOME/.hermes/node/bin:$HOME/.hermes/bin:$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin"

GW_OUT="$(env PATH="$CLEAN_PATH" "$HERMES" -p "$PROFILE" gateway install \
    --force --start-on-login --no-start-now 2>&1 || true)"
printf '%s\n' "$GW_OUT" \
  | /usr/bin/grep -vE 'Bootstrap failed|re-running the command as root|launchd cannot manage|Started gateway as a background process|It will NOT auto-start|Stop it with:|Logs: ~/' \
  | /usr/bin/sed 's/^/   /'
if printf '%s' "$GW_OUT" | /usr/bin/grep -q 'launchd cannot manage'; then
  printf '   (hermes hit the bootout/bootstrap race and fell back to an\n'
  printf '    unsupervised process — ignore its warning, this script loads the\n'
  printf '    service into launchd properly below)\n'
fi

[ -f "$GW_PLIST" ] || die "gateway plist was not written to $GW_PLIST"
plutil -lint "$GW_PLIST" >/dev/null || die "installed plist failed plutil -lint"

# Reap anything Hermes may have left running unsupervised, then load properly.
"$HERMES" -p "$PROFILE" gateway stop >/dev/null 2>&1 || true
launchctl bootout "gui/$UID_/$GW_LABEL" >/dev/null 2>&1 || true
for _ in $(seq 1 20); do
  launchctl print "gui/$UID_/$GW_LABEL" >/dev/null 2>&1 || break
  /bin/sleep 1
done
launchctl enable "gui/$UID_/$GW_LABEL" >/dev/null 2>&1 || true
if launchctl bootstrap "gui/$UID_" "$GW_PLIST" 2>/dev/null; then
  printf '   loaded into gui/%s — launchd now owns it\n' "$UID_"
else
  printf '   WARNING: launchctl bootstrap failed; the gateway is not supervised\n' >&2
fi

# Hermes may reorder the entries it was given; what matters is that nothing
# session-scoped survived, so check for the shapes that expire rather than for
# exact string equality.
BAKED="$(plutil -extract EnvironmentVariables.PATH raw -o - "$GW_PLIST" 2>/dev/null || true)"
EPHEMERAL="$(printf '%s' "$BAKED" | tr ':' '\n' \
  | /usr/bin/grep -cE '/Library/Application Support/|/private/var/folders/|^/tmp/|/T/' || true)"
if [ "${EPHEMERAL:-0}" -eq 0 ]; then
  printf '   service PATH holds no session-scoped entries (%s entries)\n' \
    "$(printf '%s' "$BAKED" | tr ':' '\n' | /usr/bin/wc -l | tr -d ' ')"
else
  printf '   WARNING: %s session-scoped PATH entries baked into the service\n' "$EPHEMERAL" >&2
fi
for k in RunAtLoad KeepAlive; do
  printf '   %-10s %s\n' "$k" "$(plutil -extract $k raw -o - "$GW_PLIST" 2>/dev/null || echo '(absent)')"
done

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
