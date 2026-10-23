"""LinkedIn Analytics export (.xlsx) -- the consented client route.

This is the only LinkedIn source that carries impressions, reach and follower
demographics. Those numbers exist nowhere in the public HTML a scraper can see,
so the export is not a cheaper substitute for scraping, it is a different and
strictly richer signal. The price is that the account owner has to hand the file
over, which is exactly why this adapter is restricted to own/client accounts.

The parser is deliberately paranoid. LinkedIn ships this file from a reporting
surface that gets redesigned without notice: sheets get renamed, preamble rows
appear above headers, and TOP POSTS is two side-by-side tables whose header
casing already differs between exports seen in the wild. So every sheet and every
column is found by fuzzy name, never by position, and anything missing degrades
to a note instead of an exception. A partial import of a file the client had to
be asked for beats a clean traceback.

target is a filesystem path, not a URL: one export file, or a folder (e.g.
~/Downloads) in which the newest recognised export is used.

Four shapes are read, recognised by CONTENT rather than filename (people rename
files, and LinkedIn renamed these twice in 2026):

  * personal content export: DISCOVERY / ENGAGEMENT / TOP POSTS / FOLLOWERS /
    DEMOGRAPHICS. Named Content_{from}_{to}_{ProfileName}.xlsx until mid-2026,
    AggregateAnalytics_{Profile Name}_{from}_{to}.xlsx since (spaces in the
    name, and TOP POSTS now links /posts/ permalinks instead of /feed/ ones).
  * per-post export: SinglePostAnalytics_{Profile Name}_{id}.xlsx, one "Post
    analytics" sheet of label/value rows (Post URL, Post Date, Impressions, ...)
    followed by a "Post viewer demographics" table; formerly
    PostAnalytics_{ProfileName}_{id}.xlsx with PERFORMANCE + TOP DEMOGRAPHICS.
    The new file carries no analytics date range.
  * company Page content export: {page}_content_{ms}.xls, sheets "Metrics"
    (daily totals) and "All posts" (banner row, then Post title / Post link /
    Post type / Posted by / Created date / Impressions / Clicks / Likes /
    Comments / Reposts / Follows / Engagement rate / Content Type). The only
    export that carries the post TEXT ("Post title" holds the whole post).
  * CSV: the same Page "All posts" table, or per-post label/value rows.

Verified against: the filename patterns and the per-post sheet layout from an
open-source pipeline written against real 2026 exports (KunojiLym/
databricks-linkedin-analytics, PR #6) and the author's change notes; the Page
"All posts" columns from three independent public parsers; LinkedIn Help (Page
exports are .xls). NOT verified against a real file: a personal per-post export
as CSV (LinkedIn's help documents only .xlsx for it), so that path is driven
purely by detecting the label/value layout and says so in the import notes.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from .base import (Adapter, FetchResult, Profile, RawItem, classify_kind,
                   stable_id)

# ── vendor vocabulary ───────────────────────────────────────────────────────
# Everything LinkedIn-specific lives in these tables. Supporting a renamed sheet
# or a newly added column is meant to be a dict edit, not a rewrite.

# canonical sheet -> substrings to look for in the sheet title, most specific
# first. Resolution is ordered and one-shot: "TOP POSTS" is claimed by top_posts
# before the looser "posts" token can steal it for something else.
_SHEETS: dict[str, tuple[str, ...]] = {
    "discovery": ("discovery",),
    "engagement": ("engagement",),
    "top_posts": ("top posts", "topposts", "top post", "posts"),
    "followers": ("followers", "follower"),
    "demographics": ("demographics", "demographic", "audience"),
}

# header substring -> canonical metric name. Ordered: the first match wins, so
# the specific phrases have to precede the words they contain.
_METRIC_HEADERS: tuple[tuple[str, str], ...] = (
    ("members reached", "reach"),
    ("member reached", "reach"),
    ("unique members", "reach"),
    ("reach", "reach"),
    ("impression", "impressions"),
    ("engagement", "engagements"),
    ("reaction", "reactions"),
    ("like", "reactions"),          # the Page export says "Likes"
    ("comment", "comments"),
    ("repost", "shares"),
    ("reshare", "shares"),
    ("share", "shares"),
    ("save", "saves"),
    ("video view", "views"),
    ("view", "views"),
    ("click", "clicks"),
    ("follow", "follows"),
)
# Derived columns are not counts. Letting "engagement rate" match "engagement"
# would write a ratio into a counter and poison every trend built on it.
_METRIC_SKIP = ("rate", "%", "percent", "average", "avg", "per ",
                # "Profile viewers from this post" is not post views.
                "profile view")
# Off-LinkedIn video plays (Page export) are not LinkedIn views; merged into
# "views" they would replace the on-platform number. Prefix only: the on-platform
# column is "Views (excluding off-site video views)".
_METRIC_SKIP_PREFIX = ("off-site", "offsite")

_URL_HEADERS = ("post url", "post link", "post_url", "content url")
_DATE_HEADERS = ("date", "published", "publish")

# The export has no media or title columns at all, so the URL shape is the only
# surviving signal for kind. Pulse is LinkedIn's long-form surface; the video
# permalink forms are rarer but do show up. /events/ is deliberately NOT here: an
# event page is a page, and a LinkedIn Live that happens to sit behind one is not
# worth guessing "video" for every event announcement in the file.
_ARTICLE_URL = ("/pulse/", "/article/")
_VIDEO_URL = ("/video/", "videoplay")

# LinkedIn caps each TOP POSTS table at 50 rows. Hitting the cap means the file
# is a sample of the account, not the account.
_TOP_POSTS_CAP = 50

ALLOWED_RELATIONSHIPS = ("own", "client")

_ACTIVITY_RE = re.compile(r"activity[:\-](\d{6,})")
_URN_RE = re.compile(r"urn:li:(ugcPost|share):(\d{6,})")
# /posts/ permalinks for some posts carry the ugcPost id instead of the activity
# id: "..._slug-ugcPost-7469...-abcd". Same id space as the urn form.
_UGC_SLUG_RE = re.compile(r"-(ugcPost|share)-(\d{6,})")
_VANITY_RE = re.compile(r"linkedin\.com/(?:posts|in)/([A-Za-z0-9\-%_.]+?)(?:_|/|$)")

# Excel's day 0. The 1900 leap-year bug means day 1 is 1900-01-01, so the anchor
# sits two days before that rather than one.
_EXCEL_EPOCH = dt.datetime(1899, 12, 30, tzinfo=dt.timezone.utc)

_DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%d/%m/%Y", "%m-%d-%Y",
                 "%Y/%m/%d", "%b %d, %Y", "%d %b %Y")


# ── cell helpers ────────────────────────────────────────────────────────────

def _norm(cell: Any) -> str:
    """Header text reduced to something matchable: lowercase, single-spaced."""
    if cell is None:
        return ""
    return re.sub(r"\s+", " ", str(cell)).strip().strip(":*").lower()


def _to_int(cell: Any) -> int | None:
    if cell is None or isinstance(cell, bool):
        return None
    if isinstance(cell, (int, float)):
        return int(cell)
    txt = str(cell).replace(",", "").replace(" ", "").strip()
    if not txt:
        return None
    try:
        return int(float(txt))
    except ValueError:
        return None


def _to_pct(cell: Any) -> float | None:
    if cell is None or isinstance(cell, bool):
        return None
    if isinstance(cell, (int, float)):
        return float(cell)
    txt = str(cell).replace("%", "").replace(",", "").strip()
    if not txt:
        return None
    # LinkedIn suppresses small demographic slices as "< 1%". Half the bound is a
    # midpoint estimate, not a reported figure; the untouched cell is kept beside
    # it so a reader can tell the two apart.
    if txt.startswith("<"):
        rest = _to_pct(txt.lstrip("< ").strip())
        return None if rest is None else rest / 2
    try:
        return float(txt)
    except ValueError:
        return None


def _is_suppressed(cell: Any) -> bool:
    return isinstance(cell, str) and cell.strip().startswith("<")


def _to_epoch(cell: Any) -> int | None:
    """Normalise whatever a date cell holds into unix seconds UTC.

    The export gives dates three ways depending on cell formatting: a real
    datetime, an Excel serial number, or a locale-formatted string. Nothing in
    the file states a timezone, so naive values are read as UTC midnight; the
    day is trustworthy, the hour is not, and no downstream question this corpus
    answers turns on the hour.
    """
    if cell is None or isinstance(cell, bool):
        return None
    if isinstance(cell, dt.datetime):
        if cell.tzinfo is None:
            cell = cell.replace(tzinfo=dt.timezone.utc)
        return int(cell.timestamp())
    if isinstance(cell, dt.date):
        return int(dt.datetime(cell.year, cell.month, cell.day,
                               tzinfo=dt.timezone.utc).timestamp())
    if isinstance(cell, (int, float)):
        # Anything outside 1900..2170 is a count that landed in a date column.
        if not 1 <= float(cell) <= 100_000:
            return None
        return int((_EXCEL_EPOCH + dt.timedelta(days=float(cell))).timestamp())

    txt = str(cell).strip()
    if not txt:
        return None
    # Drop a trailing clock: "3/8/2025 4:15 PM" -> "3/8/2025".
    head = txt.split(" ")[0] if re.match(r"^\d", txt) else txt
    for fmt in _DATE_FORMATS:
        for candidate in (txt, head):
            try:
                d = dt.datetime.strptime(candidate, fmt)
            except ValueError:
                continue
            return int(d.replace(tzinfo=dt.timezone.utc).timestamp())
    return None


def _jsonable(value: Any) -> Any:
    """raw_json goes through json.dumps, and openpyxl hands back date objects.

    persist() is deliberately NOT guarded by sync.run, so one unserialisable cell
    would abort the whole run rather than log a failed adapter. Since raw now
    carries every populated cell of the row, anything json cannot represent is
    stringified rather than trusted to be a scalar.
    """
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


# ── url helpers ─────────────────────────────────────────────────────────────

def _clean_url(url: str) -> str:
    return str(url).strip().split("?")[0].rstrip("/")


def _post_id(url: str) -> str:
    """The dedup key.

    Prefer the bare activity id: it is what LinkedIn's own permalinks and every
    scraper route expose, so a post ingested here and later re-ingested through a
    public adapter collapses onto one row instead of two. ugcPost/share urns are
    a different id space for the same post, so they stay namespaced rather than
    silently masquerading as activity ids.

    Matched against the query-stripped URL on purpose. The tracking parameters
    differ between the two ranking tables and between exports, so letting them
    reach the regex at all is how a dedup key starts drifting.
    """
    path = _clean_url(url)
    m = _ACTIVITY_RE.search(path)
    if m:
        return m.group(1)
    m = _URN_RE.search(path) or _UGC_SLUG_RE.search(path)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    return stable_id(path)


def _kind_for(url: str) -> str:
    low = url.lower()
    return classify_kind(
        has_video=any(t in low for t in _VIDEO_URL),
        media_count=0,
        duration_s=None,
        has_title=any(t in low for t in _ARTICLE_URL),
    )


def _vanity(url: str) -> str | None:
    m = _VANITY_RE.search(url)
    if not m:
        return None
    v = m.group(1)
    return v if v not in ("feed", "posts", "in", "pulse") else None


# ── sheet + header location ─────────────────────────────────────────────────

def _resolve_sheets(grids: dict[str, list[list[Any]]]) -> dict[str, str]:
    """canonical key -> actual sheet title, matched fuzzily and claimed once."""
    found: dict[str, str] = {}
    taken: set[str] = set()
    for key, tokens in _SHEETS.items():
        for token in tokens:
            hit = next((t for t in grids
                        if t not in taken and token in _norm(t)), None)
            if hit:
                found[key] = hit
                taken.add(hit)
                break
    return found


def _find_header(grid: list[list[Any]], spec: dict[str, tuple[str, ...]],
                 *, scan: int = 20, min_hits: int = 2) -> tuple[int, dict[str, int]]:
    """First row where at least `min_hits` expected column names appear.

    Several sheets carry one or more preamble rows above the real header, and how
    many is not stable between exports. Scanning for the header by content is the
    only way that survives LinkedIn adding another banner line.
    """
    for r, row in enumerate(grid[:scan]):
        cols: dict[str, int] = {}
        for c, cell in enumerate(row):
            text = _norm(cell)
            if not text:
                continue
            for key, tokens in spec.items():
                if key not in cols and any(t in text for t in tokens):
                    cols[key] = c
                    break
        if len(cols) >= min_hits:
            return r, cols
    return -1, {}


def _metric_for(header: str) -> str | None:
    if not header or any(s in header for s in _METRIC_SKIP) \
            or header.startswith(_METRIC_SKIP_PREFIX):
        return None
    return next((canon for token, canon in _METRIC_HEADERS if token in header), None)


def _post_blocks(row: list[Any]) -> list[dict[str, Any]]:
    """Split the TOP POSTS header row into its side-by-side tables.

    The sheet is one table ranked by engagements and another ranked by
    impressions, laid out left and right of a spacer column. Each block is
    delimited by its own "Post URL"; everything up to the next one belongs to it.
    Reading the blocks this way means a third ranking table, or a reordered pair,
    costs nothing.
    """
    url_cols = [c for c, cell in enumerate(row)
                if any(t in _norm(cell) for t in _URL_HEADERS)]
    blocks = []
    for i, start in enumerate(url_cols):
        end = url_cols[i + 1] if i + 1 < len(url_cols) else len(row)
        date_col = None
        metrics: dict[int, str] = {}
        # Verbatim header text for every column in the block, recognised or not.
        # A column this adapter has no mapping for is exactly the column raw has
        # to keep, so `headers` covers the whole span rather than only `metrics`.
        headers = {start: str(row[start]).strip()}
        for c in range(start + 1, end):
            text = _norm(row[c])
            if not text:
                continue
            headers[c] = str(row[c]).strip()
            if date_col is None and any(t in text for t in _DATE_HEADERS):
                date_col = c
                continue
            canon = _metric_for(text)
            if canon:
                metrics[c] = canon
        blocks.append({"url_col": start, "date_col": date_col,
                       "metrics": metrics, "headers": headers})
    return blocks


# ── per-sheet parsers ───────────────────────────────────────────────────────

def _parse_discovery(grid: list[list[Any]]) -> dict[str, Any]:
    """Label/value pairs in the first two columns, not a table."""
    out: dict[str, Any] = {}
    for row in grid:
        if not row or row[0] is None or len(row) < 2:
            continue
        label, value = _norm(row[0]), row[1]
        if "impression" in label:
            out["impressions"] = _to_int(value)
        elif "reach" in label or "member" in label:
            out["reach"] = _to_int(value)
        elif "performance" in label or "date range" in label:
            out["period"] = str(value).strip() if value is not None else None
    return {k: v for k, v in out.items() if v is not None}


def _parse_engagement(grid: list[list[Any]]) -> dict[str, Any]:
    """Daily account-level rows. Folded into a summary, never emitted as items."""
    spec = {"date": _DATE_HEADERS, "impressions": ("impression",),
            "engagements": ("engagement",)}
    hdr, cols = _find_header(grid, spec)
    if hdr < 0 or "date" not in cols:
        return {}
    days = 0
    totals = {"impressions": 0, "engagements": 0}
    first = last = None
    for row in grid[hdr + 1:]:
        if not row or cols["date"] >= len(row):
            continue
        stamp = _to_epoch(row[cols["date"]])
        if stamp is None:
            continue
        days += 1
        first = stamp if first is None else min(first, stamp)
        last = stamp if last is None else max(last, stamp)
        for key in totals:
            c = cols.get(key)
            if c is not None and c < len(row):
                totals[key] += _to_int(row[c]) or 0
    return {"days": days, **totals, "first_day": first, "last_day": last}


def _parse_followers(grid: list[list[Any]]) -> dict[str, Any]:
    """Total followers plus the daily new-follower table.

    The total is a labelled cell above the table ("Total followers on <date>:"),
    not a column, and it is the only trustworthy absolute number here. Summing
    the daily column gives growth over the window, never the total.
    """
    out: dict[str, Any] = {}
    for row in grid:
        if not row or row[0] is None or len(row) < 2:
            continue
        label = _norm(row[0])
        if label.startswith("total follower") or label == "followers":
            total = _to_int(row[1])
            if total is not None:
                out["total"] = total
            break

    hdr, cols = _find_header(grid, {"date": _DATE_HEADERS,
                                    "new": ("new follower", "follower", "gained")})
    if hdr >= 0 and "date" in cols and "new" in cols:
        gained = days = 0
        for row in grid[hdr + 1:]:
            if not row or cols["date"] >= len(row):
                continue
            if _to_epoch(row[cols["date"]]) is None:
                continue
            days += 1
            if cols["new"] < len(row):
                gained += _to_int(row[cols["new"]]) or 0
        out["days"] = days
        out["gained_in_period"] = gained
    return out


def _parse_demographics(grid: list[list[Any]], *,
                        notes: list[str]) -> list[dict[str, Any]]:
    """Rows of (category, value, percentage).

    Header rows are dropped by the same test that validates a data row: the last
    populated cell has to read as a percentage. No separate header detection, and
    a renamed header column cannot break it.
    """
    out = []
    suppressed = False
    for row in grid:
        cells = [c for c in (row or []) if c is not None and str(c).strip()]
        if len(cells) < 2:
            continue
        pct = _to_pct(cells[-1])
        if pct is None:
            continue
        suppressed = suppressed or _is_suppressed(cells[-1])
        if len(cells) >= 3:
            category, value = str(cells[0]).strip(), str(cells[1]).strip()
        else:
            category, value = None, str(cells[0]).strip()
        out.append({"category": category, "value": value, "percent": pct,
                    "cell": _jsonable(cells[-1])})

    # A percent-formatted cell reads back as the fraction (0.084, not 8.4) while a
    # text cell reads back as "8.4%", and both encodings have been seen. Deciding
    # per sheet rather than per row is what makes it safe: a lone value under 1 is
    # ambiguous, a whole sheet of them is not. Suppressed "< 1%" strings only exist
    # in the text encoding, so their presence settles it without any guessing.
    if out and not suppressed and max(r["percent"] for r in out) <= 1.0:
        for r in out:
            r["percent"] = round(r["percent"] * 100, 4)
        notes.append("DEMOGRAPHICS: percentages arrived as fractions and were "
                     "scaled to percent")
    return out


def _parse_top_posts(grid: list[list[Any]], *, source: str, sheet: str,
                     notes: list[str]) -> tuple[list[RawItem], bool]:
    """Merge the ranking tables into one row per post."""
    hdr, _ = _find_header(grid, {"url": _URL_HEADERS}, min_hits=1)
    if hdr < 0:
        notes.append("TOP POSTS: no 'Post URL' header found, no posts imported")
        return [], False

    blocks = _post_blocks(grid[hdr])
    if not any(b["metrics"] for b in blocks):
        notes.append("TOP POSTS: header has no recognised metric column")

    # Merged on the post id, not on the URL string: the two tables link to the
    # same post through different tracking query parameters often enough that
    # merging on the raw URL silently doubles the row count.
    merged: dict[str, dict[str, Any]] = {}
    per_block_rows = [0] * len(blocks)

    for row in grid[hdr + 1:]:
        if not row:
            continue
        for bi, block in enumerate(blocks):
            col = block["url_col"]
            if col >= len(row):
                continue
            cell = row[col]
            if not isinstance(cell, str) or "linkedin.com" not in cell.lower():
                continue
            per_block_rows[bi] += 1
            key = _post_id(cell)
            rec = merged.setdefault(key, {"url": _clean_url(cell), "rows": [],
                                          "date": None, "metrics": {}})

            # raw is the vendor's row, verbatim: every populated cell under the
            # header text the file actually used, values uncoerced. The projection
            # below is a guess about what those columns mean; raw is what lets that
            # guess be redone later, so it must not be the guess's output. A post
            # appears once per ranking table, hence a list.
            rec["rows"].append({
                hdr_text: _jsonable(row[c])
                for c, hdr_text in sorted(block["headers"].items())
                if c < len(row) and row[c] is not None and str(row[c]).strip()
            })

            dc = block["date_col"]
            if rec["date"] is None and dc is not None and dc < len(row):
                rec["date"] = _to_epoch(row[dc])
            for c, canon in block["metrics"].items():
                if c >= len(row):
                    continue
                v = _to_int(row[c])
                if v is None:
                    continue
                prev = rec["metrics"].get(canon)
                # Same post, same reporting window: a disagreement between the
                # two tables is a rounding artefact, so keep the larger.
                rec["metrics"][canon] = v if prev is None else max(prev, v)

    items, undated = [], 0
    for key, rec in merged.items():
        if rec["date"] is None:
            undated += 1
            continue
        metrics = dict(rec["metrics"])
        # LinkedIn ships one composite "Engagements" number with no breakdown.
        # It rides in reactions because that is the only interaction column it
        # can honestly occupy, and stays under its real name in metrics_raw so a
        # later reader can tell a composite from a like count.
        if "engagements" in metrics:
            metrics.setdefault("reactions", metrics["engagements"])
        items.append(RawItem(
            platform_item_id=key,
            kind=_kind_for(rec["url"]),
            published_at=rec["date"],
            raw={"rows": rec["rows"],
                 "_source": {"file": source, "sheet": sheet}},
            url=rec["url"],
            metrics=metrics,
        ))
    if undated:
        notes.append(f"TOP POSTS: {undated} post(s) had no parseable publish "
                     f"date and were skipped")
    # A block whose URL column never matched is the signature of LinkedIn moving
    # the permalink out of the cell value (into a hyperlink, which values_only
    # discards). Without this the file imports as a clean half-import.
    for bi, n in enumerate(per_block_rows):
        if n == 0:
            notes.append(f"TOP POSTS: ranking table at column "
                         f"{blocks[bi]['url_col'] + 1} matched no post URLs")

    truncated = any(n >= _TOP_POSTS_CAP for n in per_block_rows)
    return items, truncated


# ── other export shapes ─────────────────────────────────────────────────────

_SINGLE_POST_SHEETS = ("post analytics", "performance")
_PAGE_POST_SHEETS = ("all posts",)
_DEMO_MARKERS = ("post viewer demographics", "top demographics")

# Page "Content Type" -> kind. The Page export is the one place a LinkedIn route
# states the format outright instead of leaving it to be guessed from the URL.
_PAGE_KINDS = {"video": "short_video", "image": "image", "images": "carousel",
               "multi-image": "carousel", "document": "carousel",
               "carousel": "carousel", "article": "article", "text": "text",
               "poll": "text", "event": "text", "link": "text"}


def _label_value_rows(grid: list[list[Any]]) -> list[tuple[str, Any, list[Any]]]:
    """(label, value, row) for rows shaped "Label | value", in order."""
    out = []
    for row in grid:
        cells = list(row or [])
        if not cells or cells[0] is None or not str(cells[0]).strip():
            continue
        out.append((str(cells[0]).strip(), cells[1] if len(cells) > 1 else None, cells))
    return out


def _is_single_post_grid(grid: list[list[Any]]) -> bool:
    """A label/value sheet whose "Post URL" row holds a LinkedIn link."""
    for label, value, _ in _label_value_rows(grid[:40]):
        if _norm(label) in ("post url", "post link") and isinstance(value, str) \
                and "linkedin.com" in value.lower():
            return True
    return False


_PAGE_HEADER = {"title": ("post title",), "url": ("post link", "post url"),
                "date": ("created date", "date created"),
                "impressions": ("impression",)}


def _is_page_posts_grid(grid: list[list[Any]]) -> bool:
    hdr, cols = _find_header(grid, _PAGE_HEADER, scan=8, min_hits=3)
    return hdr >= 0 and "url" in cols and "title" in cols


def detect_format(grids: dict[str, list[list[Any]]]) -> tuple[str, str | None]:
    """("aggregate" | "single_post" | "page", sheet) from the content.

    Filenames are a hint at best: a client forwards "linkedin (2).xlsx", and
    LinkedIn renamed every one of these files in 2026 without notice.
    """
    # Page first, by its header: "All posts" is the sheet name today, but the
    # header (post title + post link + created date / impressions) is what
    # identifies it, and a CSV has no sheet name at all.
    for title, grid in sorted(grids.items(),
                              key=lambda kv: not any(t in _norm(kv[0])
                                                     for t in _PAGE_POST_SHEETS)):
        if _is_page_posts_grid(grid):
            return "page", title
    for title, grid in grids.items():
        if any(t == _norm(title) for t in _SINGLE_POST_SHEETS) or len(grids) == 1:
            if _is_single_post_grid(grid):
                return "single_post", title
    return "aggregate", None


def _post_time(date_cell: Any, time_cell: Any) -> int | None:
    """Post Date plus, when it parses, Post Publish Time ("9:41 AM", "14:05")."""
    day = _to_epoch(date_cell)
    if day is None:
        return None
    if isinstance(time_cell, dt.time):
        return day + time_cell.hour * 3600 + time_cell.minute * 60
    if isinstance(time_cell, str):
        for fmt in ("%I:%M %p", "%I:%M:%S %p", "%H:%M", "%H:%M:%S"):
            try:
                t = dt.datetime.strptime(time_cell.strip().upper(), fmt)
            except ValueError:
                continue
            return day + t.hour * 3600 + t.minute * 60
    return day


def _parse_single_post(grids: dict[str, list[list[Any]]], sheet: str, *,
                       source: str, notes: list[str]) -> list[RawItem]:
    """One post's label/value metrics (+ its viewer demographics) as one item."""
    grid = grids[sheet]
    fields: dict[str, Any] = {}
    metrics: dict[str, int] = {}
    demo_grid: list[list[Any]] = []
    in_demo = False
    for label, value, row in _label_value_rows(grid):
        low = _norm(label)
        if any(m in low for m in _DEMO_MARKERS):
            in_demo = True
            continue
        if in_demo:
            demo_grid.append(row)
            continue
        fields[label] = _jsonable(value)
        canon = _metric_for(low)
        v = _to_int(value)
        if canon and v is not None and canon not in metrics:
            metrics[canon] = v
    # Legacy file: demographics on their own sheet.
    for title, g in grids.items():
        if title != sheet and "demographic" in _norm(title):
            demo_grid += g

    def field(*names):
        for k, v in fields.items():
            if _norm(k) in names:
                return v
        return None

    url = field("post url", "post link")
    if not isinstance(url, str) or "linkedin.com" not in url.lower():
        notes.append(f"{sheet}: no Post URL, nothing imported")
        return []
    published = _post_time(field("post date", "post publish date"),
                           field("post publish time"))
    if published is None:
        notes.append(f"{sheet}: no parseable Post Date, nothing imported")
        return []
    demographics = _parse_demographics(demo_grid, notes=notes) if demo_grid else []
    if "engagements" in metrics:
        metrics.setdefault("reactions", metrics["engagements"])
    url = _clean_url(url)
    return [RawItem(
        platform_item_id=_post_id(url), kind=_kind_for(url), published_at=published,
        url=url, metrics=metrics,
        raw={"fields": fields, "demographics": demographics,
             "_source": {"file": source, "sheet": sheet, "format": "single_post"}},
    )]


