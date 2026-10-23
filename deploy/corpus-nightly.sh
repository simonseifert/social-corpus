#!/usr/bin/env bash
# corpus nightly: sync -> enrich -> ocr -> retention -> backup -> off-box copy -> health.
#
# Run by corpus-nightly.service at 01:30. If other GPU-heavy jobs share the box,
# schedule around them: enrich transcribes on a whisper server, and two jobs
# queueing on one GPU makes both slow.
#
# Off-box copy: set CORPUS_OFFSITE_HOST to an ssh host (an alias from
# ~/.ssh/config works) to rsync the newest snapshots there. Unset = no off-box
# copy, logged and skipped.
#
# Failure policy, step by step, because "stop at the first error" is wrong here:
#   * sync and enrich failing do not stop the night. A sync with one dead account
#     still stored the others, and the backup has to run either way.
#   * retention deletes data, so it only runs on a night where nothing before it
#     failed. A destructive step is the last thing to run against a database that
#     just misbehaved.
#   * no off-box copy after a failed backup: shipping the previous snapshot again
#     would look like success.
#   * the off-box host asleep or unreachable is not a failure (a laptop is often
#     away); it is logged. rsync failing while the host IS reachable is a failure.
#   * health last, always; its exit code counts.
# Anything that failed is listed on the final line and the script exits 1, which
# is what fires corpus-notify@ with this journal's tail.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
CORPUS="${CORPUS_BIN:-$REPO/.venv/bin/corpus}"
BACKUP_DIR="${CORPUS_BACKUP_DIR:-$HOME/.corpus/backups}"
OFFSITE_HOST="${CORPUS_OFFSITE_HOST:-}"                     # ssh host; empty = skip
OFFSITE_DIR="${CORPUS_OFFSITE_DIR:-corpus-backups-offsite}"   # relative to remote ~
OFFSITE_KEEP="${CORPUS_OFFSITE_KEEP:-30}"                     # per database
SYNC_ARGS=(${CORPUS_SYNC_ARGS:-})
ENRICH_ARGS=(${CORPUS_ENRICH_ARGS:-})

failed=()
log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

step() {  # step <name> <cmd...>: run, log the outcome, remember a failure
  local name=$1; shift
  log "== $name"
  "$@"
  local rc=$?
  if [ $rc -eq 75 ]; then
    # EX_TEMPFAIL: another corpus run (a manual one, or a dashboard job) holds the
    # lock. Not a failure; tonight's step is skipped and tomorrow's run catches up.
    log "-- $name skipped: another corpus run holds the lock"
    return 0
  fi
  if [ $rc -ne 0 ]; then
    log "!! $name exited $rc"
    failed+=("$name($rc)")
  fi
  return $rc
}

if [ ! -x "$CORPUS" ]; then
  log "corpus binary not found at $CORPUS"
  exit 1
fi

step sync   "$CORPUS" sync ${SYNC_ARGS[@]+"${SYNC_ARGS[@]}"}
step enrich "$CORPUS" enrich ${ENRICH_ARGS[@]+"${ENRICH_ARGS[@]}"}
# On-screen text is its own pass (enrich --ocr reads the keyframes enrich just
# wrote). Without it a music-only reel, whose message is entirely burned in, has
# no text at all in the brief.
step ocr    "$CORPUS" enrich --ocr

# Another branch adds `retention`; run it only once it exists.
if "$CORPUS" --help 2>/dev/null | grep -qw retention; then
  if [ ${#failed[@]} -eq 0 ]; then
    step retention "$CORPUS" retention --execute
  else
    log "== retention skipped: earlier step(s) failed (${failed[*]})"
  fi
fi

backup_ok=1
step backup "$CORPUS" backup || backup_ok=0

offsite() {
  if [ -z "$OFFSITE_HOST" ]; then
    log "CORPUS_OFFSITE_HOST not set: off-box copy skipped"
    return 0
  fi
  # newest snapshot of each database; the local rotation already keeps 14. By the
  # UTC stamp in the name, not mtime: a restored or copied file has a fresh mtime.
  local newest=() prefix f
  for prefix in corpus analysis; do
    f=$(ls -1 "$BACKUP_DIR"/"$prefix"-*.db.gz 2>/dev/null | sort | tail -n 1)
    [ -n "$f" ] && newest+=("$f")
  done
  if [ ${#newest[@]} -eq 0 ]; then
    log "no snapshots in $BACKUP_DIR to copy"
    return 1
  fi
  if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "$OFFSITE_HOST" \
       "mkdir -p ~/$OFFSITE_DIR" 2>/dev/null; then
    log "$OFFSITE_HOST unreachable: off-box copy skipped tonight (not a failure)"
    return 0
  fi
  rsync -a --timeout=120 -e "ssh -o BatchMode=yes -o ConnectTimeout=10" \
    "${newest[@]}" "$OFFSITE_HOST:$OFFSITE_DIR/" || return 1
  log "copied $(printf '%s ' "${newest[@]##*/}")to $OFFSITE_HOST:~/$OFFSITE_DIR"
  # Bound the remote copy: keep the newest $OFFSITE_KEEP of each database. Only
  # files matching the snapshot pattern, only inside that one directory. No glob:
  # a macOS target's login shell is zsh, where an unmatched glob aborts the command.
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$OFFSITE_HOST" \
    "cd ~/$OFFSITE_DIR && for p in corpus analysis; do
       ls -1 | grep \"^\$p-[0-9T]*\\.db\\.gz\$\" | sort -r | tail -n +$((OFFSITE_KEEP + 1)) |
       while read -r f; do rm -f -- \"\$f\"; done; done" \
    || log "remote prune failed (copy itself succeeded)"
}

if [ $backup_ok -eq 1 ]; then
  step offsite offsite
else
  log "== offsite skipped: backup failed"
fi

step health "$CORPUS" health

if [ ${#failed[@]} -gt 0 ]; then
  log "corpus nightly finished with failures: ${failed[*]}"
  exit 1
fi
log "corpus nightly ok"
