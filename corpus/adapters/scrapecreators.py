"""Instagram, TikTok and Facebook page posts through ScrapeCreators.

One vendor, three routes (ADAPTERS):
  * scrapecreators_instagram  GET /v2/instagram/user/posts?handle=   12 posts/credit
  * scrapecreators_tiktok     GET /v3/tiktok/profile/videos?handle=  10 videos/credit
  * scrapecreators_facebook   GET /v1/facebook/profile/posts?url=     3 posts/credit
Posts-per-credit measured 2026-10-23 on a local bistro (IG, FB) and
a student centre (TikTok); every call reported credits_charged=1.

PRICE. Billing is per REQUEST (credits_charged on every response), not per post. The
author holds ~7,100 credits that were free; the marginal price is the cheapest pack, $47 for
25,000 credits = $0.00188/credit (Aug 2026 evaluation), so per 1,000 posts:
Instagram $0.16, TikTok $0.19, Facebook $0.63. cost_usd reports credits at that pack
price, so scrape_run shows replacement value rather than $0 for gifted credits.

WHY NOT LINKEDIN. Re-checked 2026-10-23 against /openapi.json (196 paths): still no
person post feed. /v1/linkedin/company/posts exists but returns no engagement counts
and stops at 7 pages; linkedin_harvest does the same job with counts at $0.04/1k.
See docs/scrapecreators-evaluation.md.

CACHING DOES NOT APPLY. cache_max_age (a cached answer for 0 credits) exists on
profile and single-post endpoints only; none of the three feed endpoints accepts it
(openapi.json, 2026-10-23). Deltas save credits the other way: the feed is
newest-first, so paging stops at the first page that reaches the watermark.

ERROR SHAPES THAT MATTER. A private/age-gated/unavailable profile comes back as
success=true with HTTP 403/404, an `error` field and credits_charged=0; that is an
answer, not an outage, and is raised as one. HTTP 401 is a missing/invalid key; a
bogus key has been seen to return 402 "out of credits", so 402 says both.

INSTAGRAM IDS. The payload's `id` is a composite "<media pk>_<owner id>"; `pk` is
the bare media pk, the id instagram_public and instagram_free already store. Using
`id` would re-enter every existing post as new. The payload is Instagram's own web
API shape (media_type 1/2/8, image_versions2, carousel_media), the same one
instagram_client parses, so the media and metric projections are shared with it.
"""
from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .base import (EmptyProfile, TargetGone, Adapter, FetchResult, Profile, RawItem, RawMedia, classify_kind,
                   stable_id)
from .instagram_client import _media_rows as _ig_media_rows, _metrics as _ig_metrics

BASE = "https://api.scrapecreators.com"
TIMEOUT_S = 90
CREDIT_USD = 47 / 25_000          # cheapest pack, $0.00188/credit
PER_PAGE = {"instagram": 12, "tiktok": 10, "facebook": 3}     # measured 2026-10-23
# The pager is the only thing that spends here, so it is bounded independently of
# `limit`: 30 credits a fetch at most ($0.06 at pack price).
MAX_PAGES = 30
DEFAULT_LIMIT = 50
RETRY_WAITS_S = (2, 5)

Transport = Callable[[str, dict], tuple]       # (path, params) -> (status, data)


class ProfileUnavailable(TargetGone):
    """Private, age-gated or removed. Zero credits, and retrying will not help."""


def _http(key: str) -> Transport:
    def call(path: str, params: dict) -> tuple[int, Any]:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        req = urllib.request.Request(f"{BASE}{path}?{q}",
                                     headers={"x-api-key": key,
                                              "User-Agent": "corpus-cli/0.1"})
        for wait in (*RETRY_WAITS_S, None):
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                    return r.status, json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503) and wait is not None:
                    time.sleep(wait)
                    continue
                body = e.read().decode(errors="replace")
                try:
                    return e.code, json.loads(body)
                except ValueError:
                    return e.code, body[:300]
            except Exception as e:
                raise RuntimeError(f"scrapecreators call failed: {type(e).__name__}: {e}") from e
        raise AssertionError("unreachable")
    return call


def credits_left(key: str, transport: Transport | None = None) -> int | None:
    """GET /v1/credit-balance -> creditCount. Costs nothing (verified 2026-10-23)."""
    status, data = (transport or _http(key))("/v1/credit-balance", {})
    if status == 200 and isinstance(data, dict) and isinstance(data.get("creditCount"), int):
        return data["creditCount"]
    return None