def _parse_page_posts(grid: list[list[Any]], *, source: str, sheet: str,
                      notes: list[str]) -> list[RawItem]:
    """The Page export's "All posts" table: one row per post, with its text."""
    spec = dict(_PAGE_HEADER, type=("post type",), content=("content type",),
                by=("posted by",))
    hdr, cols = _find_header(grid, spec, scan=8, min_hits=3)
    header = grid[hdr]
    metric_cols = {c: canon for c, cell in enumerate(header)
                   if c not in cols.values() and (canon := _metric_for(_norm(cell)))}
    # "Impressions" is in the detection spec, so it is claimed there; put it back.
    if "impressions" in cols:
        metric_cols[cols["impressions"]] = "impressions"

    items, undated = [], 0
    for row in grid[hdr + 1:]:
        row = list(row or [])

        def cell(key):
            c = cols.get(key)
            return row[c] if c is not None and c < len(row) else None

        url = cell("url")
        if not isinstance(url, str) or "linkedin.com" not in url.lower():
            continue
        published = _to_epoch(cell("date"))
        if published is None:
            undated += 1
            continue
        metrics: dict[str, int] = {}
        for c, canon in metric_cols.items():
            v = _to_int(row[c]) if c < len(row) else None
            if v is not None and canon not in metrics:
                metrics[canon] = v
        url = _clean_url(url)
        ctype = _norm(cell("content"))
        text = str(cell("title")).strip() if cell("title") is not None else None
        items.append(RawItem(
            platform_item_id=_post_id(url),
            kind=_PAGE_KINDS.get(ctype) or _kind_for(url),
            published_at=published, url=url, body=text or None, metrics=metrics,
            author_name=str(cell("by")).strip() if cell("by") else None,
            raw={"row": {str(header[c]).strip(): _jsonable(v)
                         for c, v in enumerate(row)
                         if c < len(header) and header[c] is not None
                         and v is not None and str(v).strip()},
                 "_source": {"file": source, "sheet": sheet, "format": "page"}},
        ))
    if undated:
        notes.append(f"{sheet}: {undated} post(s) had no parseable Created date "
                     f"and were skipped")
    return items


