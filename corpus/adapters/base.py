"""The adapter contract.

An adapter is a plain function plus a small descriptor. No framework, no base class
to inherit, no registry magic -- adding a platform is one file and one line in
adapters/__init__.py. This is deliberately the same shape signal's collectors use,
because it survived nine heterogeneous sources without growing an abstraction layer.

An adapter's only job is: talk to one vendor, yield RawItem. It never touches the
database, never decides what is new, never writes media to disk. That keeps every
adapter independently testable against a recorded fixture.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Sequence

# ── what an adapter yields ──────────────────────────────────────────────────


class TargetGone(RuntimeError):
    """The vendor answered definitively that the profile does not exist, is private
    or is gated. No other route will find it either, so the sync stops here instead
    of falling through: a nonexistent handle went on to Apify, Bright Data (two
    billed error records) and the throttled free Instagram route (2026-10-23)."""


class EmptyProfile(RuntimeError):
    """The vendor reached the profile and found no posts on it. One vendor saying so
    could also be a changed response shape, so on its own this is still a failure;
    sync treats it as a confirmed empty account only when a second, independent
    vendor agrees (a brand-new LinkedIn page that has never posted kept the nightly
    red for three nights, 2026-10-23..25)."""


@dataclass
class RawMedia:
    role: str                       # original | poster | keyframe | slide | thumbnail
    remote_url: str | None = None
    seq: int = 0
    mime: str | None = None
    width: int | None = None
    height: int | None = None
    t_offset_s: float | None = None


@dataclass
class RawItem:
    """One piece of content, vendor-normalised but not yet stored.

    `raw` is mandatory and must be the vendor payload as received. Everything else
    is a best-effort projection; when a projection turns out wrong, raw is what
    lets you rebuild without re-paying for the fetch.
    """
    platform_item_id: str
    kind: str                       # text|image|carousel|short_video|long_video|article|comment
    published_at: int               # unix seconds UTC
    raw: dict[str, Any]

    title: str | None = None
    body: str | None = None
    transcript: str | None = None
    url: str | None = None
    lang: str | None = None
    duration_s: float | None = None
    is_repost: bool = False
    # Who actually wrote it. Differs from the account on a repost, and the
    # difference is the whole point: a repost is amplification, not authorship.
    author_name: str | None = None
    author_handle: str | None = None

    # reactions | comments | shares | saves | views, plus anything vendor-specific
    # under "extra" which lands in metric_snapshot.metrics_raw
    metrics: dict[str, Any] = field(default_factory=dict)
    media: list[RawMedia] = field(default_factory=list)


@dataclass
class Profile:
    """Account-level facts an adapter may discover while fetching."""
    handle: str
    display_name: str | None = None
    bio: str | None = None
    url: str | None = None
    platform_uid: str | None = None
    follower_count: int | None = None
    following_count: int | None = None
    posts_total: int | None = None
    raw: dict[str, Any] | None = None


@dataclass
class FetchResult:
    items: list[RawItem] = field(default_factory=list)
    profile: Profile | None = None
    cost_usd: float | None = None
    # Set when the vendor indicated more data exists past `limit`. Surfaced so a
    # truncated backfill is visible instead of looking like a complete one.
    truncated: bool = False
    # The vendor confirmed the account exists and has no posts (archived, brand new).
    # A success with nothing to store, not a route failure to fall through from.
    empty_confirmed: bool = False
    # Records the vendor returned (and billed) before parsing. None when the route
    # has no such notion. len(items) well below this means records were dropped
    # as unparseable: a vendor payload change that would otherwise land as a
    # quietly short, fully billed run. sync.record fails the run on it.
    raw_count: int | None = None


@dataclass
class Adapter:
    name: str                       # linkedin_posts | instagram_public | ...
    platform: str                   # linkedin | instagram
    fetch: Callable[..., FetchResult]
    # Which account relationships this adapter is allowed to serve. Consented
    # client routes and public competitor routes are deliberately different
    # adapters even on the same platform, so the two data classes never blur.
    relationships: Sequence[str]
    needs: Sequence[str] = ()       # required env/key names, for preflight
    cost_per_1k: float | None = None
    # A route that cannot reach the end of an account no matter what `limit` says.
    # Ordering by price alone assumes every route would eventually return the same
    # data, and a free-but-capped route breaks that: it would sort to the front and
    # quietly turn every backfill into a top-up. Cost ranks these last instead.
    shallow: bool = False
    notes: str = ""
    # The Apify actor this route bills, so the spend guard can read its live price
    # card. Empty for routes that never call Apify.
    apify_actor: str = ""
    # Metrics this route can NEVER see (the LinkedIn actor has no views at all). A
    # None from such a field is "not measured", so the item keeps its last reading;
    # a None from any other field is "measured as absent" (an owner hiding likes)
    # and blanks it. That split is what record_metrics' `carry` implements.
    unmeasured: Sequence[str] = ()
    # An environment variable that must be "1" before routing may pick this
    # adapter at all. For routes whose risk is not money but someone's account:
    # see instagram_client. Empty means always routable.
    opt_in_env: str = ""

    def opted_in(self) -> bool:
        import os
        return not self.opt_in_env or os.environ.get(self.opt_in_env) == "1"
    # Records a month the vendor gives away before cost_per_1k applies (Bright Data:
    # 5,000, shared across every scraper on the account). cost_per_1k stays the
    # MARGINAL price on purpose: corpus cannot see how much of the allowance is left
    # without a call, and a route priced at $0 because of a quota it may already have
    # spent would win every sort and then bill at full price. The router uses it as
    # a tie-break only; see adapters.for_platform.
    free_monthly_records: int = 0
    # What fetching n items costs, for dry runs and the spend guard. None means
    # n * cost_per_1k / 1000, which is right for per-item billing and 10x wrong for
    # per-page billing: HarvestAPI charges $0.004 for a page of up to 100 posts, so
    # a 10-post delta costs $0.004, not $0.0004.
    estimate_usd: Callable[[int], float] | None = None
    # Vendor-side pre-run check for routes the Apify guard cannot see (no
    # maxTotalChargeUsd to lean on). Called as spend_check(keys, planned_items,
    # est_usd) before a large run; raises keys.PlanGuardError to refuse.
    spend_check: Callable[..., None] | None = None

    def estimate(self, n_items: int) -> float:
        if self.estimate_usd is not None:
            return self.estimate_usd(n_items)
        return (self.cost_per_1k or 0.0) * max(0, n_items) / 1000


# ── helpers shared by adapters ──────────────────────────────────────────────

def stable_id(*parts: Any) -> str:
    """Deterministic id for vendors that do not expose a stable one.

    Prefer the vendor's own id whenever it exists; this is the fallback, and it
    must be derived only from fields that cannot change, or re-syncing will
    duplicate rows instead of updating them.
    """
    blob = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def classify_kind(*, has_video: bool, media_count: int, duration_s: float | None,
                  has_title: bool = False, long_video_threshold: float = 180.0) -> str:
    """Map vendor media shape onto the canonical `kind` vocabulary.

    The long/short video split is by duration, not by platform label: a 4-minute
    Reel and a 4-minute Short behave like long video regardless of what the
    platform calls them.
    """
    if has_video:
        if duration_s is not None and duration_s >= long_video_threshold:
            return "long_video"
        return "short_video"
    if media_count > 1:
        return "carousel"
    if media_count == 1:
        return "image"
    return "article" if has_title else "text"


def iter_batches(seq: Sequence[Any], n: int) -> Iterator[Sequence[Any]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
