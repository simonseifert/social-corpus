"""LinkedIn post history for competitor, influencer and own pages, through Apify.

Route: harvestapi/linkedin-profile-posts, cookie-free, and it takes the same URL
shape for a person (/in/...) and a company page (/company/...), so one adapter
covers both. Reaction, comment and share COUNTS ride along in the base record for
free; `scrapeReactions` and `scrapeComments` are separately billed events that fetch
individual reactors and comment bodies, which the corpus does not store. Both stay
off unless a caller opts in.

WHAT LINKEDIN DOES NOT GIVE YOU HERE. No video duration, so every video files as
short_video until enrichment probes the file. No view count on any surface. No saves.
No language tag. No pagination cursor -- reaching further back means raising `limit`
and re-paying for the posts you already hold, which is why `since` is pushed to the
vendor as a server-side floor rather than filtered client-side.

REPOSTS ARE THE SHARP EDGE. A repost comes back as the ORIGINAL post's CONTENT --
original author, original body, original engagement counts -- decorated with
`repostedBy` and `repostedAt`. What it does NOT borrow is the id: the reshare mints
its own activity id and that is what lands in `id`, so the bare id stays the dedup
key. The two fields that do still point at the original are `linkedinUrl` and
`engagement.id`, which is why the item's url comes from header.linkedinUrl instead.

This is the PUBLIC route. It sees what a logged-out visitor sees, which is what keeps
competitor and influencer data separable from consented client exports.
"""
from __future__ import annotations

import re
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .. import apify
from .base import (Adapter, FetchResult, Profile, RawItem, RawMedia, classify_kind,
                   stable_id)

ACTOR = "harvestapi~linkedin-profile-posts"

# The server abandons the sync wait at 300s. A client timeout below that kills the
# connection while the run keeps going and keeps billing, so sit just above it.
TIMEOUT_S = 310

# Half the Instagram default on purpose: this actor bills 4x per item, so 50 posts is
# already $0.10 a profile and a daily delta rarely needs more.
DEFAULT_LIMIT = 50

# Billing verified against /v2/actor-runs on 2026-08-05, not the price card:
# 0 items -> $0.00105, 2 -> $0.00405, 3 -> $0.00605, 6 -> $0.01205. That is one
# actor-start event plus $0.002 per post at BRONZE (= the STARTER plan), and a
# consolation charge on a query that returned nothing.
START_USD = 0.00005
POST_USD = 0.002
NO_RESULT_USD = 0.001

_TARGET_RE = re.compile(r"linkedin\.com/(in|company|school|showcase)/([^/?#]+)", re.I)
_FOLLOWERS_RE = re.compile(r"^([\d,.\s]+)\s+followers", re.I)


def _target(target: str) -> tuple[str, str, str]:
    """(canonical url, path segment, handle) from a LinkedIn URL.

    A bare handle is rejected rather than guessed: "examplecorp" is a company page and
    "taylorbrooks" is a profile, and the actor needs the right path segment. Canonical
    also means the `?miniProfileUrn=...` tail LinkedIn appends everywhere is dropped,
    so the same account never enters the corpus under two spellings.
    """
    m = _TARGET_RE.search((target or "").strip())
    if not m:
        raise ValueError(
            f"expected a linkedin.com/in/<handle> or /company/<handle> URL, got {target!r}")
    seg, handle = m.group(1).lower(), m.group(2)
    return f"https://www.linkedin.com/{seg}/{handle}/", seg, handle