def _parse_page_metrics(grids: dict[str, list[list[Any]]]) -> dict[str, Any]:
    """Daily Page totals from the "Metrics" sheet, folded into a summary."""
    title = next((t for t in grids if _norm(t) == "metrics"), None)
    if title is None:
        return {}
    grid = grids[title]
    hdr, cols = _find_header(grid, {"date": ("date",),
                                    "impressions": ("impressions (total)",),
                                    "clicks": ("clicks (total)",)}, scan=8)
    if hdr < 0 or "date" not in cols:
        return {}
    days, totals = 0, {"impressions": 0, "clicks": 0}
    for row in grid[hdr + 1:]:
        if not row or _to_epoch(row[cols["date"]]) is None:
            continue
        days += 1
        for k in totals:
            c = cols.get(k)
            if c is not None and c < len(row):
                totals[k] += _to_int(row[c]) or 0
    return {"days": days, **totals}


# ── files ───────────────────────────────────────────────────────────────────

_DUP = r"(?: \(\d+\))?"          # the browser's " (1)" on a repeated download
_FILENAME_PATTERNS = (
    ("aggregate", re.compile(r"^AggregateAnalytics_(?P<profile>.+)_(?P<start>\d{4}-\d{2}-"
                             r"\d{2})_(?P<end>\d{4}-\d{2}-\d{2})" + _DUP + r"\.xlsx$")),
    ("aggregate", re.compile(r"^Content_(?P<start>\d{4}-\d{2}-\d{2})_(?P<end>\d{4}-\d{2}-"
                             r"\d{2})_(?P<profile>.+?)" + _DUP + r"\.xlsx$")),
    ("single_post", re.compile(r"^(?:Single)?PostAnalytics_(?P<profile>.+)_(?P<post_id>"
                               r"\d{6,})" + _DUP + r"\.(?:xlsx|csv)$")),
    ("page", re.compile(r"^(?P<profile>.+?)_content_(?P<stamp>[^.]*?)" + _DUP
                        + r"\.(?:xls|xlsx|csv)$", re.I)),
)


