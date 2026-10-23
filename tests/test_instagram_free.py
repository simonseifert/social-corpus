"""instagram_free adapter, offline against a recorded payload.

FIXTURE PROVENANCE. tests/fixtures/instagram_free.json is synthetic, modelled on
a real web_profile_info response captured cookie-free on 2026-08-06 for a client's
profile, reduced by SELECTION: three of the twelve timeline edges are kept and the
edge_felix_video_timeline edge (a duplicate of an older reel the adapter never
reads) is dropped. Names, captions, ids, shortcodes and media URLs are invented;
the node structure, keys, value types, counts and timestamps are as Instagram sent
them, which is the point -- a vendor reshaping this surface has to fail here
loudly instead of quietly writing nulls into the corpus.

The three cover a reel (DASH duration, a view counter), a ten-slide carousel
whose eighth slide is a video with a real MP4, and a collab post. Values are
cross-checked against the same three posts as fetched by the PAID route and
already stored in the local corpus database, because the whole value of this adapter
is that a free top-up updates paid rows instead of duplicating them.
"""
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import store, sync  # noqa: E402
from corpus.adapters import instagram_free as ig  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "instagram_free.json"
PAYLOAD = json.loads(FIXTURE.read_text())
USER = PAYLOAD["data"]["user"]
NODES = [e["node"] for e in USER["edge_owner_to_timeline_media"]["edges"]]
REEL, CAROUSEL, COLLAB = NODES


class _Resp:
    def __init__(self, body):
        self._b = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, body=b"{}"):
    return urllib.error.HTTPError("https://www.instagram.com/", code, "err", {},
                                  io.BytesIO(body))


def _intercept(monkeypatch, *responses):
    """Swap the HTTP call for canned responses and capture the Requests made.

    time.sleep is stubbed too: the adapter's real pacing floor is 2s per call and
    its backoff doubles from 2s, so an un-stubbed retry test would take 6 seconds
    to assert something that has nothing to do with wall clock.
    """
    seen = {"reqs": [], "sleeps": []}
    queue = list(responses) or [PAYLOAD]

    def fake_urlopen(req, timeout=None):
        seen["reqs"].append(req)
        seen["timeout"] = timeout
        nxt = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(nxt, Exception):
            raise nxt
        return _Resp(nxt)

    monkeypatch.setattr(ig.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ig.time, "sleep", lambda s: seen["sleeps"].append(s))
    return seen


def _fetch(monkeypatch, *responses, **kw):
    _intercept(monkeypatch, *responses)
    return ig.fetch("jordan.novak", **kw)


# ── normalisation ───────────────────────────────────────────────────────────

def test_reel_normalisation():
    it = ig._item(REEL)
    assert it.platform_item_id == "3695457991923281623", \
        "the IG media pk, byte-identical to what the paid actor returns"
    assert it.kind == "short_video"
    assert it.published_at == 1733209369, "taken_at_timestamp is already unix seconds"
    assert isinstance(it.published_at, int)
    assert it.url == "https://www.instagram.com/p/DTcJtIDymeO/"
    assert it.body.startswith("Proud to be recognised by the PORTVILLE BUSINESS AWARDS")
    assert it.title is None and it.lang is None and it.is_repost is False

    assert it.metrics["reactions"] == 108
    assert it.metrics["comments"] == 16
    assert it.metrics["is_pinned"] is True

    assert [(m.role, m.mime) for m in it.media] == [("original", "video/mp4"),
                                                    ("poster", "image/jpeg")]
    assert it.media[0].remote_url.startswith("https://")
    assert (it.media[0].width, it.media[0].height) == (750, 1333)

    assert it.raw is REEL, "raw must be the untouched vendor payload"


def test_duration_comes_from_the_dash_manifest():
    """This surface has no video_duration field at all. Without the manifest every
    reel would file as short_video on a guess rather than on its length."""
    assert "video_duration" not in json.dumps(NODES)
    # The paid actor billed 24.85s for this same post; the manifest says 24.858427.
    assert ig._item(REEL).duration_s == pytest.approx(24.858427)
    assert ig._item(COLLAB).duration_s == pytest.approx(22.893425)
    assert ig._item(CAROUSEL).duration_s is None


