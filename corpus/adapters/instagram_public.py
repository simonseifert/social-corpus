"""Instagram back-catalog for public profiles, cookie-free, through Apify.

Route: apidojo/instagram-scraper-api, picked over the first-party
apify/instagram-post-scraper on two measured grounds. Price, $0.50 vs $2.30 per 1k
posts against real BRONZE-tier billing rather than the price card. And its `owner{}`
block carries followerCount/followingCount/postCount on every row, so
FetchResult.profile fills for free instead of costing a second billed call.

What that choice gives up: on a carousel, apidojo returns only a poster JPG for the
video slides, never the MP4 (confirmed on a real carousel, whose slides 1 and 5 are
type 2 and both .jpg). apify/instagram-post-scraper is the only actor that emits
`childPosts[].videoUrl`, so that is the fallback if a carousel's video slides ever
need transcribing. Reels are unaffected -- their MP4 comes through fine.

MEDIA URLS ARE A CACHE, NOT A REFERENCE. Instagram signs them and they expire (video
~34h, images ~106h, measured across 157 URLs). They are stored anyway so an
enrichment pass inside the window can skip a lookup, but a dead URL is not lost data:
re-fetching the item through this adapter re-mints a full window even for a post from
2019. Re-resolving means re-fetching, never patching the query string -- tampering
with the `oh`/`oe` params returns 403.

This is the PUBLIC route. It sees only what a logged-out visitor sees, which is what
keeps competitor and influencer data separable from consented client exports.
"""
from __future__ import annotations

import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import apify
from .base import (Adapter, FetchResult, Profile, RawItem, RawMedia, classify_kind,
                   stable_id)

ACTOR = "apidojo~instagram-scraper-api"

# The server abandons the sync wait at 300s. A client timeout below that kills the
# connection while the run keeps going and keeps billing, so sit just above it.
TIMEOUT_S = 310

DEFAULT_LIMIT = 100

# Billing verified against /v2/actor-runs, not the price card: 3 items -> $0.005,
# 9 -> $0.005, 50 -> $0.024. One "user-query" event per startUrl plus a per-item
# charge with the first ten items of each query free.
FIXED_QUERY_USD = 0.005
PER_ITEM_USD = 0.0005
FREE_ITEMS_PER_QUERY = 10


def _handle(target: str) -> str:
    """Accept a handle, an @handle or a profile URL. One target per fetch."""
    t = (target or "").strip().strip("/")
    if "instagram.com/" in t:
        t = t.split("instagram.com/", 1)[1]
    t = t.split("/")[0].split("?")[0].lstrip("@")
    if not t:
        raise ValueError(f"no Instagram handle in {target!r}")
    return t


def _epoch(value: Any) -> int:
    """Vendor timestamp to unix seconds UTC.

    apidojo emits ISO8601 with a Z suffix today. The numeric branches are here
    because a vendor switching to epoch ms is the kind of change that would
    otherwise write timestamps 1000x in the future without anything failing.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f"unusable timestamp {value!r}")
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        n = float(value)
        return int(n / 1000) if n > 1e11 else int(n)
    dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _since_date(since: Any) -> str:
    """Delta bound as YYYY-MM-DD, stepped back one day.

    The actor's `until` is named like an end bound but filters to posts NEWER than
    the date, and it is day-granular with undocumented boundary semantics. One day
    of overlap costs a handful of re-fetched rows, which cost nothing to persist
    (upsert is idempotent, metrics are change-only), and removes the chance of
    silently dropping a day at the seam.
    """
    ts = _epoch(since)
    return (datetime.fromtimestamp(ts, timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def _count(value: Any) -> int | None:
    """Instagram reports -1 for counts the owner has hidden. That is not a zero and
    it is not a number; storing it would poison every average computed later."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value >= 0 else None


def _estimate_cost(n_items: int) -> float:
    billable = max(0, n_items - FREE_ITEMS_PER_QUERY)
    return round(FIXED_QUERY_USD + billable * PER_ITEM_USD, 6)


