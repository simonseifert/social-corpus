"""HarvestAPI direct route: pagination, the delta stop, and parity with the actor.

No network. The fixture is a synthetic Apify actor payload: the direct API returns
the same records, which is the premise of reusing linkedin_posts._item.
"""
import json
import sys
import urllib.error
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus.adapters import linkedin_harvest as H, linkedin_posts as li  # noqa: E402

RECS = json.loads((Path(__file__).parent / "fixtures" / "linkedin_posts.json").read_text())
URL = "https://www.linkedin.com/in/caseymorganai/"


class _Resp:
    def __init__(self, data):
        self._d = json.dumps(data).encode()

    def read(self):
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False


def _pages(monkeypatch, recs, size):
    """Serve `recs` newest-first in pages of `size`, like the API."""
    recs = sorted(recs, key=lambda r: -(li._item(r).published_at if li._item(r) else 0))
    calls = []

    def fake(req, timeout=None):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query))
        calls.append(q)
        assert req.get_header("X-api-key") == "k"
        page = int(q["page"])
        chunk = recs[(page - 1) * size: page * size]
        total = -(-len(recs) // size)
        return _Resp({"elements": chunk, "pagination": {
            "pageNumber": page, "totalPages": total,
            "paginationToken": f"t{page}" if page < total else None}})
    monkeypatch.setattr(H.urllib.request, "urlopen", fake)
    return calls


def _many(n):
    """n distinct posts a day apart, cloned from the first recorded one."""
    base, out = RECS[0], []
    oid, ts = str(base["id"]), base["postedAt"]["timestamp"]
    for k in range(n):
        txt = json.dumps(base).replace(oid, str(int(oid) + k + 1))
        r = json.loads(txt)
        r["postedAt"]["timestamp"] = ts - k * 86_400_000
        r["postedAt"].pop("date", None)
        out.append(r)
    return out


def test_payload_parses_like_the_actor(monkeypatch):
    _pages(monkeypatch, RECS, size=len(RECS))
    got = H.fetch(URL, mode="backfill", keys={"HARVESTAPI_KEY": "k"}, limit=1000)
    want = [i for i in map(li._item, RECS) if i]
    assert {i.platform_item_id for i in got.items} == {i.platform_item_id for i in want}
    assert not got.truncated


def test_pages_follow_the_token_and_stop_at_the_limit(monkeypatch):
    calls = _pages(monkeypatch, _many(10), size=2)
    got = H.fetch(URL, mode="backfill", keys={"HARVESTAPI_KEY": "k"}, limit=3)
    assert len(got.items) == 3 and got.truncated
    assert len(calls) == 2 and calls[1]["paginationToken"] == "t1"


def test_a_delta_stops_paging_at_the_watermark(monkeypatch):
    """No maxTotalChargeUsd here: walking past `since` would bill pages for posts
    corpus already holds."""
    recs = _many(10)
    calls = _pages(monkeypatch, recs, size=2)
    stamps = sorted((i.published_at for i in map(li._item, recs) if i), reverse=True)
    assert len(stamps) == 10
    since = stamps[2]                     # the 3rd-newest post
    got = H.fetch(URL, mode="delta", keys={"HARVESTAPI_KEY": "k"}, since=since,
                  limit=1000)
    assert all(i.published_at >= since for i in got.items)
    assert len(calls) == 2, "paged on past the watermark"


def test_the_runaway_pager_is_bounded(monkeypatch):
    calls = _pages(monkeypatch, _many(60), size=1)
    H.fetch(URL, mode="backfill", keys={"HARVESTAPI_KEY": "k"}, limit=10_000)
    assert len(calls) == H.MAX_PAGES


def test_routed_at_its_measured_price_ahead_of_the_actor():
    """Registered once the trial measured it: $0.004/page of 100 -> $0.04/1k."""
    from corpus.adapters import REGISTRY, for_platform
    a = REGISTRY["linkedin_harvest"]
    assert a.cost_per_1k == 0.04
    names = [x.name for x in for_platform("linkedin", "competitor")]
    assert names.index("linkedin_harvest") < names.index("linkedin_posts")


def test_cost_is_what_the_vendor_reported_per_page(monkeypatch):
    """Each response carries `cost`; a 2-page fetch reports their sum, not a guess."""
    recs = _many(4)

    def fake(req, timeout=None):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query))
        page = int(q["page"])
        return _Resp({"elements": recs[(page - 1) * 2: page * 2], "cost": 0.004,
                      "pagination": {"totalPages": 2,
                                     "paginationToken": "t" if page < 2 else None}})
    monkeypatch.setattr(H.urllib.request, "urlopen", fake)
    got = H.fetch(URL, mode="backfill", keys={"HARVESTAPI_KEY": "k"}, limit=100)
    assert got.cost_usd == 0.008


