"""The weekly "what changed" message.

A corpus that syncs every night is only useful if someone looks at it, and nobody
opens a dashboard on a Monday to find out that nothing happened. So once a week
this reads what the nightly runs stored and says, in one WhatsApp message, what
moved: who posted and whether it did better than usual for them, which posts kept
climbing, who gained followers, what broke out, what the audience asked, and which
accounts have quietly stopped refreshing. That last one is not decoration: the
scheduled sync once stopped for 78 days and nothing said so.

Read-only and free: everything comes from rows the sync already paid for.

Format rules, because the reader is a phone: short lines, no tables, no markdown,
every link on its own line (WhatsApp only previews and taps cleanly that way), and
the whole thing under ~1500 characters. When there is more than fits, each section
loses its tail before any section disappears.
"""
from __future__ import annotations

import html
import os
import statistics
from datetime import datetime
from urllib.parse import urlparse

from . import freshness, outliers, store

MAX_CHARS = 1500
BREAKOUT_MIN_ENGAGEMENT = 50   # a "3x usual" on an account that gets 4 likes is noise
BASELINE_POSTS = outliers.BASELINE_POSTS
MIN_BASELINE = outliers.MIN_BASELINE
STALE_FACTOR = 2               # stale = not refreshed in 2x its freshness TTL
QUESTION_MIN_CHARS = 15


def _tz():
    name = os.environ.get("CORPUS_TZ")
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, _tz()).strftime("%-d %b")


def _eng(r) -> int:
    return sum(int(r[k] or 0) for k in ("m_reactions", "m_comments", "m_shares"))


def _label(a) -> str:
    plat = {"instagram": "ig", "linkedin": "li", "youtube": "yt"}.get(
        a["platform"], a["platform"])
    return f"{a['handle']} ({plat})"


def _opening(r, n: int = 60) -> str:
    text = ((r["body"] or r["title"] or "").strip().splitlines() or [""])[0]
    return text if len(text) <= n else text[:n - 1] + "…"


def _scope(conn, client_ref):
    if client_ref:
        return conn.execute("SELECT * FROM account WHERE client_ref=? ORDER BY "
                            "relationship, platform, handle", (client_ref,)).fetchall()
    return conn.execute("SELECT * FROM account ORDER BY relationship, platform, "
                        "handle").fetchall()


# ── sections ────────────────────────────────────────────────────────────────

def new_posts(conn, accounts, since: int) -> list[dict]:
    """Per account: posts published in the window, against that account's usual.

    "Usual" is the median of the account's posts BEFORE the window, the same rule
    outliers uses, so a good week does not raise its own bar.
    """
    out = []
    for a in accounts:
        posts = conn.execute(
            f"""SELECT i.id, i.published_at, i.url, i.body, i.title, i.kind,
                       i.m_reactions, i.m_comments, i.m_shares, i.m_views
                  FROM item i
                 WHERE {store.account_items()} AND i.deleted_at IS NULL
                   AND COALESCE(i.is_repost,0) = 0
                 ORDER BY i.published_at""", (a["id"], a["id"])).fetchall()
        fresh = [p for p in posts if p["published_at"] >= since]
        if not fresh:
            continue
        before = [_eng(p) for p in posts if p["published_at"] < since][-BASELINE_POSTS:]
        usual = statistics.median(before) if len(before) >= MIN_BASELINE else None
        best = max(fresh, key=_eng)
        med = statistics.median(_eng(p) for p in fresh)
        out.append({
            "account": _label(a), "relationship": a["relationship"],
            "n": len(fresh), "median": int(med),
            "usual": None if usual is None else int(usual),
            "x": None if usual is None else round(med / max(usual, 1), 1),
            "best_url": best["url"], "best_eng": _eng(best),
            "best_opening": _opening(best),
        })
    return sorted(out, key=lambda r: (-(r["x"] or 0), -r["n"]))