def _media(row: dict) -> list[RawMedia]:
    video = row.get("video") or {}
    image = row.get("image") or {}
    slides = row.get("carouselMedia") or []
    out: list[RawMedia] = []

    if slides:
        # Carousel children are media rows, never items. The top-level `image` on a
        # carousel is a downscaled copy of slide 0, so it is dropped rather than
        # stored twice. type 2 slides are video but carry a poster JPG only, hence
        # the image mime on every slide.
        for i, s in enumerate(slides):
            if s.get("url"):
                out.append(RawMedia(role="slide", seq=i, remote_url=s["url"],
                                    mime="image/jpeg", width=s.get("width"),
                                    height=s.get("height")))
        return out

    if video.get("url"):
        out.append(RawMedia(role="original", remote_url=video["url"], mime="video/mp4",
                            width=video.get("width"), height=video.get("height")))
    if image.get("url"):
        role = "poster" if out else "original"
        out.append(RawMedia(role=role, remote_url=image["url"], mime="image/jpeg",
                            width=image.get("width"), height=image.get("height")))
    return out


def _metrics(row: dict) -> dict[str, Any]:
    video = row.get("video") or {}
    m: dict[str, Any] = {"reactions": _count(row.get("likeCount")),
                         "comments": _count(row.get("commentCount"))}

    # playCount is the inclusive counter (every start, including repeats); viewCount
    # counts unique accounts and runs well below it. Keep the inclusive one as
    # `views` and echo viewCount whenever it is present, including when it is the
    # only counter that arrived: otherwise a `views` sourced from the unique counter
    # is indistinguishable from one sourced from the inclusive one, and a series that
    # switches between them looks like a traffic collapse.
    play, view = _count(video.get("playCount")), _count(video.get("viewCount"))
    if play is not None or view is not None:
        m["views"] = play if play is not None else view
        if view is not None:
            m["video_view_count"] = view

    for src, dst in (("isPaidPartnership", "is_paid_partnership"),
                     ("isPinned", "is_pinned"),
                     ("isLikeAndViewCountsDisabled", "counts_hidden")):
        if row.get(src) is not None:
            m[dst] = row[src]
    return {k: v for k, v in m.items() if v is not None}


def _item(row: dict) -> RawItem:
    slides = row.get("carouselMedia") or []
    is_carousel = bool(row.get("isCarousel")) or bool(slides)
    # A carousel is a carousel even when a child is video, so isVideo is deliberately
    # not consulted once the carousel flag is up. The floor of 2 covers a payload that
    # sets the flag but returns fewer children than the post has: any count below 2
    # would file the post as a plain image.
    n_media = max(len(slides), 2) if is_carousel else 1
    video = row.get("video") or {}
    # Coerced once. classify_kind compares this against a float, so handing it the
    # vendor value raw means a type change on one field raises there and takes the
    # whole paid run with it instead of costing one field.
    duration = video.get("duration")
    duration = (float(duration) if isinstance(duration, (int, float))
                and not isinstance(duration, bool) else None)

    code = row.get("code")
    # The IG media pk is stable across fetches, so it is the dedup key. stable_id is
    # only reached if both the pk and the shortcode vanish from the payload.
    pid = row.get("id") or code or stable_id(row.get("url"), row.get("createdAt"))

    return RawItem(
        platform_item_id=str(pid),
        kind=classify_kind(has_video=bool(row.get("isVideo")) and not is_carousel,
                           media_count=n_media, duration_s=duration),
        published_at=_epoch(row.get("createdAt")),
        raw=row,
        body=row.get("caption") or None,
        url=row.get("url") or (f"https://www.instagram.com/p/{code}/" if code else None),
        duration_s=duration,
        is_repost=False,          # IG has no native repost; coowners[] is a collab
        metrics=_metrics(row),
        media=_media(row),
    )


