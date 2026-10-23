"""ScrapeCreators: projections against recorded responses, paging, the delta stop,
error shapes, and the credit accounting.

No network. Fixtures are synthetic, modelled on real responses recorded 2026-10-23
(1 credit each), trimmed; accounts, text and ids are invented.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus.adapters import scrapecreators as S  # noqa: E402
from corpus.keys import PlanGuardError  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
IG = json.loads((FIX / "scrapecreators_instagram.json").read_text())
TT = json.loads((FIX / "scrapecreators_tiktok.json").read_text())
FB = json.loads((FIX / "scrapecreators_facebook.json").read_text())


class Fake:
    def __init__(self, *pages, status=200):
        self.pages, self.status, self.calls = list(pages), status, []

    def __call__(self, path, params):
        self.calls.append((path, dict(params)))
        return self.status, self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]


def _last_page(d, rows_key):
    """The same payload marked as the end of the feed."""
    d = dict(d)
    d.update({"more_available": False, "has_more": 0, "cursor": None, "next_max_id": None})
    return d


# ── Instagram ────────────────────────────────────────────────────────────────

def test_instagram_projection():
    res = S.fetch_instagram("https://www.instagram.com/bistroharbor/", mode="backfill",
                            transport=Fake(_last_page(IG, "items")), limit=100)
    by = {i.platform_item_id: i for i in res.items}
    # the bare media pk, never the composite "<pk>_<owner>" id
    assert "6130058678941320302" in by and all("_" not in k for k in by)
    img = by["6130058678941320302"]
    assert img.kind == "image" and img.metrics["reactions"] == 3143
    assert img.body == "Hvala za sve, kapetane! 🖤"
    reel = by["8342565887181345998"]
    assert reel.kind == "short_video" and reel.duration_s == pytest.approx(37.733334)
    assert reel.metrics["views"] == 6278
    assert reel.media[0].mime == "video/mp4"
    car = by["2915722160837202064"]
    assert car.kind == "carousel" and len([m for m in car.media if m.role == "slide"]) == 2
    collab = by["5592924313174700498"]
    assert collab.author_handle == "collab_partner", "a collab keeps its real author"
    p = res.profile
    assert (p.handle, p.follower_count, p.following_count, p.posts_total) == (
        "bistroharbor", 3388, 1831, 222)
    assert res.cost_usd == pytest.approx(S.CREDIT_USD)
    assert not res.truncated


def _older(d, days):
    """The page again, as distinct posts `days` older (a stand-in for page two)."""
    items = [dict(i, pk=str(int(i["pk"]) - days), taken_at=i["taken_at"] - days * 86400)
             for i in d["items"]]
    return dict(d, items=items)


def test_instagram_pages_on_the_cursor_and_stops_at_the_limit():
    fake = Fake(IG, _older(IG, 400), _last_page(IG, "items"))
    res = S.fetch_instagram("bistroharbor", mode="backfill", transport=fake, limit=6)
    assert len(fake.calls) == 2 and fake.calls[1][1]["next_max_id"] == "cursor-2"
    assert len(res.items) == 6 and res.truncated
    assert res.cost_usd == pytest.approx(2 * S.CREDIT_USD)


def test_a_delta_stops_at_the_watermark_but_not_behind_a_pinned_post():
    """The stop uses the page's LAST row. A pinned 2021 post on top of page one
    must not end a delta that still has newer posts on page two."""
    pinned_old = dict(IG["items"][1], pk="1", taken_at=1600000000)
    page1 = dict(IG, items=[pinned_old] + IG["items"])
    fake = Fake(page1, _last_page(IG, "items"))
    since = 1700000000          # older than every real fixture item
    S.fetch_instagram("bistroharbor", since=since, transport=fake, limit=1000)
    assert len(fake.calls) == 2, "stopped on page one behind the pinned post"

    fake = Fake(IG, _last_page(IG, "items"))
    newest = max(i["taken_at"] for i in IG["items"])
    res = S.fetch_instagram("bistroharbor", since=newest - 1, transport=fake, limit=1000)
    assert len(fake.calls) == 1, "paged on past the watermark"
    assert all(i.published_at >= newest - 1 for i in res.items)


def test_the_pager_is_bounded():
    fake = Fake(IG)                               # every page says "more"
    S.fetch_instagram("bistroharbor", mode="backfill", transport=fake, limit=10**6)
    assert len(fake.calls) == S.MAX_PAGES


def test_private_profile_is_a_clear_error_at_zero_credits():
    body = {"success": True, "credits_charged": 0, "error": "not_found",
            "errorStatus": 404, "message": "Profile is private"}
    with pytest.raises(S.ProfileUnavailable, match="Profile is private"):
        S.fetch_instagram("someone", transport=Fake(body, status=404))


@pytest.mark.parametrize("status,match", [(401, "invalid"), (402, "out of credits")])
def test_auth_and_credit_errors(status, match):
    with pytest.raises(RuntimeError, match=match):
        S.fetch_instagram("x", transport=Fake({"success": False}, status=status))


def test_credits_left_and_spend_check(monkeypatch):
    assert S.credits_left("k", Fake({"success": True, "creditCount": 7097})) == 7097
    monkeypatch.setattr(S, "credits_left", lambda key, transport=None: 2)
    S.spend_check({"SCRAPECREATORS_API_KEY": "k"}, 24, est_usd=2 * S.CREDIT_USD)
    with pytest.raises(PlanGuardError):
        S.spend_check({"SCRAPECREATORS_API_KEY": "k"}, 600, est_usd=50 * S.CREDIT_USD)


def test_estimate_is_per_request():
    a = next(x for x in S.ADAPTERS if x.platform == "instagram")
    assert a.estimate(1) == a.estimate(12) == pytest.approx(S.CREDIT_USD)
    assert a.estimate(13) == pytest.approx(2 * S.CREDIT_USD)


# ── TikTok ───────────────────────────────────────────────────────────────────

def test_tiktok_projection():
    res = S.fetch_tiktok("https://www.tiktok.com/@student.center.portville",
                         mode="backfill", transport=Fake(_last_page(TT, "aweme_list")))
    v = res.items[0]
    assert v.platform_item_id == "1442045530006243056"
    assert v.kind == "short_video" and v.duration_s == pytest.approx(16.902)
    assert v.metrics == {"views": 1315, "reactions": 75, "comments": 0, "shares": 3,
                         "saves": 0}
    assert [m.role for m in v.media] == ["original", "poster"]
    assert res.profile.follower_count == 1393 and res.profile.posts_total == 107


def test_tiktok_pages_on_max_cursor():
    fake = Fake(TT, _last_page(TT, "aweme_list"))
    S.fetch_tiktok("student.center.portville", mode="backfill", transport=fake, limit=5)
    assert fake.calls[1][1]["max_cursor"] == TT["max_cursor"]


# ── Facebook ─────────────────────────────────────────────────────────────────

def test_facebook_projection():
    res = S.fetch_facebook("https://www.facebook.com/bistroharbor/", mode="backfill",
                           transport=Fake(_last_page(FB, "posts")))
    by = {i.platform_item_id: i for i in res.items}
    post = by["2152249539121341"]
    assert post.kind == "image" and post.metrics["reactions"] == 7276
    assert post.metrics["shares"] == 29 and post.metrics["reaction_types"]["sad"] == 1098
    reel = by["6647166619851090"]
    assert reel.kind == "short_video" and reel.metrics["views"] == 12389
    assert reel.url == "https://www.facebook.com/reel/859039534752585/"
    a = next(x for x in S.ADAPTERS if x.platform == "facebook")
    assert "shares" in a.unmeasured and "views" in a.unmeasured
    assert res.profile is None


def test_no_linkedin_route_from_this_vendor():
    """No person feed exists (openapi.json re-checked 2026-10-23); the company feed
    has no engagement counts. HarvestAPI covers both, so nothing is registered."""
    assert {a.platform for a in S.ADAPTERS} == {"instagram", "tiktok", "facebook"}


def _empty_feed(**user):
    u = dict(IG.get("user") or {}, username="quietbiz", media_count=0, **user)
    return {"items": [], "user": u, "more_available": False, "next_max_id": None}


def test_an_account_with_no_posts_is_a_quiet_success_not_a_failure():
    """Live, 2026-10-23: an own account whose posts were all archived failed every
    night and fell through to Bright Data and an Instagram endpoint from home IP."""
    res = S.fetch_instagram("https://www.instagram.com/quietbiz/", mode="backfill",
                            transport=Fake(_empty_feed()))
    assert res.items == [] and res.empty_confirmed
    assert res.profile.posts_total == 0


def test_a_private_account_stops_the_route_chain():
    with pytest.raises(S.ProfileUnavailable):
        S.fetch_instagram("https://www.instagram.com/quietbiz/", mode="backfill",
                          transport=Fake(_empty_feed(is_private=True)))


def test_no_posts_without_a_profile_block_is_still_an_error():
    with pytest.raises(RuntimeError, match="0 posts"):
        S.fetch_instagram("https://www.instagram.com/quietbiz/", mode="backfill",
                          transport=Fake({"items": [], "more_available": False}))


def test_a_confirmed_empty_account_counts_as_synced(tmp_path):
    from corpus import store, sync
    from corpus.adapters.base import FetchResult, Profile
    conn = store.connect(tmp_path / "c.db")
    aid = store.upsert_account(conn, platform="instagram", handle="quietbiz",
                               relationship="own")
    sync.persist(conn, S.ADAPTERS[0], aid,
                 FetchResult(items=[], profile=Profile(handle="quietbiz", posts_total=0),
                             empty_confirmed=True), mode="backfill")
    assert conn.execute("SELECT last_scraped_at FROM account").fetchone()[0]