def test_iso_duration_parses_every_form_and_refuses_the_rest():
    """A minutes form parsed as seconds would put a 3m48s piece in the short half
    of the short/long split, so an unrecognised form must return None, not a
    number scraped out of the middle of it."""
    assert ig._iso_duration("PT24.858427S") == pytest.approx(24.858427)
    assert ig._iso_duration("PT3M48S") == 228
    assert ig._iso_duration("PT1H2M3.5S") == pytest.approx(3723.5)
    assert ig._iso_duration("PT0S") == 0
    for junk in ("", "24.8", "3M48S", "PT", None, "PTXS"):
        assert ig._iso_duration(junk) is None, junk


def test_a_long_video_crosses_into_long_video():
    """Nothing in the recorded page is over the 180s threshold, so the branch is
    exercised by rewriting the one field that decides it."""
    long_node = dict(REEL, dash_info=dict(
        REEL["dash_info"], video_dash_manifest='mediaPresentationDuration="PT4M1S"'))
    it = ig._item(long_node)
    assert it.duration_s == 241
    assert it.kind == "long_video"

    unreadable = dict(REEL, dash_info={"video_dash_manifest": "<MPD/>"})
    assert ig._item(unreadable).duration_s is None
    assert ig._item(unreadable).kind == "short_video", "still a video, length unknown"


def test_carousel_keeps_its_kind_and_flattens_children():
    it = ig._item(CAROUSEL)
    assert it.kind == "carousel"
    assert it.duration_s is None
    assert [m.role for m in it.media] == ["slide"] * 10
    assert [m.seq for m in it.media] == list(range(10))


def test_a_carousel_video_slide_carries_a_real_mp4():
    """The one thing this route has that apidojo does not. apidojo returns a poster
    JPG for a video slide and never the MP4, which is why a carousel's video slides
    could not be transcribed."""
    mp4 = [m for m in ig._item(CAROUSEL).media if m.mime == "video/mp4"]
    assert len(mp4) == 1 and mp4[0].seq == 7
    assert ".mp4" in mp4[0].remote_url or "/o1/v/" in mp4[0].remote_url

    kid = ig._children(CAROUSEL)[7]
    assert kid["is_video"] and kid["video_url"], "fixture must cover it"
    assert mp4[0].remote_url == kid["video_url"], "the MP4, not the poster"


def test_a_single_image_post_is_one_original_still():
    """DERIVED, not recorded: every one of the twelve posts on the captured page is
    a carousel or a video, so the plain-image branch has no real node to pin. The
    shape is a GraphImage with no children and no video_url, which is what a
    carousel child already looks like in the same payload."""
    kid = ig._children(CAROUSEL)[0]
    assert kid["__typename"] == "GraphImage" and not kid.get("video_url")

    single = dict(CAROUSEL, __typename="GraphImage", is_video=False,
                  display_url=kid["display_url"], dimensions=kid["dimensions"])
    del single["edge_sidecar_to_children"]

    it = ig._item(single)
    assert it.kind == "image"
    assert it.duration_s is None
    assert "video_view_count" not in it.metrics, "a still has no view counter to invent"
    assert [(m.role, m.mime, m.seq) for m in it.media] == [
        ("original", "image/jpeg", 0)]
    assert (it.media[0].width, it.media[0].height) == (1080, 1080)


def test_a_carousel_with_no_children_is_still_a_carousel():
    """__typename is the authority, not the child count. A payload whose children
    did not come through would otherwise file a ten-slide post as a plain image."""
    stripped = dict(CAROUSEL, edge_sidecar_to_children={"edges": []})
    assert ig._item(stripped).kind == "carousel"


def test_collab_is_not_a_repost():
    """Instagram has no native repost. A coauthored post is amplification by two
    accounts at once, not one account resharing another's work."""
    it = ig._item(COLLAB)
    assert it.is_repost is False
    assert it.author_name is None and it.author_handle is None
    assert it.metrics["coauthor_count"] == 1


# ── the view counter, which is the trap ─────────────────────────────────────

