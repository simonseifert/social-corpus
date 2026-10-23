"""Offline normalisation contract for the Instagram consented route.

Runs against tests/fixtures/instagram_client.json: synthetic, modelled on a real
session-cookie response recorded 2026-08-05 from the project owner's own account
(3 of 12 items kept; names, captions, ids and URLs invented, structure as recorded). The point is that Instagram changing its payload shape fails here loudly
instead of quietly writing nulls into the corpus.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import store, sync  # noqa: E402
from corpus.adapters import instagram_client as ig  # noqa: E402

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "instagram_client.json").read_text()
)

# Known-good values read off the recorded payload by hand. Hard-coding them is the
# whole point: deriving them from the fixture would make the test agree with any
# shape change.
CAROUSEL, REEL, VIRAL = "DGHzTEUc0jZ", "DANSLHTfZcs", "D6WsLgKC0HV"
PAGE2 = "DQfdIGtEPIM"


@pytest.fixture(autouse=True)
def _no_page_sleep(monkeypatch):
    """The adapter paces itself against the live API; replaying a fixture must not."""
    monkeypatch.setattr(ig, "_PAGE_SLEEP_S", 0)


class StubTransport:
    """Replays the two recorded feed pages in order, and records every call.

    Serving each page once (rather than repeating one) is what makes the max_id
    hand-off and the terminal more_available=False assertable.
    """

    def __init__(self, fixture=FIXTURE):
        self.profile = fixture["profile"]
        self.pages = list(fixture["pages"])
        self.calls: list[tuple[str, dict]] = []

    def get(self, path, params=None):
        self.calls.append((path, params or {}))
        if "web_profile_info" in path:
            return self.profile
        if "/feed/user/" in path:
            assert self.pages, "adapter asked for a page past the end of the feed"
            return self.pages.pop(0)
        raise AssertionError(f"unexpected call {path}")


def fetch(**kw):
    kw.setdefault("limit", 10)
    return ig.ADAPTER.fetch("alex_rivera", keys={}, transport=StubTransport(), **kw)


def by_id(result):
    return {i.platform_item_id: i for i in result.items}


# ── identity and time ───────────────────────────────────────────────────────

def test_dedup_key_is_the_shortcode():
    """The shortcode is the one id every Instagram route exposes (private API
    `code`, Graph `shortcode`, Apify `code`) and it never changes for a post, so
    a route swap updates rows instead of duplicating them."""
    assert set(by_id(fetch())) == {CAROUSEL, REEL, VIRAL, PAGE2}


def test_published_at_is_unix_seconds_utc():
    items = by_id(fetch())
    assert items[CAROUSEL].published_at == 1776076960
    assert items[REEL].published_at == 1758533450
    assert items[VIRAL].published_at == 1738831993
    for it in items.values():
        assert isinstance(it.published_at, int)
        # Seconds, not milliseconds: a ms value here lands in the year 57000 and
        # every date-bucketed report silently empties.
        assert 1_000_000_000 < it.published_at < 2_000_000_000


# ── kind, body, duration ────────────────────────────────────────────────────

def test_kind_classification():
    items = by_id(fetch())
    # media_type 8 with two image children, and it stays a carousel even though
    # Instagram would report 8 for a carousel containing video.
    assert items[CAROUSEL].kind == "carousel"
    assert items[REEL].kind == "short_video"   # 65.4s, under the 180s threshold
    assert items[VIRAL].kind == "short_video"


def test_body_and_duration():
    items = by_id(fetch())
    assert items[CAROUSEL].body is None, "a caption-less post must be None, not ''"
    assert items[CAROUSEL].duration_s is None
    assert items[REEL].body.startswith("Coastal Drift Series")
    assert items[REEL].duration_s == pytest.approx(65.433, abs=1e-3)
    assert items[VIRAL].duration_s == pytest.approx(19.851, abs=1e-3)
    assert items[REEL].url == "https://www.instagram.com/p/DANSLHTfZcs/"
    assert items[REEL].title is None and items[REEL].is_repost is False


# ── metrics ─────────────────────────────────────────────────────────────────

def test_metrics_are_mapped():
    items = by_id(fetch())
    assert items[CAROUSEL].metrics["reactions"] == 52
    assert items[CAROUSEL].metrics["comments"] == 1
    assert items[CAROUSEL].metrics["views"] == 846   # content_views_count
    assert items[VIRAL].metrics["reactions"] == 47657
    assert items[VIRAL].metrics["views"] == 1032447
    assert items[VIRAL].metrics["play_count"] == 1032447


def test_vendor_counters_ride_along_to_metrics_raw():
    """media_repost_count is a real engagement number on this payload (2 on the
    viral reel). It is reposts-to-feed, not the Insights `shares` metric, so it
    must survive under its vendor name instead of being dropped or promoted."""
    items = by_id(fetch())
    assert items[VIRAL].metrics["media_repost_count"] == 2
    assert "shares" not in items[VIRAL].metrics


def test_negative_counters_are_absent_not_recorded():
    """Instagram signals 'count not available to you' with -1, not by omitting the
    field. -1 reactions in a client brief is a fabricated number, not a gap."""
    hidden = ig._metrics({"like_count": -1, "comment_count": 3,
                          "content_views_count": -1, "play_count": -1,
                          "like_and_view_counts_disabled": True})
    assert "reactions" not in hidden and "views" not in hidden
    assert hidden["comments"] == 3
    assert hidden["counts_hidden"] is True


def test_owner_only_metrics_are_absent_not_zero():
    """The six metrics this route cannot get must not appear at all.

    A zero saves count in a client brief claims nobody saved the post. Absence
    claims we did not measure it. Only one of those is true here.
    """
    for it in fetch().items:
        for key in ("saves", "shares", "reach", "follows", "avg_watch_time_ms"):
            assert key not in it.metrics, f"{key} must not be fabricated"


# ── media ───────────────────────────────────────────────────────────────────

def test_carousel_slides_are_extracted_in_order():
    car = by_id(fetch())[CAROUSEL]
    slides = [m for m in car.media if m.role == "slide"]
    assert [m.seq for m in slides] == [0, 1]
    assert all(m.mime == "image/jpeg" and m.remote_url for m in slides)
    # Largest candidate wins, not whichever the vendor listed first.
    assert slides[0].width == 1151 and slides[0].height == 1534


def test_a_url_less_candidate_degrades_instead_of_killing_the_batch():
    """One malformed candidate must cost its own media row, not the whole fetch:
    sync.run turns any raise into a failed run for every item in the page."""
    car = json.loads(json.dumps(
        [e.get("media", e) for e in FIXTURE["pages"][0]["items"]][0]))
    cands = car["carousel_media"][0]["image_versions2"]["candidates"]
    biggest = max(cands, key=lambda c: c["width"] * c["height"])
    biggest.pop("url")

    slides = [m for m in ig._item(car).media if m.role == "slide"]
    assert len(slides) == 2, "the second-largest candidate should have stood in"
    assert all(m.remote_url for m in slides)


def test_video_gets_an_original_and_a_poster():
    reel = by_id(fetch())[REEL]
    roles = {m.role: m for m in reel.media}
    assert set(roles) == {"original", "poster"}
    assert roles["original"].mime == "video/mp4"
    assert roles["original"].remote_url.startswith("https://")
    assert roles["poster"].mime == "image/jpeg"


# ── raw and profile ─────────────────────────────────────────────────────────

def test_raw_is_the_untouched_vendor_payload():
    fixture_media = {e.get("media", e)["code"]: e.get("media", e)
                     for page in FIXTURE["pages"] for e in page["items"]}
    for it in fetch().items:
        assert it.raw == fixture_media[it.platform_item_id]
        # Fields we deliberately do not project must still be recoverable.
        assert "organic_tracking_token" in it.raw


def test_profile_is_mapped():
    p = fetch().profile
    assert p.handle == "alex_rivera"
    assert p.platform_uid == "40695738143"
    assert p.display_name == "Alex Rivera"
    assert p.follower_count == 378 and p.following_count == 2104
    assert p.posts_total == 18
    assert p.url == "https://www.instagram.com/alex_rivera/"
    assert p.bio is None, "an empty biography must normalise to None"


# ── walk control ────────────────────────────────────────────────────────────

def test_truncated_when_limit_cuts_a_paginating_feed():
    cut = fetch(limit=2)
    assert cut.truncated is True and len(cut.items) == 2
    # Walked to the end: page 2 says more_available=False, so nothing was lost.
    done = fetch(limit=10)
    assert done.truncated is False and len(done.items) == 4


def test_pagination_hands_the_cursor_back():
    t = StubTransport()
    ig.ADAPTER.fetch("alex_rivera", keys={}, transport=t, limit=10)
    feed_calls = [c for c in t.calls if "/feed/user/" in c[0]]
    assert len(feed_calls) == 2
    assert "max_id" not in feed_calls[0][1]
    assert feed_calls[1][1]["max_id"] == FIXTURE["pages"][0]["next_max_id"]


def test_since_stops_the_walk_and_is_not_truncation():
    r = ig.ADAPTER.fetch("alex_rivera", keys={}, transport=StubTransport(),
                         limit=10, since=1758000000)
    assert [i.platform_item_id for i in r.items] == [CAROUSEL, REEL]
    assert r.truncated is False, "a complete delta is not a truncated backfill"


def test_truncated_when_the_last_page_carries_more_than_the_limit():
    """`count` is a request, not a contract. A final page (more_available=False)
    holding three items while the limit leaves room for one has still lost two,
    and a backfill that reports itself complete there is the worst failure mode."""
    fixture = json.loads(json.dumps(FIXTURE))
    last = fixture["pages"][0]
    last["more_available"], last["next_max_id"] = False, None
    fixture["pages"] = [last]

    r = ig.ADAPTER.fetch("alex_rivera", keys={},
                         transport=StubTransport(fixture), limit=1)
    assert len(r.items) == 1 and len(last["items"]) == 3
    assert r.truncated is True


def test_a_replayed_page_does_not_duplicate_items():
    """Feed windows shift when a post lands mid-walk, so consecutive pages can
    genuinely overlap; a stuck cursor replays one wholesale. Either way the same id
    twice in one FetchResult makes scrape_run.items_seen a lie."""
    fixture = json.loads(json.dumps(FIXTURE))
    page = fixture["pages"][0]
    page["next_max_id"] = "STUCK"

    class Stuck(StubTransport):
        def get(self, path, params=None):          # serves the same page forever
            self.calls.append((path, params or {}))
            return self.profile if "web_profile_info" in path else page

    r = ig.ADAPTER.fetch("alex_rivera", keys={}, transport=Stuck(fixture), limit=50)
    ids = [i.platform_item_id for i in r.items]
    assert len(ids) == len(set(ids)) == 3


def test_target_forms_resolve_to_the_same_handle():
    for target in ("alex_rivera", "@alex_rivera",
                   "https://www.instagram.com/alex_rivera/",
                   # Both arrive by copy-paste; the last path segment is a tab,
                   # not a handle, and would be looked up as one.
                   "https://www.instagram.com/alex_rivera/?hl=en&igsh=abc",
                   "https://www.instagram.com/alex_rivera/reels/"):
        t = StubTransport()
        ig.ADAPTER.fetch(target, keys={}, transport=t, limit=1)
        assert t.calls[0][1]["username"] == "alex_rivera"


def test_missing_credentials_fail_before_any_request():
    with pytest.raises(ig.SessionInvalid) as e:
        ig.ADAPTER.fetch("alex_rivera", keys={"IG_SESSIONID": "x"})
    assert "IG_CSRFTOKEN" in str(e.value)


def test_graph_route_is_a_declared_gap_not_a_silent_one():
    with pytest.raises(NotImplementedError):
        ig.ADAPTER.fetch("alex_rivera", keys={}, route="graph")
    with pytest.raises(ValueError):
        ig.ADAPTER.fetch("alex_rivera", keys={}, route="nonsense")


# ── persistence ─────────────────────────────────────────────────────────────

def _persisted(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    aid = store.upsert_account(conn, platform="instagram", handle="alex_rivera",
                               relationship="own")
    return conn, aid


def test_persist_twice_is_idempotent(tmp_path):
    conn, aid = _persisted(tmp_path)
    first = sync.persist(conn, ig.ADAPTER, aid, fetch())
    assert first == {"seen": 4, "new": 4, "changed": 0}

    second = sync.persist(conn, ig.ADAPTER, aid, fetch())
    assert second == {"seen": 4, "new": 0, "changed": 0}, \
        "re-running an unchanged sync must write nothing new"

    assert conn.execute("SELECT COUNT(*) c FROM item").fetchone()["c"] == 4
    assert conn.execute("SELECT COUNT(*) c FROM metric_snapshot").fetchone()["c"] == 4
    assert conn.execute("SELECT COUNT(*) c FROM account").fetchone()["c"] == 1
    # Media is replaced wholesale per item, so a second pass must not double it.
    # 2 carousel slides + 3 reels x (original + poster).
    assert conn.execute("SELECT COUNT(*) c FROM media").fetchone()["c"] == 8


def test_persist_routes_metrics_to_the_right_columns(tmp_path):
    conn, aid = _persisted(tmp_path)
    sync.persist(conn, ig.ADAPTER, aid, fetch())
    row = conn.execute(
        """SELECT s.reactions, s.comments, s.shares, s.saves, s.views, s.metrics_raw
             FROM metric_snapshot s JOIN item i ON i.id = s.item_id
            WHERE i.platform_item_id = ?""", (VIRAL,)).fetchone()
    assert row["reactions"] == 47657 and row["comments"] == 185
    assert row["views"] == 1032447
    assert row["shares"] is None and row["saves"] is None
    assert json.loads(row["metrics_raw"])["play_count"] == 1032447


def test_persist_records_the_account_snapshot(tmp_path):
    conn, aid = _persisted(tmp_path)
    sync.persist(conn, ig.ADAPTER, aid, fetch())
    row = conn.execute(
        "SELECT followers, following, posts_total FROM account_metric_snapshot"
    ).fetchone()
    assert tuple(row) == (378, 2104, 18)
