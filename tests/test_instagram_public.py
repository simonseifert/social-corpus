"""instagram_public adapter, offline against a recorded payload.

The fixture is three synthetic rows (invented "Orbit Lab" account), modelled on a real
apidojo/instagram-scraper-api response recorded Aug 2026, covering every
normalisation branch: a reel, a single image, and a six-slide carousel whose slides
1 and 5 are video. The point of pinning it is that a vendor reshaping its output
fails here loudly instead of quietly writing nulls into the corpus.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import store, sync  # noqa: E402
from corpus.adapters import instagram_public as ig  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "instagram_public.json"
ROWS = json.loads(FIXTURE.read_text())
REEL, IMAGE, CAROUSEL = ROWS


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _intercept(monkeypatch, payload):
    """Swap the HTTP call for the fixture and hand back the captured Request."""
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["req"] = req
        seen["timeout"] = timeout
        seen["body"] = json.loads(req.data.decode())
        return _Resp(payload)

    monkeypatch.setattr(ig.urllib.request, "urlopen", fake_urlopen)
    return seen


def _fetch(monkeypatch, payload=ROWS, **kw):
    _intercept(monkeypatch, payload)
    return ig.fetch("orbitlab", keys={"APIFY_TOKEN": "t0k"}, **kw)


# ── normalisation ───────────────────────────────────────────────────────────

def test_reel_normalisation():
    it = ig._item(REEL)
    assert it.platform_item_id == "3901122334455667788", "IG media pk is the dedup key"
    assert it.kind == "short_video"
    assert it.published_at == 1785860295
    assert it.duration_s == pytest.approx(32.432)
    assert it.url == "https://www.instagram.com/p/Ab3-Zq_k9Lm/"
    assert it.body.startswith("Rovers, roll out")
    assert it.title is None and it.lang is None and it.is_repost is False

    assert it.metrics["reactions"] == 84213
    assert it.metrics["comments"] == 912
    assert it.metrics["views"] == 2904417, "playCount is the inclusive view counter"
    assert it.metrics["is_paid_partnership"] is False

    roles = [(m.role, m.mime) for m in it.media]
    assert roles == [("original", "video/mp4"), ("poster", "image/jpeg")]
    assert it.media[0].remote_url.startswith("https://")
    assert (it.media[0].width, it.media[0].height) == (720, 1280)

    assert it.raw == REEL, "raw must be the untouched vendor payload"


def test_image_normalisation():
    it = ig._item(IMAGE)
    assert it.kind == "image"
    assert it.published_at == 1785855094
    assert it.duration_s is None
    assert "views" not in it.metrics, "a still has no view counter to invent"
    assert [(m.role, m.mime, m.seq) for m in it.media] == [("original", "image/jpeg", 0)]


def test_carousel_keeps_its_kind_and_flattens_children():
    """A carousel with a video child is still a carousel, and its children are media
    rows rather than items."""
    it = ig._item(CAROUSEL)
    assert 2 in [s["type"] for s in CAROUSEL["carouselMedia"]], "fixture must cover it"
    assert it.kind == "carousel"
    assert it.duration_s is None

    assert [m.role for m in it.media] == ["slide"] * 6
    assert [m.seq for m in it.media] == [0, 1, 2, 3, 4, 5]
    assert {m.mime for m in it.media} == {"image/jpeg"}, \
        "apidojo returns a poster JPG for video slides, never the MP4"
    assert not any(m.role == "original" for m in it.media)


def test_a_carousel_with_fewer_children_than_it_has_is_still_a_carousel():
    """The flag is the authority, not the child count. A payload that sets isCarousel
    and returns one child would otherwise be filed as a plain image."""
    for children in ([], CAROUSEL["carouselMedia"][:1]):
        row = dict(CAROUSEL, carouselMedia=children)
        assert ig._item(row).kind == "carousel", f"{len(children)} children"


def test_duration_of_the_wrong_type_costs_one_field_not_the_run():
    """duration reaches classify_kind, which compares it against a float. Handing it
    the vendor value raw turns a type change on one field into a TypeError that takes
    every other item in the same paid fetch with it."""
    it = ig._item(dict(REEL, video=dict(REEL["video"], duration="32.432")))
    assert it.duration_s is None
    assert it.kind == "short_video", "still a video, just an unknown length"


def test_view_counter_provenance_survives_a_missing_playCount():
    """playCount is inclusive, viewCount is unique accounts and runs far below it.
    A `views` series that silently switches counters reads as a traffic collapse."""
    both = ig._item(dict(REEL, video=dict(REEL["video"], viewCount=900_000))).metrics
    assert (both["views"], both["video_view_count"]) == (2904417, 900_000)

    only_view = dict(REEL["video"], viewCount=900_000)
    del only_view["playCount"]
    m = ig._item(dict(REEL, video=only_view)).metrics
    assert m["views"] == 900_000
    assert m["video_view_count"] == 900_000, \
        "the unique counter must stay identifiable when it is the only one that came"


def test_published_at_is_derived_not_guessed():
    """Re-derive every timestamp from the ISO string so a vendor switching timezone
    or format cannot slip through on hardcoded numbers alone."""
    from datetime import datetime, timezone
    for row in ROWS:
        want = int(datetime.fromisoformat(
            row["createdAt"].replace("Z", "+00:00")
        ).astimezone(timezone.utc).timestamp())
        assert ig._item(row).published_at == want
        assert isinstance(ig._item(row).published_at, int)


def test_epoch_handles_numeric_forms():
    assert ig._epoch(1785860295) == 1785860295
    assert ig._epoch(1785860295000) == 1785860295, "ms must not land in the year 58500"


def test_hidden_counts_become_null():
    """Instagram reports -1 for counts the owner hid. Storing it would poison every
    average computed off the corpus later."""
    row = dict(REEL, likeCount=-1, isLikeAndViewCountsDisabled=True)
    m = ig._item(row).metrics
    assert "reactions" not in m
    assert m["counts_hidden"] is True


# ── profile ─────────────────────────────────────────────────────────────────

def test_profile_comes_from_the_owner_block():
    p = ig._profile(ROWS, "orbitlab")
    assert p.handle == "orbitlab" and p.display_name == "Orbit Lab"
    assert p.platform_uid == "604213987"
    assert p.follower_count > 10_000_000
    assert p.posts_total == REEL["owner"]["postCount"]


def test_profile_prefers_the_owner_block_that_carries_counts():
    """apidojo trims owner{} to id/username/fullName on some rows -- CAROUSEL in this
    fixture is one. persist() appends an account snapshot unconditionally, so picking
    the thin block writes followers=NULL into the series, which reads as a collapse."""
    assert CAROUSEL["owner"].get("followerCount") is None, "fixture must cover it"

    p = ig._profile([CAROUSEL, REEL], "orbitlab")
    assert p.follower_count == REEL["owner"]["followerCount"]
    assert p.posts_total == REEL["owner"]["postCount"]

    # Nothing to prefer: still a usable Profile for the identity fields.
    thin = ig._profile([CAROUSEL], "orbitlab")
    assert thin.platform_uid == "604213987" and thin.follower_count is None


def test_profile_ignores_a_foreign_owner():
    """On a collab the owner can be the other account. persist() upserts
    Profile.handle as an account row, so taking the first owner blindly would file a
    stranger's follower count under a new account."""
    other = json.loads(json.dumps(REEL))
    other["owner"]["username"] = "orbitlabmoonbase"
    assert ig._profile([other], "orbitlab") is None


