"""SQLite store for the corpus.

One writer, many readers, WAL. Everything here is idempotent: running a sync twice
over unchanged data inserts nothing and changes nothing, which is what makes the
scheduled job safe to run as often as you like.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


def now() -> int:
    return int(time.time())


class DamagedDatabase(RuntimeError):
    """The file at the database path exists but is not a usable corpus."""


def _damaged(db_path: Path, why: str) -> DamagedDatabase:
    return DamagedDatabase(
        f"{db_path} is damaged ({why}). Nothing was written to it. Restore the "
        f"newest snapshot with `corpus backup --list` then `corpus backup --restore "
        f"<snapshot>`, or move the file aside to start an empty corpus.")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) and bring the database up to date.

    Order is deliberate: tables, then column migration, then indexes/FTS/triggers.
    An index or trigger referencing a column added in a later version fails on an
    older database if it runs before the ALTER, so the schema is split at @@POST@@.
    """
    db_path = Path(db_path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # SQLite treats a 0-byte file as a new database, so a corpus truncated by a
    # full disk or a failed copy came back as an empty one: health said "ok" with no
    # accounts and the next sync started re-buying every back-catalogue
    # (field test, 2026-10-23). A real new database has no file yet.
    if db_path.is_file() and db_path.stat().st_size == 0:
        raise _damaged(db_path, "an empty file, 0 bytes")
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row

    tables, _, post = SCHEMA.read_text().partition("-- @@POST@@")
    try:
        conn.executescript(tables)
    except sqlite3.DatabaseError as e:
        if isinstance(e, sqlite3.OperationalError) and "locked" in str(e):
            raise
        conn.close()
        raise _damaged(db_path, str(e)) from e
    _migrate(conn)
    rebuilt = _migrate_fts(conn, post)
    conn.executescript(post)
    _reindex_fts(conn, force=rebuilt)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations.

    CREATE TABLE IF NOT EXISTS in schema.sql handles new installs but silently does
    nothing when a table already exists, so a column added later never appears on an
    existing database. This closes that gap. Additive only: renames and drops need a
    real migration file, not this.
    """
    wanted = {
        "item": {
            "work_key": "TEXT",
            "deleted_at": "INTEGER",
            "is_repost": "INTEGER NOT NULL DEFAULT 0",
            "author_name": "TEXT",
            "author_handle": "TEXT",
            "screen_text": "TEXT",
            "enrich_attempts": "INTEGER NOT NULL DEFAULT 0",
            "comments_at": "INTEGER",
            "comments_n": "INTEGER",
        },
        "media": {"transcript": "TEXT", "fetched_at": "INTEGER"},
        "account": {"client_ref": "TEXT", "oldest_item_at": "INTEGER",
                    "delta_floor": "INTEGER"},
        # Rows the vendor returned (and billed) before parsing; items_seen is what
        # survived it. The gap is how a vendor's silent payload change shows up.
        "scrape_run": {"items_raw": "INTEGER"},
    }
    for table, cols in wanted.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue
        for col, decl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()


def _migrate_fts(conn, post_sql: str) -> None:
    """Rebuild the FTS index when its indexed columns change.

    CREATE VIRTUAL TABLE IF NOT EXISTS is a no-op against an existing table, so
    adding a searchable column leaves the old index in place and its triggers
    referencing a column it does not have -- every subsequent insert then fails.
    Detect the drift, drop the table and its triggers, and let the caller recreate
    them; the content lives in `item`, so nothing is lost by rebuilding.
    """
    have = [r["name"] for r in conn.execute("PRAGMA table_info(item_fts)")]
    if not have:
        return False
    import re as _re
    m = _re.search(r"CREATE VIRTUAL TABLE IF NOT EXISTS item_fts USING fts5\(\s*([^,]*(?:,[^,]*)*?)\s*,\s*content=",
                   post_sql, _re.S)
    if not m:
        return False
    want = [c.strip() for c in m.group(1).split(",") if c.strip()]
    if set(want) == set(c for c in have if c != "item_fts"):
        return False

    for trig in ("item_ai", "item_ad", "item_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
    conn.execute("DROP TABLE IF EXISTS item_fts")
    # DROP on the virtual table can leave its shadow tables behind, and a fresh
    # CREATE then adopts rows written under the OLD column count. The index reads
    # fine and every subsequent write fails with "database disk image is malformed",
    # which points at the whole database rather than at the index that is actually
    # wrong. Clear the shadows so the rebuild starts from nothing.
    for shadow in ("item_fts_data", "item_fts_idx", "item_fts_docsize",
                   "item_fts_config", "item_fts_content"):
        conn.execute(f"DROP TABLE IF EXISTS {shadow}")
    conn.commit()
    return True


def _reindex_fts(conn, *, force: bool = False) -> int:
    """Repopulate the external-content FTS index from `item`.

    `force` after a column-set migration is not optional. Counting rows first and
    skipping when non-empty was the bug: stale shadow rows survive the DROP, so the
    count looks healthy while the index is structurally wrong.
    """
    if not force:
        n = conn.execute("SELECT COUNT(*) c FROM item_fts").fetchone()["c"]
        if n:
            return 0
    conn.execute("INSERT INTO item_fts(item_fts) VALUES('rebuild')")
    conn.commit()
    return conn.execute("SELECT COUNT(*) c FROM item_fts").fetchone()["c"]


def content_hash(title, body, transcript) -> str:
    blob = "\x1f".join((title or "", body or "", transcript or ""))
    return hashlib.sha256(blob.encode()).hexdigest()


# ── accounts ────────────────────────────────────────────────────────────────

def upsert_account(conn, *, platform, handle, relationship, url=None,
                   display_name=None, bio=None, platform_uid=None,
                   follower_count=None, client_ref=None, raw=None) -> int:
    """Add or update an account by (platform, handle).

    Handles match case-insensitively: both platforms treat them that way, and an
    exact match turned "AlexRivera" from a vendor payload into a second account
    beside "alexrivera", splitting one person's posts across two rows.

    client_ref=None keeps the stored value; "" clears it.
    """
    ts = now()
    prior = conn.execute(
        "SELECT handle FROM account WHERE platform=? AND handle=? COLLATE NOCASE",
        (platform, handle)).fetchone()
    if prior is not None:
        handle = prior["handle"]
    conn.execute(
        """
        INSERT INTO account (platform, platform_uid, handle, display_name, bio, url,
                             relationship, client_ref, follower_count, raw_json,
                             first_seen_at)
        VALUES (:p, :uid, :h, :dn, :bio, :url, :rel, :cref, :fc, :raw, :ts)
        ON CONFLICT(platform, handle) DO UPDATE SET
          display_name   = COALESCE(excluded.display_name, display_name),
          bio            = COALESCE(excluded.bio, bio),
          url            = COALESCE(excluded.url, url),
          platform_uid   = COALESCE(excluded.platform_uid, platform_uid),
          follower_count = COALESCE(excluded.follower_count, follower_count),
          client_ref     = CASE WHEN :clear THEN NULL
                                ELSE COALESCE(excluded.client_ref, client_ref) END,
          relationship   = excluded.relationship,
          raw_json       = COALESCE(excluded.raw_json, raw_json)
        """,
        dict(p=platform, uid=platform_uid, h=handle, dn=display_name, bio=bio,
             url=url, rel=relationship, cref=client_ref or None,
             clear=1 if client_ref == "" else 0, fc=follower_count,
             raw=json.dumps(raw) if raw is not None else None, ts=ts),
    )
    row = conn.execute(
        "SELECT id FROM account WHERE platform=? AND handle=?", (platform, handle)
    ).fetchone()
    conn.commit()
    return row["id"]


def update_account_profile(conn, account_id, *, display_name=None, bio=None,
                           url=None, platform_uid=None, follower_count=None,
                           raw=None) -> None:
    """Refresh an account's profile fields from a fetch, by id.

    Never by handle: the handle in a vendor payload is the vendor's spelling, and
    upserting on it created a second account whenever that differed from the one
    the user added (case, or a renamed username), after which the next sync wrote
    the profile there and the posts here.
    """
    conn.execute(
        """UPDATE account SET
             display_name   = COALESCE(?, display_name),
             bio            = COALESCE(?, bio),
             url            = COALESCE(?, url),
             platform_uid   = COALESCE(?, platform_uid),
             follower_count = COALESCE(?, follower_count),
             raw_json       = COALESCE(?, raw_json)
           WHERE id = ?""",
        (display_name, bio, url, platform_uid, follower_count,
         json.dumps(raw) if raw is not None else None, account_id))


def record_account_metrics(conn, account_id, *, followers=None, following=None,
                           posts_total=None) -> bool:
    """Change-only append. Returns True if a row was written."""
    prev = conn.execute(
        """SELECT followers, following, posts_total FROM account_metric_snapshot
           WHERE account_id=? ORDER BY captured_at DESC LIMIT 1""",
        (account_id,),
    ).fetchone()
    cur = (followers, following, posts_total)
    if prev is not None and tuple(prev) == cur:
        return False
    conn.execute(
        """INSERT OR REPLACE INTO account_metric_snapshot
           (account_id, captured_at, followers, following, posts_total)
           VALUES (?,?,?,?,?)""",
        (account_id, now(), followers, following, posts_total),
    )
    return True


# ── items ───────────────────────────────────────────────────────────────────

def account_items(alias: str = "i") -> str:
    """SQL predicate: items that appear on an account's feed, its own plus the
    collab posts stored under a co-author (see item_account). Takes the account id
    twice as parameters."""
    a = f"{alias}." if alias else ""
    return (f"({a}account_id = ? OR {a}id IN "
            f"(SELECT item_id FROM item_account WHERE account_id = ?))")


def link_item(conn, item_id, account_id) -> bool:
    """Record that an item stored under another account also appears on this one.
    A no-op for the owner. Returns True if a link was added."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO item_account (item_id, account_id)
           SELECT id, ? FROM item WHERE id = ? AND account_id <> ?""",
        (account_id, item_id, account_id))
    return cur.rowcount > 0


def upsert_item(conn, *, account_id, platform, platform_item_id, kind,
                published_at, raw, title=None, body=None, transcript=None,
                url=None, lang=None, duration_s=None, source=None,
                is_repost=False, parent_id=None,
                author_name=None, author_handle=None,
                screen_text=None) -> tuple[int, bool, bool]:
    """Insert or update one item.

    Returns (item_id, is_new, content_changed).

    transcript is never clobbered with NULL: enrichment writes it long after the
    fetch adapter first inserted the row, and a later metadata-only refresh must
    not wipe it.
    """
    ts = now()
    chash = content_hash(title, body, transcript)

    # The prior hash has to be read BEFORE the upsert. Trying to detect a change
    # from the RETURNING clause cannot work: the update sets content_hash to the
    # new value, so any comparison against it in the same statement is always true.
    # One writer under WAL makes this read-then-write safe.
    prev = conn.execute(
        "SELECT content_hash FROM item WHERE platform=? AND platform_item_id=?",
        (platform, str(platform_item_id)),
    ).fetchone()
    prev_hash = prev["content_hash"] if prev else None

    cur = conn.execute(
        """
        INSERT INTO item (account_id, platform, platform_item_id, kind, parent_id,
                          is_repost, author_name, author_handle,
                          title, body, transcript, screen_text, lang, published_at,
                          url, duration_s, raw_json, source, content_hash,
                          first_seen_at, last_seen_at)
        VALUES (:aid,:p,:pid,:kind,:parent,:rep,:aname,:ahandle,
                :title,:body,:tx,:screen,:lang,:pub,:url,
                :dur,:raw,:src,:hash,:ts,:ts)
        ON CONFLICT(platform, platform_item_id) DO UPDATE SET
          last_seen_at = :ts,
          deleted_at   = NULL,
          title        = CASE WHEN item.content_hash <> :hash THEN :title ELSE item.title END,
          body         = CASE WHEN item.content_hash <> :hash THEN :body  ELSE item.body  END,
          transcript   = COALESCE(:tx, item.transcript),
          screen_text  = COALESCE(:screen, item.screen_text),
          url          = COALESCE(:url, item.url),
          duration_s   = COALESCE(:dur, item.duration_s),
          raw_json      = :raw,
          content_hash  = :hash,
          is_repost     = :rep,
          author_name   = COALESCE(:aname, item.author_name),
          author_handle = COALESCE(:ahandle, item.author_handle)
        RETURNING id
        """,
        dict(aid=account_id, p=platform, pid=str(platform_item_id), kind=kind,
             parent=parent_id, rep=1 if is_repost else 0,
             aname=author_name, ahandle=author_handle, title=title, body=body,
             tx=transcript, screen=screen_text, lang=lang, pub=published_at, url=url, dur=duration_s,
             raw=json.dumps(raw, ensure_ascii=False), src=source, hash=chash, ts=ts),
    ).fetchone()

    is_new = prev_hash is None
    changed = (not is_new) and prev_hash != chash
    return cur["id"], is_new, changed


def record_metrics(conn, item_id, *, reactions=None, comments=None, shares=None,
                   saves=None, views=None, raw=None, carry=()) -> bool:
    """Append a metric snapshot only when a value actually moved.

    This is the whole anti-bloat strategy. Most of the corpus is old posts whose
    counts never change again, so re-syncing monthly costs almost no rows.
    """
    prev = conn.execute(
        """SELECT reactions, comments, shares, saves, views FROM metric_snapshot
           WHERE item_id=? ORDER BY captured_at DESC LIMIT 1""",
        (item_id,),
    ).fetchone()
    cur = (reactions, comments, shares, saves, views)
    # The snapshot records exactly what was measured (None = not seen). The item's
    # CURRENT values differ for fields in `carry` -- the ones the calling route can
    # never see (Adapter.unmeasured): those keep the last reading. Without it the
    # routine LinkedIn sync blanked the views a client's export had supplied, and
    # the views section vanished from the brief. A None in any other field still
    # blanks (an owner hiding a count must stop showing the old one). A payload
    # with no counts at all (a vendor format change) records nothing.
    if all(v is None for v in cur):
        return False
    if prev is not None and tuple(prev) == cur:
        return False

    # captured_at is second-granular and part of the primary key. Two genuine
    # changes inside the same second would collide, and INSERT OR REPLACE would
    # silently discard the earlier one -- so step forward to the next free second
    # instead of losing a data point. In normal use (syncs hours or weeks apart)
    # this loop never runs.
    ts = now()
    while conn.execute(
        "SELECT 1 FROM metric_snapshot WHERE item_id=? AND captured_at=?",
        (item_id, ts),
    ).fetchone():
        ts += 1

    conn.execute(
        """INSERT INTO metric_snapshot
           (item_id, captured_at, reactions, comments, shares, saves, views, metrics_raw)
           VALUES (?,?,?,?,?,?,?,?)""",
        (item_id, ts, reactions, comments, shares, saves, views,
         json.dumps(raw, ensure_ascii=False) if raw is not None else None),
    )
    conn.execute(
        f"""UPDATE item SET {', '.join(
                f"m_{c}=COALESCE(?, m_{c})" if c in carry else f"m_{c}=?"
                for c in ("reactions", "comments", "shares", "views"))},
                metrics_at=? WHERE id=?""",
        (reactions, comments, shares, views, ts, item_id),
    )
    return True


# ── comments ────────────────────────────────────────────────────────────────

def upsert_comment(conn, item_id, *, platform_comment_id, body=None,
                   author_name=None, published_at=None, reactions=None,
                   is_author_reply=False, lang=None, raw=None) -> tuple[int, bool]:
    """Insert or refresh one comment. Returns (id, is_new).

    Idempotent on (item_id, platform_comment_id) so re-fetching a post's thread costs
    money but never duplicates rows. The body is refreshed on conflict because
    comments can be edited, and reactions because they keep accruing.

    Existence is checked BEFORE the upsert. conn.total_changes is cumulative for the
    whole connection, so using it to mean "did this insert" reported every row as
    new; the vendor returns duplicates within a single batch, which made the count
    overstate what actually landed.
    """
    prior = {r["id"] for r in conn.execute(
        "SELECT id FROM comment WHERE item_id=? AND platform_comment_id=?",
        (item_id, platform_comment_id))}
    conn.execute(
        """INSERT INTO comment (item_id, platform_comment_id, published_at, body,
                                author_name, is_author_reply, reactions, lang,
                                fetched_at, raw_json)
           VALUES (:i,:pid,:pub,:body,:an,:ar,:rx,:lang,:ts,:raw)
           ON CONFLICT(item_id, platform_comment_id) DO UPDATE SET
             body       = COALESCE(excluded.body, body),
             reactions  = COALESCE(excluded.reactions, reactions),
             fetched_at = excluded.fetched_at""",
        dict(i=item_id, pid=platform_comment_id, pub=published_at, body=body,
             an=author_name, ar=1 if is_author_reply else 0, rx=reactions,
             lang=lang, ts=now(),
             raw=json.dumps(raw) if raw is not None else None),
    )
    row = conn.execute(
        "SELECT id FROM comment WHERE item_id=? AND platform_comment_id=?",
        (item_id, platform_comment_id)).fetchone()
    return row["id"], row["id"] not in prior


def comments_for_account(conn, account_id, *, include_author_replies=False):
    """Every comment on an account's own posts, newest first."""
    sql = f"""SELECT c.*, i.url item_url, i.body item_body, i.published_at item_at
               FROM comment c JOIN item i ON i.id = c.item_id
              WHERE {account_items()} AND COALESCE(i.is_repost,0) = 0"""
    if not include_author_replies:
        sql += " AND c.is_author_reply = 0"
    sql += " ORDER BY c.published_at DESC"
    return [dict(r) for r in conn.execute(sql, (account_id, account_id))]


def items_missing_comments(conn, account_id=None, *, limit=None, min_comments=1,
                           per_post=None):
    """Posts whose comment count says there is a thread we have not fetched.

    Driven off the metric cache rather than a queue: the work-list is derived from
    the data, so an interrupted run resumes correctly with no state to reconcile.

    `per_post` is the ceiling the fetcher will actually request, and it MUST be
    passed or this over-reports. Comparing what we hold against the full comment
    count leaves every post with more comments than the cap permanently in the
    work-list: a 185-comment post from which we fetched 25 looks 160 short forever,
    so each run re-fetches and re-BILLS the same 25 rows. One run billed 189
    comments to store 46.
    """
    target = ("MIN(COALESCE(i.m_comments,0), ?)" if per_post
              else "COALESCE(i.m_comments,0)")
    where = ["COALESCE(i.is_repost,0) = 0", "COALESCE(i.m_comments,0) >= ?"]
    args: list = [min_comments]
    if account_id is not None:
        where.append("i.account_id = ?")
        args.append(account_id)
    # Never fetched, or its comment COUNT grew since. Not "any metric moved": a
    # thread that returns fewer rows than its count claims was re-bought every
    # time its likes ticked up. Rows fetched before comments_n existed fall back
    # to the count snapshotted at or before the fetch.
    where.append("""(i.comments_at IS NULL OR COALESCE(i.m_comments,0) > COALESCE(
        i.comments_n,
        (SELECT s.comments FROM metric_snapshot s
          WHERE s.item_id = i.id AND s.captured_at <= i.comments_at
          ORDER BY s.captured_at DESC LIMIT 1),
        CASE WHEN COALESCE(i.metrics_at,0) > i.comments_at THEN 0
             ELSE i.m_comments END))""")
    if per_post:
        args.append(int(per_post))
    sql = f"""SELECT i.id, i.platform, i.url, i.platform_item_id, i.m_comments,
                     (SELECT COUNT(*) FROM comment c WHERE c.item_id = i.id) have
                FROM item i
               WHERE {' AND '.join(where)}
               GROUP BY i.id
              HAVING have < {target}
               ORDER BY i.m_comments DESC"""
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]