def classify_filename(name: str) -> dict[str, Any] | None:
    """What LinkedIn's own filename says, or None for anything else.

    Used to pick an export out of a folder and to record the profile name the
    file was issued for; never to decide how to parse it.
    """
    for fmt, pat in _FILENAME_PATTERNS:
        m = pat.match(name)
        if m:
            return {"format": fmt, **{k: v for k, v in m.groupdict().items() if v}}
    return None


def find_export(folder: Path) -> Path:
    """The newest recognised LinkedIn export in a folder.

    Refuses when the candidates were issued for different profiles: ~/Downloads
    can hold a client's export next to your own, and importing the wrong one
    files a stranger's numbers under the account being synced.
    """
    found = [(p, info) for p in folder.iterdir()
             if p.is_file() and (info := classify_filename(p.name))]
    if not found:
        raise FileNotFoundError(f"linkedin_export: no LinkedIn export in {folder} "
                                f"(AggregateAnalytics_*, Content_*, "
                                f"SinglePostAnalytics_*, *_content_*.xls)")
    profiles = {re.sub(r"\s+", "", i.get("profile", "")).lower() for _, i in found}
    if len(profiles) > 1:
        raise ValueError(
            "linkedin_export: exports for several profiles in "
            f"{folder} ({', '.join(sorted({i.get('profile', '?') for _, i in found}))});"
            " pass the file itself with --file")
    return max(found, key=lambda pi: pi[0].stat().st_mtime)[0]


