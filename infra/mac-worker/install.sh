#!/bin/sh
# Install the Remake Studio render worker for the current user (no sudo).
#   sh infra/mac-worker/install.sh [--no-launchd]
# Creates ~/.remake-worker/{config.json,secrets.json,job-signing.key} (mode 600),
# and a LaunchAgent that keeps the worker running while you are logged in.
# Nothing listens on the network; the worker only makes outbound requests.
set -eu
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DIR="$HOME/.remake-worker"
NO_LAUNCHD=0
[ "${1:-}" = "--no-launchd" ] && NO_LAUNCHD=1
umask 077
mkdir -p "$DIR/state"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "python3 not found (install Xcode Command Line Tools: xcode-select --install)"; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "python3 3.11+ needed (or point PY at ComfyUI's venv python)"; exit 1; }

if [ ! -f "$DIR/job-signing.key" ]; then
  "$PY" -c 'import secrets; print(secrets.token_hex(32))' > "$DIR/job-signing.key"
  echo "created job-signing key: $DIR/job-signing.key"
fi
chmod 600 "$DIR/job-signing.key"
[ -f "$DIR/config.json" ] || sed "s#__HOME__#$HOME#g" "$REPO/infra/mac-worker/config.example.json" > "$DIR/config.json"
[ -f "$DIR/secrets.json" ] || printf '{\n  "REMAKE_STORE_ACCESS_KEY": "",\n  "REMAKE_STORE_SECRET_KEY": ""\n}\n' > "$DIR/secrets.json"
chmod 600 "$DIR/config.json" "$DIR/secrets.json"

if [ "$NO_LAUNCHD" = 0 ]; then
  AGENT="$HOME/Library/LaunchAgents/ai.remake-studio.worker.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  sed -e "s#__PY__#$PY#g" -e "s#__REPO__#$REPO#g" -e "s#__HOME__#$HOME#g" \
    "$REPO/infra/mac-worker/ai.remake-studio.worker.plist" > "$AGENT"
  echo "LaunchAgent written: $AGENT"
  echo "start it after filling in secrets.json:  launchctl bootstrap gui/$(id -u) $AGENT"
fi
echo "next: fill $DIR/secrets.json, then run:  cd $REPO && $PY -m remake_studio.hybrid.worker --selftest"