def _page(transport: Transport, path: str, params: dict, who: str) -> tuple[dict, int]:
    status, data = transport(path, params)
    if status == 401:
        raise RuntimeError("scrapecreators 401: SCRAPECREATORS_API_KEY missing or invalid")
    if status == 402:
        raise RuntimeError("scrapecreators 402: out of credits, or a mistyped key "
                           "(the vendor reports both this way)")
    if isinstance(data, dict) and data.get("error") and status in (403, 404) \
            or (isinstance(data, dict) and data.get("errorStatus") in (403, 404)):
        raise ProfileUnavailable(f"scrapecreators: {who}: {data.get('message') or data.get('error')}")
    if status != 200 or not isinstance(data, dict) or data.get("success") is False:
        msg = data.get("message") or data.get("error") if isinstance(data, dict) else data
        raise RuntimeError(f"scrapecreators HTTP {status} on {path}: {str(msg)[:300]}")
    c = data.get("credits_charged")
    return data, int(c) if isinstance(c, (int, float)) and not isinstance(c, bool) else 1


def _walk(transport: Transport, path: str, base: dict, *, rows_of, next_of, ts_of,
          limit: int, floor: int | None, who: str) -> tuple[list[dict], dict, int, bool]:
    """Page newest-first until `limit` rows, the end of the feed, MAX_PAGES, or the
    delta watermark. Returns (rows, first page payload, credits, more_exists).

    The watermark test uses the LAST row of a page, not the oldest: pinned posts sit
    at the top of Instagram and TikTok feeds whatever their age, so "oldest on the
    page" would stop a delta on page one behind a pinned post from 2021."""
    rows: list[dict] = []
    first: dict = {}
    credits = 0
    cursor = None
    more = False
    for _ in range(MAX_PAGES):
        params = dict(base)
        params.update(cursor or {})
        data, c = _page(transport, path, params, who)
        credits += c
        first = first or data
        got = [r for r in (rows_of(data) or []) if isinstance(r, dict)]
        rows += got
        cursor = next_of(data)
        more = bool(got) and cursor is not None
        if len(rows) >= limit or not more:
            break
        last = ts_of(got[-1]) if got else None
        if floor is not None and last is not None and last < floor:
            more = False
            break
    return rows, first, credits, more


def _finish(items: list[RawItem], *, floor, limit, more) -> tuple[list[RawItem], bool]:
    seen, out = set(), []
    for i in sorted(items, key=lambda i: -i.published_at):
        if i.platform_item_id not in seen:       # a pinned post can repeat across pages
            seen.add(i.platform_item_id)
            out.append(i)
    if floor is not None:
        out = [i for i in out if i.published_at >= floor]
    return out[:limit], len(out) > limit or (more and len(out) >= limit)


def _key(keys: dict | None) -> str:
    key = (keys or {}).get("SCRAPECREATORS_API_KEY")
    if not key:
        raise RuntimeError("SCRAPECREATORS_API_KEY missing; expected in ~/.reach/keys.env")
    return key


def _count(v: Any) -> int | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v) if v >= 0 else None


def _handle(target: str, host: str) -> str:
    t = (target or "").strip().split("?", 1)[0].split("#", 1)[0]
    if host in t:
        t = t.split(host, 1)[1]
    parts = [p for p in t.strip("/").split("/") if p]
    if not parts:
        raise ValueError(f"no {host} handle in {target!r}")
    return parts[0].lstrip("@")


def _estimator(platform: str):
    def est(n_items: int) -> float:
        pages = min(MAX_PAGES, max(1, math.ceil(max(0, n_items) / PER_PAGE[platform])))
        return round(pages * CREDIT_USD, 6)
    return est


def spend_check(keys: dict, planned_items: int, est_usd: float = 0.0) -> None:
    """Refuse a run the credit balance cannot cover (balance reads are free)."""
    from ..keys import PlanGuardError
    key = (keys or {}).get("SCRAPECREATORS_API_KEY")
    if not key:
        return
    left = credits_left(key)
    need = math.ceil(est_usd / CREDIT_USD) if est_usd else 0
    if left is not None and left < max(need, 1):
        raise PlanGuardError(f"ScrapeCreators has {left} credits; this run needs ~{need}. "
                             f"Buy a pack at scrapecreators.com or lower --limit.")


# ── Instagram ───────────────────────────────────────────────────────────────

