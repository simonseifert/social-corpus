#!/usr/bin/env python3
"""Measure HarvestAPI's real price and payload parity with the trial credit.

    .venv/bin/python scripts/harvest_trial.py https://www.linkedin.com/in/someone/ [pages]
    .venv/bin/python scripts/harvest_trial.py https://www.linkedin.com/company/x/ [pages]

Reads HARVESTAPI_KEY from ~/.reach/keys.env. Spends at most `pages` (default 2)
profile-posts calls. Prints credits before/after, cost per call and per post, and
how the parsed posts compare with what corpus already stores for that account.
Writes nothing to the corpus database.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import keys as keys_mod, store  # noqa: E402
from corpus.adapters import linkedin_harvest as H  # noqa: E402


def credits(info: dict):
    """(field name, remaining USD). The balance lives at usage.balance; the old
    top-level guesses (credits, balance, ...) all missed it, so the 2026-10-22 trial
    printed "credits before: None" and the cost had to be read off the dashboard."""
    return "usage.balance", H.balance(info)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    url, pages = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 2
    k = keys_mod.load()
    if not k.get("HARVESTAPI_KEY"):
        print("HARVESTAPI_KEY is not set in ~/.reach/keys.env")
        return 2

    before = H.account(k["HARVESTAPI_KEY"])
    field, c0 = credits(before)
    print("account fields:", sorted(before),         # names only, never values
          "usage:", sorted(before.get("usage") or {}))
    print(f"credits before: {c0} ({field})")

    H.MAX_PAGES = pages
    res = H.fetch(url, mode="backfill", keys=k, limit=10_000)
    after = H.account(k["HARVESTAPI_KEY"])
    _, c1 = credits(after)
    print(f"credits after:  {c1}")
    n = len(res.items)
    print(f"posts parsed: {n} over up to {pages} page(s); truncated={res.truncated}")
    if c0 is not None and c1 is not None:
        used = c0 - c1
        print(f"credits used: {used:.4f}  -> {used / max(n, 1):.6f} per post, "
              f"${used / max(n, 1) * 1000:.3f} per 1k (vendor-reported: ${res.cost_usd})")

    with_metrics = sum(1 for i in res.items if i.metrics and i.metrics.get("reactions") is not None)
    with_media = sum(1 for i in res.items if i.media)
    print(f"with reaction counts: {with_metrics}/{n}, with media: {with_media}/{n}")

    _, _, handle = H._target(url)
    db = Path(os.environ.get("CORPUS_DB") or "~/.corpus/corpus.db").expanduser()
    if not db.exists():
        return 0
    conn = store.connect(db)
    row = conn.execute("SELECT id FROM account WHERE platform='linkedin' AND handle=? "
                       "COLLATE NOCASE", (handle,)).fetchone()
    if row:
        have = {r[0] for r in conn.execute(
            "SELECT platform_item_id FROM item WHERE account_id=?", (row["id"],))}
        got = {i.platform_item_id for i in res.items}
        print(f"stored for {handle}: {len(have)}; overlap with this fetch: {len(have & got)}"
              f" (same ids = drop-in replacement for the actor)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
