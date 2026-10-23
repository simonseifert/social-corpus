"""Discovery and freshness tests.

Discovery's contract is mostly about what it must NOT do: never turn a name into an
answer, never mistake a post URL for a profile. Freshness's contract is that a quiet
account costs one item instead of a full pull.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import discover as D  # noqa: E402
from corpus import freshness as F  # noqa: E402
from corpus import store  # noqa: E402
from corpus.adapters.base import FetchResult, RawItem  # noqa: E402


# ── parsing ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,platform,handle", [
    ("https://www.linkedin.com/in/alexrivera/", "linkedin", "alexrivera"),
    ("linkedin.com/in/alexrivera", "linkedin", "alexrivera"),
    ("http://de.linkedin.com/in/Alex-Rivera-45b333231", "linkedin",
     "alex-rivera-45b333231"),
    ("https://www.linkedin.com/company/example-media/", "linkedin", "example-media"),
    ("https://www.instagram.com/alex_rivera/", "instagram", "alex_rivera"),
    ("instagram.com/alex.rivera", "instagram", "alex.rivera"),
    ("@alex_rivera", "instagram", "alex_rivera"),
])
def test_parses_profile_refs(raw, platform, handle):
    t = D.parse_target(raw)
    assert t is not None, raw
    assert (t.platform, t.handle) == (platform, handle)


@pytest.mark.parametrize("raw", [
    "https://www.instagram.com/p/DJGvRJ1iNt0/",      # a post
    "https://www.instagram.com/reel/ABC123/",         # a reel
    "https://www.instagram.com/stories/someone/1/",   # a story
    "https://www.instagram.com/explore/tags/x/",      # platform furniture
    "Alex Rivera",                                  # a name
    "",
])
def test_rejects_non_profiles(raw):
    assert D.parse_target(raw) is None, f"should not parse: {raw}"


def test_company_vs_person_kind():
    assert D.parse_target("linkedin.com/company/acme").kind == "company"
    assert D.parse_target("linkedin.com/in/acme").kind == "person"


# ── ranking ─────────────────────────────────────────────────────────────────

def _hit(url, title, snippet=""):
    return {"url": url, "title": title, "snippet": snippet}


def test_hints_lift_the_right_person_above_namesakes(monkeypatch):
    """The real case: four German Alex Riveras outrank the correct profile on
    name alone. A location hint is what separates them."""
    monkeypatch.setattr(D, "_reach_search", lambda q, timeout=60: [
        _hit("https://linkedin.com/in/alex-rivera-novaholding",
             "Alex Rivera", "Founder Nova Holding Berlin"),
        _hit("https://linkedin.com/in/alexrivera",
             "Alex Rivera", "I build and improve digital products Portville Croatia"),
        _hit("https://linkedin.com/in/alex-rivera-a8445b228",
             "Alex Rivera", "Product Manager Bearings Munich Germany"),
    ])
    got = D.find_by_name("Alex Rivera", platforms=("linkedin",),
                         hints={"location": ["Portville", "Croatia"]})
    assert got[0].target.handle == "alexrivera"
    assert got[0].score > got[1].score
    assert any("location" in s for s in got[0].signals)


def test_auto_generated_slugs_are_penalised(monkeypatch):
    monkeypatch.setattr(D, "_reach_search", lambda q, timeout=60: [
        _hit("https://linkedin.com/in/alex-rivera-006592150", "Alex Rivera", ""),
        _hit("https://linkedin.com/in/alexrivera", "Alex Rivera", ""),
    ])
    got = D.find_by_name("Alex Rivera", platforms=("linkedin",))
    assert got[0].target.handle == "alexrivera"
    assert any("auto slug" in s for s in got[-1].signals)


def test_post_urls_never_become_candidates(monkeypatch):
    """Instagram name search mostly returns captions, not profiles."""
    monkeypatch.setattr(D, "_reach_search", lambda q, timeout=60: [
        _hit("https://www.instagram.com/p/DJGvRJ1iNt0/", "ALEX C RIVAS on Instagram"),
        _hit("https://www.instagram.com/alex_rivera/", "Alex Rivera"),
    ])
    got = D.find_by_name("Alex Rivera", platforms=("instagram",))
    assert [c.target.handle for c in got] == ["alex_rivera"]


def test_search_failure_returns_empty_not_exception(monkeypatch):
    monkeypatch.setattr(D, "_reach_search", lambda q, timeout=60: [])
    assert D.find_by_name("Nobody At All") == []


def test_resolve_splits_urls_from_names(monkeypatch):
    monkeypatch.setattr(D, "_reach_search", lambda q, timeout=60: [
        _hit("https://linkedin.com/in/someone", "Some One")])
    targets, cands = D.resolve(
        ["https://www.linkedin.com/in/alexrivera/", "Some One"])
    assert [t.handle for t in targets] == ["alexrivera"]
    assert cands and cands[0].target.handle == "someone"


# ── freshness ───────────────────────────────────────────────────────────────

def _acct(conn, handle="x", rel="competitor", last_scraped=None):
    aid = store.upsert_account(conn, platform="linkedin", handle=handle,
                               relationship=rel)
    if last_scraped is not None:
        conn.execute("UPDATE account SET last_scraped_at=? WHERE id=?",
                     (last_scraped, aid))
        conn.commit()
    return aid


def _item(conn, aid, pid="p1", when=None):
    iid, _, _ = store.upsert_item(conn, account_id=aid, platform="linkedin",
                                  platform_item_id=pid, kind="text",
                                  published_at=when or store.now(), raw={})
    conn.commit()
    return iid


def test_empty_account_is_a_backfill(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    _acct(conn)
    assert F.plan(conn)[0].action == "backfill"


def test_recently_synced_account_is_skipped(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    aid = _acct(conn, rel="competitor", last_scraped=store.now() - 3600)
    _item(conn, aid)
    d = F.plan(conn)[0]
    assert d.action == "skip" and "fresh" in d.reason


def test_expired_ttl_becomes_a_probe(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    aid = _acct(conn, rel="competitor", last_scraped=store.now() - 5 * 86400)
    _item(conn, aid)
    d = F.plan(conn)[0]
    assert d.action == "probe" and d.limit == 1


def test_very_stale_account_widens_to_backfill(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    aid = _acct(conn, last_scraped=store.now() - 60 * 86400)
    _item(conn, aid)
    assert F.plan(conn)[0].action == "backfill"


def test_client_ttl_is_tighter_than_competitor():
    assert F.ttl_for("client") < F.ttl_for("competitor")
    assert F.ttl_for("own") < F.ttl_for("client")


def test_force_overrides_everything(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    aid = _acct(conn, last_scraped=store.now())
    _item(conn, aid)
    assert F.plan(conn, force=True)[0].action == "sync"


class _Stub:
    name = "stub"
    platform = "linkedin"
    needs = ()
    unmeasured = ()

    def __init__(self, items):
        self._items = items

    def fetch(self, target, **kw):
        return FetchResult(items=self._items)


def _probe(conn, adapter, aid) -> bool:
    """A probe through the production path (sync.execute + sync.record), the one
    `corpus sync` runs. True means the full fetch went ahead."""
    from corpus import sync
    job = sync.Job(account_id=aid, target="url", routes=[adapter], probe=True,
                   newest_id=F.newest_stored(conn, aid)[0], fetch_kwargs={"limit": 5})
    sync.record(conn, sync.execute(job))
    return job.has_work


def test_probe_says_no_work_when_newest_is_unchanged(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    aid = _acct(conn)
    _item(conn, aid, "p1")
    adapter = _Stub([RawItem(platform_item_id="p1", kind="text",
                             published_at=store.now(), raw={})])
    assert _probe(conn, adapter, aid) is False


def test_probe_records_metric_movement_even_when_skipping(tmp_path):
    """The probe already paid for the item; take the engagement delta from it."""
    conn = store.connect(tmp_path / "c.db")
    aid = _acct(conn)
    iid = _item(conn, aid, "p1")
    store.record_metrics(conn, iid, reactions=10)
    conn.commit()

    adapter = _Stub([RawItem(platform_item_id="p1", kind="text",
                             published_at=store.now(), raw={},
                             metrics={"reactions": 25})])
    assert _probe(conn, adapter, aid) is False
    assert conn.execute("SELECT m_reactions FROM item WHERE id=?",
                        (iid,)).fetchone()["m_reactions"] == 25


def test_probe_says_work_when_newest_differs(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    aid = _acct(conn)
    _item(conn, aid, "p1")
    adapter = _Stub([RawItem(platform_item_id="p2", kind="text",
                             published_at=store.now(), raw={})])
    assert _probe(conn, adapter, aid) is True


def test_probe_errs_toward_syncing_on_failure(tmp_path):
    """A silent skip would strand an account for another whole TTL window."""
    conn = store.connect(tmp_path / "c.db")
    aid = _acct(conn)
    _item(conn, aid, "p1")

    class Boom(_Stub):
        def __init__(self):
            pass

        def fetch(self, *a, **k):
            raise RuntimeError("vendor down")

    assert _probe(conn, Boom(), aid) is True


def test_probe_errs_toward_syncing_on_empty_result(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    aid = _acct(conn)
    _item(conn, aid, "p1")
    assert _probe(conn, _Stub([]), aid) is True


def test_a_pinned_post_cannot_freeze_an_account(tmp_path):
    """The stored newest post is pinned and still first in vendor order. A real new
    post sits behind it: the probe must see that, not answer 'nothing new' forever."""
    conn = store.connect(tmp_path / "c.db")
    aid = _acct(conn)
    _item(conn, aid, "pinned-old")
    adapter = _Stub([
        RawItem(platform_item_id="pinned-old", kind="image", published_at=store.now() - 9e6,
                raw={}, metrics={"is_pinned": True}),
        RawItem(platform_item_id="brand-new", kind="image", published_at=store.now(),
                raw={}, metrics={}),
    ])
    assert _probe(conn, adapter, aid) is True


def test_instagram_probes_see_past_three_pinned_posts():
    assert F.probe_limit("instagram") >= 4 and F.probe_limit("linkedin") == 1


# ── delta watermark ─────────────────────────────────────────────────────────

def _ad():
    from corpus.adapters.base import Adapter
    return Adapter(name="t", platform="linkedin", fetch=lambda *a, **k: None, relationships=("competitor",))


def _posts(*stamps):
    return [RawItem(platform_item_id=f"p{t}", kind="text", published_at=t, raw={})
            for t in stamps]


def test_delta_starts_before_the_newest_post_not_at_the_last_sync(tmp_path):
    """A post published just before a sync but indexed just after it is older than
    last_scraped_at, so a delta from there never saw it."""
    conn = store.connect(tmp_path / "c.db")
    aid = _acct(conn, last_scraped=2_000_000)
    _item(conn, aid, when=1_000_000)
    row = conn.execute("SELECT * FROM account WHERE id=?", (aid,)).fetchone()
    assert F.delta_since(conn, row) == 1_000_000 - F.DELTA_OVERLAP


def test_a_truncated_delta_pins_the_floor_until_a_run_reaches_it(tmp_path):
    from corpus import sync
    conn = store.connect(tmp_path / "c.db")
    aid = _acct(conn, last_scraped=store.now() - 30 * 3600)
    _item(conn, aid, pid="old", when=1_000_000)
    floor = lambda: conn.execute("SELECT delta_floor FROM account WHERE id=?",  # noqa: E731
                                 (aid,)).fetchone()[0]
    since = 1_000_000 - F.DELTA_OVERLAP
    # A full page, all far newer than `since`: whatever lies between is unseen.
    sync.persist(conn, _ad(), aid, FetchResult(items=_posts(3_000_000, 3_000_100),
                                               truncated=True), since=since)
    assert floor() == since
    row = conn.execute("SELECT * FROM account WHERE id=?", (aid,)).fetchone()
    assert F.delta_since(conn, row) == since, "the next delta must start at the hole"
    d = F.plan(conn, account_ids=[aid])[0]
    assert d.action == "sync" and "missing" in d.reason, \
        "a probe would say 'nothing new' and leave the hole for good"

    # A run that got all the way down closes it.
    sync.persist(conn, _ad(), aid, FetchResult(items=_posts(2_000_000)), since=since)
    assert floor() is None