def test_views_is_never_set_from_this_route():
    """video_view_count here is NOT the paid route's playCount. Measured on the
    same 8 reels it runs 1.3x to 43x lower with no stable ratio, so a `views`
    series that mixed them would show reach collapsing by up to 97% on a day
    nothing happened. Absent reads as 'not measured'; wrong reads as a disaster."""
    for node in NODES:
        assert "views" not in ig._item(node).metrics

    m = ig._item(REEL).metrics
    assert m["video_view_count"] == 1398
    assert REEL["video_view_count"] == 1398, "fixture must cover it"


def test_hidden_counts_become_null_not_zero():
    """Instagram reports -1 for counts the owner hid. A zero would poison every
    average computed off the corpus later."""
    hidden = dict(REEL, edge_media_preview_like={"count": -1},
                  edge_liked_by={"count": -1},
                  video_view_count=-1, like_and_view_counts_disabled=True)
    m = ig._item(hidden).metrics
    assert "reactions" not in m
    assert "video_view_count" not in m
    assert m["counts_hidden"] is True


def test_a_missing_caption_is_none_not_empty_string():
    assert ig._item(dict(REEL, edge_media_to_caption={"edges": []})).body is None


# ── ordering ────────────────────────────────────────────────────────────────

def test_pinned_posts_are_re_sorted_before_limit_applies(monkeypatch):
    """Vendor order is NOT chronological: pinned posts are hoisted to the front
    whatever their age. Taking the vendor's first entry would hand a limit-1
    freshness probe a pinned 2024 reel to compare against the newest stored post,
    forever."""
    vendor = [n["taken_at_timestamp"] for n in NODES]
    assert vendor != sorted(vendor, reverse=True), "fixture must cover it"

    got = [i.published_at for i in _fetch(monkeypatch).items]
    assert got == sorted(vendor, reverse=True)

    top = _fetch(monkeypatch, limit=1).items
    assert len(top) == 1 and top[0].published_at == max(vendor)


# ── request shape ───────────────────────────────────────────────────────────

def test_the_request_carries_no_credential_of_any_kind(monkeypatch):
    """The moment this route carries a cookie it stops being the public route and
    becomes instagram_client, with that adapter's consent rules attached."""
    seen = _intercept(monkeypatch)
    ig.fetch("https://www.instagram.com/jordan.novak/?hl=en", keys={"APIFY_TOKEN": "t0k"})

    req = seen["reqs"][0]
    assert req.get_header("X-ig-app-id") == "936619743392459", \
        "without the app id Instagram serves the login page at HTTP 200"
    assert req.get_header("Cookie") is None
    assert req.get_header("Authorization") is None
    assert "t0k" not in req.full_url and "t0k" not in str(req.headers)
    assert req.full_url == (
        "https://www.instagram.com/api/v1/users/web_profile_info/"
        "?username=jordan.novak")
    assert seen["timeout"] == 30

    assert ig.ADAPTER.needs == (), "a route with no key must not claim to need one"


def test_handle_accepts_the_shapes_a_human_pastes():
    for target in ("jordan.novak", "@jordan.novak", "instagram.com/jordan.novak",
                   "https://www.instagram.com/jordan.novak/?hl=en"):
        assert ig._handle(target) == "jordan.novak", target
    with pytest.raises(ValueError):
        ig._handle("")


def test_calls_are_paced(monkeypatch):
    """The block is per-IP and slow to forgive, so spacing has to survive across
    calls rather than living inside a paging loop this route does not have."""
    seen = _intercept(monkeypatch)
    monkeypatch.setattr(ig, "_last_request_at", 0.0)
    monkeypatch.setattr(ig.time, "monotonic", lambda: 100.0)

    ig.fetch("jordan.novak")
    ig.fetch("jordan.novak")
    assert seen["sleeps"][-1] == pytest.approx(ig.MIN_INTERVAL_S), \
        "a second call on the heels of the first has to wait"


# ── failure modes ───────────────────────────────────────────────────────────

def test_a_throttle_raises_and_is_never_retried(monkeypatch):
    """Measured: the 401 does not clear in the "few minutes" it claims, it applies
    to every instagram.com endpoint, and retrying is what extends it."""
    seen = _intercept(monkeypatch, _http_error(401, b'{"require_login": true}'))
    with pytest.raises(ig.Throttled) as e:
        ig.fetch("jordan.novak")
    assert len(seen["reqs"]) == 1, "a soft block must not be hammered"
    assert "do not retry" in str(e.value)

    _intercept(monkeypatch, _http_error(429))
    with pytest.raises(ig.Throttled):
        ig.fetch("jordan.novak")


