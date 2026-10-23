#!/usr/bin/env bash
# corpus installer. Idempotent: safe to re-run after a git pull.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

command -v python3 >/dev/null || { echo "python3 not found"; exit 1; }

# Test for a WORKING venv, not just a directory. An interrupted create leaves a
# .venv with no pip in it, and a plain -d check then skips recreation forever,
# failing on every subsequent run with "no such file or directory: ./.venv/bin/pip".
if [ ! -x .venv/bin/pip ]; then
  rm -rf .venv
  python3 -m venv .venv || { echo "could not create venv"; exit 1; }
fi
./.venv/bin/pip -q install --upgrade pip
./.venv/bin/pip -q install -e ".[dev]"

mkdir -p "$HOME/.corpus" "$HOME/.local/bin" "$HOME/.reach"
ln -sf "$HERE/.venv/bin/corpus" "$HOME/.local/bin/corpus"

if [ ! -f "$HOME/.reach/keys.env" ]; then
  cat > "$HOME/.reach/keys.env" <<'KEYS'
# corpus reads these. Nothing here is required to install; each unlocks a route.
# APIFY_TOKEN     paid public routes (LinkedIn + Instagram). Get one at apify.com.
#                 IMPORTANT: use a PAID plan. Some Store actors bill 100x more per
#                 item on the free tier and corpus will refuse bulk runs there.
# DEEPSEEK_API_KEY  analysis. 1M tokens/minute, the right default for bulk work.
# GROQ_API_KEY      analysis fallback + Whisper for non-English audio.
APIFY_TOKEN=
DEEPSEEK_API_KEY=
GROQ_API_KEY=
KEYS
  chmod 600 "$HOME/.reach/keys.env"
  echo "created ~/.reach/keys.env - add your keys there"
fi

# Optional, both improve things but nothing breaks without them.
command -v ffmpeg >/dev/null || echo "note: ffmpeg missing (keyframes disabled): brew install ffmpeg"
command -v yap    >/dev/null || echo "note: yap missing (free local transcription): brew install yap"

echo
echo "installed. 'corpus' is on ~/.local/bin (ensure it is on PATH)."
./.venv/bin/corpus adapters
