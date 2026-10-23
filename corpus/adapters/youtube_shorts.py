"""YouTube Shorts from a channel, through yt-dlp. Free, keyless, no account.

Two steps, because the cheap one is not enough on its own:
  1. the channel's /shorts tab as a flat list (one request): ids, titles, view
     counts and the channel block (name, id, subscriber count) -- but no dates;
  2. each Short's own page (~1.4s each, a few in parallel): publish time, likes,
     comments, duration, description, thumbnail.

The tab is newest-first, so a delta fetches details in small batches and stops at
the first batch that reaches back past `since`, instead of opening every Short.

Shares and saves are not public on YouTube; views are, which LinkedIn and the
Instagram routes cannot give.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .base import Adapter, FetchResult, Profile, RawItem, RawMedia

DEFAULT_LIMIT = 50
DETAIL_WORKERS = 4
TIMEOUT_S = 120
_TARGET_RE = re.compile(r"youtube\.com/(@[A-Za-z0-9_.-]+|channel/UC[A-Za-z0-9_-]+|c/[^/?#]+)",
                        re.I)


def ytdlp() -> str:
    """launchd strips Homebrew and ~/.local from PATH, so look there too."""
    for c in (shutil.which("yt-dlp"), str(Path("~/.local/bin/yt-dlp").expanduser()),
              "/opt/homebrew/bin/yt-dlp", "/usr/local/bin/yt-dlp"):
        if c and Path(c).exists():
            return c
    raise RuntimeError("yt-dlp not found (pipx install yt-dlp, or brew install yt-dlp)")


def _json(args: list[str]) -> dict:
    proc = subprocess.run([ytdlp(), "-J", "--no-warnings", *args], capture_output=True,
                          text=True, timeout=TIMEOUT_S)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        raise RuntimeError(f"yt-dlp: {err[-1] if err else f'exit {proc.returncode}'}")
    return json.loads(proc.stdout)


def channel_url(target: str) -> str:
    m = _TARGET_RE.search(target or "")
    if not m:
        raise ValueError(f"expected a youtube.com/@handle or /channel/UC... URL, got {target!r}")
    return f"https://www.youtube.com/{m.group(1)}"


def _count(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None


def _published(d: dict) -> int | None:
    """Exact upload time, else the upload DAY at 12:00 UTC.

    YouTube's degraded responses (seen on a tourist-board channel, 2026-10-23: 11 of 12 Shorts)
    carry `upload_date` but no `timestamp`; requiring the timestamp dropped them all
    and failed the sync as vendor drift. Noon UTC keeps the calendar day right in
    every European zone; the hour is not used by any report."""
    ts = d.get("timestamp")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return int(ts)
    ud = str(d.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", ud):
        import datetime as _dt
        try:
            day = _dt.datetime.strptime(ud, "%Y%m%d").replace(
                hour=12, tzinfo=_dt.timezone.utc)
        except ValueError:
            return None
        return int(day.timestamp())
    return None


def _item(d: dict) -> RawItem | None:
    vid, ts = d.get("id"), _published(d)
    if not vid or ts is None:
        return None
    url = f"https://www.youtube.com/shorts/{vid}"
    desc = (d.get("description") or "").strip()
    title = (d.get("title") or "").strip() or None
    media = [RawMedia(role="original", remote_url=url, mime="video/mp4")]
    if d.get("thumbnail"):
        media.append(RawMedia(role="thumbnail", remote_url=d["thumbnail"], mime="image/jpeg"))
    return RawItem(
        platform_item_id=str(vid), kind="short_video", published_at=int(ts),
        raw={k: v for k, v in d.items() if k not in ("formats", "thumbnails",
                                                      "automatic_captions", "subtitles",
                                                      "requested_formats", "heatmap")},
        # No lang: yt-dlp's `language` is the uploader's channel default, not the
        # video's. A Croatian Short ("Kako prepoznati dobar komad mesa?") came back
        # 'en', and enrich hands a stored lang straight to the transcriber, which
        # then writes invented English. Let enrich detect it from the caption.
        title=title, body=desc or title, url=url, lang=None,
        duration_s=float(d["duration"]) if isinstance(d.get("duration"), (int, float)) else None,
        metrics={"reactions": _count(d.get("like_count")),
                 "comments": _count(d.get("comment_count")),
                 "views": _count(d.get("view_count"))},
        media=media,
    )


# Metadata only: without --ignore-no-formats-error a Short whose formats YouTube
# withholds from this client ("Requested format is not available") fails outright
# even though its title, date and counts came back. The android client answers for
# Shorts the web client calls "This video is not available" (same date).
_DETAIL_ATTEMPTS = ([], ["--extractor-args", "youtube:player_client=android"])


def _detail(entry: dict) -> RawItem | None:
    for extra in _DETAIL_ATTEMPTS:
        try:
            it = _item(_json(["--skip-download", "--no-playlist",
                              "--ignore-no-formats-error", *extra,
                              f"https://www.youtube.com/shorts/{entry['id']}"]))
        except Exception:  # noqa: BLE001 - one private/removed Short must not sink the run
            it = None
        if it is not None:
            return it
    return None


def fetch(target: str, *, mode: str = "delta", keys: dict | None = None,
          limit: int = DEFAULT_LIMIT, since: Any = None, **_) -> FetchResult:
    base = channel_url(target)
    limit = max(1, int(limit))
    flat = _json(["--flat-playlist", "--playlist-end", str(limit + 1), f"{base}/shorts"])
    entries = [e for e in (flat.get("entries") or []) if isinstance(e, dict) and e.get("id")]
    more = len(entries) > limit
    entries = entries[:limit]
    floor = int(since) if (mode != "backfill" and since is not None) else None

    items: list[RawItem] = []
    tried = 0       # detail pages requested; each one that yields nothing is a drop
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        for i in range(0, len(entries), DETAIL_WORKERS):
            batch = entries[i:i + DETAIL_WORKERS]
            tried += len(batch)
            got = [x for x in pool.map(_detail, batch) if x]
            items += got
            if floor is not None and got and min(x.published_at for x in got) < floor:
                more = False
                break
    parsed = len(items)
    if floor is not None:
        items = [x for x in items if x.published_at >= floor]
    if not entries and floor is None:
        raise RuntimeError(f"no Shorts listed at {base}/shorts (no Shorts, a wrong "
                           "channel, or YouTube asking yt-dlp to sign in)")

    handle = str(flat.get("uploader_id") or "").lstrip("@") or None
    prof = Profile(
        handle=handle or base.rsplit("/", 1)[-1].lstrip("@"),
        display_name=flat.get("channel"),
        bio=(flat.get("description") or None),
        url=flat.get("uploader_url") or base,
        platform_uid=flat.get("channel_id"),
        follower_count=_count(flat.get("channel_follower_count")),
        raw={k: flat.get(k) for k in ("channel", "channel_id", "uploader_id",
                                      "channel_follower_count", "description")},
    )
    # raw_count is set so that len(items) compares like for like: Shorts filtered
    # out by `since` were parsed fine and are not drops.
    return FetchResult(items=items, profile=prof, cost_usd=0.0,
                       truncated=more and len(items) >= limit,
                       raw_count=tried - parsed + len(items))


ADAPTER = Adapter(
    name="youtube_shorts",
    platform="youtube",
    fetch=fetch,
    relationships=("competitor", "influencer", "own", "client"),
    cost_per_1k=0.0,
    notes="yt-dlp, keyless: every public Short with views, likes, comments and "
          "duration. No shares or saves (YouTube does not publish them).",
    unmeasured=("shares", "saves"),
)