def _read_csv(path: Path) -> list[list[Any]]:
    import csv
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"{path}: not a readable CSV")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [[c if c.strip() else None for c in row]
            for row in csv.reader(text.splitlines(), dialect)]



# Where LibreOffice lives when it is not on PATH (the macOS app bundle).
SOFFICE_PATHS = ("/Applications/LibreOffice.app/Contents/MacOS/soffice",)

def _xls_via_converter(path: Path) -> dict[str, list[list[Any]]]:
    """Legacy binary .xls (what the Page export is): xlrd if it happens to be
    installed, else LibreOffice headless to .xlsx. Neither is a dependency, so
    without both the error says what to do instead of failing obscurely."""
    try:
        import xlrd  # type: ignore
        book = xlrd.open_workbook(str(path))
        out = {}
        for sh in book.sheets():
            out[sh.name] = [
                [(xlrd.xldate_as_datetime(c.value, book.datemode)
                  if c.ctype == xlrd.XL_CELL_DATE else (c.value if c.value != "" else None))
                 for c in sh.row(r)] for r in range(sh.nrows)]
        return out
    except ImportError:
        pass
    import shutil
    import subprocess
    import tempfile
    office = shutil.which("soffice") or shutil.which("libreoffice") or next(
        (p for p in SOFFICE_PATHS if Path(p).exists()), None)
    if not office:
        raise ValueError(
            f"{path.name} is a legacy binary .xls (LinkedIn Page exports are). "
            "Open it and save as .xlsx, or install LibreOffice (or `pip install "
            "xlrd`) so it can be read directly")
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([office, "--headless", "--convert-to", "xlsx", "--outdir", tmp,
                        str(path)], capture_output=True, timeout=120, check=False)
        out = Path(tmp) / (path.stem + ".xlsx")
        if not out.exists():
            raise ValueError(f"{path.name}: LibreOffice could not convert it to .xlsx")
        return _load_xlsx(out)


