"""Retention for third-party comment data on accounts we only study.

Comments on a competitor's or influencer's post are written by people who never
chose to be in this database. Holding them indefinitely is the part of this tool
hardest to justify under a legitimate-interest basis, and the EDPB's 03/2026
guidance on web scraping (public consultation closing 30 October 2026) puts
minimisation and a defined retention period at the centre of that test. So:

  * comment text on competitor/influencer posts older than N months (default 12)
    is deleted, aged by when it was written, or by when we fetched it if the
    vendor gave no date;
  * the raw vendor payload beside each such comment is blanked sooner (default 3
    months): it is only needed to re-project a field shortly after a fetch, and it
    carries more about the commenter than the projected columns do.

Own and client data is never touched here: it is held on the client's behalf and
their contract governs it, and `forget` already erases it on request. A collab
post shared with an own/client account counts as theirs.

Dry run by default, like every other destructive command.
"""
from __future__ import annotations

import time

STUDIED = ("competitor", "influencer")
MONTH_S = 30.44 * 86400

# Comments on items that belong only to studied accounts: the owner is studied and
# no own/client account shares the item.
_SCOPE = """
  c.item_id IN (
    SELECT i.id FROM item i JOIN account a ON a.id = i.account_id
     WHERE a.relationship IN ('competitor','influencer')
       AND NOT EXISTS (SELECT 1 FROM item_account ia
                         JOIN account a2 ON a2.id = ia.account_id
                        WHERE ia.item_id = i.id
                          AND a2.relationship IN ('own','client')))
"""


def apply(conn, *, months: float = 12, raw_months: float = 3,
          execute: bool = False, now: int | None = None) -> dict:
    """Count (and with execute=True, do) the deletions. Returns the counts."""
    now = int(now or time.time())
    cutoff = int(now - months * MONTH_S)
    raw_cutoff = int(now - min(raw_months, months) * MONTH_S)
    old = f"FROM comment c WHERE {_SCOPE} AND COALESCE(c.published_at, c.fetched_at) < ?"
    raw = (f"FROM comment c WHERE {_SCOPE} AND c.raw_json IS NOT NULL "
           f"AND c.fetched_at < ? AND COALESCE(c.published_at, c.fetched_at) >= ?")
    res = {
        "months": months, "raw_months": min(raw_months, months),
        "delete": conn.execute(f"SELECT COUNT(*) {old}", (cutoff,)).fetchone()[0],
        "blank_raw": conn.execute(f"SELECT COUNT(*) {raw}",
                                  (raw_cutoff, cutoff)).fetchone()[0],
        "kept_private": conn.execute(
            f"SELECT COUNT(*) FROM comment c WHERE NOT ({_SCOPE})").fetchone()[0],
        "execute": execute,
    }
    if execute:
        conn.execute(f"UPDATE comment SET raw_json = NULL WHERE id IN "
                     f"(SELECT c.id {raw})", (raw_cutoff, cutoff))
        conn.execute(f"DELETE FROM comment WHERE id IN (SELECT c.id {old})", (cutoff,))
        conn.commit()
    return res