def test_throttling_reaches_the_run_log_rather_than_killing_the_roster(tmp_path,
                                                                      monkeypatch):
    """sync.run guards adapter failures, and a per-IP block is exactly the case
    where the remaining accounts must still be attempted."""
    _intercept(monkeypatch, _http_error(401))
    conn = store.connect(tmp_path / "t.db")
    aid = store.upsert_account(conn, platform="instagram", handle="jordan.novak",
                               relationship="competitor")
    out = sync.run(conn, ig.ADAPTER, account_id=aid, target="jordan.novak")
    assert out["status"] == "error" and "Throttled" in out["error"] or \
        "throttling" in out["error"]
    assert conn.execute("SELECT status FROM scrape_run").fetchone()["status"] == "error"


def test_transport_failures_back_off_and_then_succeed(monkeypatch):
    seen = _intercept(monkeypatch, urllib.error.URLError("boom"),
                      _http_error(503), PAYLOAD)
    assert len(ig.fetch("jordan.novak").items) == 3
    assert len(seen["reqs"]) == 3
    assert seen["sleeps"][-3:] == [2.0, 4.0, ig.MIN_INTERVAL_S] or \
        [s for s in seen["sleeps"] if s in (2.0, 4.0)] == [2.0, 4.0], \
        "backoff has to actually double"


def test_transport_failure_gives_up_loudly(monkeypatch):
    _intercept(monkeypatch, urllib.error.URLError("boom"))
    with pytest.raises(RuntimeError, match="unreachable"):
        ig.fetch("jordan.novak")


def test_a_200_that_is_not_json_is_a_block_wearing_a_success_code(monkeypatch):
    """The HTML profile page and /embed/captioned/ both return 200 empty shells."""
    seen = _intercept(monkeypatch, b"<!DOCTYPE html><html>login</html>")
    with pytest.raises(RuntimeError, match="unreachable"):
        ig.fetch("jordan.novak")
    assert len(seen["reqs"]) == ig.TRANSIENT_ATTEMPTS


def test_a_missing_profile_says_so(monkeypatch):
    _intercept(monkeypatch, _http_error(404))
    with pytest.raises(RuntimeError, match="no instagram profile"):
        ig.fetch("jordan.novak")

    _intercept(monkeypatch, {"data": {"user": None}, "status": "ok"})
    with pytest.raises(RuntimeError, match="no user block"):
        ig.fetch("jordan.novak")


def test_an_empty_grid_is_loud_unless_the_account_really_is_empty(monkeypatch):
    """A private account and a block both look exactly like "this profile posts
    nothing" once persisted, and that is a claim the corpus must not make by
    accident."""
    def page(**over):
        u = dict(USER, **over)
        u["edge_owner_to_timeline_media"] = dict(
            USER["edge_owner_to_timeline_media"], edges=[], **over.pop("tl", {}))
        return {"data": {"user": u}, "status": "ok"}

    _intercept(monkeypatch, page(is_private=True))
    with pytest.raises(RuntimeError, match="private"):
        ig.fetch("jordan.novak")

    _intercept(monkeypatch, page())
    with pytest.raises(RuntimeError, match="returned none"):
        ig.fetch("jordan.novak")

    empty = dict(USER, is_private=False)
    empty["edge_owner_to_timeline_media"] = {"count": 0, "edges": [],
                                             "page_info": {"has_next_page": False}}
    res = _fetch(monkeypatch, {"data": {"user": empty}, "status": "ok"})
    assert res.items == [] and res.truncated is False
    assert res.profile.posts_total == 0, "an account with no posts is still an account"


# ── profile ─────────────────────────────────────────────────────────────────