def movers(conn, accounts, since: int, limit: int = 5) -> list[dict]:
    """Posts whose interactions grew most inside the window.

    Measured from metric_snapshot, last reading before the window to last reading
    in it, field by field and only where both readings exist: a route that cannot
    see a counter records NULL there, and treating NULL as 0 would turn a views
    column going dark into a "-40,000 views" mover. Posts first published inside
    the window are left out, since "new posts" already covers them and their whole
    count would read as growth.
    """
    if not accounts:
        return []
    ids = [a["id"] for a in accounts]
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT i.id, i.url, i.body, i.title, a.handle, a.platform,
                   b.reactions br, b.comments bc, b.shares bs, b.views bv,
                   e.reactions er, e.comments ec, e.shares es, e.views ev
              FROM item i JOIN account a ON a.id = i.account_id
              JOIN metric_snapshot b ON b.item_id = i.id AND b.captured_at = (
                   SELECT MAX(captured_at) FROM metric_snapshot
                    WHERE item_id = i.id AND captured_at < ?)
              JOIN metric_snapshot e ON e.item_id = i.id AND e.captured_at = (
                   SELECT MAX(captured_at) FROM metric_snapshot
                    WHERE item_id = i.id AND captured_at >= ?)
             WHERE i.account_id IN ({marks}) AND i.deleted_at IS NULL
               AND i.published_at < ?""",
        (since, since, *ids, since)).fetchall()

    def d(r, a, b):
        return (r[b] - r[a]) if r[a] is not None and r[b] is not None else 0

    out = []
    for r in rows:
        delta = d(r, "br", "er") + d(r, "bc", "ec") + d(r, "bs", "es")
        if delta <= 0:
            continue
        out.append({"account": _label(r), "item_id": r["id"], "url": r["url"],
                    "delta": delta, "views_delta": d(r, "bv", "ev"),
                    "opening": _opening(r)})
    return sorted(out, key=lambda m: -m["delta"])[:limit]


def follower_changes(conn, accounts, since: int) -> list[dict]:
    """Follower count now vs at the start of the window, per account.

    The start is the last snapshot taken before the window; an account first
    measured inside the window has no start and is skipped, rather than reporting
    its whole audience as this week's gain.
    """
    out = []
    for a in accounts:
        start = conn.execute(
            """SELECT followers FROM account_metric_snapshot
                WHERE account_id=? AND captured_at < ? AND followers IS NOT NULL
                ORDER BY captured_at DESC LIMIT 1""", (a["id"], since)).fetchone()
        end = conn.execute(
            """SELECT followers FROM account_metric_snapshot
                WHERE account_id=? AND followers IS NOT NULL
                ORDER BY captured_at DESC LIMIT 1""", (a["id"],)).fetchone()
        if not start or not end or end["followers"] == start["followers"]:
            continue
        out.append({"account": _label(a), "start": start["followers"],
                    "end": end["followers"],
                    "delta": end["followers"] - start["followers"]})
    return sorted(out, key=lambda r: -abs(r["delta"]))


def breakouts(conn, accounts, days: int) -> list[dict]:
    if not accounts:
        return []
    # account_ids, never None: an empty scope must not widen to every account.
    found = outliers.find(conn, days=days, account_ids=[a["id"] for a in accounts],
                          relationships=None)
    return [o for o in found if o["engagement"] >= BREAKOUT_MIN_ENGAGEMENT]


def _is_question(body: str) -> bool:
    body = (body or "").strip()
    return len(body) >= QUESTION_MIN_CHARS and "?" in body


def questions(conn, accounts, since: int, limit: int = 3) -> list[dict]:
    """Audience comments that ask something, new this window.

    New means published in the window, or fetched in it when the platform gave no
    date. Author names are left out on purpose: the digest leaves the box, and the
    commenter never agreed to be in it (see the comment table's header).
    """
    if not accounts:
        return []
    ids = [a["id"] for a in accounts]
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT c.body, c.reactions, i.url, a.handle, a.platform
              FROM comment c JOIN item i ON i.id = c.item_id
              JOIN account a ON a.id = i.account_id
             WHERE i.account_id IN ({marks}) AND c.is_author_reply = 0
               AND COALESCE(c.published_at, c.fetched_at) >= ?
             ORDER BY COALESCE(c.reactions,0) DESC, c.published_at DESC""",
        (*ids, since)).fetchall()
    out, seen = [], set()
    for r in rows:
        if not _is_question(r["body"]):
            continue
        text = " ".join(r["body"].split())
        if text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append({"account": _label(r), "url": r["url"],
                    "text": text if len(text) <= 120 else text[:119] + "…"})
        if len(out) >= limit:
            break
    return out


