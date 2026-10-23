"""Fetch what the audience said back.

Comment TEXT, not counts. The counts arrive free with every post sync; the bodies
are a separate paid call, which is why this is its own opt-in command rather than
part of `sync`.

What this deliberately does not buy
-----------------------------------
Both vendors will enrich each comment author with their profile -- headline,
follower count, company, even an email -- for roughly two to five times the price.
This module always requests the cheapest mode and stores only the display name the
platform already shows next to the comment. Mining objections does not need a
dossier on the person who raised them, and the difference between reading a public
reply and profiling a stranger is exactly that dossier.

Cost, verified against the Apify API on 2026-08-08:
    LinkedIn  harvestapi~linkedin-post-comments   $0.002 / comment
    Instagram apidojo~instagram-comments-scraper  $0.0005 / comment
Every call carries maxTotalChargeUsd. A renamed input key does not fail on Apify,
it succeeds -- and without a cap a run that ignores `maxItems` bills the whole
thread on every post.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from . import apify

LINKEDIN_ACTOR = "harvestapi~linkedin-post-comments"
INSTAGRAM_ACTOR = "apidojo~instagram-comments-scraper"

PRICE_PER_COMMENT = {"linkedin": 0.002, "instagram": 0.0005}

# Past this, a thread stops being audience feedback and starts being a comment
# section. The first few dozen carry the objections; the tail is "great post!".
DEFAULT_PER_POST = 25


@dataclass
class RawComment:
    platform_comment_id: str
    body: str | None = None
    author_name: str | None = None
    published_at: int | None = None
    reactions: int | None = None
    is_author_reply: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


def _call(actor: str, body: dict, token: str, *, charge_cap: float) -> list[dict]:
    return apify.call(actor, body, token, charge_cap=charge_cap,
                      too_slow="fetch fewer posts per batch")


def _epoch(value: Any) -> int | None:
    """Vendors return dates as ms, seconds, or ISO strings depending on the field."""
    if isinstance(value, dict):
        value = _dig(value, "timestamp", "date", "value", "time")
    if isinstance(value, list):
        value = value[0] if value else None
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        v = int(value)
        return v // 1000 if v > 10_000_000_000 else v
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def _dig(rec: dict, *paths: str) -> Any:
    """First non-empty value among dotted paths. Vendor payloads rename fields."""
    for path in paths:
        cur: Any = rec
        for part in path.split("."):
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(part)
        if cur not in (None, "", [], {}):
            return cur
    return None


def _text(value: Any) -> str | None:
    """Coerce a vendor field to text.

    Payload shapes drift per record, not just per actor: the same field arrives as
    a string on one comment and a {"text": ...} object or a list of runs on the
    next. Binding that straight to SQLite raises "type 'list' is not supported"
    mid-run, after earlier batches have already been paid for.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for k in ("text", "value", "name", "commentary", "content"):
            if isinstance(value.get(k), str):
                return value[k].strip() or None
        return None
    if isinstance(value, list):
        parts = [t for t in (_text(v) for v in value) if t]
        return " ".join(parts) or None
    return str(value)


