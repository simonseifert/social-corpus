"""Instagram, free route: the logged-out web client's own profile endpoint.

    GET https://www.instagram.com/api/v1/users/web_profile_info/?username=<h>
    Header: x-ig-app-id: 936619743392459

No cookie, no token, no vendor, no bill. The app-id header is the whole trick:
without it Instagram answers a request for JSON with the login page at HTTP 200.

THIS IS A TOP-UP AND A PREVIEW, NOT A BACK-CATALOGUE ROUTE. The endpoint returns
the newest TWELVE entries and nothing else. There is no cursor here that works
logged out: `page_info.end_cursor` comes back populated and every unauthenticated
way to spend it is dead (query_hash -> 400, doc_id -> 403, both verified against
the live endpoint on 2026-08-06). Twelve is the ceiling per call and re-calling
returns the same twelve. So this route serves three jobs and no others:

  * a zero-cost preview of an account nobody has paid to scrape yet
  * a free daily delta once a back-catalogue exists, which is the common case
  * a free freshness probe, replacing the $0.0005 limit-1 paid probe outright

Backfilling a history still costs money and still belongs to instagram_public.
`truncated` is True on any fetch that did not reach the end of the account, which
against a real profile is nearly always, and that flag is the honest signal a
caller must not ignore.

A DEEPER FREE ROUTE DOES EXIST, AND IS DELIBERATELY NOT THIS MODULE. The sibling
private endpoint /api/v1/feed/user/<user_id>/?count=33&max_id=<cursor> takes the
same app-id header, needs no cookie either, and walked 190 posts back to 2018 in
16 pages during the same probe. It is not wired up here because throughput, not
capability, is the constraint: that walk is 16 requests against a limit that
starts blocking around 40, and the block costs tens of minutes for the WHOLE
platform including this endpoint. Backfilling five profiles that way would spend
one IP block per roster and leave the daily deltas dead behind it, whereas the
paid route backfills the same five for about a cent. Turning it on means owning a
cross-run request budget with persistent state, which is a scheduler feature, not
an adapter one. Recorded here so the next person does not have to re-probe it.

WHAT IT RETURNS, MEASURED AGAINST THE PAID ROUTE
-----------------------------------------------
Checked item-by-item against 12 apidojo-sourced rows already in the corpus:
ids 12/12 identical, timestamps 12/12 identical, likes 12/12 identical, comments
11/12 (the one diff is drift since the paid pull). So the two routes dedup against
each other cleanly and a free top-up updates paid rows instead of duplicating them.

It is strictly BETTER than apidojo in two places. Carousel video slides carry a
real `video_url` MP4 here; apidojo returns only a poster JPG for them, which is
the documented reason a carousel's video slides could never be transcribed. And
video duration is recoverable from the DASH manifest, agreeing with the paid
actor's own duration to within 20ms across 8 reels.

It is worse in one place that matters and one that does not. No comment bodies
(the paid route has none either). And `video_view_count` is NOT playCount, which
is the trap below.

THE VIEW COUNTER IS A DIFFERENT COUNTER AND MUST NOT BE CALLED `views`
----------------------------------------------------------------------
Measured on the same 8 reels, this endpoint's `video_view_count` against the paid
route's `playCount`: 1398/4430, 50/1725, 28/4009, 1284/4564, 952/3486, 2123/5258,
655/4775, 709/30981. Between 1.3x and 43x lower, with no stable ratio, so it is
not a rescaling of the same number. Whatever it counts, writing it into `views`
would make any profile that switched routes show its reach collapsing by up to
97% on a day nothing happened. It therefore rides in metrics_raw under its own
name and `views` stays ABSENT. An absent metric reads as "not measured"; a wrong
one reads as a catastrophe that never occurred.

KNOWN SEAM WHEN TOPPING UP A PAID CORPUS. Because `views` is absent rather than
zero, store.record_metrics writes NULL into the item.m_views CACHE on every free
pass over a video the paid route had measured (it sets the cache columns
unconditionally, by design, so that a metric the owner hides stops reporting a
stale number). The metric_snapshot HISTORY is untouched and still holds the paid
reading, so nothing is lost, but anything reading item.m_views between a free
top-up and the next paid sync sees NULL. That is a store-level property of any
route measuring a subset of the metric vocabulary, not something an adapter can
fix from its side, and it is pinned by a test here so it cannot regress quietly.

THE RATE LIMIT IS THE REAL CONSTRAINT, NOT THE COST
---------------------------------------------------
Measured 2026-08-06 on a residential IP: 74 consecutive calls in 85 seconds
earned `401 {"require_login": true, "message": "Please wait a few minutes..."}`.
That message is false. The block did not lift in 45 minutes of polling, it is
per-IP rather than per-endpoint (the sibling feed endpoint went down with it while
a LinkedIn control request stayed 200 throughout), and there is no fallback: the
HTML profile page and /embed/captioned/ both return 200 empty SPA shells.

Which is why a 401 here raises instead of retrying. Retrying a soft block is how
it becomes a long one, and a sync that stalls for an unknown number of minutes
inside a roster loop is worse than one account erroring and the rest finishing.
The pacing floor below is the actual defence.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .base import Adapter, FetchResult, Profile, RawItem, RawMedia, classify_kind

_IG = "https://www.instagram.com"
ENDPOINT = _IG + "/api/v1/users/web_profile_info/"

# instagram.com's own web client app id. Public, hardcoded in their JS bundle, and
# not a credential: it identifies the surface, it does not authenticate anyone.
# This is why `needs` is empty and no key ever reaches this module.
APP_ID = "936619743392459"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")

TIMEOUT_S = 30

# The endpoint's own page size. Not a parameter: `count`/`first` are ignored here,
# twelve is what it returns. Kept named so the ceiling is greppable.
PAGE_SIZE = 12
DEFAULT_LIMIT = PAGE_SIZE

# Minimum spacing between requests from this process. 74 calls in 85s (0.87/s)
# tripped the block; one call per profile per sync at 2s apart is roughly 1/40th
# of that rate, which leaves headroom for a roster loop of any realistic size.
MIN_INTERVAL_S = 2.0

# Transport-level retries only. A 401 is deliberately NOT in this budget.
TRANSIENT_ATTEMPTS = 3
BACKOFF_BASE_S = 2.0

_last_request_at = 0.0

_DUR_RE = re.compile(
    r"^P(?:\d+D)?T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?$")
_MPD_DUR_RE = re.compile(r'mediaPresentationDuration="([^"]+)"')


class Throttled(RuntimeError):
    """Instagram is refusing this IP.

    Kept distinct from a transport error because the two need different responses:
    this one means "stop calling Instagram for a while", a URLError means "retry".
    """


# ── normalisation ───────────────────────────────────────────────────────────

def _handle(target: str) -> str:
    """Accept a handle, an @handle or a profile URL. One target per fetch."""
    t = (target or "").strip().strip("/")
    if "instagram.com/" in t:
        t = t.split("instagram.com/", 1)[1]
    t = t.split("/")[0].split("?")[0].lstrip("@")
    if not t:
        raise ValueError(f"no Instagram handle in {target!r}")
    return t


def _count(value: Any) -> int | None:
    """Instagram reports -1 for counts the owner has hidden. That is not a zero and
    it is not a number; storing it would poison every average computed later."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value >= 0 else None


