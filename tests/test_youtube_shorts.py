"""YouTube Shorts via yt-dlp. No network: subprocess is faked with a recorded
channel listing and one recorded Short (synthetic, modelled on a real
channel recorded 2026-10-22)."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from corpus import discover  # noqa: E402
from corpus.adapters import youtube_shorts as Y  # noqa: E402

FIX = json.loads((Path(__file__).parent / "fixtures" / "youtube_shorts.json").read_text())
DAY = 86400


def _fake(monkeypatch, n_entries=3, step_days=7):
    calls = []
    base_ts = FIX["short"]["timestamp"]
    flat = dict(FIX["flat"], entries=[dict(FIX["flat"]["entries"][0], id=f"v{i}")
                                     for i in range(n_entries)])

    def run(cmd, capture_output, text, timeout):
        calls.append(cmd)
        url = cmd[-1]
        if url.endswith("/shorts"):
            end = int(cmd[cmd.index("--playlist-end") + 1])
            out = dict(flat, entries=flat["entries"][:end])
        else:
            i = int(url.rsplit("/v", 1)[1])
            out = dict(FIX["short"], id=f"v{i}", timestamp=base_ts - i * step_days * DAY)
        return SimpleNamespace(returncode=0, stdout=json.dumps(out), stderr="")
    monkeypatch.setattr(Y.subprocess, "run", run)
    monkeypatch.setattr(Y, "ytdlp", lambda: "yt-dlp")
    return calls


def test_a_short_projects_with_views_likes_comments_and_duration(monkeypatch):
    _fake(monkeypatch, n_entries=1)
    r = Y.fetch("https://www.youtube.com/@portvilletourism", mode="backfill", limit=5)
    it = r.items[0]
    assert it.kind == "short_video" and it.url == "https://www.youtube.com/shorts/v0"
    assert it.metrics == {"reactions": 4718, "comments": 115, "views": 76023}
    assert it.duration_s == 58.0
    assert r.profile.handle == "portvilletourism" and r.profile.follower_count == 21_400_000
    assert not r.truncated


def test_a_delta_stops_opening_shorts_once_it_passes_the_watermark(monkeypatch):
    calls = _fake(monkeypatch, n_entries=20, step_days=7)
    since = FIX["short"]["timestamp"] - 10 * DAY          # only v0 and v1 are newer
    r = Y.fetch("https://www.youtube.com/@portvilletourism", since=since, limit=50)
    assert {i.platform_item_id for i in r.items} == {"v0", "v1"}
    details = [c for c in calls if not c[-1].endswith("/shorts")]
    assert len(details) == Y.DETAIL_WORKERS, "opened Shorts past the watermark"


def test_a_full_page_is_reported_as_truncated(monkeypatch):
    _fake(monkeypatch, n_entries=6)
    r = Y.fetch("https://www.youtube.com/@portvilletourism", mode="backfill", limit=3)
    assert len(r.items) == 3 and r.truncated


def test_channel_urls_parse_and_others_are_refused():
    t = discover.parse_target("https://www.youtube.com/@PortvilleTourism/shorts")
    assert (t.platform, t.handle, t.url) == ("youtube", "portvilletourism",
                                             "https://www.youtube.com/@portvilletourism")
    assert Y.channel_url("https://youtube.com/@x/videos") == "https://www.youtube.com/@x"
    with pytest.raises(ValueError):
        Y.channel_url("https://www.youtube.com/watch?v=abc")


def test_a_youtube_page_url_is_fetched_with_ytdlp_not_urllib(monkeypatch, tmp_path):
    from corpus.enrich import media
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"x")
        return SimpleNamespace(returncode=0, stderr="")
    monkeypatch.setattr(media.subprocess, "run", run)
    monkeypatch.setattr(Y, "ytdlp", lambda: "yt-dlp")
    monkeypatch.setattr(media, "_open",
                        lambda *a, **k: pytest.fail("a page URL went to urllib"))
    media.download("https://www.youtube.com/shorts/abc", tmp_path / "src.mp4")
    assert "--merge-output-format" in seen["cmd"]


def test_the_uploaders_language_setting_is_not_stored_as_the_videos(monkeypatch):
    """yt-dlp's `language` is the channel default; trusting it sent a Croatian Short
    to the transcriber as English."""
    _fake(monkeypatch, n_entries=1)
    r = Y.fetch("https://www.youtube.com/@portvilletourism", mode="backfill", limit=1)
    assert FIX["short"]["language"] == "en"
    assert r.items[0].lang is None


def test_a_short_without_a_timestamp_is_dated_by_its_upload_day(monkeypatch):
    """A tourist board channel, 2026-10-23: 11 of 12 Shorts came back with upload_date and no
    timestamp, were all dropped, and the sync failed as vendor drift."""
    calls = _fake(monkeypatch, n_entries=1)
    real = Y.subprocess.run

    def run(cmd, **kw):
        r = real(cmd, **kw)
        if not cmd[-1].endswith("/shorts"):
            d = json.loads(r.stdout)
            d.pop("timestamp")
            d["upload_date"] = "20260918"
            r.stdout = json.dumps(d)
        return r
    monkeypatch.setattr(Y.subprocess, "run", run)
    r = Y.fetch("https://www.youtube.com/@portvilletourism", mode="backfill", limit=5)
    assert len(r.items) == 1 and r.raw_count == 1
    import datetime as dt
    day = dt.datetime.fromtimestamp(r.items[0].published_at, dt.timezone.utc)
    assert (day.year, day.month, day.day) == (2026, 9, 18)
    detail = [c for c in calls if not c[-1].endswith("/shorts")][0]
    assert "--ignore-no-formats-error" in detail


def test_a_short_the_web_client_refuses_is_retried_on_android(monkeypatch):
    calls = _fake(monkeypatch, n_entries=1)
    real = Y.subprocess.run

    def run(cmd, **kw):
        if not cmd[-1].endswith("/shorts") and "youtube:player_client=android" not in cmd:
            return SimpleNamespace(returncode=1, stdout="",
                                   stderr="ERROR: [youtube] v0: This video is not available")
        return real(cmd, **kw)
    monkeypatch.setattr(Y.subprocess, "run", run)
    r = Y.fetch("https://www.youtube.com/@portvilletourism", mode="backfill", limit=5)
    assert [i.platform_item_id for i in r.items] == ["v0"]
