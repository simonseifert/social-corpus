#!/usr/bin/env bash
# Weekly yt-dlp upgrade. YouTube changes its player often enough that a yt-dlp a
# few weeks old starts failing, and youtube_shorts / enrich fail with it.
#
# Upgrades the pipx install, then checks the yt-dlp actually first on PATH. On the
# author's server those were two different copies (Oct 2026): a `pip install --user`
# script in ~/.local/bin shadowed pipx's link, so `pipx upgrade` alone would have
# reported success while the binary in use stayed old. That copy is upgraded too,
# and the log says to remove it.
set -euo pipefail

before=$(yt-dlp --version 2>/dev/null || echo none)
if command -v pipx >/dev/null && pipx list --short 2>/dev/null | grep -q '^yt-dlp '; then
  pipx upgrade yt-dlp
fi

bin=$(command -v yt-dlp || true)
if [ -z "$bin" ]; then
  echo "yt-dlp not on PATH (pipx install yt-dlp)"
  exit 1
fi
if [ ! -L "$bin" ] && head -n 1 "$bin" | grep -q python; then
  py=$(head -n 1 "$bin" | sed 's/^#!//; s/ .*//')
  if "$py" -c 'import site, yt_dlp, sys; sys.exit(0 if yt_dlp.__file__.startswith(site.getusersitepackages()) else 1)' 2>/dev/null; then
    echo "note: $bin is a pip --user install shadowing pipx; upgrading it too."
    echo "      cleanup: $py -m pip uninstall --break-system-packages yt-dlp && pipx reinstall yt-dlp"
    "$py" -m pip install --user --upgrade --quiet --break-system-packages yt-dlp
  fi
fi
after=$(yt-dlp --version)
echo "yt-dlp $before -> $after ($bin)"
