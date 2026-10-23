"""LinkedIn public post permalinks, cookie-free and unbilled. A REFRESH engine.

WHAT THIS IS NOT. It is not a route that can find a person's posts. Every way in is
shut: `/in/<handle>/recent-activity/all/` is HTTP 999 unconditionally, and the bare
profile page 999s for 5 of 10 accounts probed -- including BOTH accounts already in
this corpus (the author's and a client's) and both public influencer profiles
tried. The four that did render leaked 3-11 of their own
posts out of 15-23 links. Search-engine fallback was worse: Exa returned 2/0/9 own
ids across three profiles, DuckDuckGo and Bing scraping returned none. So this
adapter takes POST IDS YOU ALREADY HOLD and re-reads them. Discovery stays on the
paid actor; `relationships=()` keeps this route out of every automatic sync plan
rather than letting cost_per_1k=0 float it to the head of the list where it would
fail on a profile URL once per account per run.

WHAT IT IS. A permalink answers cookie-free, from any User-Agent including none. Run
against all 53 LinkedIn posts already in the corpus and diffed field by field
against the paid route that put them there: body byte-exact 53/53 (a 2919-char post
came back whole, no "see more" truncation), published_at 53/53 to the second,
is_repost 53/53, true author 53/53, comment count 53/53, reaction count 50/53 with
all three diffs exactly +1 -- drift since the paid pull, not error. 29 of the 53 are
reposts. It also beats the paid route on two fields: video DURATION, which
harvestapi never reports at all, and the author's follower count.

WHAT IT GIVES UP. SHARES. Apify reports them, no public surface does, and the corpus
records absence as NULL -- so persisting a refresh through this route writes
m_shares=NULL over a share count the paid route had measured. The snapshot history
survives; the item row's current value does not. That is the one place this adapter
is strictly worse, and it is why `notes` says so.

RATE LIMIT, MEASURED, NOT GUESSED. 200 requests at up to 18.5 req/s were clean. The
first 429 landed at request 420 of a 600-request burst at 22.6 req/s, and it cleared
in 0.6s -- a burst throttle, not a ban. ~1500 requests in one day drew no daily
quota. Defaults here sit at 8 req/s, roughly a third of the observed break point.

THE TWO TRAPS. First, a permalink page also renders up to nine RELATED posts, each a
full card with its own data-num-reactions; every read here is scoped to the main
card's slice or it silently reports a stranger's engagement. Second, on a repost the
JSON-LD `interactionStatistic` counts the RESHARE's own reactions (0 on a live
example whose original had 12) while the DOM chip counts the ORIGINAL's, which is
what the paid route reports. The DOM chip wins for that reason; JSON-LD is a
last-resort fallback and only off a repost.
"""
from __future__ import annotations

import html as _html
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Iterable

from .base import Adapter, FetchResult, Profile, RawItem, RawMedia, classify_kind

# LinkedIn served 200 to `curl/8.4.0` and to an empty UA alike, so this is not a
# disguise -- it is a stable identity. Accept-Language is the one header that
# matters: the repost marker is the literal string "reposted this", and a page
# negotiated into another language would silently stop being detectable as a repost.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA,
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9"}

TIMEOUT_S = 25          # median page 0.62s, slowest of 53 was 2.15s

# One request per post, so the limit is a post count, not a page count.
DEFAULT_LIMIT = 200

# Measured clean: 1 worker 1.6 req/s, 4 workers 6.7, 8 workers 9.7, 16 workers 18.5.
# First 429 at 22.6 req/s. The cap, not the worker count, is what keeps us clear;
# workers only decide how quickly we reach the cap.
MAX_RPS = 8.0
WORKERS = 4

# The observed 429 cleared in 0.6s, so the first wait only has to outlast that.
RETRIES = 3
BACKOFF_S = 0.75

# LinkedIn's block status. It is the *normal* answer for a profile page and an
# abnormal one for a permalink, which is why the two are reported differently.
BLOCKED = 999

_ACTIVITY_RE = re.compile(
    r"(?:urn:li:activity:|[?&]activity[=:]|-activity-)(\d{6,})", re.I)
_BARE_ID_RE = re.compile(r"^\d{6,}$")
_PROFILE_RE = re.compile(r"linkedin\.com/(in|company|school|showcase)/([^/?#]+)", re.I)


# ── refs ────────────────────────────────────────────────────────────────────