# ── workbook ────────────────────────────────────────────────────────────────

def _load_grids(path: Path) -> dict[str, list[list[Any]]]:
    """Sheets as grids, whatever the container. By magic bytes, not extension:
    the Page export is a binary .xls, and a re-saved file keeps whatever name the
    person gave it."""
    head = path.read_bytes()[:8]
    if head.startswith(b"PK"):
        return _load_xlsx(path)
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return _xls_via_converter(path)
    if head.lstrip(b"\xef\xbb\xbf \r\n\t").startswith(b"<"):
        raise ValueError(f"{path.name}: an HTML table saved as a spreadsheet; open it "
                         f"and save as .xlsx")
    return {"CSV": _read_csv(path)}


def _load_xlsx(path: Path) -> dict[str, list[list[Any]]]:
    try:
        import openpyxl
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "linkedin_export needs openpyxl to read .xlsx "
            "(pip install openpyxl, see requirements.txt)"
        ) from e

    try:
        # A file object, not the path: openpyxl refuses a ".xls" name even when
        # the bytes are a perfectly good .xlsx (a re-saved Page export).
        import io
        wb = openpyxl.load_workbook(io.BytesIO(path.read_bytes()), data_only=True)
    except Exception as e:
        raise ValueError(f"{path}: not a readable .xlsx ({type(e).__name__}: {e})") from e
    try:
        return {ws.title: [list(r) for r in ws.iter_rows(values_only=True)]
                for ws in wb.worksheets}
    finally:
        wb.close()