def _ig_item(m: dict) -> RawItem | None:
    ts = m.get("taken_at")
    if not isinstance(ts, (int, float)):
        return None
    code = m.get("code")
    pid = m.get("pk") or (str(m.get("id") or "").split("_")[0]) or code \
        or stable_id(m.get("url"), ts)
    mt = m.get("media_type")
    dur = m.get("video_duration")
    dur = float(dur) if isinstance(dur, (int, float)) and not isinstance(dur, bool) else None
    slides = m.get("carousel_media_count") or len(m.get("carousel_media") or [])
    owner = (m.get("owner") or m.get("user") or {}).get("username")
    metrics = _ig_metrics(m)
    if m.get("is_paid_partnership") is not None:
        metrics["is_paid_partnership"] = m["is_paid_partnership"]
    return RawItem(
        platform_item_id=str(pid),
        kind=classify_kind(has_video=(mt == 2), media_count=max(slides, 2) if mt == 8 else 1,
                           duration_s=dur),
        published_at=int(ts),
        raw=m,
        body=(m.get("caption") or {}).get("text") if isinstance(m.get("caption"), dict)
        else m.get("caption"),
        url=m.get("url") or (f"https://www.instagram.com/p/{code}/" if code else None),
        duration_s=dur,
        # On a collab the owner can be the other account; author_handle keeps that.
        author_handle=owner,
        metrics=metrics,
        media=_ig_media_rows(m),
    )


def _ig_profile(user: dict, handle: str) -> Profile | None:
    if not user or str(user.get("username", "")).lower() != handle.lower():
        return None
    return Profile(handle=user["username"], display_name=user.get("full_name") or None,
                   bio=user.get("biography") or None,
                   url=f"https://www.instagram.com/{user['username']}/",
                   platform_uid=str(user.get("pk") or user.get("id") or "") or None,
                   follower_count=_count(user.get("follower_count")),
                   following_count=_count(user.get("following_count")),
                   posts_total=_count(user.get("media_count")),
                   raw={k: user.get(k) for k in ("pk", "username", "full_name",
                                                 "follower_count", "following_count",
                                                 "media_count", "category", "is_verified")})


def fetch_instagram(target: str, *, mode: str = "delta", keys: dict | None = None,
                    limit: int = DEFAULT_LIMIT, since: Any = None,
                    transport: Transport | None = None, **_) -> FetchResult:
    tr = transport or _http(_key(keys))
    handle = _handle(target, "instagram.com")
    limit = max(1, int(limit))
    floor = int(since) if (mode != "backfill" and since is not None) else None
    rows, first, credits, more = _walk(
        tr, "/v2/instagram/user/posts", {"handle": handle},
        rows_of=lambda d: d.get("items"),
        next_of=lambda d: ({"next_max_id": d["next_max_id"]}
                           if d.get("more_available") and d.get("next_max_id") else None),
        ts_of=lambda r: r.get("taken_at"), limit=limit, floor=floor, who=f"@{handle}")
    user = first.get("user") or {}
    if not rows and floor is None:
        # The posts feed still carries the profile block. An account that says it
        # has 0 posts is a real, quiet answer (the owner archived everything), not a
        # broken vendor: falling through sent it on to Bright Data and then to an
        # Instagram endpoint from this IP every night, and alarmed every night.
        if user.get("is_private"):
            raise ProfileUnavailable(f"scrapecreators: @{handle} is private")
        prof = _ig_profile(user, handle)
        if prof is not None and prof.posts_total == 0:
            return FetchResult(items=[], profile=prof,
                               cost_usd=round(credits * CREDIT_USD, 6), truncated=False,
                               empty_confirmed=True)
        raise EmptyProfile(f"scrapecreators returned 0 posts for @{handle} unfiltered")
    items, truncated = _finish([i for i in map(_ig_item, rows) if i],
                               floor=floor, limit=limit, more=more)
    return FetchResult(items=items, profile=_ig_profile(user, handle),
                       cost_usd=round(credits * CREDIT_USD, 6), truncated=truncated)


# ── TikTok ──────────────────────────────────────────────────────────────────

def _first_url(node: Any) -> str | None:
    urls = (node or {}).get("url_list") if isinstance(node, dict) else None
    return next((u for u in urls or [] if u), None)