def _int(value: Any) -> int | None:
    """Counts arrive as ints, numeric strings, or a list of per-type breakdowns."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        digits = "".join(c for c in value if c.isdigit())
        return int(digits) if digits else None
    if isinstance(value, list):
        # LinkedIn returns a per-reaction-type breakdown; the total is what we store.
        vals = [v for v in (_int(x) for x in value) if v is not None]
        return sum(vals) if vals else len(value) or None
    if isinstance(value, dict):
        for k in ("count", "total", "value", "numLikes"):
            if k in value:
                return _int(value[k])
    return None


# ── per-platform projection ─────────────────────────────────────────────────

def _linkedin_comment(rec: dict, author_handle: str | None) -> RawComment | None:
    cid = _dig(rec, "id", "commentId", "urn", "commentUrn")
    if not cid:
        return None
    actor_name = _dig(rec, "actor.name", "author.name", "authorName",
                      "commenter.name", "author.fullName")
    actor_url = _dig(rec, "actor.linkedinUrl", "author.linkedinUrl",
                     "author.publicIdentifier", "actor.publicIdentifier") or ""
    return RawComment(
        platform_comment_id=str(cid),
        body=_text(_dig(rec, "commentary", "text", "comment", "content")),
        author_name=_text(actor_name),
        published_at=_epoch(_dig(rec, "createdAt", "postedAt.timestamp",
                                 "postedAt", "date", "publishedAt")),
        reactions=_int(_dig(rec, "engagement.reactions", "reactionsCount",
                            "numLikes", "likesCount")),
        # The subject replying in their own thread. Matched on the handle in the
        # commenter's profile URL rather than on display name, which is not unique,
        # and matched whole: a substring test filed every comment by "anamarija"
        # under the author when the author was "ana".
        is_author_reply=bool(author_handle and _li_slug(actor_url)
                             == _li_slug(author_handle)),
        raw=rec,
    )


def _li_slug(value: Any) -> str:
    """The public identifier in a LinkedIn profile URL, or the value itself when it
    already is one: ".../in/Ana-K/?x=1" -> "ana-k"."""
    from urllib.parse import unquote
    v = str(value or "").strip()
    for marker in ("/in/", "/company/"):
        if marker in v:
            v = v.split(marker, 1)[1]
            break
    return unquote(v.split("?", 1)[0].split("#", 1)[0].strip("/").split("/")[0]).lower()


def _instagram_comment(rec: dict, author_handle: str | None) -> RawComment | None:
    cid = _dig(rec, "id", "pk", "commentId")
    if not cid:
        return None
    owner = _dig(rec, "user.username", "ownerUsername", "owner.username",
                 "username")
    return RawComment(
        platform_comment_id=str(cid),
        # "message" is what apidojo actually returns; the others are kept because
        # a body read as empty is silently DROPPED, so field drift here looks
        # exactly like a post with no comments.
        body=_text(_dig(rec, "message", "text", "comment", "content")),
        author_name=_text(owner),
        published_at=_epoch(_dig(rec, "timestamp", "createdAt", "created_at",
                                 "takenAt")),
        reactions=_int(_dig(rec, "likeCount", "likesCount",
                            "comment_like_count")),
        is_author_reply=bool(author_handle and owner
                             and str(owner).lower() == author_handle.lower()),
        raw=rec,
    )


# ── fetch ───────────────────────────────────────────────────────────────────

def estimate_cost(platform: str, n_posts: int, per_post: int) -> float:
    return round(n_posts * per_post * PRICE_PER_COMMENT[platform], 4)


def fetch(platform: str, urls: list[str], *, token: str,
          per_post: int = DEFAULT_PER_POST, author_handle: str | None = None,
          include_replies: bool = False,
          meta: dict | None = None) -> dict[str, list[RawComment]]:
    """Comments for a batch of post URLs, grouped by the URL they came from.

    Never touches the database, in keeping with the adapter contract, so it stays
    testable against a recorded payload.

    `meta`, if given, receives {"rows", "saturated"}. Saturated means the run
    stopped at its item or charge limit rather than at the end of the threads, so
    a post that got nothing was cut off, not empty.
    """
    if not urls:
        return {}
    if platform not in PRICE_PER_COMMENT:
        raise ValueError(f"no comment route for platform {platform!r}")

    cap = round(estimate_cost(platform, len(urls), per_post) * 1.5 + 0.01, 4)
    if platform == "linkedin":
        body = {
            "posts": urls,
            "maxItems": per_post,
            "scrapeReplies": bool(include_replies),
            # "short" is explicitly titled "no charge for profile details" in the
            # actor's own schema. "main" would add $0.002 per comment for a dossier
            # we have decided not to hold.
            "profileScraperMode": "short",
        }
        records = _call(LINKEDIN_ACTOR, body, token, charge_cap=cap)
        project = _linkedin_comment
    else:
        body = {"startUrls": [{"url": u} for u in urls],
                "maxItems": per_post * len(urls)}
        records = _call(INSTAGRAM_ACTOR, body, token, charge_cap=cap)
        project = _instagram_comment
    if meta is not None:
        meta["rows"] = len(records)
        meta["saturated"] = (len(records) >= body["maxItems"]
                             or apify.hit_cap(len(records),
                                              PRICE_PER_COMMENT[platform], cap))

    out: dict[str, list[RawComment]] = {u: [] for u in urls}
    for rec in records:
        c = project(rec, author_handle)
        if c is None or not (c.body or "").strip():
            continue
        # Which post did this come from? Vendors label it differently, and a
        # comment we cannot attribute is worse than useless: it would be filed
        # under whichever post happened to be first in the batch.
        # inputSource is apidojo's echo of the URL we sent; postId is the
        # shortcode. Either attributes the row; neither was in the first draft,
        # so every Instagram comment was dropped as unattributable.
        src = _dig(rec, "inputSource", "postUrl", "post.url", "postInput",
                   "inputUrl", "postId", "url", "post.postUrl")
        key = _match_url(str(src or ""), urls)
        if key is None:
            continue
        out[key].append(c)
    return out


def _match_url(src: str, urls: list[str]) -> str | None:
    """Attribute a returned comment to one of the requested URLs.

    Exact match first, then by the platform's own id inside the URL, because the
    vendor commonly echoes a normalised or share-suffixed variant of what we sent.
    """
    if src in urls:
        return src
    if not src:
        return urls[0] if len(urls) == 1 else None
    for u in urls:
        for token in _ids_in(u):
            if token and token in src:
                return u
    return urls[0] if len(urls) == 1 else None


def _ids_in(url: str) -> list[str]:
    """The stable id fragments of a post URL: activity urn, shortcode, ugcPost."""
    out = []
    for sep in ("activity-", "activity:", "ugcPost-", "ugcPost:"):
        if sep in url:
            tail = url.split(sep, 1)[1]
            # Up to the first non-digit, not every digit in the tail. LinkedIn
            # appends a share suffix, so "...-7488881230671839234-m-x1" would
            # otherwise yield the id with a stray "1" glued on and match nothing.
            out.append("".join(itertools.takewhile(str.isdigit, tail)))
    for marker in ("/p/", "/reel/", "/tv/"):
        if marker in url:
            out.append(url.split(marker, 1)[1].split("/")[0].split("?")[0])
    return [o for o in out if o]


# ── orchestration ───────────────────────────────────────────────────────────

# One run-sync call has a 300s ceiling. Batching keeps each call well inside it and
# bounds the blast radius of a failure: a dropped batch loses that batch, not the run.
#
# LinkedIn goes one post per call. A post carries TWO ids -- the activity urn in its
# public URL and the ugcPost urn in the payload -- and for some posts they are
# different numbers entirely: a URL reading activity-7269647108465459204 comes back
# as ugcPost:7269647107551109120. There is then no field linking a returned comment
# to the URL that asked for it, so in a multi-post batch every comment is
# unattributable and silently dropped. Ten posts returned zero comments that way
# while the same posts returned three each when asked for alone.
BATCH = {"linkedin": 1, "instagram": 10}
DEFAULT_BATCH = 10


def assert_affordable(conn, accounts, *, token, per_post=DEFAULT_PER_POST, limit=None,
                      include_replies=False) -> float:
    """The spend guard for comment fetches, which bill per comment and used to run
    with no guard at all (CLI and dashboard both). Raises keys.PlanGuardError;
    returns the estimate. Planning is read-only, so calling it twice is free."""
    from . import keys
    plans = [(a, sync(conn, a, token=token, per_post=per_post, limit=limit,
                      include_replies=include_replies, dry_run=True)) for a in accounts]
    items = sum((p.get("posts") or 0) * per_post for _, p in plans)
    est = sum(p.get("estimate") or 0.0 for _, p in plans)
    actors = tuple(sorted({LINKEDIN_ACTOR if a["platform"] == "linkedin" else INSTAGRAM_ACTOR
                           for a, p in plans if p.get("posts")}))
    keys.assert_can_spend(token, items, est_usd=est, actors=actors, prices={
        LINKEDIN_ACTOR: PRICE_PER_COMMENT["linkedin"],
        INSTAGRAM_ACTOR: PRICE_PER_COMMENT["instagram"]})
    return est


def sync(conn, account, *, token, per_post=DEFAULT_PER_POST, limit=None,
         include_replies=False, dry_run=False, on_batch=None) -> dict:
    """Fetch and store missing comment threads for one account.

    The work-list is derived from the data (posts whose comment count exceeds what
    we hold), so an interrupted run resumes correctly with no queue to reconcile,
    and a thread already fetched is never paid for twice.
    """
    from . import store

    platform = account["platform"]
    if platform not in PRICE_PER_COMMENT:
        return {"skipped": f"no comment route for {platform}", "posts": 0,
                "comments": 0, "cost": 0.0}

    # per_post is what we will actually request, so it is also the bar for "done".
    # Without it a post with more comments than the cap is never satisfied and gets
    # re-fetched, and re-billed, on every single run.
    todo = [r for r in store.items_missing_comments(
                conn, account["id"], limit=limit, per_post=per_post)
            if r.get("url")]
    est = estimate_cost(platform, len(todo),
                        min(per_post, max((r["m_comments"] for r in todo),
                                          default=0) or per_post))
    if dry_run or not todo:
        return {"posts": len(todo), "comments": 0, "cost": 0.0, "estimate": est,
                "dry_run": bool(dry_run)}

    new = seen = billed = deferred = 0
    spent = 0.0
    errors: list[str] = []
    step = BATCH.get(platform, DEFAULT_BATCH)
    for i in range(0, len(todo), step):
        batch = todo[i:i + step]
        urls = [r["url"] for r in batch]
        by_url = {r["url"]: r for r in batch}
        meta: dict = {}
        try:
            got = fetch(platform, urls, token=token, per_post=per_post,
                        author_handle=account["handle"],
                        include_replies=include_replies, meta=meta)
        except RuntimeError as e:
            # One bad batch must not lose the batches already stored. The run
            # reports what failed instead of unwinding.
            errors.append(str(e)[:200])
            continue

        reached = any(got.values())
        for url, cs in got.items():
            row = by_url.get(url)
            if row is None:
                continue
            for c in cs:
                _id, is_new = store.upsert_comment(
                    conn, row["id"], platform_comment_id=c.platform_comment_id,
                    body=c.body, author_name=c.author_name,
                    published_at=c.published_at, reactions=c.reactions,
                    is_author_reply=c.is_author_reply, raw=c.raw)
                seen += bool(is_new)
                new += bool(is_new)
            # Billed per returned row, including the duplicates the vendor sends
            # within a batch. Reporting only the rows that landed would understate
            # the bill.
            spent += len(cs) * PRICE_PER_COMMENT[platform]
            billed += len(cs)
            # Mark on ATTEMPT, not on yield. A thread that legitimately returns
            # nothing must not be retried on every run. The exception is a batch
            # that ran out of items or budget: Instagram's maxItems is shared across
            # the batch, so one viral post can take all of it, and the posts after
            # it come back empty because they were never reached. Those stay in the
            # work-list for the next run instead of being filed as fetched.
            # Only when the batch's rows did land on posts: if none could be
            # attributed, deferring would re-buy the same batch on every run.
            if not cs and meta.get("saturated") and reached:
                deferred += 1
                continue
            store.mark_comments_fetched(conn, row["id"])
        conn.commit()
        if on_batch:
            on_batch(min(i + step, len(todo)), len(todo), round(spent, 4))

    return {"posts": len(todo), "comments": seen, "new": new,
            "duplicates": max(0, billed - seen), "deferred": deferred,
            "cost": round(spent, 4), "errors": errors}