def _parse_aggregate(grids, path: Path, notes: list[str]):
    """The personal content export (TOP POSTS + account sheets)."""
    sheets = _resolve_sheets(grids)
    notes += [f"sheet missing: {key.upper().replace('_', ' ')}"
              for key in _SHEETS if key not in sheets]

    items, truncated = ([], False)
    if "top_posts" in sheets:
        items, truncated = _parse_top_posts(grids[sheets["top_posts"]],
                                            source=path.name,
                                            sheet=sheets["top_posts"],
                                            notes=notes)
    account: dict[str, Any] = {}
    if "discovery" in sheets:
        account["discovery"] = _parse_discovery(grids[sheets["discovery"]])
    if "engagement" in sheets:
        account["engagement"] = _parse_engagement(grids[sheets["engagement"]])
    if "followers" in sheets:
        account["followers"] = _parse_followers(grids[sheets["followers"]])
    if "demographics" in sheets:
        # Nothing in the schema models an audience breakdown, and it is the whole
        # reason this route exists, so it is preserved verbatim in account raw
        # rather than dropped for want of a column.
        account["demographics"] = _parse_demographics(
            grids[sheets["demographics"]], notes=notes)
    return items, truncated, account


def parse_workbook(target: str | Path, *, limit: int | None = None,
                   since: int | None = None,
                   handle: str | None = None) -> tuple[FetchResult, list[str]]:
    """Parse an export into a FetchResult plus the list of things it could not find.

    Split out from fetch() so a CLI can show the notes without a database round
    trip; fetch() is the thin wrapper that satisfies the adapter contract.
    """
    path = Path(target).expanduser()
    if path.is_dir():
        path = find_export(path)
    if not path.is_file():
        raise FileNotFoundError(f"linkedin_export: no such file: {path}")

    grids = _load_grids(path)
    fmt, sheet = detect_format(grids)
    if fmt == "aggregate" and "CSV" in grids:
        # The personal content export only ever ships as .xlsx; a text file that is
        # neither of the CSV shapes is something else entirely.
        raise ValueError(f"{path.name}: not a readable LinkedIn export (no Page "
                         f"'All posts' columns, no per-post 'Post URL' row)")
    named = classify_filename(path.name) or {}
    notes: list[str] = []
    if named and named["format"] != fmt:
        notes.append(f"filename says {named['format']} but the content reads as "
                     f"{fmt}; parsed as {fmt}")

    truncated = False
    account: dict[str, Any] = {}
    if fmt == "single_post":
        items = _parse_single_post(grids, sheet, source=path.name, notes=notes)
        if "CSV" in grids:
            notes.append("per-post CSV: layout inferred (label/value rows), not "
                         "verified against a real LinkedIn CSV")
    elif fmt == "page":
        items = _parse_page_posts(grids[sheet], source=path.name, sheet=sheet,
                                  notes=notes)
        account["page_metrics"] = _parse_page_metrics(grids)
    else:
        items, truncated, account = _parse_aggregate(grids, path, notes)

    if since is not None:
        before = len(items)
        items = [i for i in items if i.published_at >= since]
        if before != len(items):
            notes.append(f"since filter dropped {before - len(items)} post(s)")

    items.sort(key=lambda i: i.published_at, reverse=True)
    if limit is not None and len(items) > limit:
        items = items[:limit]
        truncated = True

    followers_total = (account.get("followers") or {}).get("total")

    # The file never names the account. The vanity slug in the post permalinks is
    # the only identifier in it, so it is used as a fallback and the caller can
    # override: a wrong handle would open a second account row rather than update
    # the registered one. (The filename's profile name is a display name, not a
    # handle, so it is recorded but never used as one.)
    if handle is None and fmt == "page" and named.get("profile"):
        # Page post links are /feed/update/urn:li:activity:... with no slug in
        # them; the filename's page name is all there is. It only labels the
        # import (the profile is written by account id, and no URL is derived
        # from it below), so a wrong guess cannot redirect the account.
        handle = named["profile"]
    if handle is None:
        seen: dict[str, int] = {}
        for it in items:
            v = _vanity(it.url or "")
            if v:
                seen[v] = seen.get(v, 0) + 1
        handle = max(seen, key=seen.get) if seen else None
        if handle is None:
            notes.append("no handle: not in the file and none passed, "
                         "profile and follower total not attached")

    profile = None
    if handle:
        profile = Profile(
            handle=handle,
            url=None if fmt == "page" else f"https://www.linkedin.com/in/{handle}",
            follower_count=followers_total,
            # posts_total stays None on purpose: TOP POSTS is the top 50, and
            # reporting it as the account's post count would be a lie the rest of
            # the corpus would then trust.
            raw={"file": path.name, "sheets": sorted(grids), "format": fmt,
                 "filename": named, "import_notes": notes, **account},
        )
    elif items:
        # Without a profile nothing else in the FetchResult reaches the database,
        # so the notes ride on one item instead of evaporating.
        items[0].raw["_import_notes"] = notes

    result = FetchResult(items=items, profile=profile, cost_usd=0.0,
                         truncated=truncated)
    return result, notes