def activity_id(ref: Any) -> str | None:
    """The bare activity id out of any shape LinkedIn hands you, or None.

    Accepts /feed/update/urn:li:activity:<id>, /posts/<slug>-activity-<id>-<hash>,
    a raw urn, and a bare id. Deliberately NOT a ugcPost or share urn: those live in
    a different id space, and linkedin_posts keys rows on the bare ACTIVITY id, so
    accepting a ugcPost id here would file the same post under a second row.
    """
    s = str(ref or "").strip()
    if not s:
        return None
    if _BARE_ID_RE.match(s):
        return s
    m = _ACTIVITY_RE.search(s)
    return m.group(1) if m else None


def permalink(aid: str) -> str:
    """The canonical form. The slug form works too, but its slug is generated from
    the post's first words and LinkedIn regenerates it on edit, so it is not a
    stable thing to store or re-request."""
    return f"https://www.linkedin.com/feed/update/urn:li:activity:{aid}/"


def _profile_ref(s: str) -> tuple[str, str] | None:
    m = _PROFILE_RE.search(str(s or ""))
    return (m.group(1).lower(), m.group(2)) if m else None


def _refs(target: Any, urls: Iterable[Any] | None) -> tuple[list[str], list[str]]:
    """(activity ids in first-seen order, strings that were not post refs).

    A string target is split on whitespace and commas so a caller can paste a
    newline-separated list, which is what a "re-read these" list actually looks like
    when it comes out of a shell or a SQL column.
    """
    raw: list[Any] = []
    if isinstance(target, (list, tuple, set)):
        raw += list(target)
    elif target:
        raw += re.split(r"[\s,]+", str(target).strip())
    if urls:
        raw += list(urls)

    ids, rejected = [], []
    for r in raw:
        aid = activity_id(r)
        if aid is None:
            if str(r).strip():
                rejected.append(str(r).strip())
        elif aid not in ids:
            ids.append(aid)
    return ids, rejected


# ── transport ───────────────────────────────────────────────────────────────

class _Throttle:
    """A shared floor on the interval between requests, across all workers.

    Worker count alone does not bound throughput -- a fast page returns in 0.2s and
    four workers then run at 20 req/s, past the measured break point. The floor does
    bound it, so the two knobs stay independent.
    """

    def __init__(self, rps: float):
        self._gap = 1.0 / rps if rps > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if not self._gap:
            return
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + self._gap
        if delay:
            time.sleep(delay)


def _get(url: str, throttle: _Throttle) -> tuple[int, str]:
    """One page, with backoff. Returns (status, body). Never raises for HTTP.

    A 429 is a burst throttle that cleared in 0.6s when measured, so it is retried
    rather than surfaced. Exhausting the retries returns the status: the caller drops
    that one post and marks the result truncated, because a run that fails wholesale
    on one throttled page is worse than a run that is honest about a gap.
    """
    last = -1
    for attempt in range(RETRIES + 1):
        throttle.wait()
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e.code
            try:
                e.read()
            except Exception:  # noqa: BLE001 - draining the socket is best effort
                pass
            if e.code not in (429, BLOCKED, 500, 502, 503, 504):
                return e.code, ""
            # Retry-After is honoured when sane; LinkedIn does not send one today,
            # but obeying it is the difference between backing off and being banned
            # the day it starts sending one.
            hinted = str(e.headers.get("Retry-After") or "").strip()
            wait = float(hinted) if hinted.isdigit() and int(hinted) <= 60 else \
                BACKOFF_S * (2 ** attempt)
        except Exception as e:  # noqa: BLE001 - network, DNS, timeout
            last = -1
            _ = e
            wait = BACKOFF_S * (2 ** attempt)
        if attempt < RETRIES:
            time.sleep(wait)
    return last, ""


# ── html ────────────────────────────────────────────────────────────────────

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>", re.S)


def _text(fragment: str | None) -> str | None:
    """Inner HTML to the text a reader sees.

    Mentions arrive as <a>Name</a> and must survive as their display name: the
    corpus stores what the post SAID, and dropping the anchor text would delete the
    person's name from the middle of a sentence. Verified byte-exact against the
    paid route on 53 of 53 posts.
    """
    if fragment is None:
        return None
    s = _COMMENT_RE.sub("", fragment)
    s = _BR_RE.sub("\n", s)
    s = _TAG_RE.sub("", s)
    return _html.unescape(s).strip() or None