def health(conn, accounts, since: int, now: int) -> list[str]:
    """Accounts that stopped refreshing, and routes that failed this window."""
    problems = []
    for a in accounts:
        limit = STALE_FACTOR * freshness.ttl_for(a["relationship"])
        if not a["last_scraped_at"]:
            problems.append(f"{_label(a)}: never synced")
        elif now - a["last_scraped_at"] > limit:
            problems.append(f"{_label(a)}: not synced for "
                            f"{(now - a['last_scraped_at']) // 86400}d")
    ids = [a["id"] for a in accounts]
    if ids:
        marks = ",".join("?" * len(ids))
        for r in conn.execute(
                f"""SELECT adapter, COUNT(*) n FROM scrape_run
                     WHERE status='error' AND started_at >= ?
                       AND (account_id IS NULL OR account_id IN ({marks}))
                     GROUP BY adapter ORDER BY n DESC""", (since, *ids)):
            problems.append(f"{r['adapter']}: {r['n']} failed run(s)")
    return problems


def build(conn, *, days: int = 7, client_ref: str | None = None,
          now: int | None = None) -> dict:
    now = now or store.now()
    since = now - days * 86400
    accounts = _scope(conn, client_ref)
    d = {
        "days": days, "since": since, "now": now, "client_ref": client_ref,
        "accounts": len(accounts),
        "new_posts": new_posts(conn, accounts, since),
        "movers": movers(conn, accounts, since),
        "followers": follower_changes(conn, accounts, since),
        "breakouts": breakouts(conn, accounts, days),
        "questions": questions(conn, accounts, since),
        "health": health(conn, accounts, since, now),
    }
    d["empty"] = not any(d[k] for k in ("new_posts", "movers", "followers",
                                        "breakouts", "questions", "health"))
    return d


# ── rendering ───────────────────────────────────────────────────────────────

def _title(d) -> str:
    scope = f" {d['client_ref']}" if d["client_ref"] else ""
    return f"corpus weekly{scope}, {_day(d['since'])} to {_day(d['now'])}"


def _signed(n: int) -> str:
    return f"+{n:,}" if n >= 0 else f"{n:,}"


def _sections(d, cap: int) -> list[tuple[str, list[str]]]:
    """(heading, lines) in priority order, each list cut to `cap` entries."""
    def more(lst):
        return [f"+{len(lst) - cap} more"] if len(lst) > cap else []

    out = []
    if d["health"]:
        out.append(("Needs attention", d["health"][:cap] + more(d["health"])))
    if d["breakouts"]:
        lines = []
        for o in d["breakouts"][:cap]:
            lines += [f"{_label(o)}: {o['x']}x usual ({o['engagement']:,} vs "
                      f"{o['usual']:,})", o["url"] or ""]
        out.append(("Breakouts", [ln for ln in lines if ln] + more(d["breakouts"])))
    if d["new_posts"]:
        lines = []
        for p in d["new_posts"][:cap]:
            vs = (f", typical {p['median']:,} vs usual {p['usual']:,}"
                  if p["usual"] is not None else f", typical {p['median']:,}")
            lines.append(f"{p['account']}: {p['n']} new{vs}")
            if p["best_url"]:
                lines.append(p["best_url"])
        out.append(("New posts", lines + more(d["new_posts"])))
    if d["movers"]:
        lines = []
        for m in d["movers"][:cap]:
            views = (f", {_signed(m['views_delta'])} views"
                     if m["views_delta"] > 0 else "")
            lines += [f"{m['account']}: {_signed(m['delta'])} interactions{views}"]
            if m["url"]:
                lines.append(m["url"])
        out.append(("Still climbing", lines + more(d["movers"])))
    if d["followers"]:
        out.append(("Followers", [
            f"{f['account']}: {_signed(f['delta'])} ({f['end']:,})"
            for f in d["followers"][:cap]] + more(d["followers"])))
    if d["questions"]:
        lines = []
        for q in d["questions"][:cap]:
            lines.append(f"{q['account']}: \"{q['text']}\"")
            if q["url"]:
                lines.append(q["url"])
        out.append(("Audience asked", lines + more(d["questions"])))
    return out