def _iso_duration(text: str) -> float | None:
    """xs:duration ("PT24.858427S", "PT3M48S") to seconds.

    Anchored and fully parsed rather than regex-scraped for a number, because a
    form this does not understand must return None and let the item file as an
    unknown-length video. Guessing 3 from "PT3M48S" would file a 3m48s piece as a
    3-second one and put it in the wrong half of the short/long split.
    """
    m = _DUR_RE.match((text or "").strip())
    if not m or not any(m.groups()):
        return None
    h, mi, s = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + mi * 60 + s


def _duration(node: dict) -> float | None:
    """Video length, from the DASH manifest the payload already carries.

    There is no `video_duration` field on this surface at all, so without this
    every reel would file as short_video regardless of length. The manifest's
    mediaPresentationDuration is the same number the paid actor bills for: across
    8 reels it agreed with apidojo's own duration to within 20ms.
    """
    manifest = (node.get("dash_info") or {}).get("video_dash_manifest")
    if not isinstance(manifest, str):
        return None
    m = _MPD_DUR_RE.search(manifest)
    return _iso_duration(m.group(1)) if m else None


def _children(node: dict) -> list[dict]:
    edges = (node.get("edge_sidecar_to_children") or {}).get("edges") or []
    return [e["node"] for e in edges if isinstance(e, dict) and isinstance(
        e.get("node"), dict)]


