#!/usr/bin/env bash
# Install corpus's systemd user units (Linux). Idempotent: re-run after a git pull.
#
#   deploy/install-systemd.sh            copy units, daemon-reload, enable timers
#   deploy/install-systemd.sh --no-enable  copy + reload only
#
# Units are COPIED, not linked: `systemctl --user link` into a git checkout breaks
# the moment the checkout moves or a branch without deploy/ is checked out, and
# the failure is a timer that silently never fires. Which is how this project
# went 78 days without a scheduled sync.
#
# The units run the checkout this script lives in, or $CORPUS_REPO if set (e.g. a
# stable main checkout while you run the script from a worktree). The @CORPUS_REPO@
# placeholder in deploy/systemd/*.service is filled in at install time.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${CORPUS_REPO:-$(dirname "$HERE")}"
DEST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
TIMERS=(corpus-nightly.timer corpus-weekly.timer ytdlp-upgrade.timer)
enable=1
[ "${1:-}" = "--no-enable" ] && enable=0

if [ "$(uname -s)" != "Linux" ]; then
  echo "systemd units are Linux-only; on macOS schedule deploy/corpus-nightly.sh with a launchd agent (StartCalendarInterval) instead"
  exit 1
fi
[ -x "$REPO/.venv/bin/corpus" ] || { echo "missing $REPO/.venv/bin/corpus (run ./install.sh there)"; exit 1; }
[ -x "$REPO/deploy/corpus-nightly.sh" ] || { echo "missing $REPO/deploy/corpus-nightly.sh (is this branch merged?)"; exit 1; }

mkdir -p "$DEST" "$HOME/.cache"
for f in "$HERE"/systemd/*.service "$HERE"/systemd/*.timer; do
  sed "s#@CORPUS_REPO@#$REPO#g" "$f" > "$DEST/$(basename "$f")"
  chmod 644 "$DEST/$(basename "$f")"
  echo "installed $(basename "$f")"
done

# Another yt-dlp upgrader may already exist on a box; two would just race pipx.
if ls "$DEST"/*.service 2>/dev/null | grep -v ytdlp-upgrade.service \
     | xargs grep -l "pipx upgrade yt-dlp\|yt-dlp -U" 2>/dev/null | grep -q .; then
  echo "note: another unit already upgrades yt-dlp; not enabling ytdlp-upgrade.timer"
  TIMERS=(corpus-nightly.timer corpus-weekly.timer)
fi

systemctl --user daemon-reload
if [ $enable -eq 1 ]; then
  systemctl --user enable --now "${TIMERS[@]}"
fi

# Without lingering the user manager stops at logout and no timer fires on a
# headless box.
linger=$(loginctl show-user "$USER" -p Linger 2>/dev/null || echo "Linger=unknown")
if [ "$linger" != "Linger=yes" ]; then
  echo "WARNING: $linger. Timers only run while you are logged in. Fix: sudo loginctl enable-linger $USER"
fi

[ -f "$HOME/.config/corpus/notify.json" ] || [ -f "$HOME/.config/notify-bridge.json" ] \
  || echo "WARNING: no ~/.config/corpus/notify.json: failures and the weekly digest will not reach you"

systemctl --user list-timers --no-pager "${TIMERS[@]}" || true