# ── request shape ───────────────────────────────────────────────────────────

def test_token_travels_in_the_header_only(monkeypatch):
    seen = _intercept(monkeypatch, ROWS)
    ig.fetch("https://www.instagram.com/orbitlab/?hl=en", keys={"APIFY_TOKEN": "t0k"},
             mode="backfill", limit=50)

    req = seen["req"]
    assert req.get_header("Authorization") == "Bearer t0k"
    assert "t0k" not in req.full_url, "a token in the query string leaks into logs"
    assert "maxTotalChargeUsd=" in req.full_url, "the run needs a hard charge cap"
    assert seen["timeout"] == 310, "below the 300s server cap abandons a billing run"

    assert seen["body"] == {"startUrls": ["https://www.instagram.com/orbitlab/"],
                            "maxItems": 50}, "input keys are schema-verified names"


def test_delta_sends_until_stepped_back_one_day(monkeypatch):
    """`until` is a start bound despite the name, and is day-granular. The overlap
    costs a few idempotent re-fetches and removes the chance of losing a day."""
    seen = _intercept(monkeypatch, ROWS)
    ig.fetch("@orbitlab", keys={"APIFY_TOKEN": "t0k"}, since=1785855094, limit=10)
    assert seen["body"]["until"] == "2026-08-03"

    ig.fetch("orbitlab", keys={"APIFY_TOKEN": "t0k"}, mode="backfill", since=1785855094)
    assert "until" not in seen["body"], "a backfill must not carry a lower bound"