def test_profile_is_returned_in_full_and_for_free():
    p = ig._profile(USER, "jordan.novak")
    assert p.handle == "jordan.novak"
    assert p.display_name.startswith("Jordan Novak")
    assert p.bio.startswith("Helping teams sell better")
    assert p.platform_uid == "6116877230"
    assert (p.follower_count, p.following_count, p.posts_total) == (3100, 4196, 191)
    assert p.url == "https://www.instagram.com/jordan.novak/"


def test_profile_raw_does_not_carry_a_second_copy_of_the_feed():
    """account.raw_json is rewritten on every sync. Carrying twelve full posts in
    it would rewrite a quarter-megabyte per account per run for a duplicate."""
    p = ig._profile(USER, "jordan.novak")
    assert "edge_owner_to_timeline_media" not in p.raw
    assert "edge_felix_video_timeline" not in p.raw
    assert p.raw["biography"] == USER["biography"], "the rest is kept intact"


def test_a_renamed_account_does_not_branch_the_corpus():
    """persist() upserts Profile.handle as an account row, so returning the new
    name would create a second account while the items kept landing under the
    old one."""
    assert ig._profile(USER, "someone.else") is None


# ── cost, ranking, persistence ──────────────────────────────────────────────

def test_it_is_free_and_says_so(monkeypatch):
    assert ig.ADAPTER.cost_per_1k == 0.0
    assert _fetch(monkeypatch).cost_usd == 0.0, \
        "an explicit zero, so the run row records a measured free fetch"


def test_a_free_but_capped_route_must_not_outrank_a_complete_one():
    """for_platform sorts on price, and this route is the cheapest thing on the
    platform. Without `shallow` every instagram backfill would silently become a
    twelve-post top-up."""
    from corpus.adapters import for_platform

    assert ig.ADAPTER.shallow is True
    for rel in ("competitor", "influencer", "own", "client"):
        names = [a.name for a in for_platform("instagram", rel)]
        assert names[-1] == "instagram_free", rel
        assert "instagram_public" in names


def test_truncated_is_honest_about_the_ceiling(monkeypatch):
    """Nearly always True, because twelve entries out of a 191-post account is
    what this route is. A backfill that reads as complete is worse than one that
    fails."""
    assert _fetch(monkeypatch, mode="backfill").truncated is True
    assert _fetch(monkeypatch, limit=2).truncated is True

    # A delta whose window sits inside the page IS complete: everything newer
    # than `since` came back, so flagging it would make every delta look partial.
    oldest = min(n["taken_at_timestamp"] for n in NODES)
    assert _fetch(monkeypatch, since=oldest).truncated is False
    assert _fetch(monkeypatch, since=oldest - 1).truncated is True

    newest = max(n["taken_at_timestamp"] for n in NODES)
    only_new = _fetch(monkeypatch, since=newest)
    assert [i.published_at for i in only_new.items] == [newest]
    assert only_new.truncated is False


def test_a_short_account_is_not_truncated(monkeypatch):
    """The one case where this route is complete: fewer posts than the page holds."""
    u = json.loads(json.dumps(USER))
    u["edge_owner_to_timeline_media"]["count"] = 3
    u["edge_owner_to_timeline_media"]["page_info"] = {"has_next_page": False}
    res = _fetch(monkeypatch, {"data": {"user": u}, "status": "ok"})
    assert len(res.items) == 3 and res.truncated is False


def test_persist_twice_is_idempotent(tmp_path, monkeypatch):
    conn = store.connect(tmp_path / "t.db")
    aid = store.upsert_account(conn, platform="instagram", handle="jordan.novak",
                               relationship="competitor")

    first = sync.persist(conn, ig.ADAPTER, aid, _fetch(monkeypatch))
    assert (first["seen"], first["new"], first["changed"]) == (3, 3, 0)

    counts = lambda t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]  # noqa: E731
    media_first, snaps_first = counts("media"), counts("metric_snapshot")
    assert media_first == 14, "reel mp4 + poster, 10 slides, collab mp4 + poster"

    second = sync.persist(conn, ig.ADAPTER, aid, _fetch(monkeypatch))
    assert (second["new"], second["changed"]) == (0, 0)
    assert counts("item") == 3
    assert counts("media") == media_first, "media is replaced, not appended"
    assert counts("metric_snapshot") == snaps_first, "metrics are change-only"
    assert counts("account") == 1

    row = conn.execute("SELECT kind, published_at, duration_s, m_views, source "
                       "FROM item WHERE platform_item_id=?",
                       (REEL["id"],)).fetchone()
    assert row["kind"] == "short_video"
    assert row["published_at"] == 1733209369
    assert row["duration_s"] == pytest.approx(24.858427)
    assert row["m_views"] is None, "the wrong view counter must never reach the column"
    assert row["source"] == "instagram_free"

    extra = conn.execute("SELECT metrics_raw FROM metric_snapshot ms JOIN item i "
                         "ON i.id=ms.item_id WHERE i.platform_item_id=?",
                         (REEL["id"],)).fetchone()["metrics_raw"]
    assert json.loads(extra)["video_view_count"] == 1398, \
        "it still rides along, just never as `views`"