def render_text(d: dict, max_chars: int = MAX_CHARS) -> str:
    if d["empty"]:
        scope = f" for {d['client_ref']}" if d["client_ref"] else ""
        return (f"corpus weekly{scope}: nothing changed in the last {d['days']} "
                f"days ({d['accounts']} accounts tracked).")
    text = ""
    for cap in (5, 3, 2, 1):
        parts = [_title(d)]
        for heading, lines in _sections(d, cap):
            parts.append("\n" + heading.upper() + "\n" + "\n".join(lines))
        text = "\n".join(parts)
        if len(text) <= max_chars:
            return text
    # Still too long at one entry a section: long URLs. Cut at a line boundary.
    cut = text[:max_chars - 2].rsplit("\n", 1)[0]
    return cut + "\n…"


def render_html(d: dict) -> str:
    e = html.escape

    def link(url, label=None):
        # Post URLs are vendor data. A "javascript:" or "data:" URL would become a
        # live link in a file opened in a browser, so only http(s) gets an <a>.
        if not url:
            return ""
        if urlparse(str(url)).scheme.lower() not in ("http", "https"):
            return e(str(url))
        return f'<a href="{e(url)}">{e(label or url)}</a>'

    body = [f"<h1>{e(_title(d))}</h1>"]
    if d["empty"]:
        body.append(f"<p>Nothing changed in the last {d['days']} days.</p>")
    for heading, key in (("Needs attention", "health"), ("Breakouts", "breakouts"),
                         ("New posts", "new_posts"), ("Still climbing", "movers"),
                         ("Followers", "followers"), ("Audience asked", "questions")):
        rows = d[key]
        if not rows:
            continue
        lis = []
        for r in rows:
            if key == "health":
                lis.append(e(r))
            elif key == "breakouts":
                lis.append(f"{e(r['handle'])}: {r['x']}x usual ({r['engagement']:,} vs "
                           f"{r['usual']:,}) {e(r['opening'] or '')} {link(r['url'], 'open')}")
            elif key == "new_posts":
                vs = f" vs usual {r['usual']:,}" if r["usual"] is not None else ""
                lis.append(f"{e(r['account'])}: {r['n']} new, typical "
                           f"{r['median']:,}{vs}. Best: {e(r['best_opening'])} "
                           f"{link(r['best_url'], 'open')}")
            elif key == "movers":
                lis.append(f"{e(r['account'])}: {_signed(r['delta'])} interactions "
                           f"{e(r['opening'])} {link(r['url'], 'open')}")
            elif key == "followers":
                lis.append(f"{e(r['account'])}: {_signed(r['delta'])} "
                           f"({r['start']:,} to {r['end']:,})")
            else:
                lis.append(f"{e(r['account'])}: &ldquo;{e(r['text'])}&rdquo; "
                           f"{link(r['url'], 'post')}")
        body.append(f"<h2>{e(heading)}</h2><ul>"
                    + "".join(f"<li>{x}</li>" for x in lis) + "</ul>")
    return ("<!doctype html><html><head><meta charset=\"utf-8\">"
            f"<title>{e(_title(d))}</title>"
            "<style>body{font:15px/1.5 system-ui,sans-serif;max-width:46rem;"
            "margin:2rem auto;padding:0 1rem}h2{font-size:1rem;margin-top:1.5rem}"
            "li{margin:.3rem 0}</style></head><body>"
            + "".join(body) + "</body></html>")
