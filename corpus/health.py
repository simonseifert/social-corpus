"""`corpus health`: is the unattended pipeline actually working?

Every check returns a line with a level: ok, warn, or red. Red means something
is broken or about to cost data and exits non-zero, so a timer (or a human) can
act on the exit code alone. Warn is worth a look but not a page.

Staleness is judged per ACCOUNT against that account's own TTL. It used to be one
line per adapter, from whichever run of that adapter was newest: one healthy
account kept the adapter green while another account behind it had not synced in
a month.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import backup as backup_mod, freshness, store

OK, WARN, RED = "ok", "warn", "red"

# A daily backup that drifts an hour must not page; one that skipped a night must.
BACKUP_MAX_AGE_S = 26 * 3600
# Non-sync run rows: these share scrape_run but say nothing about an account's
# freshness.
NON_SYNC = ("enrich", "comments")


@dataclass
class Check:
    level: str
    check: str
    subject: str
    message: str

    def as_dict(self) -> dict:
        return asdict(self)


def _ago(seconds: float) -> str:
    s = int(seconds)
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 2 * 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def accounts(conn, now: int | None = None) -> list[Check]:
    now = now or store.now()
    ph = ",".join("?" * len(NON_SYNC))
    out = []
    for a in conn.execute(
            """SELECT a.id, a.platform, a.handle, a.relationship, a.last_scraped_at,
                      (SELECT COUNT(*) FROM item i WHERE i.account_id = a.id) n
                 FROM account a ORDER BY a.platform, a.handle"""):
        subject = f"{a['platform']}/{a['handle']}"
        ok = conn.execute(
            f"""SELECT MAX(finished_at) t FROM scrape_run WHERE account_id=?
                 AND status='ok' AND adapter NOT IN ({ph})""",
            (a["id"], *NON_SYNC)).fetchone()["t"]
        last_ok = max(ok or 0, a["last_scraped_at"] or 0) or None
        ttl = freshness.ttl_for(a["relationship"])
        # Failures AFTER the last success. A route that failed and fell through to
        # one that worked finished before the job's own row, so it is not counted.
        err = conn.execute(
            f"""SELECT status, error, finished_at FROM scrape_run
                 WHERE account_id=? AND status IN ('error','interrupted')
                   AND adapter NOT IN ({ph}) AND finished_at > ?
                 ORDER BY finished_at DESC, id DESC LIMIT 1""",
            (a["id"], *NON_SYNC, ok or 0)).fetchone()
        info = f"{a['n']} items"
        if last_ok is None:
            lvl, msg = (RED if err else WARN), "never synced successfully"
        elif now - last_ok > 2 * ttl:
            lvl, msg = RED, (f"STALE: last good sync {_ago(now - last_ok)}, expected "
                             f"every {ttl // 3600}h ({a['relationship']})")
        else:
            lvl, msg = OK, f"synced {_ago(now - last_ok)}"
        if err is not None:
            why = store.one_line(err["error"]) or err["status"]
            lvl = RED if err["status"] == "error" else max(lvl, WARN, key=_rank)
            msg += f"; last attempt {err['status']}: {why}"
        out.append(Check(lvl, "account", subject, f"{msg}, {info}"))
    return out


def _rank(level: str) -> int:
    return {OK: 0, WARN: 1, RED: 2}[level]


def runs(conn, now: int | None = None) -> list[Check]:
    now = now or store.now()
    out = []
    orphans = conn.execute(
        "SELECT COUNT(*) c FROM scrape_run WHERE status='running' AND started_at < ?",
        (now - store.ORPHAN_AFTER_S,)).fetchone()["c"]
    if orphans:
        out.append(Check(RED, "runs", "orphaned",
                         f"{orphans} run(s) still 'running' after "
                         f"{store.ORPHAN_AFTER_S // 3600}h: a process was killed "
                         f"mid-run (the next locked run closes them)"))
    for adapter in NON_SYNC:
        r = conn.execute(
            "SELECT status, error, started_at FROM scrape_run WHERE adapter=? "
            "AND status <> 'running' ORDER BY started_at DESC, id DESC LIMIT 1",
            (adapter,)).fetchone()
        if r is None:
            continue
        if r["status"] == "ok":
            out.append(Check(OK, "runs", adapter, f"last run ok, {_ago(now - r['started_at'])}"))
        else:
            out.append(Check(RED if r["status"] == "error" else WARN, "runs", adapter,
                             f"last run {r['status']} {_ago(now - r['started_at'])}: "
                             f"{store.one_line(r['error'])}"))
    # Vendor payload drift: the newest run of each route that reported a raw count.
    for r in conn.execute(
            """SELECT adapter, items_raw, items_seen FROM scrape_run s
                WHERE items_raw IS NOT NULL AND id = (
                   SELECT MAX(id) FROM scrape_run t
                    WHERE t.adapter = s.adapter AND t.items_raw IS NOT NULL)"""):
        dropped = (r["items_raw"] or 0) - (r["items_seen"] or 0)
        if dropped <= 0:
            continue
        from .sync import DROP_MAX_SHARE, DROP_MIN_RECORDS
        bad = (r["items_raw"] >= DROP_MIN_RECORDS
               and dropped / r["items_raw"] > DROP_MAX_SHARE)
        out.append(Check(RED if bad else WARN, "drift", r["adapter"],
                         f"last run dropped {dropped} of {r['items_raw']} vendor "
                         f"records as unparseable"))
    from .enrich.run import MAX_ENRICH_ATTEMPTS
    stuck = conn.execute(
        """SELECT COUNT(*) c FROM item WHERE enrich_attempts >= ?
            AND (transcript IS NULL OR transcript = '') AND deleted_at IS NULL""",
        (MAX_ENRICH_ATTEMPTS,)).fetchone()["c"]
    if stuck:
        out.append(Check(WARN, "enrich", "gave up",
                         f"{stuck} video(s) failed transcription {MAX_ENRICH_ATTEMPTS} "
                         f"times and are no longer retried (enrich --redo to force)"))
    return out


def spend(conn, now: int | None = None) -> list[Check]:
    """Month-to-date vendor spend recorded on run rows (sync probes and fetches,
    comment batches). Informational; the spend guard enforces the budget."""
    import calendar
    t = time.gmtime(now or store.now())
    start = calendar.timegm((t.tm_year, t.tm_mon, 1, 0, 0, 0))
    rows = conn.execute("SELECT adapter, SUM(cost_usd) c FROM scrape_run "
                        "WHERE started_at >= ? AND cost_usd > 0 GROUP BY adapter "
                        "ORDER BY c DESC", (start,)).fetchall()
    usd = sum(r["c"] for r in rows)
    # Per route: since ScrapeCreators and HarvestAPI took over, "Apify fetches" was
    # wrong, and one total cannot say which balance to top up.
    by = ", ".join(f"{r['adapter']} ${r['c']:.3f}" for r in rows)
    return [Check(OK, "spend", time.strftime("%Y-%m", t),
                  f"${usd:.2f} recorded this month" + (f" ({by})" if by else ""))]


def backups(backup_dir: str | Path, now: int | None = None) -> list[Check]:
    now = now or store.now()
    state = backup_mod.last_run(backup_dir)
    snaps = backup_mod.list_snapshots(backup_dir)
    if state is None and not snaps:
        return [Check(RED, "backup", str(backup_dir), "no backup has ever run here")]
    at = state["at"] if state else snaps[0].taken_at
    out = []
    age = now - at
    out.append(Check(RED if age > BACKUP_MAX_AGE_S else OK, "backup", "age",
                     f"last backup run {_ago(age)}, {len(snaps)} snapshot(s) kept"))
    if state is not None and not state.get("ok"):
        errs = "; ".join(state.get("errors") or []) or "nothing verified"
        out.append(Check(RED, "backup", "verify", f"last run failed: {errs[:200]}"))
    elif state is not None:
        out.append(Check(OK, "backup", "verify",
                         f"{len(state.get('verified') or [])} snapshot(s) verified"))
    return out


def transcription(*, ping=None, yap=None, groq=None) -> list[Check]:
    """Whisper reachability, against what else could transcribe."""
    from .enrich import transcribe as tx
    ping = tx.ping_whisper if ping is None else ping
    yap = tx.available() if yap is None else yap
    groq = bool(tx._env_key("GROQ_API_KEY")) if groq is None else groq
    res = ping()
    down = {u: why for u, why in res.items() if why}
    up = [u for u, why in res.items() if not why]
    other = [n for n, have in (("yap", yap), ("Groq", groq)) if have]
    if not res:
        if other:
            return [Check(OK, "transcribe", "engines", f"{', '.join(other)} "
                          f"(no self-hosted whisper configured)")]
        return [Check(WARN, "transcribe", "engines",
                      "no transcriber: set WHISPER_SERVER_URL or GROQ_API_KEY")]
    out = [Check(OK, "transcribe", u, "whisper server reachable") for u in up]
    for u, why in down.items():
        lvl = WARN if (up or other) else RED
        out.append(Check(lvl, "transcribe", u, f"whisper server unreachable ({why})"
                         + ("" if lvl == RED else
                            f"; falling back to {', '.join(other) or 'another server'}")))
    return out


def run_all(conn, *, backup_dir, unavailable_adapters=None, keys=None,
            transcription_kw=None) -> list[Check]:
    out = accounts(conn) + runs(conn) + backups(backup_dir)
    out += transcription(**(transcription_kw or {}))
    for name, why in (unavailable_adapters or {}).items():
        out.append(Check(RED, "adapters", name, f"failed to load: {why}"))
    if keys is not None and not keys.get("APIFY_TOKEN"):
        out.append(Check(WARN, "keys", "APIFY_TOKEN",
                         "missing: paid routes cannot run (~/.reach/keys.env)"))
    out += spend(conn)
    return out