def test_a_free_top_up_updates_the_paid_rows_instead_of_duplicating_them(tmp_path,
                                                                        monkeypatch):
    """The whole point of the route. Both adapters key on the same IG media pk, so
    a paid backfill followed by a free delta must leave one row per post."""
    from corpus.adapters import instagram_public as paid

    conn = store.connect(tmp_path / "t.db")
    aid = store.upsert_account(conn, platform="instagram", handle="jordan.novak",
                               relationship="competitor")

    # The paid actor's shape for the same reel, carrying what it alone has (a
    # playCount) and missing what it alone lacks (a duration).
    paid_row = {"id": REEL["id"], "code": REEL["shortcode"],
                "createdAt": "2024-12-03T07:02:49Z", "caption": "old caption",
                "isVideo": True, "video": {"url": "https://cdn/x.mp4",
                                           "playCount": 4430},
                "owner": {"id": "6116877230", "username": "jordan.novak",
                          "followerCount": 3100, "postCount": 191}}
    sync.persist(conn, paid.ADAPTER, aid,
                 paid.FetchResult(items=[paid._item(paid_row)],
                                  profile=paid._profile([paid_row], "jordan.novak")))
    sync.persist(conn, ig.ADAPTER, aid, _fetch(monkeypatch))

    rows = conn.execute("SELECT COUNT(*) c FROM item WHERE platform_item_id=?",
                        (REEL["id"],)).fetchone()["c"]
    assert rows == 1, "one post, one row, across two routes"

    row = conn.execute("SELECT duration_s, m_views, source FROM item "
                       "WHERE platform_item_id=?", (REEL["id"],)).fetchone()
    assert row["duration_s"] == pytest.approx(24.858427), \
        "the free route fills the duration the paid one never had"

    views = [r["views"] for r in conn.execute(
        "SELECT ms.views FROM metric_snapshot ms JOIN item i ON i.id=ms.item_id "
        "WHERE i.platform_item_id=? ORDER BY ms.captured_at", (REEL["id"],))]
    assert views == [4430, None], \
        "the paid reading survives; the free pass records 'not measured', not 3x lower"

    # "Not measured" and "measured as absent" are now told apart by the route:
    # this one declares views unmeasured, so the item keeps the paid 4430 while the
    # history above still records that the free pass did not see it.
    assert row["m_views"] == 4430


def test_the_paid_route_restores_the_view_cache_on_its_next_pass(tmp_path,
                                                                monkeypatch):
    """The seam above is transient, which is what makes it a caveat and not a bug."""
    from corpus.adapters import instagram_public as paid

    conn = store.connect(tmp_path / "t.db")
    aid = store.upsert_account(conn, platform="instagram", handle="jordan.novak",
                               relationship="competitor")
    paid_row = {"id": REEL["id"], "code": REEL["shortcode"],
                "createdAt": "2024-12-03T07:02:49Z", "caption": "c", "isVideo": True,
                "video": {"url": "https://cdn/x.mp4", "playCount": 4430}}
    as_paid = lambda: paid.FetchResult(items=[paid._item(paid_row)])  # noqa: E731

    sync.persist(conn, ig.ADAPTER, aid, _fetch(monkeypatch))
    sync.persist(conn, paid.ADAPTER, aid, as_paid())
    assert conn.execute("SELECT m_views FROM item WHERE platform_item_id=?",
                        (REEL["id"],)).fetchone()["m_views"] == 4430