def _profile(rows: list[dict], handle: str) -> Profile | None:
    """Owner block of a row that actually belongs to `handle`.

    The match is not paranoia. On a collab post the owner is whoever published it,
    which can be the other account, and persist() upserts Profile.handle as an
    account row -- so taking the first owner blindly would write a stranger's
    follower count under a new account and silently branch the corpus.

    Among matching rows the counted block wins. apidojo trims owner{} down to
    id/username/fullName on some payloads, and persist() appends an account metric
    snapshot unconditionally, so a thin block picked over a full one writes
    followers=NULL into the time series -- which reads as a collapse to zero, not as
    "not measured".
    """
    owner = None
    for row in rows:
        o = row.get("owner") or {}
        if str(o.get("username", "")).lower() != handle.lower():
            continue
        owner = owner or o
        if o.get("followerCount") is not None:
            owner = o
            break
    if owner is None:
        return None
    return Profile(
        handle=owner["username"],
        display_name=owner.get("fullName"),
        url=f"https://www.instagram.com/{owner['username']}/",
        platform_uid=str(owner["id"]) if owner.get("id") else None,
        follower_count=_count(owner.get("followerCount")),
        following_count=_count(owner.get("followingCount")),
        posts_total=_count(owner.get("postCount")),
        raw=owner,
    )


def _call(body: dict, token: str, *, charge_cap: float) -> list[dict]:
    return apify.call(ACTOR, body, token, charge_cap=charge_cap, timeout=TIMEOUT_S,
                      too_slow="lower `limit`")


def fetch(target: str, *, mode: str = "delta", keys: dict | None = None,
          limit: int = DEFAULT_LIMIT, since: Any = None, **kw) -> FetchResult:
    """Posts and reels from one public profile, newest first.

    mode: 'backfill' walks as far back as `limit` allows; anything else applies
    `since` (unix seconds or a date string) as a lower bound on published_at.
    """
    token = (keys or {}).get("APIFY_TOKEN")
    if not token:
        raise RuntimeError("APIFY_TOKEN missing; expected in ~/.reach/keys.env")

    handle = _handle(target)
    limit = max(1, int(limit))
    body: dict[str, Any] = {"startUrls": [f"https://www.instagram.com/{handle}/"],
                            "maxItems": limit}
    if mode != "backfill" and since is not None:
        body["until"] = _since_date(since)

    rows = _call(body, token, charge_cap=round(_estimate_cost(limit) * 2 + 0.01, 4))

    if not rows and "until" not in body:
        # An unfiltered pull of a live profile is never legitimately empty. Empty here
        # means a renamed input key, a private/removed account, or a block -- all of
        # which must be loud, because a run recorded as ok with 0 items reads as
        # "this profile posts nothing".
        raise RuntimeError(f"{ACTOR} returned 0 rows for @{handle} unfiltered "
                           "(input key drift, private/removed account, or blocked)")

    # Per row: a record this cannot parse (no usable timestamp) is dropped and
    # counted, not allowed to raise and throw away the rest of a paid run.
    items = []
    for r in rows:
        try:
            items.append(_item(r))
        except (ValueError, TypeError, KeyError):
            continue
    profile = _profile(rows, handle)
    total = profile.posts_total if profile else None
    # Only claim completeness when the vendor's own post count says we reached it.
    truncated = len(items) >= limit and (total is None or total > len(items))

    return FetchResult(items=items, profile=profile,
                       cost_usd=_estimate_cost(len(rows)), truncated=truncated,
                       raw_count=len(rows))


ADAPTER = Adapter(
    name="instagram_public",
    platform="instagram",
    fetch=fetch,
    relationships=("competitor", "influencer", "own", "client"),
    needs=("APIFY_TOKEN",),
    cost_per_1k=0.50,
    notes=f"apify:{ACTOR}; public profile posts+reels. No shares/saves from any IG "
          "actor, and carousel video slides come back as poster JPGs only.",
    apify_actor=ACTOR,
    unmeasured=("shares", "saves"),
)
