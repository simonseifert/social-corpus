"""Bright Data: the async trigger -> progress -> snapshot flow, the record caps, and
the LinkedIn/Instagram projections.

No network. The transport is a fake that plays Bright Data's documented responses;
the fixtures are synthetic records modelled on the vendor's own documented samples
(invented accounts, ids and text, same shape), because the account answered "Customer is not active" on 2026-10-23.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus.adapters import brightdata as B  # noqa: E402
from corpus.keys import PlanGuardError  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
LI = json.loads((FIX / "brightdata_linkedin.json").read_text())
IG = json.loads((FIX / "brightdata_instagram.json").read_text())


class FakeBD:
    """Plays trigger/progress/snapshot. `states` is the progress sequence."""

    def __init__(self, rows, states=("running", "ready"), trigger=(200, None),
                 building=0):
        self.rows, self.states, self.trigger = rows, list(states), trigger
        self.building = building
        self.calls = []

    def __call__(self, method, path, params, body):
        self.calls.append((method, path, dict(params or {}), body))
        if path == "/datasets/v3/trigger":
            st, data = self.trigger
            return st, data if data is not None else {"snapshot_id": "sd_1"}, {}
        if path.startswith("/datasets/v3/progress/"):
            s = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            if s == 429:
                return 429, "slow down", {"Retry-After": "30"}
            return 200, {"status": s, "snapshot_id": "sd_1"}, {}
        if path.endswith("/cancel"):
            return 200, "ok", {}
        if path.startswith("/datasets/v3/snapshot/"):
            if self.building:
                self.building -= 1
                return 202, {"status": "building"}, {}
            return 200, self.rows, {}
        raise AssertionError(path)


def _clock():
    t = [0.0]

    def clock():
        return t[0]

    def sleep(s):
        t[0] += s
    return clock, sleep


# ── the async flow ───────────────────────────────────────────────────────────

def test_trigger_poll_download():
    fake = FakeBD(LI["records"], states=("starting", "running", "ready"))
    clock, sleep = _clock()
    rows, errs = B.collect(B.LINKEDIN_POSTS, "profile_url", [{"url": "u"}], limit=10,
                           transport=fake, sleep=sleep, clock=clock)
    assert len(rows) == 2 and errs == []
    method, path, params, body = fake.calls[0]
    assert (method, params["type"], params["discover_by"]) == ("POST", "discover_new",
                                                               "profile_url")
    assert params["dataset_id"] == B.LINKEDIN_POSTS
    assert [c[1].split("/")[3] for c in fake.calls[1:4]] == ["progress"] * 3


def test_a_building_snapshot_is_waited_for_not_read_as_empty():
    fake = FakeBD(LI["records"], building=2)
    clock, sleep = _clock()
    rows, _ = B.collect(B.LINKEDIN_POSTS, "profile_url", [{}], limit=5, transport=fake,
                        sleep=sleep, clock=clock)
    assert len(rows) == 2


def test_failed_snapshot_raises():
    fake = FakeBD([], states=("failed",))
    clock, sleep = _clock()
    with pytest.raises(B.SnapshotFailed):
        B.collect(B.LINKEDIN_POSTS, "profile_url", [{}], limit=5, transport=fake,
                  sleep=sleep, clock=clock)


def test_timeout_cancels_the_snapshot():
    """Records bill as delivered: a job nobody waits for must not keep running."""
    fake = FakeBD([], states=("running",))
    clock, sleep = _clock()
    with pytest.raises(TimeoutError):
        B.collect(B.LINKEDIN_POSTS, "profile_url", [{}], limit=5, transport=fake,
                  sleep=sleep, clock=clock, timeout_s=60)
    assert fake.calls[-1][1] == "/datasets/v3/snapshot/sd_1/cancel"
    assert sum(1 for c in fake.calls if "progress" in c[1]) <= 8


def test_a_429_while_polling_honours_retry_after():
    """25 x 429 in 5 minutes blacklists the IP on every Bright Data API."""
    fake = FakeBD(LI["records"], states=(429, "ready"))
    waits = []
    rows, _ = B.collect(B.LINKEDIN_POSTS, "profile_url", [{}], limit=5, transport=fake,
                        sleep=waits.append, clock=lambda: 0.0)
    assert waits == [30] and rows


def test_refused_trigger_is_loud():
    fake = FakeBD([], trigger=(400, "Customer is not active"))
    with pytest.raises(RuntimeError, match="Customer is not active"):
        B.collect(B.LINKEDIN_POSTS, "profile_url", [{}], limit=5, transport=fake)


def test_error_records_are_split_out_and_counted_in_cost():
    """Error records are billed too, so cost must count them."""
    fake = FakeBD(LI["records"] + [LI["error_record"]])
    clock, sleep = _clock()
    rows, errs = B.collect(B.LINKEDIN_POSTS, "company_url", [{}], limit=5,
                           transport=fake, sleep=sleep, clock=clock)
    assert len(rows) == 2 and len(errs) == 1


def test_only_error_records_is_an_error_not_an_empty_success():
    fake = FakeBD([LI["error_record"]])
    with pytest.raises(RuntimeError, match="dead_page"):
        B.fetch_linkedin("https://www.linkedin.com/company/does-not-exist/",
                         transport=fake, mode="backfill")


@pytest.fixture(autouse=True)
def _instant(monkeypatch):
    clock, sleep = _clock()
    monkeypatch.setattr(B.time, "sleep", sleep)
    monkeypatch.setattr(B.time, "monotonic", clock)
    # collect()'s defaults were bound at import; route them through the patched time.
    monkeypatch.setattr(B, "collect", _with_time(B.collect, sleep, clock))


def _with_time(fn, sleep, clock):
    def wrapped(*a, **kw):
        kw.setdefault("sleep", sleep)
        kw.setdefault("clock", clock)
        return fn(*a, **kw)
    return wrapped


# ── caps ─────────────────────────────────────────────────────────────────────

def test_the_record_cap_holds_whatever_limit_says():
    fake = FakeBD(LI["records"])
    B.fetch_linkedin("https://www.linkedin.com/in/someone/", limit=100_000,
                     mode="backfill", transport=fake)
    params, body = fake.calls[0][2], fake.calls[0][3]
    assert body["limit_per_input"] == B.MAX_RECORDS_PER_RUN
    assert params["limit_multiple_results"] == B.MAX_RECORDS_PER_RUN


def test_a_small_limit_is_passed_through():
    fake = FakeBD(IG["records"])
    B.fetch_instagram("https://www.instagram.com/bijuexemplo/", limit=7, transport=fake,
                      mode="backfill")
    body = fake.calls[0][3]
    assert body["limit_per_input"] == 7 and body["input"][0]["num_of_posts"] == 7


def test_estimate_never_exceeds_the_cap():
    a = next(x for x in B.ADAPTERS if x.platform == "instagram")
    assert a.estimate(10) == pytest.approx(0.015)
    assert a.estimate(10_000) == pytest.approx(B.MAX_RECORDS_PER_RUN * B.RECORD_USD)


def test_spend_check_refuses_a_sync_that_would_eat_the_free_month():
    B.spend_check({}, B.SYNC_RECORD_BUDGET)
    with pytest.raises(PlanGuardError):
        B.spend_check({}, B.SYNC_RECORD_BUDGET + 1)


# ── LinkedIn projection ──────────────────────────────────────────────────────

def test_linkedin_projection():
    fake = FakeBD(LI["records"])
    res = B.fetch_linkedin("https://www.linkedin.com/company/beispielhof/",
                           mode="backfill", transport=fake)
    assert fake.calls[0][2]["discover_by"] == "company_url"
    by = {i.platform_item_id: i for i in res.items}
    post = by["7381234567890123456"]           # the activity id, same as HarvestAPI's
    assert post.kind == "image" and post.metrics == {"reactions": 2, "comments": 0}
    assert post.published_at == 1773743406
    assert post.body.startswith("Auszeichnung") and not post.is_repost
    vid = by["7381000000000000001"]
    assert vid.kind == "short_video" and vid.duration_s == 42
    assert [m.role for m in vid.media] == ["original", "poster"]
    assert res.profile.follower_count == 388
    assert res.cost_usd == pytest.approx(2 * B.RECORD_USD)


def test_linkedin_delta_sends_an_iso_start_date_and_filters():
    fake = FakeBD(LI["records"])
    since = 1773500000                         # between the two posts
    res = B.fetch_linkedin("https://www.linkedin.com/in/someone/", since=since,
                           transport=fake)
    inp = fake.calls[0][3]["input"][0]
    assert fake.calls[0][2]["discover_by"] == "profile_url"
    assert inp["start_date"].endswith("T00:00:00.000Z")
    assert [i.platform_item_id for i in res.items] == ["7381234567890123456"]


def test_id_falls_back_to_the_activity_in_the_url():
    rec = dict(LI["records"][0], id=None)
    assert B._li_item(rec).platform_item_id == "7381234567890123456"


# ── Instagram projection ─────────────────────────────────────────────────────

def test_instagram_projection():
    fake = FakeBD(IG["records"])
    res = B.fetch_instagram("bijuexemplo", mode="backfill", transport=fake)
    by = {i.platform_item_id: i for i in res.items}
    car = by["3512345678901234567"]            # bare pk, as instagram_public stores
    assert car.kind == "carousel" and len(car.media) == 4
    assert {m.role for m in car.media} == {"slide"}
    assert car.metrics == {"reactions": 51, "comments": 0, "is_paid_partnership": False}
    reel = by["3512000000000000001"]
    assert reel.kind == "short_video" and reel.duration_s == 31.2
    # hidden likes (-1) are absent, not zero; views take the inclusive play counter
    assert "reactions" not in reel.metrics
    assert reel.metrics["views"] == 1500 and reel.metrics["video_view_count"] == 900
    assert res.profile.follower_count == 18352 and res.profile.posts_total == 5214


def test_instagram_unmeasured_is_honest():
    a = next(x for x in B.ADAPTERS if x.platform == "instagram")
    assert set(a.unmeasured) == {"shares", "saves"}
    li = next(x for x in B.ADAPTERS if x.platform == "linkedin")
    assert li.shallow, "Bright Data documents ~10 posts per LinkedIn profile"


def test_instagram_delta_uses_the_vendor_date_format():
    fake = FakeBD(IG["records"])
    B.fetch_instagram("bijuexemplo", since=1744400000, transport=fake)
    sd = fake.calls[0][3]["input"][0]["start_date"]
    assert len(sd) == 10 and sd[2] == "-" and sd[5] == "-"     # MM-DD-YYYY


def test_collect_itself_clamps_so_no_caller_can_skip_the_cap():
    fake = FakeBD(LI["records"])
    B.collect(B.LINKEDIN_POSTS, "profile_url", [{}], limit=10**6, transport=fake)
    assert fake.calls[0][3]["limit_per_input"] == B.MAX_RECORDS_PER_RUN
    assert fake.calls[0][2]["limit_multiple_results"] == B.MAX_RECORDS_PER_RUN