def test_truncated_is_set_only_when_more_exists(monkeypatch):
    assert _fetch(monkeypatch, limit=3).truncated is True, \
        "a full page against a 2417-post profile means more is out there"
    assert _fetch(monkeypatch, limit=99).truncated is False


def test_empty_unfiltered_pull_is_loud(monkeypatch):
    """A renamed input key makes an Apify run SUCCEED with an empty dataset. Recorded
    as ok with 0 items it would read as 'this profile posts nothing'."""
    with pytest.raises(RuntimeError, match="0 rows"):
        _fetch(monkeypatch, payload=[], limit=10)

    empty = _fetch(monkeypatch, payload=[], since=1785855094)
    assert empty.items == [] and empty.truncated is False


def test_cost_matches_observed_billing():
    # verified against /v2/actor-runs: 3 -> $0.005, 9 -> $0.005, 50 -> $0.024
    assert ig._estimate_cost(3) == 0.005
    assert ig._estimate_cost(9) == 0.005
    assert ig._estimate_cost(50) == 0.025
    assert ig.ADAPTER.cost_per_1k == 0.50


# ── persistence ─────────────────────────────────────────────────────────────

def test_persist_twice_is_idempotent(tmp_path, monkeypatch):
    conn = store.connect(tmp_path / "t.db")
    aid = store.upsert_account(conn, platform="instagram", handle="orbitlab",
                               relationship="competitor")

    first = sync.persist(conn, ig.ADAPTER, aid, _fetch(monkeypatch))
    assert (first["seen"], first["new"], first["changed"]) == (3, 3, 0)

    counts = lambda t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]  # noqa: E731
    media_after_first, snaps_after_first = counts("media"), counts("metric_snapshot")
    assert media_after_first == 9, \
        "reel mp4 + its poster, 1 still, 6 slides; a carousel stores no extra poster"

    second = sync.persist(conn, ig.ADAPTER, aid, _fetch(monkeypatch))
    assert second["new"] == 0, "a re-sync of unchanged data must insert nothing"
    assert second["changed"] == 0
    assert counts("item") == 3
    assert counts("media") == media_after_first, "media is replaced, not appended"
    assert counts("metric_snapshot") == snaps_after_first, "metrics are change-only"

    row = conn.execute("SELECT kind, published_at, duration_s, m_views, url FROM item "
                       "WHERE platform_item_id=?", (REEL["id"],)).fetchone()
    assert row["kind"] == "short_video" and row["m_views"] == 2904417
    assert row["published_at"] == 1785860295

    extra = conn.execute("SELECT metrics_raw FROM metric_snapshot ms JOIN item i "
                         "ON i.id=ms.item_id WHERE i.platform_item_id=?",
                         (REEL["id"],)).fetchone()["metrics_raw"]
    assert json.loads(extra)["is_paid_partnership"] is False, \
        "vendor-specific counters ride along in metrics_raw"

    acct = conn.execute("SELECT follower_count FROM account WHERE id=?",
                        (aid,)).fetchone()
    assert acct["follower_count"] == REEL["owner"]["followerCount"]
    assert counts("account") == 1, "the owner block must not spawn a second account"