def _tt_item(v: dict) -> RawItem | None:
    vid, ts = v.get("aweme_id"), v.get("create_time")
    if not vid or not isinstance(ts, (int, float)):
        return None
    s = v.get("statistics") or {}
    video = v.get("video") or {}
    dur_ms = video.get("duration")
    dur = dur_ms / 1000 if isinstance(dur_ms, (int, float)) and dur_ms > 0 else None
    images = ((v.get("image_post_info") or {}).get("images") or [])
    media: list[RawMedia] = []
    if images:
        for i, im in enumerate(images):
            u = _first_url(im.get("display_image") or im.get("owner_watermark_image"))
            if u:
                media.append(RawMedia(role="slide", seq=i, remote_url=u, mime="image/jpeg"))
    else:
        u = _first_url(video.get("play_addr"))
        if u:
            media.append(RawMedia(role="original", remote_url=u, mime="video/mp4",
                                  width=video.get("width"), height=video.get("height")))
        c = _first_url(video.get("cover"))
        if c:
            media.append(RawMedia(role="poster", remote_url=c, mime="image/jpeg"))
    author = v.get("author") or {}
    metrics = {k: x for k, x in (("views", _count(s.get("play_count"))),
                                 ("reactions", _count(s.get("digg_count"))),
                                 ("comments", _count(s.get("comment_count"))),
                                 ("shares", _count(s.get("share_count"))),
                                 ("saves", _count(s.get("collect_count"))))
               if x is not None}
    if v.get("is_top"):
        metrics["is_pinned"] = True
    handle = author.get("unique_id")
    return RawItem(
        platform_item_id=str(vid),
        kind=classify_kind(has_video=not images, media_count=len(images) or 1,
                           duration_s=dur),
        published_at=int(ts), raw=v, body=v.get("desc") or None,
        url=v.get("url") or (f"https://www.tiktok.com/@{handle}/video/{vid}" if handle else None),
        duration_s=dur, author_handle=handle, author_name=author.get("nickname"),
        metrics=metrics, media=media)


def _tt_profile(rows: list[dict], handle: str) -> Profile | None:
    a = next(((r.get("author") or {}) for r in rows
              if str((r.get("author") or {}).get("unique_id", "")).lower() == handle.lower()),
             None)
    if not a:
        return None
    return Profile(handle=a["unique_id"], display_name=a.get("nickname") or None,
                   bio=a.get("signature") or None,
                   url=f"https://www.tiktok.com/@{a['unique_id']}",
                   platform_uid=str(a.get("uid") or "") or None,
                   follower_count=_count(a.get("follower_count")),
                   following_count=_count(a.get("following_count")),
                   posts_total=_count(a.get("aweme_count")),
                   raw={k: a.get(k) for k in ("uid", "unique_id", "nickname", "follower_count",
                                              "following_count", "aweme_count")})


def fetch_tiktok(target: str, *, mode: str = "delta", keys: dict | None = None,
                 limit: int = DEFAULT_LIMIT, since: Any = None,
                 transport: Transport | None = None, **_) -> FetchResult:
    tr = transport or _http(_key(keys))
    handle = _handle(target, "tiktok.com")
    limit = max(1, int(limit))
    floor = int(since) if (mode != "backfill" and since is not None) else None
    rows, _, credits, more = _walk(
        tr, "/v3/tiktok/profile/videos", {"handle": handle},
        rows_of=lambda d: d.get("aweme_list"),
        next_of=lambda d: ({"max_cursor": d["max_cursor"]}
                           if d.get("has_more") and d.get("max_cursor") else None),
        ts_of=lambda r: r.get("create_time"), limit=limit, floor=floor, who=f"@{handle}")
    if not rows and floor is None:
        raise RuntimeError(f"scrapecreators returned 0 videos for @{handle} unfiltered")
    items, truncated = _finish([i for i in map(_tt_item, rows) if i],
                               floor=floor, limit=limit, more=more)
    return FetchResult(items=items, profile=_tt_profile(rows, handle),
                       cost_usd=round(credits * CREDIT_USD, 6), truncated=truncated)


# ── Facebook pages ──────────────────────────────────────────────────────────

