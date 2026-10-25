"""LinkedIn and Instagram posts through Bright Data's Scraper API (async datasets).

One vendor, two routes, so this module exports ADAPTERS rather than ADAPTER:
  * brightdata_linkedin   dataset gd_lyy3tktm25m4avu764 ("LinkedIn posts"),
                          discover_by=profile_url for /in/, company_url for /company/
  * brightdata_instagram  dataset gd_lk5ns7kz21pck8jpis ("Instagram - Posts"),
                          discover_by=url (a profile URL)
Dataset ids and discover_by names come from docs.brightdata.com/scrapers/<domain>.json,
read 2026-10-23, not guessed.

THE FLOW IS ASYNC: POST /datasets/v3/trigger returns a snapshot_id, GET
/datasets/v3/progress/<id> moves starting -> running -> ready|failed, then GET
/datasets/v3/snapshot/<id>?format=json returns the records. Discovery runs past the
sync endpoint's 1-minute window, so the sync /scrape is not used. Polling is bounded
by TIMEOUT_S; on timeout the snapshot is cancelled (best effort) so a job nobody is
waiting for stops accruing records.

PRICE AND THE FREE TIER. 1 credit per delivered record, from a 5,000 credit/month
pool shared by every Bright Data scraper on the account; afterwards $1.50 per 1,000
(pay as you go, Sept 2026 FAQ). Without funds deposited the account hard-stops at the
quota instead of billing, so the operator's exposure today is a failed route, not a bill.
Error records (a dead page, a bad input) are billed too. MAX_RECORDS_PER_RUN caps
every fetch through both `limit_per_input` and `limit_multiple_results`, so one run
can never spend more than 2% of the month's pool however large --limit is.

NOT VERIFIED LIVE. On 2026-10-23 every trigger on the author's key answered HTTP 400
"Customer is not active" (account not activated in the control panel; nothing was
billed), and the key lacks the permission to read /customer/balance. The
projections below are built from the record samples in Bright Data's own reference
pages (tests/fixtures/brightdata_*.json) and need one real run to confirm ids and
fields before this route is trusted for a backfill.

LINKEDIN IS SHALLOW. Bright Data's limits table says profile post discovery returns
"the posts a profile shows publicly, typically about 10. Older posts are not
collected." That is the definition of Adapter.shallow, so it ranks behind the paid
deep routes and serves as a delta/fallback route, never as a backfill.

INSTAGRAM HONESTY. No shares and no saves on any public Instagram surface, so both
stay `unmeasured`; video views come as video_view_count/video_play_count and only on
video posts. Media links die 24 hours after collection (Bright Data limits table).
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .base import (EmptyProfile, Adapter, FetchResult, Profile, RawItem, RawMedia, classify_kind,
                   stable_id)

BASE = "https://api.brightdata.com"
LINKEDIN_POSTS = "gd_lyy3tktm25m4avu764"
INSTAGRAM_POSTS = "gd_lk5ns7kz21pck8jpis"

FREE_MONTHLY_RECORDS = 5000
RECORD_USD = 0.0015
# 100 records = 2% of the monthly pool. A sync over 10 accounts at the cap is 20%,
# which leaves room for a month of daily deltas instead of one enthusiastic backfill.
MAX_RECORDS_PER_RUN = 100
DEFAULT_LIMIT = 30

# Per `corpus sync`: a sync over 50 due accounts at the per-run cap would otherwise
# spend the whole month's pool in one go. spend_check refuses past this.
SYNC_RECORD_BUDGET = 1000

POLL_S = 10
TIMEOUT_S = 600          # Bright Data's own example gives up after ~10 minutes
HTTP_TIMEOUT_S = 60

# Transport: (method, path, params, body) -> (status, parsed body or text, headers).
Transport = Callable[[str, str, dict, Any], tuple]


class SnapshotFailed(RuntimeError):
    pass


def _http(key: str) -> Transport:
    def call(method: str, path: str, params: dict | None = None, body: Any = None):
        q = urllib.parse.urlencode({k: v for k, v in (params or {}).items()
                                    if v is not None})
        req = urllib.request.Request(
            f"{BASE}{path}{'?' + q if q else ''}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json",
                     "User-Agent": "corpus-cli/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
                status, text, headers = r.status, r.read().decode(), dict(r.headers)
        except urllib.error.HTTPError as e:
            status, text, headers = e.code, e.read().decode(errors="replace"), dict(e.headers or {})
        try:
            data = json.loads(text) if text.strip() else None
        except ValueError:
            data = text
        return status, data, headers
    return call


def _msg(data: Any) -> str:
    if isinstance(data, dict):
        return str(data.get("error") or data.get("message") or data)[:300]
    return str(data)[:300]


def collect(dataset_id: str, discover_by: str, inputs: list[dict], *, limit: int,
            transport: Transport, sleep: Callable[[float], None] = time.sleep,
            clock: Callable[[], float] = time.monotonic,
            timeout_s: float = TIMEOUT_S) -> tuple[list[dict], list[dict]]:
    """Trigger a discovery snapshot, wait for it, download it.

    Returns (records, error_records). Raises on a refused trigger, a failed
    snapshot, or a timeout. `limit` is clamped to MAX_RECORDS_PER_RUN here, at the
    one place every request passes through, so no caller can forget the cap.
    """
    cap = max(1, min(int(limit), MAX_RECORDS_PER_RUN))
    status, data, _ = transport(
        "POST", "/datasets/v3/trigger",
        {"dataset_id": dataset_id, "type": "discover_new", "discover_by": discover_by,
         "include_errors": "true", "format": "json",
         # Belt and braces: the body field caps per input, this caps the snapshot.
         "limit_multiple_results": cap},
        {"input": inputs, "limit_per_input": cap})
    if status != 200 or not isinstance(data, dict) or not data.get("snapshot_id"):
        raise RuntimeError(f"brightdata trigger refused (HTTP {status}): {_msg(data)}")
    sid = data["snapshot_id"]

    deadline = clock() + timeout_s
    while True:
        status, prog, headers = transport("GET", f"/datasets/v3/progress/{sid}", {}, None)
        state = prog.get("status") if isinstance(prog, dict) else None
        if status == 200 and state == "ready":
            break
        if status == 200 and state in ("failed", "canceled"):
            raise SnapshotFailed(f"brightdata snapshot {sid} {state}: "
                                 f"{_msg(prog.get('error_message') or prog)}")
        if status not in (200, 429):
            raise RuntimeError(f"brightdata progress HTTP {status}: {_msg(prog)}")
        if clock() >= deadline:
            # Records are billed as delivered; a job left running keeps delivering.
            transport("POST", f"/datasets/v3/snapshot/{sid}/cancel", {}, None)
            raise TimeoutError(f"brightdata snapshot {sid} not ready after "
                               f"{timeout_s:.0f}s (last status {state!r}); cancelled")
        # A 429 here means slow down, and 25 of them in 5 minutes blacklists the IP
        # on every Bright Data API: honour Retry-After, never poll faster than POLL_S.
        wait = POLL_S
        if status == 429:
            try:
                wait = max(POLL_S, int(headers.get("Retry-After") or POLL_S))
            except ValueError:
                pass
        sleep(wait)

    for _ in range(int(timeout_s // POLL_S) + 1):
        status, rows, _ = transport("GET", f"/datasets/v3/snapshot/{sid}",
                                    {"format": "json"}, None)
        if status == 200 and isinstance(rows, list):
            break
        if status == 202:            # "snapshot is building": ready but not packed
            sleep(POLL_S)
            continue
        raise RuntimeError(f"brightdata snapshot {sid} download HTTP {status}: {_msg(rows)}")
    else:
        raise TimeoutError(f"brightdata snapshot {sid} never finished building")

    rows = [r for r in rows if isinstance(r, dict)]
    errors = [r for r in rows if r.get("error") or r.get("error_code")]
    good = [r for r in rows if not (r.get("error") or r.get("error_code"))]
    return good, errors


# ── shared helpers ───────────────────────────────────────────────────────────

def _epoch(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        n = float(value)
        return int(n / 1000) if n > 1e11 else int(n)
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _count(v: Any) -> int | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v) if v >= 0 else None


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _since_day(since: Any) -> datetime | None:
    """Delta floor, stepped back a day: the vendor's date filters are day-granular
    with undocumented boundary semantics, and a re-fetched row costs one record
    while a dropped day costs the post."""
    ts = _epoch(since)
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc) - timedelta(days=1)


def _finish(items: list[RawItem], *, floor: int | None, limit: int,
            n_records: int, n_errors: int, cap: int) -> tuple[list[RawItem], bool]:
    items = sorted(items, key=lambda i: -i.published_at)
    if floor is not None:
        items = [i for i in items if i.published_at >= floor]
    cut = len(items) > limit
    # A snapshot that came back AT the cap may have stopped there.
    return items[:limit], cut or (n_records + n_errors) >= cap


def _estimate(n_items: int) -> float:
    return round(min(max(0, n_items), MAX_RECORDS_PER_RUN) * RECORD_USD, 6)


def spend_check(keys: dict, planned_items: int, est_usd: float = 0.0) -> None:
    """Refuse a sync whose Bright Data share would eat a fifth of the free month.

    The key cannot read /customer/balance (HTTP 403 on 2026-10-23), so this is a
    budget on what corpus asks for, not on what is left."""
    from ..keys import PlanGuardError
    if planned_items > SYNC_RECORD_BUDGET:
        raise PlanGuardError(
            f"this sync could pull up to {planned_items} Bright Data records, past the "
            f"{SYNC_RECORD_BUDGET}-record per-sync budget (the free pool is "
            f"{FREE_MONTHLY_RECORDS}/month). Sync fewer accounts or lower --limit.")


def _key(keys: dict | None) -> str:
    key = (keys or {}).get("BRIGHTDATA_API_KEY")
    if not key:
        raise RuntimeError("BRIGHTDATA_API_KEY missing; expected in ~/.reach/keys.env")
    return key


def _raise_if_only_errors(records, errors, where: str) -> None:
    if not records and errors:
        e = errors[0]
        if all(x.get("error_code") == "dead_page"
               and "activities are not found" in str(x.get("error")).lower() for x in errors):
            # the page exists; it just has no posts ("dead_page" is BD's word for it)
            raise EmptyProfile(f"brightdata: no posts on {where} "
                               f"({e.get('error_code')} {str(e.get('error'))[:120]})")
        raise RuntimeError(f"brightdata returned only error records for {where}: "
                           f"{e.get('error_code') or ''} {str(e.get('error'))[:200]}")


# ── LinkedIn ────────────────────────────────────────────────────────────────

_LI_TARGET = re.compile(r"linkedin\.com/(in|company|school|showcase)/([^/?#]+)", re.I)
_ACTIVITY = re.compile(r"activity[-:](\d{15,})")


def _li_target(target: str) -> tuple[str, str, str]:
    m = _LI_TARGET.search((target or "").strip())
    if not m:
        raise ValueError(f"expected a linkedin.com/in/<handle> or /company/<handle> "
                         f"URL, got {target!r}")
    seg, handle = m.group(1).lower(), m.group(2)
    return f"https://www.linkedin.com/{seg}/{handle}/", seg, handle


def _li_id(rec: dict) -> str | None:
    """The activity id, the same number HarvestAPI and the Apify actor key posts on
    (their `id` for 7381234567890123456 is that same activity), so switching routes
    updates rows instead of duplicating them. The url carries it too, as
    activity-<id>, for the record that arrives without `id`."""
    rid = rec.get("id")
    if rid and str(rid).isdigit():
        return str(rid)
    m = _ACTIVITY.search(str(rec.get("url") or ""))
    return m.group(1) if m else (str(rid) if rid else None)


def _li_media(rec: dict) -> list[RawMedia]:
    out: list[RawMedia] = []
    videos = [v for v in (rec.get("videos") or []) if v]
    for i, v in enumerate(videos):
        url = v if isinstance(v, str) else (v.get("url") or v.get("video_url"))
        if url:
            out.append(RawMedia(role="original", seq=i, remote_url=url, mime="video/mp4"))
    if videos and rec.get("video_thumbnail"):
        out.append(RawMedia(role="poster", remote_url=rec["video_thumbnail"],
                            mime="image/jpeg"))
    if not videos:
        imgs = [u for u in (rec.get("images") or []) if isinstance(u, str) and u]
        role = "slide" if len(imgs) > 1 else "original"
        for i, u in enumerate(imgs):
            out.append(RawMedia(role=role, seq=i, remote_url=u, mime="image/jpeg"))
        if not imgs and rec.get("document_cover_image"):
            out.append(RawMedia(role="thumbnail", remote_url=rec["document_cover_image"],
                                mime="image/jpeg"))
    return out


def _li_item(rec: dict) -> RawItem | None:
    pid = _li_id(rec)
    ts = _epoch(rec.get("date_posted"))
    if not pid or ts is None:
        return None
    videos = [v for v in (rec.get("videos") or []) if v]
    imgs = [u for u in (rec.get("images") or []) if u]
    pages = _count(rec.get("document_page_count")) or 0
    is_article = str(rec.get("post_type") or "").lower() == "article"
    duration = _num(rec.get("video_duration"))
    metrics = {k: v for k, v in (("reactions", _count(rec.get("num_likes"))),
                                 ("comments", _count(rec.get("num_comments"))))
               if v is not None}
    repost = rec.get("repost")
    return RawItem(
        platform_item_id=pid,
        kind=classify_kind(has_video=bool(videos),
                           media_count=pages or len(imgs), duration_s=duration,
                           has_title=is_article),
        published_at=ts,
        raw=rec,
        title=rec.get("title") if is_article else None,
        body=rec.get("post_text") or None,
        url=rec.get("url"),
        duration_s=duration,
        # The repost block is null on an authored post; anything in it means the
        # content is someone else's, which is amplification, not authorship.
        is_repost=bool(repost) and repost != {} and any(
            v for v in (repost.values() if isinstance(repost, dict) else [repost])),
        author_name=rec.get("user_name") or None,
        author_handle=rec.get("user_id") or None,
        metrics=metrics,
        media=_li_media(rec),
    )


def _li_profile(records: list[dict], *, url: str, handle: str) -> Profile | None:
    mine = [r for r in records
            if handle.lower() in str(r.get("use_url") or "").lower()
            or str(r.get("user_id") or "").lower() == handle.lower()]
    if not mine:
        return None
    followers = next((_count(r.get("user_followers")) for r in mine
                      if _count(r.get("user_followers")) is not None), None)
    return Profile(handle=handle, url=url, follower_count=followers,
                   display_name=next((r.get("user_name") for r in mine
                                      if r.get("user_name")), None),
                   raw={k: mine[0].get(k) for k in ("user_id", "use_url", "user_name",
                                                    "user_followers", "account_type",
                                                    "user_posts", "user_articles")})


def fetch_linkedin(target: str, *, mode: str = "delta", keys: dict | None = None,
                   limit: int = DEFAULT_LIMIT, since: Any = None,
                   transport: Transport | None = None, **_) -> FetchResult:
    key = _key(keys) if transport is None else ""
    url, seg, handle = _li_target(target)
    limit = max(1, int(limit))
    cap = min(limit, MAX_RECORDS_PER_RUN)
    inp: dict[str, Any] = {"url": url}
    floor = _epoch(since) if (mode != "backfill" and since is not None) else None
    if floor is not None:
        inp["start_date"] = _since_day(since).strftime("%Y-%m-%dT00:00:00.000Z")
    by = "profile_url" if seg == "in" else "company_url"
    records, errors = collect(LINKEDIN_POSTS, by, [inp], limit=cap,
                              transport=transport or _http(key))
    _raise_if_only_errors(records, errors, url)
    if not records and floor is None:
        raise EmptyProfile(f"brightdata returned 0 posts for {url} unfiltered "
                           "(removed/gated page, or a changed response shape)")
    items, truncated = _finish([i for i in map(_li_item, records) if i], floor=floor,
                               limit=limit, n_records=len(records),
                               n_errors=len(errors), cap=cap)
    return FetchResult(items=items, profile=_li_profile(records, url=url, handle=handle),
                       cost_usd=round((len(records) + len(errors)) * RECORD_USD, 6),
                       truncated=truncated)


# ── Instagram ───────────────────────────────────────────────────────────────

def _ig_handle(target: str) -> str:
    t = (target or "").strip().split("?", 1)[0].split("#", 1)[0]
    if "instagram.com" in t:
        t = t.split("instagram.com", 1)[1]
    parts = [p for p in t.strip("/").split("/") if p]
    if not parts:
        raise ValueError(f"no Instagram handle in {target!r}")
    return parts[0].lstrip("@")


def _ig_media(rec: dict) -> list[RawMedia]:
    out: list[RawMedia] = []
    content = [c for c in (rec.get("post_content") or []) if isinstance(c, dict)]
    if len(content) > 1:
        for c in content:
            if not c.get("url"):
                continue
            vid = str(c.get("type") or "").lower() == "video"
            out.append(RawMedia(role="slide", seq=int(c.get("index") or 0),
                                remote_url=c["url"],
                                mime="video/mp4" if vid else "image/jpeg"))
        return out
    videos = [v for v in (rec.get("videos") or []) if isinstance(v, str) and v]
    if videos:
        out.append(RawMedia(role="original", remote_url=videos[0], mime="video/mp4"))
        if rec.get("thumbnail"):
            out.append(RawMedia(role="poster", remote_url=rec["thumbnail"],
                                mime="image/jpeg"))
        return out
    photos = [p for p in (rec.get("photos") or []) if isinstance(p, str) and p]
    if photos:
        out.append(RawMedia(role="original", remote_url=photos[0], mime="image/jpeg"))
    return out


def _ig_metrics(rec: dict) -> dict[str, Any]:
    m: dict[str, Any] = {"reactions": _count(rec.get("likes")),
                         "comments": _count(rec.get("num_comments"))}
    # Same split instagram_public keeps: play count is the inclusive counter and
    # becomes `views`; the unique-viewer counter rides along under its own name.
    play, view = _count(rec.get("video_play_count")), _count(rec.get("video_view_count"))
    if play is not None or view is not None:
        m["views"] = play if play is not None else view
        if view is not None:
            m["video_view_count"] = view
    if rec.get("is_paid_partnership") is not None:
        m["is_paid_partnership"] = rec["is_paid_partnership"]
    return {k: v for k, v in m.items() if v is not None}


def _ig_item(rec: dict) -> RawItem | None:
    ts = _epoch(rec.get("date_posted"))
    if ts is None:
        return None
    code = rec.get("shortcode") or rec.get("content_id")
    # The bare media pk, byte-identical to the id instagram_public (apidojo) and
    # instagram_free store, so the routes update each other's rows.
    pid = rec.get("pk") or rec.get("post_id") or code or stable_id(rec.get("url"), ts)
    ctype = str(rec.get("content_type") or "").lower()
    content = [c for c in (rec.get("post_content") or []) if isinstance(c, dict)]
    is_carousel = ctype == "carousel" or len(content) > 1
    has_video = (not is_carousel) and (ctype in ("video", "reel")
                                       or bool(rec.get("videos")))
    duration = _num(rec.get("videos_duration"))
    if duration is None and isinstance(rec.get("videos_duration"), list):
        duration = next((_num(d) for d in rec["videos_duration"] if _num(d)), None)
    n_media = max(len(content), _count(rec.get("photos_number")) or 0,
                  2 if is_carousel else 1)
    return RawItem(
        platform_item_id=str(pid),
        kind=classify_kind(has_video=has_video, media_count=n_media,
                           duration_s=duration),
        published_at=ts,
        raw=rec,
        body=rec.get("description") or None,
        url=rec.get("url") or (f"https://www.instagram.com/p/{code}/" if code else None),
        duration_s=duration,
        is_repost=False,
        author_handle=rec.get("user_posted") or None,
        metrics=_ig_metrics(rec),
        media=_ig_media(rec),
    )


def _ig_profile(records: list[dict], handle: str) -> Profile | None:
    mine = [r for r in records if str(r.get("user_posted") or "").lower() == handle.lower()]
    if not mine:
        return None
    r = next((x for x in mine if x.get("followers") is not None), mine[0])
    return Profile(handle=r["user_posted"], url=f"https://www.instagram.com/{r['user_posted']}/",
                   platform_uid=str(r["user_posted_id"]) if r.get("user_posted_id") else None,
                   follower_count=_count(r.get("followers")),
                   posts_total=_count(r.get("posts_count")),
                   raw={k: r.get(k) for k in ("user_posted", "user_posted_id", "followers",
                                              "posts_count", "is_verified", "profile_url")})


def fetch_instagram(target: str, *, mode: str = "delta", keys: dict | None = None,
                    limit: int = DEFAULT_LIMIT, since: Any = None,
                    transport: Transport | None = None, **_) -> FetchResult:
    key = _key(keys) if transport is None else ""
    handle = _ig_handle(target)
    limit = max(1, int(limit))
    cap = min(limit, MAX_RECORDS_PER_RUN)
    inp: dict[str, Any] = {"url": f"https://www.instagram.com/{handle}/",
                           "num_of_posts": cap}
    floor = _epoch(since) if (mode != "backfill" and since is not None) else None
    if floor is not None:
        inp["start_date"] = _since_day(since).strftime("%m-%d-%Y")   # vendor format
    records, errors = collect(INSTAGRAM_POSTS, "url", [inp], limit=cap,
                              transport=transport or _http(key))
    _raise_if_only_errors(records, errors, f"@{handle}")
    if not records and floor is None:
        raise EmptyProfile(f"brightdata returned 0 posts for @{handle} unfiltered "
                           "(private/removed account, or a changed response shape)")
    items, truncated = _finish([i for i in map(_ig_item, records) if i], floor=floor,
                               limit=limit, n_records=len(records),
                               n_errors=len(errors), cap=cap)
    profile = _ig_profile(records, handle)
    if profile and profile.posts_total is not None and profile.posts_total <= len(items):
        truncated = False
    return FetchResult(items=items, profile=profile,
                       cost_usd=round((len(records) + len(errors)) * RECORD_USD, 6),
                       truncated=truncated)


_COMMON = dict(relationships=("competitor", "influencer", "own", "client"),
               needs=("BRIGHTDATA_API_KEY",), cost_per_1k=RECORD_USD * 1000,
               free_monthly_records=FREE_MONTHLY_RECORDS, estimate_usd=_estimate,
               spend_check=spend_check)

ADAPTERS = (
    Adapter(name="brightdata_linkedin", platform="linkedin", fetch=fetch_linkedin,
            shallow=True,
            notes=f"Bright Data dataset {LINKEDIN_POSTS}, async. 5k free records/mo "
                  f"(shared), then $1.50/1k; capped at {MAX_RECORDS_PER_RUN}/run. "
                  "Only the ~10 newest posts per profile, no shares/views.",
            unmeasured=("views", "saves", "shares"), **_COMMON),
    Adapter(name="brightdata_instagram", platform="instagram", fetch=fetch_instagram,
            notes=f"Bright Data dataset {INSTAGRAM_POSTS}, async. 5k free records/mo "
                  f"(shared), then $1.50/1k; capped at {MAX_RECORDS_PER_RUN}/run. "
                  "No shares/saves; media URLs die after 24h.",
            unmeasured=("shares", "saves"), **_COMMON),
)