def _attr(blob: str, name: str, *, near: str = "") -> str | None:
    """First value of `name` in `blob`, optionally only within a tag containing
    `near`. Cheap stand-in for a DOM query; the codebase carries no html parser and
    one attribute lookup does not justify one."""
    pat = (rf'<[^>]*{re.escape(near)}[^>]*\b{re.escape(name)}="([^"]*)"' if near
           else rf'\b{re.escape(name)}="([^"]*)"')
    m = re.search(pat, blob, re.S)
    return m.group(1) if m else None


_MAIN_CARD_RE = re.compile(
    r'<article\b[^>]*\bclass="[^"]*\bmain-feed-activity-card\b[^"]*"[^>]*>', re.I)


def main_card(page: str) -> str | None:
    """The slice of HTML belonging to the post that was requested.

    A permalink page appends up to nine RELATED posts, each rendered as a full
    main-feed-activity-card carrying its own data-num-reactions and its own author.
    On one live page that meant nine reaction counts and four comment counts in the
    document for a single post. Unscoped extraction does not error there, it reports
    a stranger's numbers, so every field below reads out of this slice only.
    """
    starts = [m for m in _MAIN_CARD_RE.finditer(page)
              if "related-posts__crosslink" not in m.group(0)]
    if not starts:
        return None
    begin = starts[0].start()
    after = _MAIN_CARD_RE.search(page, starts[0].end())
    return page[begin:after.start() if after else len(page)]


def json_ld(page: str, aid: str | None = None) -> dict | None:
    """LinkedIn's own structured record of the post, verbatim.

    Missing on 14 of 53 pages -- every one of them a repost -- so it is a bonus
    layer, never the floor. When several blocks exist the one whose @id names this
    activity wins, because a related post's block would otherwise overwrite the
    subject's body, date and author with someone else's.
    """
    blocks = []
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>',
                         page, re.S):
        try:
            d = json.loads(m.group(1))
        except Exception:  # noqa: BLE001 - a malformed block is not a failed fetch
            continue
        if isinstance(d, dict) and "@graph" in d:
            blocks += [x for x in (d.get("@graph") or []) if isinstance(x, dict)]
        elif isinstance(d, dict):
            blocks.append(d)
    if not blocks:
        return None
    if aid:
        for b in blocks:
            if aid in str(b.get("@id") or ""):
                return b
    return blocks[0]


# ── field extraction ────────────────────────────────────────────────────────