def _caption(node: dict) -> str | None:
    edges = (node.get("edge_media_to_caption") or {}).get("edges") or []
    for e in edges:
        text = ((e or {}).get("node") or {}).get("text")
        if text:
            return text
    return None


def _media(node: dict) -> list[RawMedia]:
    kids = _children(node)
    out: list[RawMedia] = []

    if kids:
        # One row per slide, same shape instagram_public emits, so switching routes
        # rewrites the same rows instead of doubling them. The difference is the
        # mime: a video slide here has a real MP4, which is the one thing apidojo
        # cannot give and the reason a carousel's video slides were untranscribable.
        for i, k in enumerate(kids):
            dims = k.get("dimensions") or {}
            url = k.get("video_url") if k.get("is_video") else None
            mime = "video/mp4" if url else "image/jpeg"
            url = url or k.get("display_url")
            if url:
                out.append(RawMedia(role="slide", seq=i, remote_url=url, mime=mime,
                                    width=dims.get("width"),
                                    height=dims.get("height")))
        return out

    dims = node.get("dimensions") or {}
    if node.get("is_video") and node.get("video_url"):
        out.append(RawMedia(role="original", remote_url=node["video_url"],
                            mime="video/mp4", width=dims.get("width"),
                            height=dims.get("height")))
    if node.get("display_url"):
        # enrich picks the video by role='original', so the still may only claim
        # that role when there is no video above it.
        out.append(RawMedia(role="poster" if out else "original",
                            remote_url=node["display_url"], mime="image/jpeg",
                            width=None if out else dims.get("width"),
                            height=None if out else dims.get("height")))
    return out


def _metrics(node: dict) -> dict[str, Any]:
    """Counts only. This surface carries no reactor list and no comment bodies.

    `views` is deliberately never set. See the module docstring: this endpoint's
    video_view_count is a different counter from the paid route's playCount, and
    mixing them into one series manufactures a traffic collapse.
    """
    likes = node.get("edge_media_preview_like") or node.get("edge_liked_by") or {}
    m: dict[str, Any] = {
        "reactions": _count(likes.get("count")),
        "comments": _count((node.get("edge_media_to_comment") or {}).get("count")),
    }

    vvc = _count(node.get("video_view_count"))
    if vvc is not None:
        m["video_view_count"] = vvc

    # pinned_for_users maps 12/12 onto apidojo's isPinned on the same posts, so the
    # two routes agree on this flag rather than each inventing a spelling.
    if "pinned_for_users" in node:
        m["is_pinned"] = bool(node.get("pinned_for_users"))
    if node.get("like_and_view_counts_disabled") is not None:
        m["counts_hidden"] = bool(node["like_and_view_counts_disabled"])
    if node.get("coauthor_producers"):
        m["coauthor_count"] = len(node["coauthor_producers"])
    return {k: v for k, v in m.items() if v is not None}


def _item(node: dict) -> RawItem:
    kids = _children(node)
    # __typename is the authority and is_video the fallback: a GraphSidecar whose
    # children did not come through is still a carousel, and the floor of 2 stops
    # it filing as a plain image.
    is_carousel = node.get("__typename") == "GraphSidecar" or bool(kids)
    media_count = max(len(kids), 2) if is_carousel else 1
    duration = None if is_carousel else _duration(node)

    code = node.get("shortcode")
    # The IG media pk, byte-identical to the id apidojo returns for the same post
    # (verified 12/12 against rows the paid route already wrote). Keeping it bare
    # is what makes a free top-up update those rows instead of duplicating them.
    pid = node.get("id") or code

    return RawItem(
        platform_item_id=str(pid),
        kind=classify_kind(has_video=bool(node.get("is_video")) and not is_carousel,
                           media_count=media_count, duration_s=duration),
        # Already unix seconds UTC on this surface, unlike the ISO string apidojo
        # sends. Verified 12/12 equal to the timestamps the paid route stored.
        published_at=int(node["taken_at_timestamp"]),
        raw=node,
        body=_caption(node),
        url=f"https://www.instagram.com/p/{code}/" if code else None,
        duration_s=duration,
        is_repost=False,          # IG has no native repost; coauthors are a collab
        metrics=_metrics(node),
        media=_media(node),
    )


