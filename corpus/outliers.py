"""Posts that did several times better than their own account's usual.

The useful competitor signal is not "they got 400 likes" but "they got 400 likes on
an account that normally gets 60": something in that post worked, and it is worth a
look while it is still fresh. Each post is measured against the same account's
posts BEFORE it, so a channel that grows steadily does not light up every week,
and one viral post does not raise the bar for itself.

Read-only and free: it works off counts the sync already stored.
"""
from __future__ import annotations

import statistics

from . import store

BASELINE_POSTS = 30      # how many earlier posts make the account's "usual"
MIN_BASELINE = 5         # fewer than this and "usual" is an anecdote
DEFAULT_X = 3.0
DEFAULT_DAYS = 14
# An absolute floor next to the multiple. "5x usual" on an account that usually
# gets 4 is a post with 20 likes, and it topped the list above a 286-reaction post
# on a real account; nobody should study a post because it got 20 likes.
DEFAULT_MIN = 50


def _eng(r) -> int:
    return sum(int(r[k] or 0) for k in ("m_reactions", "m_comments", "m_shares"))


def find(conn, *, days: int = DEFAULT_DAYS, min_x: float = DEFAULT_X,
         account_ids=None, relationships=("competitor", "influencer"),
         item_ids=None, min_eng: int = DEFAULT_MIN) -> list[dict]:
    """Recent posts at >= min_x times their account's median, best first.

    `item_ids` narrows the candidates to those posts (e.g. the ones a sync just
    added); the baseline is still the account's full history before each.
    """
    where, args = ["1=1"], []
    if account_ids:
        where.append(f"id IN ({','.join('?' * len(account_ids))})")
        args += list(account_ids)
    if relationships:
        where.append(f"relationship IN ({','.join('?' * len(relationships))})")
        args += list(relationships)
    accounts = conn.execute(
        f"SELECT * FROM account WHERE {' AND '.join(where)}", args).fetchall()

    since = store.now() - days * 86400
    wanted = set(item_ids) if item_ids is not None else None
    out = []
    for a in accounts:
        posts = conn.execute(
            f"""SELECT i.id, i.published_at, i.kind, i.url, i.body, i.title,
                       i.m_reactions, i.m_comments, i.m_shares, i.m_views
                  FROM item i
                 WHERE {store.account_items()} AND i.deleted_at IS NULL
                   AND COALESCE(i.is_repost,0) = 0
                   AND (COALESCE(i.author_handle, '') = ''
                        OR lower(i.author_handle) = lower(?))
                 ORDER BY i.published_at""", (a["id"], a["id"], a["handle"])).fetchall()
        for n, p in enumerate(posts):
            if p["published_at"] < since or (wanted is not None and p["id"] not in wanted):
                continue
            before = [_eng(q) for q in posts[max(0, n - BASELINE_POSTS):n]]
            if len(before) < MIN_BASELINE:
                continue
            usual = statistics.median(before)
            e = _eng(p)
            # A floor of 1 so an account that usually gets 0 does not produce
            # "infinity times usual" from a post with 3 likes.
            x = e / max(usual, 1)
            if x >= min_x and e >= max(1, min_eng):
                text = ((p["body"] or p["title"] or "").strip().splitlines() or [""])[0]
                out.append({
                    "account_id": a["id"], "platform": a["platform"],
                    "handle": a["handle"], "relationship": a["relationship"],
                    "item_id": p["id"], "published_at": p["published_at"],
                    "kind": p["kind"], "url": p["url"], "engagement": e,
                    "usual": int(usual), "x": round(x, 1), "views": p["m_views"],
                    "opening": text[:140],
                })
    return sorted(out, key=lambda o: -o["x"])