def fetch(target: str | Path, *, mode: str = "delta",
          keys: dict[str, str] | None = None, limit: int | None = 500,
          since: int | None = None, handle: str | None = None,
          relationship: str | None = None, **kw) -> FetchResult:
    """Read one LinkedIn Analytics export.

    mode is accepted for contract symmetry but changes nothing: a file is
    all-or-nothing, and `since` is the only real narrowing this route has.
    """
    if relationship is not None and relationship not in ALLOWED_RELATIONSHIPS:
        raise ValueError(
            f"linkedin_export refuses relationship={relationship!r}: an analytics "
            f"export can only come from whoever owns the account"
        )
    result, _notes = parse_workbook(target, limit=limit, since=since, handle=handle)
    return result


ADAPTER = Adapter(
    name="linkedin_export",
    platform="linkedin",
    fetch=fetch,
    relationships=ALLOWED_RELATIONSHIPS,
    needs=(),
    cost_per_1k=0.0,
    notes=("LinkedIn's own Analytics exports: personal (AggregateAnalytics_* / "
           "Content_* .xlsx), per-post (SinglePostAnalytics_*), and company Page "
           "(*_content_*.xls). target is a file or a folder. Zero cost, zero "
           "exposure, and the only route with impressions, reach and follower "
           "demographics. The personal export carries no post text and only the "
           "top ~50 posts per ranking; the Page export carries the text."),
)
