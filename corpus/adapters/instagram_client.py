"""Instagram, consented route: an account we own or a client has authorised.

WHAT THIS ROUTE RETURNS, AND WHAT IT DOES NOT
---------------------------------------------
Returns   : likes (-> reactions), comments, views, plays, caption, media URLs,
            video duration, carousel slides, profile counts.
Does NOT  : reach, saves, shares, follows-from-post, average watch time,
            skip rate, profile visits.

Those six are the owner-only metrics that make a client route worth having, and
this route cannot produce them. Verified live on 2026-08-05, not inferred: the
private insights endpoints (/api/v1/insights/media_organic_insights/,
/api/v1/insights/account_organic_insights/, /api/v1/media/<pk>/insights/, and the
i.instagram.com equivalents) all 404 while a control call on the same cookie
returns 200. The professional dashboard moved to rotating GraphQL doc_id queries.
So the 404s are removals, not auth failures, and no amount of header tuning brings
them back.

Where a metric is unavailable its key is ABSENT from RawItem.metrics. It is never
zero. A zero saves count in a client brief reads as "nobody saved this", which is
a different and much more damaging claim than "we did not measure saves".

THE ROUTE THAT DOES RETURN THEM
-------------------------------
Meta Graph API v26.0 with Facebook Login, at Standard Access. No App Review is
needed as long as the app is claimed by a Business Portfolio the app user has a
role on, so the 20-day review queue is avoidable entirely. Setup, once per client:

  1. Meta app (type Business), claimed under the operator's Business Portfolio.
  2. Client adds that portfolio as a Partner on their own portfolio and assigns
     the Facebook Page + Instagram account with the "Insights" task
     (PROFILE_PLUS_ANALYZE). Nothing else grants instagram_manage_insights.
  3. System User in the operator's portfolio + non-expiring token, so unattended syncs
     do not ride the 60-day user-token treadmill.

That route needs a token nobody has issued yet, so it is a seam here rather than
an implementation: see _fetch_graph. Adding it changes nothing about this module's
public shape, only which entry in _ROUTES the `route` argument selects.

OPT-IN ONLY: CORPUS_ALLOW_IG_SESSION=1
-------------------------------------
This route is excluded from routing unless CORPUS_ALLOW_IG_SESSION=1 is set in the
environment of the process that runs the sync. It used to sort FIRST for every
own/client Instagram account (it is free), which made it the route a nightly timer
would use. The author's own Instagram account was flagged for automation twice when a
private-API session script ran on a timer (in a separate project): Instagram
reads cookie-authenticated private-API traffic on a schedule as a bot, and the
penalty lands on the account whose cookie it is, not on this tool. Without the
switch, own/client accounts fall through to instagram_public (Apify, no cookie,
no account at risk). Set it only for a deliberate, attended run, and never in a
systemd unit or cron environment. The Graph API seam below is the sanctioned
long-term route and is not gated.

CREDENTIAL WARNING FOR THE 'client' RELATIONSHIP
-----------------------------------------------
This route authenticates with an Instagram session cookie. A sessionid is the
client's entire account: DM read/write, post, delete. It breaks on password change
and on 2FA, and it is against Instagram's terms. It is the right tool for
relationship='own' and a poor one for relationship='client'. The adapter still
declares both because the store's relationship vocabulary is about the legal path,
not the transport, but reach for the Graph route before pointing this at a client.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import urlencode

from .base import (Adapter, FetchResult, Profile, RawItem, RawMedia,
                   classify_kind)

_IG = "https://www.instagram.com"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
       "(KHTML, like Gecko) Version/17.5 Safari/605.1.15")
# The instagram.com web client's own app id. Without this header the API answers a
# request for JSON with the login page, at HTTP 200.
_APP_ID = "936619743392459"
_PAGE_SIZE = 50
_PAGE_SLEEP_S = 1.0

# Instagram's own media_type enum, used unchanged so the mapping stays greppable
# against a raw payload.
_IMAGE, _VIDEO, _CAROUSEL = 1, 2, 8


class SessionInvalid(RuntimeError):
    """The cookie is dead.

    Kept distinct from a transport error because the two need different humans:
    this one means "re-provision the credential", a URLError means "Instagram or
    the network is down, retry later".
    """


# ── transport ───────────────────────────────────────────────────────────────

class _WebSession:
    """Authenticated GET against instagram.com's private web API.

    The header set is lifted from ig-saves/sync.py, which has run it daily for
    months. All of it is load-bearing; the non-JSON content type is the only
    reliable session-dead signal, since Instagram serves HTML with a 200 rather
    than returning 401.
    """

    def __init__(self, *, sessionid: str, csrftoken: str, ds_user_id: str):
        self._cookie = (f"sessionid={sessionid}; csrftoken={csrftoken}; "
                        f"ds_user_id={ds_user_id}")
        self._csrf = csrftoken

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        url = _IG + path + ("?" + urlencode(params) if params else "")
        req = urllib.request.Request(url, headers={
            "User-Agent": _UA,
            "X-IG-App-ID": _APP_ID,
            "X-CSRFToken": self._csrf,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": _IG + "/",
            "Accept": "*/*",
            "Cookie": self._cookie,
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                if "application/json" not in r.headers.get("Content-Type", ""):
                    raise SessionInvalid(
                        f"{path} returned non-JSON; the sessionid is dead")
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise SessionInvalid(f"{path} -> HTTP {e.code}") from e
            raise


# ── normalisation ───────────────────────────────────────────────────────────

def _published_at(m: dict) -> int:
    """taken_at is already unix seconds UTC on this API.

    device_timestamp sits next to it and is neither: microseconds, and on the
    poster's local clock. Never fall back to it.
    """
    ts = m.get("taken_at")
    if ts is None:
        raise ValueError(f"media {m.get('code')!r} has no taken_at")
    return int(ts)


def _largest(candidates: list[dict] | None) -> dict | None:
    """Biggest candidate that actually has a URL.

    Filtering on url first rather than indexing it later: a candidate block without
    one is unusable, and letting it win the max() would kill the whole batch on a
    KeyError for a single malformed entry.
    """
    usable = [c for c in candidates or [] if c.get("url")]
    if not usable:
        return None
    return max(usable, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))


def _image(node: dict) -> dict | None:
    return _largest((node.get("image_versions2") or {}).get("candidates"))


def _video(node: dict) -> dict | None:
    return _largest(node.get("video_versions"))


def _media_rows(m: dict) -> list[RawMedia]:
    """Vendor media shape -> media rows.

    Carousel slides keep their vendor order in `seq`; sync.persist replaces the
    whole set per item precisely because that order can change between fetches.
    """
    mt = m.get("media_type")

    if mt == _CAROUSEL:
        rows: list[RawMedia] = []
        for i, child in enumerate(m.get("carousel_media") or []):
            if child.get("media_type") == _VIDEO:
                v = _video(child)
                if v:
                    rows.append(RawMedia(role="slide", seq=i, remote_url=v["url"],
                                         mime="video/mp4", width=v.get("width"),
                                         height=v.get("height")))
                poster = _image(child)
                if poster:
                    rows.append(RawMedia(role="poster", seq=i,
                                         remote_url=poster["url"],
                                         mime="image/jpeg",
                                         width=poster.get("width"),
                                         height=poster.get("height")))
            else:
                img = _image(child)
                if img:
                    rows.append(RawMedia(role="slide", seq=i, remote_url=img["url"],
                                         mime="image/jpeg", width=img.get("width"),
                                         height=img.get("height")))
        return rows

    if mt == _VIDEO:
        rows = []
        v = _video(m)
        if v:
            rows.append(RawMedia(role="original", seq=0, remote_url=v["url"],
                                 mime="video/mp4", width=v.get("width"),
                                 height=v.get("height")))
        poster = _image(m)
        if poster:
            rows.append(RawMedia(role="poster", seq=0, remote_url=poster["url"],
                                 mime="image/jpeg", width=poster.get("width"),
                                 height=poster.get("height")))
        return rows

    img = _image(m)
    if img:
        return [RawMedia(role="original", seq=0, remote_url=img["url"],
                         mime="image/jpeg", width=img.get("width"),
                         height=img.get("height"))]
    return []


def _count(m: dict, key: str) -> int | float | None:
    """A counter, or None when the vendor is saying it has no number for us.

    Instagram signals "this count is not available to you" with -1 rather than by
    omitting the field, so a post with hidden likes otherwise reaches a client
    brief as minus one reaction. Absence is the honest answer; see the module
    docstring on why a fabricated number is worse than a gap.
    """
    v = m.get(key)
    if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
        return None
    return v


def _metrics(m: dict) -> dict[str, Any]:
    """Map the counters this route actually has.

    Absent keys are absent on purpose; see the module docstring. `views` prefers
    content_views_count because that is the counter Instagram now labels "views"
    in the app, and it is the only one present on non-video posts. play_count is
    kept alongside it (it rides to metrics_raw) so a later change in what IG counts
    as a view is visible rather than silently rewriting history.
    """
    out: dict[str, Any] = {}
    if (likes := _count(m, "like_count")) is not None:
        out["reactions"] = likes
    if (comments := _count(m, "comment_count")) is not None:
        out["comments"] = comments

    views = _count(m, "content_views_count")
    if views is None:
        views = _count(m, "play_count")
    if views is not None:
        out["views"] = views

    # media_repost_count is reposts-to-feed, NOT the Insights "shares" metric
    # (which also counts DM sends and story adds). It rides to metrics_raw under
    # its vendor name rather than into the shares column, because filling shares
    # with a strictly smaller number would understate reach and read as measured.
    for key in ("play_count", "ig_play_count", "media_repost_count"):
        if (v := _count(m, key)) is not None:
            out[key] = v

    # The owner can hide counts; when they do the numbers above are stale or
    # missing, and a reader of the snapshot needs to know that before charting it.
    if m.get("like_and_view_counts_disabled"):
        out["counts_hidden"] = True
    return out


def _item(m: dict) -> RawItem:
    code = m.get("code")
    if not code:
        raise ValueError(f"media {m.get('pk')!r} has no shortcode")

    mt = m.get("media_type")
    duration = m.get("video_duration")
    # A carousel reports media_type 8 even when it holds video, so has_video keys
    # on the top-level type only. Otherwise a mixed carousel classifies as video.
    slides = m.get("carousel_media_count") or len(m.get("carousel_media") or [])

    return RawItem(
        platform_item_id=code,
        kind=classify_kind(has_video=(mt == _VIDEO),
                           media_count=slides if mt == _CAROUSEL else 1,
                           duration_s=duration),
        published_at=_published_at(m),
        raw=m,
        body=(m.get("caption") or {}).get("text"),
        url=f"{_IG}/p/{code}/",
        duration_s=float(duration) if isinstance(duration, (int, float)) else None,
        metrics=_metrics(m),
        media=_media_rows(m),
    )


def _profile(node: dict) -> Profile:
    handle = node["username"]
    return Profile(
        handle=handle,
        display_name=node.get("full_name") or None,
        bio=node.get("biography") or None,
        url=f"{_IG}/{handle}/",
        platform_uid=str(node["id"]),
        follower_count=(node.get("edge_followed_by") or {}).get("count"),
        following_count=(node.get("edge_follow") or {}).get("count"),
        posts_total=(node.get("edge_owner_to_timeline_media") or {}).get("count"),
        raw=node,
    )


# ── routes ──────────────────────────────────────────────────────────────────

def _handle_of(target: str) -> str:
    """Accept a handle, an @handle, or a profile URL.

    Query and fragment go first, then the FIRST path segment wins: a URL pasted out
    of a browser carries both (?hl=en, /reels, /tagged), and taking the last segment
    would look up a profile called "reels" and then report the session as dead.
    """
    t = target.strip().split("?", 1)[0].split("#", 1)[0]
    if "instagram.com" in t:
        t = t.split("instagram.com", 1)[1]
    parts = [p for p in t.strip("/").split("/") if p]
    return parts[0].lstrip("@") if parts else ""


def _fetch_session(target, *, mode, keys, limit, since, transport=None):
    if transport is None:
        missing = [k for k in ("IG_SESSIONID", "IG_CSRFTOKEN", "IG_DS_USER_ID")
                   if not keys.get(k)]
        if missing:
            # Not declared in Adapter.needs: needs is static and this module has
            # two routes wanting different credentials, so a static list would
            # demand cookies from the token-based route.
            raise SessionInvalid(
                f"instagram_client[session]: missing {', '.join(missing)} "
                f"(expected in ~/.reach/keys.env)")
        transport = _WebSession(sessionid=keys["IG_SESSIONID"],
                                csrftoken=keys["IG_CSRFTOKEN"],
                                ds_user_id=keys["IG_DS_USER_ID"])

    handle = _handle_of(target)
    payload = transport.get("/api/v1/users/web_profile_info/", {"username": handle})
    node = (payload.get("data") or {}).get("user")
    if not node:
        raise SessionInvalid(f"no profile in web_profile_info for {handle!r}")
    profile = _profile(node)

    items: list[RawItem] = []
    seen_ids: set[str] = set()
    max_id = None
    more = False
    cut_page = False
    stopped_at_since = False

    while len(items) < limit:
        params: dict[str, Any] = {"count": min(_PAGE_SIZE, limit - len(items))}
        if max_id:
            params["max_id"] = max_id
        page = transport.get(f"/api/v1/feed/user/{node['id']}/", params)

        entries = page.get("items") or []
        taken = 0
        for entry in entries:
            item = _item(entry.get("media", entry))
            if since is not None and item.published_at < since:
                # The feed is newest-first, so the first item older than the
                # watermark ends the walk; everything past it is already stored.
                stopped_at_since = True
                break
            taken += 1
            if item.platform_item_id in seen_ids:
                # Pages overlap whenever a post lands mid-walk and the window
                # shifts under us, and a stuck cursor replays one wholesale. The
                # same id twice in one result makes scrape_run.items_seen a lie.
                continue
            seen_ids.add(item.platform_item_id)
            items.append(item)
            if len(items) >= limit:
                break

        # `count` is a request, not a contract: a page that hands back more entries
        # than the limit leaves room for loses the rest to this run, and it can do
        # that on the final page, where more_available is already False.
        cut_page = not stopped_at_since and taken < len(entries)

        more = bool(page.get("more_available") and page.get("next_max_id"))
        if stopped_at_since or not more or len(items) >= limit:
            break
        if page["next_max_id"] == max_id:
            # A cursor that does not advance replays the page it just served, which
            # would put the same item in one FetchResult twice and make items_seen
            # in scrape_run a lie.
            break
        max_id = page["next_max_id"]
        time.sleep(_PAGE_SLEEP_S)

    return FetchResult(
        items=items,
        profile=profile,
        cost_usd=0.0,
        # Stopping at `since` is a complete delta, not a truncation. Only the limit
        # cutting the walk short is, whether the feed said so or the page did.
        truncated=(more or cut_page) and not stopped_at_since and len(items) >= limit,
    )


def _fetch_graph(target, *, mode, keys, limit, since, transport=None):
    """Meta Graph API v26.0 route. Seam only, see the module docstring.

    Left unimplemented rather than written blind: no Business Portfolio token
    exists yet, so there is nothing to record a fixture from, and an untested
    normaliser against an API nobody has called is worse than an honest gap.
    Filling this in means writing the function and adding it to _ROUTES; the
    adapter's public shape does not move.
    """
    raise NotImplementedError(
        "instagram_client[graph]: needs a Business Portfolio system-user token "
        "with instagram_manage_insights (see module docstring for the setup). "
        "Use route='session' until then.")


_ROUTES: dict[str, Callable[..., FetchResult]] = {
    "session": _fetch_session,
    "graph": _fetch_graph,
}


def fetch(target: str, *, mode: str = "delta", keys: dict[str, str] | None = None,
          limit: int = 50, since: int | None = None, route: str = "session",
          transport=None, **kw) -> FetchResult:
    """Fetch one owned/consented Instagram account.

    target    : handle, @handle, or profile URL.
    mode      : advisory. This vendor has one media endpoint, so the real lever is
                `since`: pass the account's last watermark for a delta, omit it and
                raise `limit` for a backfill.
    since     : unix seconds; stop walking once the feed goes older than this.
    route     : which transport to use, see _ROUTES. Default stays 'session'
                deliberately rather than auto-selecting on a token being present in
                keys, so an unrelated Meta token landing in keys.env cannot silently
                reroute a working sync into a NotImplementedError.
    transport : injected HTTP client. The offline test seam; None means build one
                from `keys`.
    """
    try:
        impl = _ROUTES[route]
    except KeyError:
        raise ValueError(f"unknown route {route!r}, expected one of "
                         f"{sorted(_ROUTES)}") from None
    return impl(target, mode=mode, keys=keys or {}, limit=limit, since=since,
                transport=transport)


ADAPTER = Adapter(
    name="instagram_client",
    platform="instagram",
    fetch=fetch,
    relationships=("own", "client"),
    # Route-dependent, so validated inside fetch instead. See _fetch_session.
    needs=(),
    cost_per_1k=0.0,
    notes=("OPT-IN (CORPUS_ALLOW_IG_SESSION=1, attended runs only: session "
           "cookies on a timer got an account flagged). "
           "Session-cookie route: content, likes, comments, views, media, "
           "duration. No reach/saves/shares/follows/watch-time -- those need the "
           "Graph API route (see module docstring)."),
    unmeasured=("shares", "saves"),
    # See "OPT-IN ONLY" in the module docstring: an account was flagged twice.
    opt_in_env="CORPUS_ALLOW_IG_SESSION",
)