def _snowflake_epoch(aid: str) -> int | None:
    """Publish time straight out of the activity id.

    LinkedIn activity ids are snowflakes: id >> 22 is epoch milliseconds. Checked
    against all 53 stored posts and the maximum error was ZERO seconds, reposts
    included (a reshare mints its own id at reshare time, so the id decodes to the
    reshare, which is exactly the instant the corpus records).

    This is the primary rather than the fallback because JSON-LD -- the only other
    date on the page -- is absent on 14 of 53 pages, and the DOM's only other
    timestamp is a rounded "3y".
    """
    try:
        ms = int(aid) >> 22
    except (TypeError, ValueError):
        return None
    return int(ms // 1000) if 1e12 < ms < 4e12 else None


def _iso_epoch(value: Any) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


_DUR_RE = re.compile(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?$", re.I)


def _duration_s(value: Any) -> float | None:
    """ISO8601 "PT3M48S" to seconds. The paid actor reports no duration at all, so
    this is the one field where the free route is strictly richer -- and it is what
    decides short_video vs long_video instead of defaulting everything to short."""
    m = _DUR_RE.match(str(value or "").strip())
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = (float(g) if g else 0.0 for g in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


_FOLLOWERS_RE = re.compile(r"^([\d,.\s]+)\s+followers", re.I)
_LABEL_PREFIX_RE = re.compile(r"^View (?:profile|organization page) for\s+", re.I)


def _lockup(card: str, kind: str) -> tuple[str | None, str | None, str | None]:
    """(display name, handle, subtitle line) from an entity lockup block.

    kind is 'main-feed-activity-card__entity-lockup' or
    'feed-reshare-content__entity-lockup'. The name comes off the anchor tagged
    feed-actor-NAME specifically: a lockup opens with the actor IMAGE anchor, which
    carries the same href and an empty body, so matching on href alone yields a name
    of None and then falls through to an aria-label that reads "View organization
    page for X" -- which is how a company's name ends up stored with a five-word
    prefix.

    The subtitle is a person's headline or a company's "4,216 followers" line, the
    same overloaded field the paid actor exposes as `info`.
    """
    i = card.find(kind)
    if i < 0:
        return (None, None, None)
    blob = card[i:i + 4000]
    m = re.search(r'<a\b[^>]*feed-actor-name[^>]*>(.*?)</a>', blob, re.S)
    name = _text(m.group(1)) if m else None
    if not name:
        lbl = _attr(blob, "aria-label", near="feed-actor-name") or ""
        name = _LABEL_PREFIX_RE.sub("", lbl).strip() or None

    href = re.search(
        r'href="([^"]*linkedin\.com/(?:in|company|school|showcase)/[^"?]+)', blob)
    ref = _profile_ref(href.group(1)) if href else None

    sub = re.search(r'<p class="![^"]*truncate"[^>]*>(.*?)</p>', blob, re.S)
    return (name, ref[1] if ref else None, _text(sub.group(1)) if sub else None)


def _followers_line(info: Any) -> int | None:
    """Company lockups put "4,216 followers" where people put a headline.

    This is the only follower source on the 14 of 53 pages that carry no JSON-LD.
    """
    m = _FOLLOWERS_RE.match(str(info or "").strip())
    return int(re.sub(r"\D", "", m.group(1))) if m else None


def _ld_author(ld: Any) -> dict | None:
    """The author block JSON-LD carries, whatever it calls it.

    SocialMediaPosting says `author`, VideoObject says `creator`. On a repost this
    is the RESHARER, not the true author, which is why it is only ever a fallback
    for what the DOM lockup already said.
    """
    if not isinstance(ld, dict):
        return None
    for key in ("author", "creator"):
        if isinstance(ld.get(key), dict):
            return ld[key]
    return None


def _follow_count(node: Any) -> int | None:
    """Follower count off a JSON-LD author/creator block. Matched the stored 706."""
    if not isinstance(node, dict):
        return None
    stat = node.get("interactionStatistic")
    for s in (stat if isinstance(stat, list) else [stat]):
        kind = str(s.get("interactionType") or "") if isinstance(s, dict) else ""
        if "FollowAction" in kind:
            n = s.get("userInteractionCount")
            if isinstance(n, (int, float)) and not isinstance(n, bool):
                return int(n)
    return None


def _counts(card: str) -> tuple[int | None, int | None, list[str]]:
    """(reactions, comments, top reaction types) from the main card's social bar.

    LinkedIn omits a chip entirely when its count is zero -- it does not render a 0.
    Absence therefore means zero, but only once the social bar itself has rendered;
    without that guard a page that failed to render its footer would be recorded as
    a post with no engagement. Verified against the paid route on 25 posts whose
    comment chip was missing and whose true count was 0, and on the one post whose
    reaction chip was missing and whose true count was 0.
    """
    if "social-actions" not in card:
        return (None, None, [])
    r = _attr(card, "data-num-reactions", near="social-actions__reactions")
    c = _attr(card, "data-num-comments", near="social-actions__comments")
    types = re.findall(r'data-reaction-type="([A-Z]+)"', card)
    return (int(r) if r and r.isdigit() else 0,
            int(c) if c and c.isdigit() else 0,
            list(dict.fromkeys(types)))


def _video(region: str, ld: dict | None, *, allow_ld: bool
           ) -> tuple[str | None, str | None, int | None, int | None]:
    """(mp4 url, poster url, width, height) for a native video post.

    data-sources is a JSON array of renditions; the highest bitrate is taken because
    transcription quality tracks the audio and the corpus keeps only a small opus
    copy of it afterwards. All six videos probed downloaded cookie-free (HTTP 206,
    video/mp4).

    allow_ld is False whenever `region` is a trimmed slice of the card, because
    JSON-LD describes the WHOLE page: on a quote reshare of a video it would hand
    back the reshared post's mp4 as if this post carried one.
    """
    src = poster = None
    raw = _attr(region, "data-sources", near="share-native-video__node")
    if raw:
        try:
            rends = json.loads(_html.unescape(raw))
        except Exception:  # noqa: BLE001 - a mangled attribute is not a failed fetch
            rends = []
        best = None
        for r in rends if isinstance(rends, list) else []:
            if isinstance(r, dict) and r.get("src"):
                rate = r.get("data-bitrate") or 0
                if best is None or rate > (best.get("data-bitrate") or 0):
                    best = r
        src = best.get("src") if best else None
        poster = _attr(region, "data-poster-url", near="share-native-video__node")

    ld = ld if isinstance(ld, dict) and allow_ld else {}
    if not src and str(ld.get("@type")) == "VideoObject":
        src = ld.get("contentUrl") or None
    if src and not poster and isinstance(ld.get("thumbnailUrl"), str):
        poster = ld["thumbnailUrl"]
    w, h = ld.get("width"), ld.get("height")
    return (src, poster if src else None,
            int(w) if isinstance(w, int) and src else None,
            int(h) if isinstance(h, int) and src else None)


def _images(region: str) -> list[str]:
    """Post images in render order.

    DOM only. JSON-LD's `image` looks like the same thing and is not: on a link
    share it holds the LINK CARD's thumbnail, and on a quote reshare it holds the
    RESHARED post's images. Falling back to it turned seven text posts into
    image/carousel posts when checked against the paid route, so there is no
    fallback -- a post with no rendered image block has no images.
    """
    urls = []
    for m in re.finditer(r'data-test-id="feed-images-content__list-item"(.*?)</li>',
                         region, re.S):
        u = _attr(m.group(1), "data-delayed-url") or _attr(m.group(1), "src")
        if u and "media.licdn.com" in u:
            urls.append(_html.unescape(u))
    return list(dict.fromkeys(urls))


def _link_card(region: str) -> tuple[str | None, str | None, str | None]:
    """(headline, thumbnail url, destination) of a shared link's preview card.

    Same treatment the paid route gives `article{}`: the headline is content the
    post displays and worth having in FTS, but the card image is a thumbnail, never
    the post's own creative, so it must not count toward the image/carousel
    decision. The href is LinkedIn's /redir/redirect wrapper, unwrapped so a native
    Pulse piece is still recognisable as one.
    """
    m = re.search(r'<a\b[^>]*data-test-id="article-content"(.*?)</a>', region, re.S)
    if not m:
        return (None, None, None)
    blob = m.group(0)
    title = _text(re.search(r'data-test-id="article-content__title"[^>]*>(.*?)</span>',
                            blob, re.S).group(1)) \
        if 'article-content__title' in blob else None
    thumb = _attr(blob, "data-delayed-url")
    href = _attr(blob, "href") or ""
    dest = urllib.parse.unquote(_html.unescape(href))
    if "url=" in dest:
        dest = urllib.parse.unquote(dest.split("url=", 1)[1].split("&")[0])
    return (title, _html.unescape(thumb) if thumb else None, dest or None)


def _comments(ld: dict | None, card: str) -> list[dict]:
    """Comment bodies, capped by LinkedIn at 10 and usually far below it.

    A post with 804 comments still returned 10, and small posts returned 3 of 6.
    They are kept as evidence, not as a census: any ratio computed from them would
    be computed from an arbitrary top-N slice.
    """
    out = []
    for c in ((ld or {}).get("comment") or []):
        if not isinstance(c, dict):
            continue
        out.append({"author": (c.get("creator") or {}).get("name"),
                    "text": c.get("text"),
                    "published_at": _iso_epoch(c.get("datePublished"))})
    if out:
        return out
    for m in re.finditer(
            r'<section[^>]*class="[^"]*\bcomment\b[^"]*"[^>]*>(.*?)</section>',
            card, re.S):
        blk = m.group(1)
        a = re.search(r'comment__author\b[^>]*>(.*?)</a>', blk, re.S)
        b = re.search(r'class="[^"]*comment__text[^"]*"[^>]*>(.*?)</p>', blk, re.S)
        if b:
            out.append({"author": _text(a.group(1)) if a else None,
                        "text": _text(b.group(1)), "published_at": None})
    return out


def _commentary(card: str, test_id: str) -> str | None:
    m = re.search(rf'data-test-id="{test_id}"[^>]*>(.*?)</p>', card, re.S)
    return _text(m.group(1)) if m else None


# ── page -> RawItem ─────────────────────────────────────────────────────────

def parse(page: str, *, aid: str, url: str | None = None,
          captions: bool = False) -> RawItem | None:
    """One fetched permalink to a RawItem. None when the main card did not render.

    Returning None rather than a half-item is deliberate: published_at is NOT NULL
    and every count on this page belongs to some post, so a page whose main card is
    missing would still hand back plausible-looking numbers that belong to a related
    post. The caller counts the drop and marks the result truncated.
    """
    card = main_card(page)
    if card is None:
        return None

    ld = json_ld(page, aid) or {}
    canonical = _attr(page, "href", near='rel="canonical"') or url or permalink(aid)
    owner = _profile_ref(canonical)

    # A bare reshare renders the ORIGINAL's card under a "<name> reposted this"
    # header; a reshare WITH commentary renders the resharer's own card with the
    # original nested underneath. The paid actor calls only the first a repost
    # (repostedBy is set) and treats the second as an original post by the
    # commenter, so the same split is reproduced here or the two routes would
    # disagree about is_repost on the same row.
    header = _commentary(card, "main-feed-activity-card__header") or ""
    if not header:
        m = re.search(
            r'class="[^"]*main-feed-activity-card__header[^"]*"[^>]*>(.*?)</p>',
            card, re.S)
        header = _text(m.group(1)) or "" if m else ""
    own_body = _commentary(card, "main-feed-activity-card__commentary")
    reshare_body = _commentary(card, "feed-reshare-content__commentary")
    is_repost = "reposted this" in header.lower()

    # The nested reshare card is a second post rendered inside this one, with its
    # own images. Which of the two owns the media depends on whether this post said
    # anything itself: a quote reshare's media block belongs to the post being
    # quoted (four such posts came back as image/carousel against the paid route's
    # `text` before this scope existed), while a bare reshare has no media of its
    # own and the nested card IS the content. Counts are NOT scoped this way -- the
    # social bar is the parent's and sits outside the nested article either way.
    reshare_at = card.find("feed-reshare-content")
    quoting = reshare_at >= 0 and own_body is not None
    region = card[:reshare_at] if quoting else card

    if is_repost and reshare_body is not None and own_body is None:
        # The nested card is the post; its author is the true author.
        author_name, author_handle, info = _lockup(
            card, "feed-reshare-content__entity-lockup")
        body = reshare_body
    else:
        author_name, author_handle, info = _lockup(
            card, "main-feed-activity-card__entity-lockup")
        body = own_body if own_body is not None else reshare_body

    if not author_name:
        node = _ld_author(ld)
        if node:
            author_name = node.get("name") or None
            ref = _profile_ref(str(node.get("url") or ""))
            author_handle = author_handle or (ref[1] if ref else None)

    # A permalink can resolve to a card whose author is not the account that owns
    # the URL. That IS a repost even when the header did not render -- verified on a
    # live post the paid route also flags as a repost, whose page carries no reshare
    # block and no JSON-LD at all.
    if owner and author_handle and owner[1].lower() != author_handle.lower():
        is_repost = True

    published_at = _snowflake_epoch(aid) or _iso_epoch(ld.get("datePublished"))
    if published_at is None:
        return None

    reactions, comments, reaction_types = _counts(card)
    ld_comment_n = ld.get("commentCount")
    if comments == 0 and isinstance(ld_comment_n, int) and ld_comment_n > 0:
        comments = ld_comment_n

    vid, poster, vw, vh = _video(region, ld, allow_ld=not quoting)
    images = _images(region)
    card_title, card_thumb, card_link = _link_card(region)
    duration = _duration_s(ld.get("duration"))

    media: list[RawMedia] = []
    if vid:
        media.append(RawMedia(role="original", remote_url=vid, mime="video/mp4",
                              width=vw, height=vh))
    if poster:
        media.append(RawMedia(role="poster", remote_url=poster, mime="image/jpeg"))
    # enrich picks a video by role='original', so a still may only claim that role
    # when it is the whole post. Same rule as linkedin_posts.
    still_role = "slide" if (media or len(images) > 1) else "original"
    for i, u in enumerate(images):
        media.append(RawMedia(role=still_role, seq=i, remote_url=u, mime="image/jpeg"))
    if card_thumb:
        media.append(RawMedia(role="thumbnail", remote_url=card_thumb,
                              mime="image/jpeg"))

    comment_bodies = _comments(ld, card)
    metrics: dict[str, Any] = {"reactions": reactions, "comments": comments}
    if reaction_types:
        # Deliberately NOT named reaction_types: linkedin_posts stores a COUNTED
        # breakdown under that key and this is an unordered top-3 with no counts.
        # One key holding two different things is how a chart starts lying.
        metrics["reaction_types_top"] = reaction_types
    if comment_bodies:
        metrics["comment_sample"] = comment_bodies
    metrics = {k: v for k, v in metrics.items() if v is not None}

    # LinkedIn's own auto-caption. It is NOT a transcript: on the one non-English
    # video probed it rendered Croatian speech as nonsense German and Dutch, and it
    # was present on only 2 of 6 videos. Writing it into `transcript` would be
    # permanent damage, because enrich selects "transcript IS NULL OR ''" -- a
    # garbage caption there means the video is never really transcribed. It stays in
    # raw for anyone who wants it and only reaches the column on explicit opt-in.
    caption_text = ld.get("transcript")
    caption_text = caption_text if isinstance(caption_text, str) else None

    # A native Pulse piece or newsletter issue IS an article; a post that merely
    # shares a link is still a text post that happens to display a headline. Same
    # split linkedin_posts makes, so the two routes classify one post one way.
    article = "linkedin.com/pulse" in f"{card_link} {ld.get('@id') or ''}".lower()
    return RawItem(
        platform_item_id=aid,
        kind=classify_kind(has_video=bool(vid), media_count=len(images),
                           duration_s=duration, has_title=article),
        published_at=published_at,
        # The vendor payload as received is the JSON-LD block; `dom` carries the
        # facts the DOM alone holds. The full page is NOT stored: at ~110 KB a post
        # it would be 30x the paid route's raw and it adds nothing that is not
        # already projected here. html_bytes is enough to spot a page that changed.
        raw={"ld": ld or None,
             "dom": {"header": header or None,
                     "reshare_activity_urn": _attr(card, "data-activity-urn",
                                                   near="feed-reshare-content"),
                     "activity_urn": _attr(card, "data-activity-urn"),
                     "author_info": info,
                     "reactions": reactions, "comments": comments,
                     "reaction_types_top": reaction_types,
                     "comment_sample": comment_bodies,
                     "video_url": vid, "image_urls": images,
                     "link_card": {"title": card_title, "image": card_thumb,
                                   "url": card_link} if card_link else None,
                     "auto_caption": caption_text},
             "_source": {"adapter": "linkedin_public", "url": canonical,
                         "html_bytes": len(page)}},
        title=card_title,
        body=body,
        transcript=caption_text if captions else None,
        url=canonical,
        duration_s=duration,
        is_repost=is_repost,
        author_name=author_name,
        author_handle=author_handle,
        metrics=metrics,
        media=media,
    )


def _profile_for(handle: str, seg: str, items: list[RawItem]) -> Profile | None:
    """Account facts harvested from the pages, for the requested handle only.

    The match is the same guard the other adapters carry: on a repost the author is
    a stranger, persist() upserts Profile.handle as an account row, and taking the
    first author would file that stranger's follower count under this account and
    branch the corpus in two.

    A profile is only returned once a follower count was actually found, because
    persist() appends an account metric snapshot unconditionally: a Profile with
    follower_count=None writes NULL into the time series, which reads as a collapse
    to zero rather than as "not measured".
    """
    for it in items:
        raw = it.raw or {}
        ld = raw.get("ld") or {}
        node = _ld_author(ld)
        ref = _profile_ref(str(node.get("url") or "")) if node else None

        if ref and ref[1].lower() == handle.lower():
            followers = _follow_count(node)
            if followers is not None:
                return Profile(handle=ref[1], display_name=node.get("name") or None,
                               url=f"https://www.linkedin.com/{ref[0]}/{ref[1]}/",
                               follower_count=followers, raw=node)

        # No JSON-LD on this page (14 of 53 were reposts without one), but the
        # lockup subtitle carries a company's follower line.
        if (it.author_handle or "").lower() != handle.lower():
            continue
        info = (raw.get("dom") or {}).get("author_info")
        followers = _followers_line(info)
        if followers is not None:
            return Profile(
                handle=it.author_handle, display_name=it.author_name,
                url=f"https://www.linkedin.com/{seg}/{it.author_handle}/",
                follower_count=followers,
                # People carry a headline where companies carry the follower line.
                # Storing "4,216 followers" as a bio is worse than storing nothing.
                bio=None, raw={"info": info})
    return None


# ── adapter ─────────────────────────────────────────────────────────────────

def fetch(target: Any = None, *, mode: str = "delta", keys: dict | None = None,
          limit: int = DEFAULT_LIMIT, since: Any = None,
          urls: Iterable[Any] | None = None,
          workers: int = WORKERS, max_rps: float = MAX_RPS,
          captions: bool = False, **kw) -> FetchResult:
    """Re-read posts you already have ids for. Free, cookie-free, no key.

    `target` and `urls` are pooled: either may hold permalinks, urns or bare
    activity ids, and `target` may also be the profile URL of the account being
    refreshed, in which case it only supplies the account context for the returned
    Profile. It cannot supply the POSTS -- see the module docstring for why.

    `since` filters client-side, unlike the paid route where it is a server-side
    floor. Here there is nothing to save by pushing it down: the publish time is
    only known once the page is in hand.
    """
    _ = keys, mode          # takes no key; mode changes nothing about a re-read
    ids, rejected = _refs(target, urls)

    if not ids:
        profiles = [r for r in rejected if _profile_ref(r)]
        if profiles:
            raise RuntimeError(
                f"linkedin_public cannot enumerate {profiles[0]}: "
                "/recent-activity/all/ is HTTP 999 always and the profile page is 999 "
                "for most accounts. Pass post permalinks or activity ids (urls=[...]); "
                "use linkedin_posts to DISCOVER posts.")
        raise RuntimeError(
            "linkedin_public needs post permalinks, urns or activity ids; "
            f"got nothing usable in {rejected[:3] or target!r}")

    limit = max(1, int(limit))
    wanted, over = ids[:limit], len(ids) > limit

    throttle = _Throttle(max_rps)
    pages: dict[str, tuple[int, str]] = {}
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        for aid, (status, body) in zip(
                wanted, ex.map(lambda a: _get(permalink(a), throttle), wanted)):
            pages[aid] = (status, body)

    items, failed = [], []
    for aid in wanted:
        status, body = pages[aid]
        if status != 200 or not body:
            failed.append((aid, status))
            continue
        it = parse(body, aid=aid, url=permalink(aid), captions=captions)
        if it is None:
            failed.append((aid, "unparsed"))
            continue
        if since is not None and it.published_at < int(since):
            continue
        items.append(it)

    if wanted and not items:
        # Every requested permalink failing at once is a block or a markup change,
        # not a quiet week. It has to be loud, because a run logged as ok with 0
        # items reads downstream as "these posts are gone".
        raise RuntimeError(
            f"linkedin_public got nothing usable from {len(wanted)} permalink(s) "
            f"(statuses: {sorted({str(s) for _, s in failed})}); "
            "LinkedIn markup change, or blocked")

    items.sort(key=lambda i: i.published_at, reverse=True)

    prof = None
    ref = _profile_ref(str(target or ""))
    if ref:
        prof = _profile_for(ref[1], ref[0], items)

    return FetchResult(
        items=items,
        profile=prof,
        cost_usd=0.0,
        # Both meanings of incomplete: more ids than `limit` allowed, and ids that
        # were asked for but did not come back. Either way the caller did not get
        # everything it named, and that must not read as a clean full pull.
        truncated=bool(over or failed),
    )


ADAPTER = Adapter(
    name="linkedin_public",
    platform="linkedin",
    fetch=fetch,
    # Empty ON PURPOSE, and a stronger statement than instagram_free's
    # shallow=True. That route is a real route that stops early; this one does not
    # take the input a route is handed. cmd_sync passes a PROFILE URL, and there is
    # no depth setting at which a profile URL yields a post here -- it raises. An
    # adapter listed as serving 'competitor' that fails on every competitor is worse
    # than one that is not listed, so it is reachable only by name:
    # adapters.get("linkedin_public").
    relationships=(),
    needs=(),
    cost_per_1k=0.0,
    # True by construction as well: it never reaches the end of an account, because
    # it never starts from one. Set so that adding a relationship later cannot
    # accidentally float a $0.00 route to the head of a backfill plan.
    shallow=True,
    notes="free cookie-free permalinks; REFRESH ONLY -- cannot discover posts "
          "(profile pages are HTTP 999), so pass ids you already hold. Adds video "
          "duration and follower count the paid actor lacks; reports NO shares (the "
          "stored count is kept, see Adapter.unmeasured). Not auto-selected: "
          "adapters.get('linkedin_public').",
    unmeasured=("views", "shares", "saves"),
)
