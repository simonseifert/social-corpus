"""Per-account freshness: probe cheaply, pull fully only when something moved.

Lifted from the living-docs `sources.yaml` model, where every stale-able claim carries
a TTL and a change detector so a refresh patches deltas instead of rebuilding. The
same shape applies here and matters more, because re-pulling is billed: syncing 50
profiles monthly at 200 posts each is 10,000 paid items, and most months most of
those profiles have added five posts.

The detector for a social account is a limit-1 fetch. It costs one item (about
$0.0002 on LinkedIn, $0.0005 on Instagram) and answers "is the newest post still the
newest post I already have". If yes, the full pull is skipped entirely. Across a
roster of quiet accounts that is a 100-500x saving; across busy ones it costs one
extra item. Both directions are fine.

Deliberately NOT a separate manifest file. living-docs needs YAML because a brief is
a file on disk with no database behind it; here the account row already is the
manifest, so the state lives in columns and cannot drift out of sync with the data.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from . import store

# How long an account is considered fresh without any probe at all. Chosen per
# relationship: a client's own account is worth checking often because you act on it,
# a competitor rarely changes your plans day to day.
DEFAULT_TTL = {
    "own": 6 * 3600,
    "client": 12 * 3600,
    "competitor": 3 * 86400,
    "influencer": 3 * 86400,
}

# Beyond this, a "delta" sync is not really a delta any more: too much has happened
# since the last successful pull to trust a small limit, so widen it.
STALE_BACKFILL_AFTER = 30 * 86400
# An account vendors confirmed has no posts (a brand-new page) is looked at weekly.
EMPTY_RECHECK = 7 * 86400


@dataclass
class Decision:
    account_id: int
    handle: str
    platform: str
    action: str          # skip | probe | sync | backfill
    reason: str
    limit: int | None = None

    def __str__(self) -> str:
        lim = f" limit={self.limit}" if self.limit else ""
        return f"{self.platform}/{self.handle}: {self.action}{lim} ({self.reason})"


def ttl_for(relationship: str) -> int:
    return DEFAULT_TTL.get(relationship, 3 * 86400)


def newest_stored(conn, account_id: int) -> tuple[str | None, int | None]:
    # Collab posts stored under a co-author count: otherwise one sitting at the top
    # of this feed made every probe answer "something new".
    row = conn.execute(
        f"""SELECT platform_item_id, published_at FROM item
            WHERE {store.account_items("")} AND deleted_at IS NULL
            ORDER BY published_at DESC LIMIT 1""",
        (account_id, account_id),
    ).fetchone()
    return (row["platform_item_id"], row["published_at"]) if row else (None, None)


# How far a delta reaches back past the newest post already held. The watermark is
# the vendor's own published_at, not our clock: a post that went up just before a
# sync but reached the vendor's index just after it was older than last_scraped_at
# and so fell outside every later delta for good. LinkedIn dates are also relative
# ("2d"), so they wobble. Re-buying a few days of posts is cents.
DELTA_OVERLAP = 3 * 86400


def delta_since(conn, account) -> int | None:
    """Lower bound for a delta fetch of `account` (a row)."""
    if account["delta_floor"]:
        return account["delta_floor"]
    _, newest_at = newest_stored(conn, account["id"])
    if newest_at:
        return newest_at - DELTA_OVERLAP
    return account["last_scraped_at"]


def plan(conn, *, account_ids=None, force: bool = False,
         default_limit: int = 50) -> list[Decision]:
    """Decide what to do with each account before spending anything."""
    where, args = ["1=1"], []
    if account_ids:
        where.append("id IN (%s)" % ",".join("?" * len(account_ids)))
        args += list(account_ids)
    rows = conn.execute(
        f"SELECT * FROM account WHERE {' AND '.join(where)} ORDER BY platform, handle",
        args,
    ).fetchall()

    now = store.now()
    out = []
    for a in rows:
        newest_id, _ = newest_stored(conn, a["id"])
        age = now - (a["last_scraped_at"] or 0)
        ttl = ttl_for(a["relationship"])

        if force:
            d = Decision(a["id"], a["handle"], a["platform"], "sync",
                         "forced", default_limit)
        elif newest_id is None and a["last_scraped_at"] and age < max(ttl, EMPTY_RECHECK):
            # Checked and confirmed empty: look again weekly, not every night.
            d = Decision(a["id"], a["handle"], a["platform"], "skip",
                         f"no posts yet, checked {age // 3600}h ago "
                         f"(rechecked every {max(ttl, EMPTY_RECHECK) // 86400}d)")
        elif newest_id is None:
            # Never successfully pulled: there is nothing to compare against, so a
            # probe would tell us nothing we do not already know.
            d = Decision(a["id"], a["handle"], a["platform"], "backfill",
                         "no content stored yet", default_limit)
        elif a["delta_floor"]:
            # A probe would answer "nothing new" and leave the hole for good.
            gap = time.strftime("%Y-%m-%d", time.gmtime(a["delta_floor"]))
            d = Decision(a["id"], a["handle"], a["platform"], "sync",
                         f"last delta was cut short; posts since {gap} may be "
                         f"missing", default_limit)
        elif age > STALE_BACKFILL_AFTER:
            d = Decision(a["id"], a["handle"], a["platform"], "backfill",
                         f"last synced {age // 86400}d ago", default_limit)
        elif age < ttl:
            d = Decision(a["id"], a["handle"], a["platform"], "skip",
                         f"fresh, synced {age // 3600}h ago (ttl {ttl // 3600}h)")
        else:
            d = Decision(a["id"], a["handle"], a["platform"], "probe",
                         f"ttl expired ({age // 3600}h)", 1)
        out.append(d)
    return out


def probe_verdict(result, newest_id: str | None) -> bool:
    """True means work to do. Pure: no database, so it runs on a worker thread.

    Errs toward True: if the probe cannot answer confidently (adapter failed, vendor
    returned nothing), the caller should do the full sync rather than silently skip
    an account for another TTL window.
    """
    if newest_id is None or result is None or not result.items:
        return True
    # Pinned posts come first whatever their age (Instagram allows three), and not
    # every route sorts them out. Comparing items[0] meant a pinned post that was
    # also the newest stored one answered "nothing new" forever: last_scraped_at
    # kept moving, so the account never went stale and never synced again.
    live = [i for i in result.items if not (i.metrics or {}).get("is_pinned")]
    if not live:
        return True
    newest = max(live, key=lambda i: i.published_at or 0)
    return str(newest.platform_item_id) != str(newest_id)


# Enough to see past Instagram's maximum of three pinned posts. LinkedIn routes
# return chronological order, so one item answers there.
PROBE_LIMIT = {"instagram": 4, "tiktok": 4}   # both let an account pin 3 posts


def probe_limit(platform: str) -> int:
    return PROBE_LIMIT.get(platform, 1)


def record_probe_metrics(conn, adapter, result) -> bool:
    """Bank the metric movement on the post the probe already paid for.

    Owner thread only. This is the half of the probe that earns its keep: the
    detector itself costs a full actor cold start (measured 4.4-5.1s, ~$0.002)
    to avoid a since-bounded delta that bills $0.001 when it finds nothing, so
    refreshing the top post's counts is most of what you get for the money.
    """
    if result is None or not result.items:
        return False
    top = result.items[0]
    if not top.metrics:
        return False
    row = conn.execute(
        "SELECT id FROM item WHERE platform=? AND platform_item_id=?",
        (adapter.platform, str(top.platform_item_id)),
    ).fetchone()
    if not row:
        return False
    store.record_metrics(
        conn, row["id"],
        **{k: top.metrics.get(k) for k in
           ("reactions", "comments", "shares", "saves", "views")},
        raw={k: v for k, v in top.metrics.items()
             if k not in ("reactions", "comments", "shares", "saves", "views")}
        or None,
        carry=tuple(getattr(adapter, "unmeasured", ())),
    )
    conn.commit()
    return True


def summarize(decisions: list[Decision]) -> str:
    counts: dict[str, int] = {}
    for d in decisions:
        counts[d.action] = counts.get(d.action, 0) + 1
    parts = [f"{v} {k}" for k, v in sorted(counts.items())]
    return ", ".join(parts) if parts else "nothing to do"