def _epoch(node: Any) -> int | None:
    """postedAt/repostedAt to unix seconds UTC.

    The vendor sends {"timestamp": <ms>, "date": "<iso>"}. Milliseconds are the
    failure that writes silently: 1785375347243 stored as seconds lands in the year
    58500 and nothing downstream complains. The ISO branch is the fallback for the
    day `timestamp` disappears.

    A quoted number is accepted because it is the cheap vendor drift here: returning
    None for it would drop every record while `records` stays non-empty, so the
    "0 rows unfiltered" guard would not fire and the run would log ok with 0 items.
    """
    if not isinstance(node, dict):
        return None
    ts = node.get("timestamp")
    if isinstance(ts, str) and ts.strip().isdigit():
        ts = int(ts)
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
        return int(ts // 1000) if ts > 1e11 else int(ts)
    raw = node.get("date")
    if not raw:
        return None
    dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value >= 0 else None


def _followers(info: Any) -> int | None:
    """Company authors put "28,798,802 followers" in `info`; people put a headline."""
    m = _FOLLOWERS_RE.match(str(info or "").strip())
    return int(re.sub(r"\D", "", m.group(1))) if m else None


def _estimate_cost(n_items: int) -> float:
    return round(START_USD + (n_items * POST_USD if n_items else NO_RESULT_USD), 6)


def _document_pages(doc: Any) -> list[str]:
    """Page images of a LinkedIn document (PDF carousel) post.

    coverPages is one entry per rendition WIDTH, each holding every page at that
    width -- not one entry per page. Taking the widest rendition and enumerating its
    imageUrls is what turns a document post into slides.
    """
    if not isinstance(doc, dict):
        return []
    pages = [p for p in (doc.get("coverPages") or []) if isinstance(p, dict)]
    if not pages:
        return []
    best = max(pages, key=lambda p: p.get("width") or 0)
    return [u for u in (best.get("imageUrls") or []) if u]


def _media(rec: dict) -> list[RawMedia]:
    video = rec.get("postVideo") or {}
    images = [i for i in (rec.get("postImages") or []) if isinstance(i, dict)]
    slides = _document_pages(rec.get("document"))
    out: list[RawMedia] = []

    if video.get("videoUrl"):
        out.append(RawMedia(role="original", remote_url=video["videoUrl"],
                            mime="video/mp4"))
    if video.get("thumbnailUrl"):
        out.append(RawMedia(role="poster", remote_url=video["thumbnailUrl"],
                            mime="image/jpeg"))

    # enrich picks a video by role='original', so a still may only claim that role
    # when it is the whole post.
    still_role = "slide" if (out or len(images) + len(slides) > 1) else "original"
    for i, img in enumerate(images):
        if img.get("url"):
            out.append(RawMedia(role=still_role, seq=i, remote_url=img["url"],
                                mime="image/jpeg", width=img.get("width"),
                                height=img.get("height")))
    for i, url in enumerate(slides):
        out.append(RawMedia(role="slide", seq=len(images) + i, remote_url=url,
                            mime="image/jpeg"))

    card = (rec.get("article") or {}).get("image") or {}
    if card.get("url"):
        # The link-card image is a thumbnail, never the post's own creative, so it
        # does not count toward the carousel/image decision in _item.
        out.append(RawMedia(role="thumbnail", remote_url=card["url"], mime="image/jpeg",
                            width=card.get("width"), height=card.get("height")))
    return out


def _metrics(rec: dict) -> dict[str, Any]:
    """engagement{} to the canonical metric vocabulary.

    `likes` is LinkedIn's name for the TOTAL reaction count, not the LIKE bucket:
    across the recorded fixture the per-type breakdown sums to it exactly. So it maps
    to `reactions`, and the breakdown rides along under reaction_types, which persist
    routes into metric_snapshot.metrics_raw.
    """
    eng = rec.get("engagement") or {}
    m: dict[str, Any] = {
        "reactions": _count(eng.get("likes")),
        "comments": _count(eng.get("comments")),
        "shares": _count(eng.get("shares")),
    }
    breakdown = {r["type"]: _count(r.get("count"))
                 for r in (eng.get("reactions") or [])
                 if isinstance(r, dict) and r.get("type")}
    m["reaction_types"] = {k: v for k, v in breakdown.items() if v is not None} or None
    return {k: v for k, v in m.items() if v is not None}


def _true_author(rec: dict) -> tuple[str | None, str | None]:
    """Who actually wrote this, which on a repost is not the account being synced.

    The actor keeps the ORIGINAL author under `author` and records the amplifier
    under `repostedBy`, so the naive read of `author` is already correct -- what was
    missing was carrying it through. Verified on real data: the author's LinkedIn feed is
    20 reposts and 1 original, and the top item by reactions was written by someone
    else entirely.
    """
    au = rec.get("author") or {}
    if not isinstance(au, dict):
        return (str(au) or None, None)
    name = (au.get("name")
            or " ".join(x for x in (au.get("firstName"), au.get("lastName")) if x)
            or None)
    handle = au.get("publicIdentifier") or au.get("universalName")
    if not handle:
        url = str(au.get("linkedinUrl") or au.get("url") or "")
        for marker in ("/in/", "/company/"):
            if marker in url:
                handle = url.split(marker, 1)[1].strip("/").split("?")[0]
                break
    return (name, handle)


def _is_repost(rec: dict) -> bool:
    header = str((rec.get("header") or {}).get("text") or "")
    return bool(rec.get("repostedBy")) or "reposted this" in header.lower()


def _post_id(rec: dict) -> str | None:
    """The dedup key: LinkedIn's bare activity id.

    `id` is the activity id of THIS timeline entry, including for a repost -- a
    reshare mints its own activity, it does not borrow the original's. Verified by
    decoding the snowflake (id >> 22 is epoch ms) on a live repost: `id` decodes to
    repostedAt and matches the activity urn in header.linkedinUrl (the reshare's
    permalink), while the original's activity id sits in linkedinUrl and engagement.id
    nine days earlier. So the bare id is already unique per timeline entry and must
    stay bare: linkedin_export._post_id emits the same activity id from a consented
    export, and decorating it here would file one post as two rows.

    shareUrn is the fallback rather than a hash of linkedinUrl, because that URL's
    slug is generated from the post's first words and LinkedIn regenerates it when
    the post is edited. The urn keeps export's namespacing so a ugcPost id can never
    masquerade as an activity id.
    """
    for key in ("id", "entityId"):
        if rec.get(key):
            return str(rec[key])
    m = re.search(r"urn:li:(ugcPost|share):(\d{6,})", str(rec.get("shareUrn") or ""))
    return f"{m.group(1)}:{m.group(2)}" if m else None


def _permalink(rec: dict, *, repost: bool) -> str | None:
    """Where this timeline entry actually lives.

    For a repost `linkedinUrl` is the ORIGINAL's permalink, which would leave the row
    pointing at a different account's post than the one it records. header.linkedinUrl
    is the reshare, and stripping its updateEntityUrn tail leaves the canonical
    /feed/update/urn:li:activity:<id> form -- the same id this row is keyed on.
    """
    if repost:
        head = str((rec.get("header") or {}).get("linkedinUrl") or "")
        if head:
            return head.split("?")[0]
    return rec.get("linkedinUrl") or rec.get("shareLinkedinUrl") or None


def _item(rec: dict) -> RawItem | None:
    """One dataset record to a RawItem. None when it carries no usable timestamp.

    Dropping a record is deliberate: published_at is NOT NULL and inventing a date
    for a malformed row corrupts every recency query it later lands in. The row count
    in scrape_run is what surfaces it.
    """
    repost = _is_repost(rec)
    # postedAt and repostedAt are the same instant on this actor (both are when the
    # entry hit the feed), but repostedAt is read first so that a vendor which starts
    # reporting the ORIGINAL's publish time in postedAt cannot drag oldest_item_at,
    # the backfill watermark, years past what was actually fetched.
    published_at = (_epoch(rec.get("repostedAt")) if repost else None) \
        or _epoch(rec.get("postedAt"))
    if published_at is None:
        return None

    pid = _post_id(rec) or stable_id(rec.get("linkedinUrl"), published_at)

    article = rec.get("article") or {}
    # A native Pulse piece or newsletter issue IS an article; a post that merely
    # shares a link is still a text post. The headline is kept either way because it
    # is content the post displays and worth having in FTS, but only the native case
    # is allowed to change `kind`.
    native_article = bool(rec.get("newsletterUrl")) or \
        "linkedin.com/pulse" in str(article.get("link") or "").lower()

    images = rec.get("postImages") or []
    slides = _document_pages(rec.get("document"))

    return RawItem(
        platform_item_id=pid,
        kind=classify_kind(
            has_video=bool((rec.get("postVideo") or {}).get("videoUrl")),
            media_count=len(images) + len(slides),
            duration_s=None,          # the actor never reports one; enrichment probes it
            has_title=native_article,
        ),
        published_at=published_at,
        raw=rec,
        title=article.get("title") or rec.get("newsletterTitle") or None,
        body=rec.get("content") or None,
        url=_permalink(rec, repost=repost),
        is_repost=repost,
        author_name=_true_author(rec)[0],
        author_handle=_true_author(rec)[1],
        metrics=_metrics(rec),
        media=_media(rec),
    )


PROFILE_ACTOR = "harvestapi~linkedin-profile-scraper"


def fetch_profile(handle: str, *, keys: dict, charge_cap: float = 0.05) -> dict | None:
    """The profile page itself: experience, education, skills, awards, followers.

    The posts actor only ever exposes an author STUB (name, avatar, headline), which
    is why an account synced from posts alone has no career history and no follower
    count. Experience is not decoration on a content audit: "Founder at Example Media,
    Feb 2024 to Dec 2025" is what makes a positioning finding checkable against what
    the person actually does.

    Roughly $0.004 a call, so it runs on backfill rather than on every delta.
    """
    token = keys.get("APIFY_TOKEN")
    if not token:
        return None
    seg = "company" if handle.startswith("company:") else "in"
    h = handle.split(":", 1)[-1]
    body = ({"companies": [f"https://www.linkedin.com/company/{h}/"]}
            if seg == "company" else
            {"urls": [f"https://www.linkedin.com/in/{h}/"]})
    actor = ("harvestapi~linkedin-company" if seg == "company" else PROFILE_ACTOR)
    try:
        recs = _call(body, token, charge_cap=charge_cap, actor=actor)
    except Exception:
        return None
    return recs[0] if recs else None


def profile_from_payload(d: dict, *, handle: str) -> Profile:
    """Project the profile actor's payload onto Profile, keeping the rest in raw."""
    loc = d.get("location") or {}
    name = " ".join(x for x in (d.get("firstName"), d.get("lastName")) if x)
    return Profile(
        handle=handle,
        display_name=name or d.get("name") or handle,
        bio=d.get("headline") or None,
        url=d.get("linkedinUrl") or f"https://www.linkedin.com/in/{handle}/",
        platform_uid=str(d["id"]) if d.get("id") else None,
        follower_count=d.get("followerCount"),
        raw=d,
    )


def _profile(records: list[dict], *, handle: str, seg: str) -> Profile | None:
    """Author block of a record that actually belongs to `handle`.

    The match matters more here than on other platforms: a repost's author is the
    ORIGINAL poster, and persist() upserts Profile.handle as an account row. Taking
    the first author blindly would file a stranger's page under this account and
    branch the corpus in two.
    """
    for rec in records:
        a = rec.get("author") or {}
        name = a.get("publicIdentifier") or a.get("universalName") or ""
        if str(name).lower() != handle.lower():
            continue
        info = a.get("info")
        followers = _followers(info)
        return Profile(
            handle=name,
            display_name=a.get("name"),
            # People carry a headline in `info`, companies carry their follower line.
            # Storing "93,868 followers" as a bio is worse than storing nothing.
            bio=None if followers is not None else (info or None),
            url=f"https://www.linkedin.com/{seg}/{name}/",
            platform_uid=str(a["id"]) if a.get("id") else None,
            follower_count=followers,
            raw=a,
        )
    return None


def _call(body: dict, token: str, *, charge_cap: float,
          actor: str = ACTOR) -> list[dict]:
    # The input keys sent to it come from the actor's published openapi.json: a
    # renamed key does not fail, it succeeds with the wrong run (see corpus.apify).
    return apify.call(actor, body, token, charge_cap=charge_cap, timeout=TIMEOUT_S,
                      too_slow="lower `limit`")


def fetch(target: str, *, mode: str = "delta", keys: dict | None = None,
          limit: int = DEFAULT_LIMIT, since: Any = None,
          scrape_reactions: bool = False, scrape_comments: bool = False,
          **kw) -> FetchResult:
    """Posts from one LinkedIn profile or company page, newest first.

    mode: 'backfill' walks as far back as `limit` allows; anything else pushes
    `since` (unix seconds) to the vendor as a lower bound, so pages older than the
    watermark are never fetched and never billed.

    scrape_reactions / scrape_comments are opt-in because each bills a separate event
    per reactor and per comment body. The counts the corpus stores are free without
    them.
    """
    token = (keys or {}).get("APIFY_TOKEN")
    if not token:
        raise RuntimeError("APIFY_TOKEN missing; expected in ~/.reach/keys.env")

    url, seg, handle = _target(target)
    limit = max(1, int(limit))
    body: dict[str, Any] = {
        "targetUrls": [url],
        "maxPosts": limit,
        "scrapeReactions": bool(scrape_reactions),
        "scrapeComments": bool(scrape_comments),
    }
    if mode != "backfill" and since is not None:
        # postedLimitDate reads like an end bound but is a floor: "scrape posts from
        # now up to and including this date". Verified against the live actor on
        # 2026-08-05 in both directions (a 6-day bound returned 0, a 10-day bound
        # returned 2), because a filter that silently returns nothing would look
        # exactly like a quiet week.
        body["postedLimitDate"] = datetime.fromtimestamp(
            int(since), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    records = _call(body, token, charge_cap=round(_estimate_cost(limit) * 2 + 0.01, 4))

    if not records and "postedLimitDate" not in body:
        # An unfiltered pull of a live page is never legitimately empty. Empty means
        # input key drift, a removed or gated page, or a block -- all of which must be
        # loud, because a run recorded as ok with 0 items reads as "they post nothing".
        raise RuntimeError(f"{ACTOR} returned 0 rows for {url} unfiltered "
                           "(input key drift, removed/gated page, or blocked)")

    items = [i for i in map(_item, records) if i]

    # The posts feed carries only an author STUB. On a backfill it is worth $0.004 to
    # pull the real profile: experience, education, skills, follower count. Experience
    # is not decoration on a content audit -- "Founder at Example Media, Feb 2024 to Dec
    # 2025" is what makes a positioning claim checkable. A delta skips it, because a
    # career does not change weekly.
    prof = None
    if mode == "backfill":
        payload = fetch_profile(handle if seg == "in" else f"company:{handle}",
                                keys=keys or {})
        if payload:
            prof = profile_from_payload(payload, handle=handle)
    if prof is None:
        prof = _profile(records, handle=handle, seg=seg)

    return FetchResult(
        items=items,
        profile=prof,
        cost_usd=_estimate_cost(len(records)),
        # The actor exposes no cursor and no total, so a full page is the only signal
        # that more exists past `limit`.
        truncated=len(records) >= limit,
        raw_count=len(records),
    )


ADAPTER = Adapter(
    name="linkedin_posts",
    platform="linkedin",
    fetch=fetch,
    # A public route works for any relationship, but own/client pages are better
    # served by the consented export adapter, which carries impressions this cannot.
    relationships=("competitor", "influencer", "own", "client"),
    needs=("APIFY_TOKEN",),
    cost_per_1k=2.0,
    notes=f"apify:{ACTOR}; profile and company posts, reposts included. No views, "
          "no saves, no video duration and no pagination cursor from this actor.",
    apify_actor=ACTOR,
    unmeasured=("views", "saves"),
)