def _fb_item(p: dict) -> RawItem | None:
    pid, ts = p.get("id"), p.get("publishTime")
    if not pid or not isinstance(ts, (int, float)):
        return None
    vd = p.get("videoDetails") or {}
    video = vd.get("hdUrl") or vd.get("sdUrl")
    images = [u for u in (p.get("images") or []) if isinstance(u, str) and u] \
        or ([p["image"]] if p.get("image") else [])
    media: list[RawMedia] = []
    if video:
        media.append(RawMedia(role="original", remote_url=video, mime="video/mp4"))
        if vd.get("thumbnailUrl") or p.get("image"):
            media.append(RawMedia(role="poster", remote_url=vd.get("thumbnailUrl") or p["image"],
                                  mime="image/jpeg"))
    else:
        role = "slide" if len(images) > 1 else "original"
        media += [RawMedia(role=role, seq=i, remote_url=u, mime="image/jpeg")
                  for i, u in enumerate(images)]
    metrics: dict[str, Any] = {k: x for k, x in (
        ("reactions", _count(p.get("reactionCount"))),
        ("comments", _count(p.get("commentCount"))),
        ("shares", _count(p.get("share_count"))),
        ("views", _count(p.get("videoViewCount")))) if x is not None}
    if isinstance(p.get("reaction_counts"), dict):
        metrics["reaction_types"] = {k: v for k, v in p["reaction_counts"].items() if v}
    dur = vd.get("duration") or vd.get("length_in_second")
    dur = float(dur) if isinstance(dur, (int, float)) and not isinstance(dur, bool) else None
    author = p.get("author") or {}
    return RawItem(
        platform_item_id=str(pid),
        kind=classify_kind(has_video=bool(video), media_count=len(images), duration_s=dur),
        published_at=int(ts), raw=p, body=p.get("text") or None,
        url=p.get("permalink") or p.get("url"), duration_s=dur,
        author_name=author.get("name"), metrics=metrics, media=media)


def fetch_facebook(target: str, *, mode: str = "delta", keys: dict | None = None,
                   limit: int = DEFAULT_LIMIT, since: Any = None,
                   transport: Transport | None = None, **_) -> FetchResult:
    tr = transport or _http(_key(keys))
    handle = _handle(target, "facebook.com")
    url = f"https://www.facebook.com/{handle}/"
    limit = max(1, int(limit))
    floor = int(since) if (mode != "backfill" and since is not None) else None
    rows, _, credits, more = _walk(
        tr, "/v1/facebook/profile/posts", {"url": url},
        rows_of=lambda d: d.get("posts"),
        next_of=lambda d: {"cursor": d["cursor"]} if d.get("cursor") else None,
        ts_of=lambda r: r.get("publishTime"), limit=limit, floor=floor, who=url)
    if not rows and floor is None:
        raise EmptyProfile(f"scrapecreators returned 0 posts for {url} unfiltered")
    items, truncated = _finish([i for i in map(_fb_item, rows) if i],
                               floor=floor, limit=limit, more=more)
    # The posts feed carries no page follower count; /v1/facebook/profile does, for
    # one more credit, so it is left out rather than bought on every delta.
    return FetchResult(items=items, profile=None,
                       cost_usd=round(credits * CREDIT_USD, 6), truncated=truncated)


_COMMON = dict(relationships=("competitor", "influencer", "own", "client"),
               needs=("SCRAPECREATORS_API_KEY",), spend_check=spend_check)

ADAPTERS = (
    Adapter(name="scrapecreators_instagram", platform="instagram", fetch=fetch_instagram,
            cost_per_1k=round(CREDIT_USD * 1000 / PER_PAGE["instagram"], 3),
            notes="scrapecreators.com /v2/instagram/user/posts: 12 posts per credit "
                  "(measured), carousel video slides as MP4, follower counts free. "
                  "No shares/saves.",
            unmeasured=("shares", "saves"), estimate_usd=_estimator("instagram"),
            **_COMMON),
    Adapter(name="scrapecreators_tiktok", platform="tiktok", fetch=fetch_tiktok,
            cost_per_1k=round(CREDIT_USD * 1000 / PER_PAGE["tiktok"], 3),
            notes="scrapecreators.com /v3/tiktok/profile/videos: 10 videos per credit; "
                  "views, likes, comments, shares and saves all present. Media URLs "
                  "are session-signed and short-lived.",
            estimate_usd=_estimator("tiktok"), **_COMMON),
    Adapter(name="scrapecreators_facebook", platform="facebook", fetch=fetch_facebook,
            cost_per_1k=round(CREDIT_USD * 1000 / PER_PAGE["facebook"], 3),
            notes="scrapecreators.com /v1/facebook/profile/posts: public PAGES, 3 posts "
                  "per credit. Views on video posts only; no saves; no page follower "
                  "count in the feed.",
            unmeasured=("shares", "views", "saves"), estimate_usd=_estimator("facebook"),
            **_COMMON),
)