def _profile(user: dict, handle: str) -> Profile | None:
    """The account block, which this route returns in full and for free.

    Returns None when the payload is for a different username than the one asked
    for, which happens on a rename or a redirect. persist() upserts Profile.handle
    as an account row, so writing the new name here would branch the corpus into
    two accounts while the items kept landing under the old one.
    """
    name = str(user.get("username") or "")
    if name.lower() != handle.lower():
        return None

    tl = user.get("edge_owner_to_timeline_media") or {}
    # The timeline edges are dropped from raw on purpose: they are already stored
    # as items, and account.raw_json is rewritten on every sync, so carrying twelve
    # full posts in it would rewrite ~250KB per account per run for a second copy.
    raw = {k: v for k, v in user.items()
           if k not in ("edge_owner_to_timeline_media", "edge_felix_video_timeline")}

    return Profile(
        handle=name,
        display_name=user.get("full_name") or None,
        bio=user.get("biography") or None,
        url=f"https://www.instagram.com/{name}/",
        platform_uid=str(user["id"]) if user.get("id") else None,
        follower_count=_count((user.get("edge_followed_by") or {}).get("count")),
        following_count=_count((user.get("edge_follow") or {}).get("count")),
        posts_total=_count(tl.get("count")),
        raw=raw,
    )


# ── transport ───────────────────────────────────────────────────────────────

def _pace() -> None:
    """Hold the floor between requests. Module-level because the limit is per-IP.

    One fetch is one request, so spacing cannot live inside a paging loop the way
    instagram_client's does; it has to survive across calls in a roster loop.
    """
    global _last_request_at
    wait = MIN_INTERVAL_S - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def _get(handle: str) -> dict:
    """One paced GET, with backoff on transport failures only."""
    url = f"{ENDPOINT}?username={urllib.parse.quote(handle)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "x-ig-app-id": APP_ID,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": _IG + "/",
    })

    last: Exception | None = None
    for attempt in range(TRANSIENT_ATTEMPTS):
        _pace()
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                body = r.read().decode("utf-8", "replace")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise RuntimeError(
                    f"expected a JSON object, got {type(payload).__name__}")
            return payload
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            if e.code in (401, 429):
                # Measured: this does not clear in "a few minutes", it is per-IP
                # and it takes every Instagram endpoint down with it. Retrying is
                # what extends it, so the run ends here and the caller records it.
                raise Throttled(
                    f"instagram is throttling this IP ({e.code}): {detail}. "
                    "Measured recovery is tens of minutes, not the few it claims, "
                    "and it applies to every instagram.com endpoint. Back off the "
                    "whole platform, do not retry this account."
                ) from e
            if e.code == 404:
                raise RuntimeError(f"no instagram profile @{handle} (404)") from e
            if e.code < 500:
                raise RuntimeError(f"instagram {e.code} on @{handle}: {detail}") from e
            last = e
        except json.JSONDecodeError as e:
            # A 200 that is not JSON is the login page or an SPA shell, which is a
            # block wearing a success code. Retrying costs nothing and sometimes
            # clears, so it stays in the transient budget.
            last = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e

        if attempt + 1 < TRANSIENT_ATTEMPTS:
            time.sleep(BACKOFF_BASE_S * (2 ** attempt))

    raise RuntimeError(
        f"instagram unreachable for @{handle} after {TRANSIENT_ATTEMPTS} attempts: "
        f"{type(last).__name__}: {last}") from last


