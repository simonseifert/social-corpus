"""LinkedIn posts from HarvestAPI's own API, without Apify in between.

The same vendor runs the harvestapi~linkedin-profile-posts actor that
linkedin_posts.py uses, and sells the data directly: pay-as-you-go credits, no
subscription, a $1 trial credit. The posts come back in the same shape the actor
returns, so the projection is linkedin_posts._item, not a second copy of it.

What is different, and better, than the actor:
  * real pagination (page + paginationToken), so a delta walks newest-first and
    STOPS at the first page older than `since` instead of re-buying a window;
  * a credits endpoint, so the cost of a call can be measured, not estimated.

PRICE, MEASURED (not read off the JS-only pricing page, where third-party reports
disagree by 10x): $0.004 per page call, profile-posts and company-posts alike. On
2026-10-22 two pages of a client's profile took the balance from $1.000 to $0.992 and
returned 200 posts (100 per page), and 60/60 ids the Apify actor had stored came
back identical. On 2026-10-23 one ferry company's page returned 86 posts and
moved the balance $0.992 -> $0.988; the response's own `cost` field said 0.004,
which is what fetch() now reports instead of an estimate. A full page is therefore
$0.04 per 1,000 posts, 50x under the actor's $2.00. A short page costs the same
$0.004, which is why estimate() prices pages, not posts.

No maxTotalChargeUsd exists here: MAX_PAGES is the cap (20 pages = $0.08 a fetch at
most), the delta stops paging at the watermark, and spend_check reads the remaining
balance (a free call) before a large run.

The free tier runs ONE request at a time (usage.requestsConcurrency = 1), so a
parallel sync over several accounts gets 429s; _get backs off and retries those.

Docs: https://docs.harvestapi.io/linkedin-api-reference/profile/profile-posts
"""
from __future__ import annotations

import json
import re
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .base import EmptyProfile, TargetGone, Adapter, FetchResult
from .linkedin_posts import _item, _profile, _target

BASE = "https://api.harvestapi.io"
TIMEOUT_S = 60
DEFAULT_LIMIT = 50
# A runaway pager is the failure that costs money here (no maxTotalChargeUsd on
# this API), so the page count is bounded independently of `limit`.
MAX_PAGES = 20
# Measured 2026-10-22/23, see the module docstring.
PAGE_USD = 0.004
PAGE_SIZE = 100
# 429 backoff (free tier: 1 concurrent request). Short on purpose: a sync worker
# waiting on another worker's request needs seconds, not minutes.
RETRY_WAITS_S = (2, 4, 8, 16)


def _get(path: str, params: dict, key: str) -> dict:
    url = f"{BASE}{path}?{urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, '')})}"
    req = urllib.request.Request(url, headers={"X-API-Key": key,
                                               "User-Agent": "corpus-cli/0.1"})
    for wait in (*RETRY_WAITS_S, None):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                data = json.loads(r.read().decode())
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and wait is not None:
                time.sleep(wait)
                continue
            raise RuntimeError(f"harvestapi {e.code} on {path}: "
                               f"{e.read().decode(errors='replace')[:300]}") from e
        except Exception as e:
            raise RuntimeError(f"harvestapi call failed: {type(e).__name__}: {e}") from e
    if not isinstance(data, dict):
        raise RuntimeError(f"harvestapi {path}: expected an object, got {type(data).__name__}")
    if data.get("error"):
        msg = f"harvestapi {path}: {data['error']}"
        if re.search(r"\bnot found\b", str(data["error"]), re.I):
            raise TargetGone(msg)       # "Company not found: <slug>"
        raise RuntimeError(msg)
    return data


def account(key: str) -> dict:
    """The API user behind `key`. Free to call."""
    return _get("/users/my-api-user", {}, key)


