#!/bin/sh
# Remake Studio Mac worker: one-command user-level setup (no sudo).
# Repository: https://github.com/MohammadAbwini98/Cartoon (clone to ~/RemakeStudio/Cartoon)
#
#   sh infra/mac-worker/setup.sh --queue-endpoint https://<account-id>.r2.cloudflarestorage.com \
#                                --queue-bucket remake-queue [--reuse-comfyui ~/ComfyUI] [--models-from ~/ComfyUI/models]
#
# Finds Python 3.10–3.13 (Homebrew, python.org or uv); if none exists it installs uv into ~/.local/bin
# and a Python 3.12 managed by uv, both inside your home folder. Then runs remake_studio/hybrid/mac_setup.py.
set -eu
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
[ "$(uname -s)" = "Darwin" ] || { echo "This setup is for macOS."; exit 1; }
[ "$(uname -m)" = "arm64" ] || { echo "Apple Silicon (arm64) is required for MPS."; exit 1; }
command -v git >/dev/null 2>&1 || { echo "git is missing: run  xcode-select --install  and rerun."; exit 1; }

pick() {
  for p in python3.12 python3.13 python3.11 python3.10 /opt/homebrew/bin/python3 python3; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)' 2>/dev/null; then
      command -v "$p"; return 0
    fi
  done
  return 1
}
PY="$(pick || true)"
if [ -z "$PY" ]; then
  echo "No Python 3.10–3.13 found; installing uv and Python 3.12 into your home folder."
  command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
  UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
  "$UV" python install 3.12
  PY="$("$UV" python find 3.12)"
fi
echo "using $PY"
cd "$REPO"
exec "$PY" -m remake_studio.hybrid.mac_setup --python "$PY" "$@"