def test_estimate_prices_whole_pages():
    assert H.estimate(1) == H.estimate(100) == 0.004
    assert H.estimate(101) == 0.008
    assert H.estimate(10**6) == H.MAX_PAGES * 0.004, "MAX_PAGES bounds the estimate"


def test_balance_reads_usage_balance_not_total_balance():
    """The 2026-10-22 trial printed 'credits before: None': the balance lives at
    usage.balance, and user.totalBalance is the package size, not what is left."""
    info = {"user": {"totalBalance": 1}, "usage": {"balance": 0.988}}
    assert H.balance(info) == 0.988
    assert H.balance({"user": {"totalBalance": 1}}) is None


def test_spend_check_refuses_when_the_balance_cannot_cover_the_run(monkeypatch):
    import pytest
    from corpus.keys import PlanGuardError
    monkeypatch.setattr(H, "account", lambda key: {"usage": {"balance": 0.01}})
    H.spend_check({"HARVESTAPI_KEY": "k"}, 100)                 # one page: fine
    with pytest.raises(PlanGuardError):
        H.spend_check({"HARVESTAPI_KEY": "k"}, 1000)            # ten pages: $0.04


def test_a_429_is_retried_after_a_wait(monkeypatch):
    """Free tier: one concurrent request. A parallel sync gets 429s, not failures."""
    import io
    calls, waits = [], []

    def fake(req, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "busy", {}, io.BytesIO(b""))
        return _Resp({"elements": RECS, "pagination": {}})
    monkeypatch.setattr(H.urllib.request, "urlopen", fake)
    monkeypatch.setattr(H.time, "sleep", waits.append)
    got = H.fetch(URL, mode="backfill", keys={"HARVESTAPI_KEY": "k"}, limit=1000)
    assert got.items and len(calls) == 2 and waits == [H.RETRY_WAITS_S[0]]


def test_company_pages_use_the_company_endpoint(monkeypatch):
    seen = []

    def fake(req, timeout=None):
        seen.append(urllib.parse.urlsplit(req.full_url))
        return _Resp({"elements": RECS, "pagination": {}})
    monkeypatch.setattr(H.urllib.request, "urlopen", fake)
    H.fetch("https://www.linkedin.com/company/harbor-ferries/", mode="backfill",
            keys={"HARVESTAPI_KEY": "k"}, limit=1000)
    q = dict(urllib.parse.parse_qsl(seen[0].query))
    assert seen[0].path == "/linkedin/company-posts"
    assert q["company"] == "https://www.linkedin.com/company/harbor-ferries/"


def test_recorded_company_page_parses_with_counts_and_vendor_cost(monkeypatch):
    """A company page, page 1, modelled on a 2026-10-23 response (3 of 86 kept)."""
    page = json.loads((Path(__file__).parent / "fixtures" / "harvest_company.json")
                      .read_text())
    last = dict(page, pagination={"totalPages": 1, "paginationToken": None})
    monkeypatch.setattr(H.urllib.request, "urlopen", lambda req, timeout=None: _Resp(last))
    got = H.fetch("https://www.linkedin.com/company/harbor-ferries/", mode="backfill",
                  keys={"HARVESTAPI_KEY": "k"}, limit=100)
    assert len(got.items) == 3 and got.cost_usd == 0.004
    assert all(i.metrics.get("reactions") is not None for i in got.items)
    assert got.profile and got.profile.follower_count and got.profile.display_name == "Harbor Ferries"