def balance(info: dict) -> float | None:
    """Remaining USD balance from an account() payload: usage.balance.

    user.totalBalance sits next to it and is the package size as an integer (1 on
    the $1 trial), not what is left; reading it would report a drained account as
    full."""
    v = (info.get("usage") or {}).get("balance")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def estimate(n_items: int) -> float:
    """Billing is per page call, so a 10-post delta costs a whole page."""
    pages = min(MAX_PAGES, max(1, math.ceil(max(0, n_items) / PAGE_SIZE)))
    return round(pages * PAGE_USD, 6)


def spend_check(keys: dict, planned_items: int, est_usd: float = 0.0) -> None:
    """Refuse a run the remaining balance cannot cover.

    Running dry mid-sync is the failure: pages stop with an error halfway through a
    backfill, and the fall-through then lands the rest on the $2/1k Apify actor."""
    from ..keys import PlanGuardError
    key = (keys or {}).get("HARVESTAPI_KEY")
    if not key:
        return
    try:
        left = balance(account(key))
    except RuntimeError as e:
        raise PlanGuardError(f"could not read the HarvestAPI balance: {e}") from e
    need = max(est_usd, estimate(planned_items))
    if left is not None and left < need:
        raise PlanGuardError(
            f"HarvestAPI balance ${left:.3f} does not cover this run (~${need:.3f}). "
            f"Top up at harvestapi.io, or re-run with a smaller --limit.")


def fetch(target: str, *, mode: str = "delta", keys: dict | None = None,
          limit: int = DEFAULT_LIMIT, since: Any = None, **_) -> FetchResult:
    key = (keys or {}).get("HARVESTAPI_KEY")
    if not key:
        raise RuntimeError("HARVESTAPI_KEY missing; expected in ~/.reach/keys.env")
    url, seg, handle = _target(target)
    path, param = (("/linkedin/profile-posts", "profile") if seg == "in"
                   else ("/linkedin/company-posts", "company"))
    limit = max(1, int(limit))
    floor = int(since) if (mode != "backfill" and since is not None) else None

    records: list[dict] = []
    token = None
    more = False
    pages = 0
    spent = 0.0
    for page in range(1, MAX_PAGES + 1):
        data = _get(path, {param: url, "page": page, "paginationToken": token}, key)
        pages += 1
        # The vendor states what each call cost; trust that over the page estimate.
        c = data.get("cost")
        spent += float(c) if isinstance(c, (int, float)) and not isinstance(c, bool) \
            else PAGE_USD
        got = [r for r in (data.get("elements") or []) if isinstance(r, dict)]
        records += got
        pg = data.get("pagination") or {}
        token = pg.get("paginationToken")
        total = pg.get("totalPages")
        more = bool(got) and (token is not None or (total or 0) > page)
        oldest = min((i.published_at for i in map(_item, got) if i), default=None)
        if len(records) >= limit or not more:
            break
        if floor is not None and oldest is not None and oldest < floor:
            more = False                       # walked past the watermark
            break

    items = [i for i in map(_item, records) if i]
    if floor is not None:
        items = [i for i in items if (i.published_at or 0) >= floor]
    cut = len(items) > limit
    items = items[:limit]
    if not records and floor is None:
        raise EmptyProfile(f"harvestapi returned 0 posts for {url} unfiltered "
                           "(removed/gated page, or a changed response shape)")
    return FetchResult(
        items=items,
        profile=_profile(records, handle=handle, seg=seg),
        cost_usd=round(spent, 6),
        truncated=cut or (more and len(items) >= limit),
    )


ADAPTER = Adapter(
    name="linkedin_harvest",
    platform="linkedin",
    fetch=fetch,
    relationships=("competitor", "influencer", "own", "client"),
    needs=("HARVESTAPI_KEY",),
    cost_per_1k=PAGE_USD * 1000 / PAGE_SIZE,          # $0.04, measured 2026-10-22/23
    notes="harvestapi.io direct API, person and company pages: same payload as the "
          "Apify actor, real pagination, $0.004 per page of up to 100 posts "
          "(measured), no subscription.",
    unmeasured=("views", "saves"),
    estimate_usd=estimate,
    spend_check=spend_check,
)