def mark_comments_fetched(conn, item_id) -> None:
    """Record that we paid for this thread, whatever the vendor chose to return,
    and the comment count it had then (the bar for fetching it again)."""
    conn.execute("UPDATE item SET comments_at=?, comments_n=m_comments WHERE id=?",
                 (now(), item_id))


def add_media(conn, item_id, *, role, seq=0, remote_url=None, mime=None,
              width=None, height=None, duration_hint=None, t_offset_s=None) -> int:
    cur = conn.execute(
        """INSERT INTO media (item_id, role, seq, mime, width, height, remote_url,
                              fetched_at, t_offset_s, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (item_id, role, seq, mime, width, height, remote_url, now(), t_offset_s, now()),
    )
    return cur.lastrowid


# ── runs ────────────────────────────────────────────────────────────────────

def start_run(conn, *, adapter, mode, account_id=None, started_at=None) -> int:
    """Open a run row. `started_at` overrides the clock.

    The override exists for concurrent fetches: a route that failed on a worker
    thread is written here afterwards, on the owner thread, and stamping it with
    the time the row was written rather than the time the fetch began would make
    a 30s vendor timeout look instantaneous.
    """
    cur = conn.execute(
        """INSERT INTO scrape_run (account_id, adapter, mode, started_at, status)
           VALUES (?,?,?,?, 'running')""",
        (account_id, adapter, mode, started_at or now()),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id, *, status, seen=0, new=0, changed=0,
               cost_usd=None, error=None, raw=None) -> None:
    conn.execute(
        """UPDATE scrape_run SET finished_at=?, status=?, items_seen=?, items_new=?,
                                 items_changed=?, cost_usd=?, error=?,
                                 items_raw=COALESCE(?, items_raw) WHERE id=?""",
        (now(), status, seen, new, changed, cost_usd,
         (error or "")[:2000] or None, raw, run_id),
    )
    conn.commit()


# A run row still 'running' after this long belongs to a process that is gone:
# Apify's sync calls cap at 310s, and the lock means a live run would be holding
# it. Two hours leaves room for a long enrich backfill.
ORPHAN_AFTER_S = 2 * 3600


def mark_orphaned_runs(conn, *, older_than_s: int = ORPHAN_AFTER_S) -> int:
    """Close run rows a killed process left 'running'. Returns how many.

    Before SIGTERM was handled, every `systemctl stop` (and every reboot) left its
    rows 'running' forever, which reads as a run still in progress and hides that
    the last attempt never finished. Call only while holding the run lock, so no
    live run can be mistaken for a dead one.
    """
    cur = conn.execute(
        """UPDATE scrape_run SET status='interrupted', finished_at=COALESCE(finished_at, ?),
                  error=COALESCE(error, 'process ended without closing this run')
            WHERE status='running' AND started_at < ?""",
        (now(), now() - older_than_s))
    conn.commit()
    return cur.rowcount


def one_line(error: str | None, width: int = 160) -> str:
    """The first meaningful line of a stored error, for a terminal.

    scrape_run.error holds "Type: message" followed by the full traceback, and
    printing a slice of it showed "...Traceback (most" on every failed adapter.
    """
    for line in (error or "").splitlines():
        line = line.strip()
        if line and not line.startswith("Traceback (most recent call last)"):
            return line[:width]
    return ""


def health(conn, stale_after_s: int = 60 * 60 * 26) -> list[dict]:
    """Last outcome per adapter, with a staleness flag.

    Default threshold is 26h so a daily job that drifts an hour does not read as
    broken.
    """
    rows = conn.execute(
        """SELECT adapter, MAX(started_at) AS last_run, status, items_new, error
           FROM scrape_run GROUP BY adapter ORDER BY adapter"""
    ).fetchall()
    t = now()
    return [
        {**dict(r), "stale": (t - (r["last_run"] or 0)) > stale_after_s}
        for r in rows
    ]


def search(conn, query: str, limit: int = 25, *,
           open_mark: str = "[", close_mark: str = "]") -> list[sqlite3.Row]:
    """Full-text hits with the matched term wrapped in `open_mark`/`close_mark`.

    The delimiters are caller-supplied because a caption containing a literal "["
    is indistinguishable from a highlight marker once the snippet is a plain
    string; a terminal can live with that, an HTML renderer turning them into tags
    cannot.
    """
    return conn.execute(
        f"""SELECT i.id, i.platform, a.handle, a.relationship, i.kind,
                  i.published_at, i.url, i.m_reactions, i.m_comments,
                  snippet(item_fts, -1, ?, ?, ' … ', 18) AS snip
           FROM item_fts f
           JOIN item i ON i.id = f.rowid
           JOIN account a ON a.id = i.account_id
           WHERE item_fts MATCH ?
           ORDER BY bm25(item_fts) LIMIT ?""",
        (open_mark, close_mark, query, limit),
    ).fetchall()


# ── erasure ─────────────────────────────────────────────────────────────────

def forget(conn, *, handle: str | None = None, account_id: int | None = None,
           platform: str | None = None, dry_run: bool = True,
           media_root: str | Path | None = None,
           thumb_dir: str | Path | None = None) -> dict:
    """Delete everything held about one account.

    A GDPR erasure request is not satisfied by stopping future syncs, and the corpus
    keeps raw_json forever by design, which makes "just delete the row" insufficient
    without something that reaches the derived tables too. FK cascades handle item,
    media and metric_snapshot; analysis lives in a separate database with no
    cross-database foreign keys, so the caller prunes it (see analyze.prune).

    Returns what would be, or was, removed. Defaults to a dry run because this is
    the one operation with no undo.
    """
    where, args = [], []
    if account_id is not None:
        where.append("id = ?"); args.append(account_id)
    if handle:
        where.append("handle = ?"); args.append(handle)
    if platform:
        where.append("platform = ?"); args.append(platform)
    if not where:
        raise ValueError("refusing to forget everything; name an account")

    rows = conn.execute(
        f"SELECT id, platform, handle, relationship FROM account "
        f"WHERE {' AND '.join(where)}", args).fetchall()
    if not rows:
        return {"accounts": [], "items": 0, "media": 0, "comments": 0,
                "metrics": 0, "dry_run": dry_run}

    ids = [r["id"] for r in rows]
    ph = ",".join("?" * len(ids))
    # The items that actually go: owned here and not also shown by an account we
    # keep (a shared collab post is re-homed below, not deleted).
    gone = (f"SELECT id FROM item WHERE account_id IN ({ph}) AND NOT EXISTS ("
            f"SELECT 1 FROM item_account l WHERE l.item_id = item.id "
            f"AND l.account_id NOT IN ({ph}))")
    gargs = ids * 2
    counts = {
        "items": conn.execute(
            f"SELECT COUNT(*) c FROM ({gone})", gargs).fetchone()["c"],
        "media": conn.execute(
            f"SELECT COUNT(*) c FROM media WHERE item_id IN "
            f"({gone})", gargs).fetchone()["c"],
        "comments": conn.execute(
            f"SELECT COUNT(*) c FROM comment WHERE item_id IN "
            f"({gone})", gargs).fetchone()["c"],
        "metrics": conn.execute(
            f"SELECT COUNT(*) c FROM metric_snapshot WHERE item_id IN "
            f"({gone})", gargs).fetchone()["c"],
    }
    out = {"accounts": [dict(r) for r in rows], **counts, "dry_run": dry_run,
           "media_files": []}

    # Stored keyframes and stills are personal data on disk, not just rows. Files are
    # content-addressed, so one can be shared with an account we are KEEPING (two
    # identical black frames): only a file no surviving row points at may go.
    # Compared as RESOLVED paths: a kept row may still hold a legacy absolute path
    # to the same file another row holds relative, and unlinking the stored string
    # rather than the resolved file deleted nothing once paths became relative.
    from .enrich.media import resolve
    kept = {str(resolve(r["local_path"], media_root)) for r in conn.execute(
        f"SELECT local_path FROM media WHERE local_path IS NOT NULL AND item_id NOT IN "
        f"({gone})", gargs)}
    for r in conn.execute(
            f"SELECT DISTINCT local_path FROM media WHERE local_path IS NOT NULL AND item_id IN "
            f"({gone})", gargs):
        f = str(resolve(r["local_path"], media_root))
        if f not in kept and f not in out["media_files"]:
            out["media_files"].append(f)
    item_ids = [r["id"] for r in conn.execute(gone, gargs)]
    # Derived files no row records: --keep-audio archives (named by item id, see
    # enrich.run) and the brief's downscaled thumbnails (named by source path+mtime,
    # so the key has to be computed while the source still exists).
    if media_root:
        for iid in item_ids:
            d = hashlib.sha256(str(iid).encode()).hexdigest()[:16]
            a = Path(media_root).expanduser() / "audio" / f"{d}.opus"
            if a.exists():
                out["media_files"].append(str(a))
    if thumb_dir:
        for f in list(out["media_files"]):
            try:
                mt = Path(f).stat().st_mtime_ns
            except OSError:
                continue
            # Must match brief._small's cache key (THUMB_PX = 200).
            key = hashlib.sha256(f"{Path(f)}\x1f{mt}\x1f200".encode()).hexdigest()[:20]
            t = Path(thumb_dir).expanduser() / f"{key}.jpg"
            if t.exists():
                out["media_files"].append(str(t))

    if dry_run:
        return out

    # Rows first, in one transaction, files after: a failure must leave the account
    # whole (retryable), never rows without files. scrape_run keeps its cost and
    # timing as provenance but loses the link to the person.
    with conn:
        # A collab post this account owns but a kept co-author also shows is that
        # co-author's content too: re-home it rather than let the cascade take it.
        conn.execute(
            f"""UPDATE item SET account_id = (
                    SELECT MIN(l.account_id) FROM item_account l
                     WHERE l.item_id = item.id AND l.account_id NOT IN ({ph}))
                 WHERE account_id IN ({ph}) AND EXISTS (
                    SELECT 1 FROM item_account l
                     WHERE l.item_id = item.id AND l.account_id NOT IN ({ph}))""",
            ids * 3)
        conn.execute("""DELETE FROM item_account WHERE EXISTS (SELECT 1 FROM item
                         WHERE item.id = item_account.item_id
                           AND item.account_id = item_account.account_id)""")
        conn.execute(f"UPDATE scrape_run SET account_id=NULL WHERE account_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM account WHERE id IN ({ph})", ids)   # cascades
    for f in out["media_files"]:
        try:
            Path(f).unlink(missing_ok=True)
        except OSError:
            pass
    return out


# ── media path migration ────────────────────────────────────────────────────

def fix_paths(conn, *, media_root=None, execute: bool = False) -> dict:
    """Rewrite legacy absolute media.local_path values to media-root-relative, and
    clear the ones whose file is gone. Dry run unless `execute`.

    A dangling path is cleared rather than kept because a row that claims a stored
    file is never fetched again: the image stays missing from every brief for good.
    Cleared, a still with a remote_url goes back into enrich's download queue (its
    state returns to 'referenced'); a keyframe comes back with `enrich --redo`.
    """
    from .enrich.media import media_root as _root, resolve, to_stored
    root = _root(media_root)
    out = {"checked": 0, "ok": 0, "rewritten": 0, "dangling": 0, "outside_root": 0,
           "dry_run": not execute}
    for r in conn.execute("SELECT id, local_path FROM media WHERE local_path IS NOT NULL "
                          "ORDER BY id").fetchall():
        out["checked"] += 1
        stored = r["local_path"]
        p = resolve(stored, root)
        if p is None or not p.exists():
            out["dangling"] += 1
            if execute:
                conn.execute("UPDATE media SET local_path=NULL, state=CASE WHEN "
                             "state='stored' THEN 'referenced' ELSE state END "
                             "WHERE id=?", (r["id"],))
            continue
        new = to_stored(p, root)
        if new == stored:
            if Path(new).is_absolute():
                out["outside_root"] += 1     # exists, but not under this root
            else:
                out["ok"] += 1
            continue
        out["rewritten"] += 1
        if execute:
            conn.execute("UPDATE media SET local_path=? WHERE id=?", (new, r["id"]))
    if execute:
        conn.commit()
    return out