# ── fetch ───────────────────────────────────────────────────────────────────

def fetch(target: str, *, mode: str = "delta", keys: dict | None = None,
          limit: int = DEFAULT_LIMIT, since: Any = None, **kw) -> FetchResult:
    """The newest posts of one public profile, free, capped at twelve.

    mode is accepted for contract compatibility and changes nothing: there is no
    deeper page to walk, so a 'backfill' here returns the same twelve entries a
    delta does and sets truncated. `since` still filters, because a delta that
    already covers its window can honestly report truncated=False.

    `keys` is ignored. This route has no credential and must never grow one: the
    moment it carries a cookie it stops being the public route and becomes
    instagram_client, with that adapter's consent rules attached.
    """
    handle = _handle(target)
    limit = max(1, int(limit))

    payload = _get(handle)
    user = (payload.get("data") or {}).get("user")
    if not isinstance(user, dict):
        raise RuntimeError(
            f"no user block for @{handle} (status={payload.get('status')!r}); "
            "removed, renamed, or served a shell page")

    tl = user.get("edge_owner_to_timeline_media") or {}
    edges = [e["node"] for e in (tl.get("edges") or [])
             if isinstance(e, dict) and isinstance(e.get("node"), dict)]
    total = _count(tl.get("count"))

    if not edges:
        # An empty grid on a live account is never legitimately silent. A private
        # account and a block both look exactly like "this profile posts nothing"
        # once persisted, and that is a claim the corpus must not make by accident.
        if user.get("is_private"):
            raise RuntimeError(f"@{handle} is private; this route sees only what a "
                               "logged-out visitor sees")
        if total:
            raise RuntimeError(f"@{handle} reports {total} posts but returned none "
                               "(blocked, or the payload shape changed)")
        return FetchResult(items=[], profile=_profile(user, handle), cost_usd=0.0)

    items = [_item(n) for n in edges]
    # Vendor order is NOT chronological: pinned posts are hoisted to the front
    # whatever their age. In the recorded payload entry 3 is 14 months older than
    # entry 4. Sorting before `limit` is what stops a limit-1 freshness probe from
    # comparing a pinned 2024 reel against the newest stored post forever.
    items.sort(key=lambda i: i.published_at, reverse=True)
    oldest_on_page = items[-1].published_at

    if since is not None:
        items = [i for i in items if i.published_at >= int(since)]

    kept, items = len(items), items[:limit]

    # Two separate ways this can be incomplete, and both must raise the flag.
    dropped_by_limit = kept > len(items)
    # Everything older than the oldest entry on the page is unreachable: there is
    # no working logged-out cursor. Unless the account has no more posts, or the
    # caller only asked for a window this page already covers, in which case
    # nothing is missing and saying otherwise would make every delta look partial.
    reached_the_end = not (tl.get("page_info") or {}).get("has_next_page") \
        and (total is None or total <= len(edges))
    covers_window = since is not None and int(since) >= oldest_on_page

    return FetchResult(
        items=items,
        profile=_profile(user, handle),
        cost_usd=0.0,
        truncated=dropped_by_limit or not (reached_the_end or covers_window),
    )


ADAPTER = Adapter(
    name="instagram_free",
    platform="instagram",
    fetch=fetch,
    relationships=("competitor", "influencer", "own", "client"),
    needs=(),                     # no key, by construction
    cost_per_1k=0.0,
    # shallow keeps it out of the cheapest-first default pick. It is free AND
    # incomplete, and for_platform()'s ordering assumes routes differ only in
    # price, so without this every instagram backfill would silently become a
    # 12-post top-up.
    shallow=True,
    notes="free, no key: instagram.com web_profile_info, cookie-free. TOP-UP AND "
          "PREVIEW ONLY, hard ceiling of 12 posts with no logged-out cursor, so "
          "backfills still belong to instagram_public. Gives carousel-slide MP4s "
          "and video duration, which the paid actor does not. No views (its view "
          "counter is not playCount), no shares, no saves, no comment bodies. "
          "Rate limited per IP and slow to forgive.",
    unmeasured=("views", "shares", "saves"),
)
