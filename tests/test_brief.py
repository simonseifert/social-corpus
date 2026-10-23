"""Brief-generation tests.

The load-bearing assertions here are about honesty and self-containment: a brief that
silently omits the "these metrics are impossible" section, or that pulls a font from
a CDN and renders broken on a client's laptop, has failed at its actual job.
"""
import sys
import re
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import brief, store  # noqa: E402

DAY = 86400


@pytest.fixture
def populated(tmp_path):
    conn = store.connect(tmp_path / "c.db")

    def mk(handle, relationship):
        return store.upsert_account(conn, platform="linkedin", handle=handle,
                                    relationship=relationship,
                                    display_name=handle.title())

    comp = mk("rival", "competitor")
    base = int(time.time()) - 30 * DAY
    specs = [
        ("text", "a post about widgets", 100, 10, 5),
        ("text", "another widget post", 50, 2, 1),
        ("short_video", "a reel about gadgets", 900, 80, 40),
        ("carousel", "a carousel on process", 300, 20, 10),
    ]
    for i, (kind, body, r, c, s) in enumerate(specs):
        iid, _, _ = store.upsert_item(
            conn, account_id=comp, platform="linkedin",
            platform_item_id=f"p{i}", kind=kind,
            published_at=base + i * 3 * DAY, body=body,
            url=f"https://example.com/p{i}", raw={})
        store.record_metrics(conn, iid, reactions=r, comments=c, shares=s)
    store.record_account_metrics(conn, comp, followers=10_000)
    conn.commit()
    return conn, comp


def test_writes_self_contained_html(populated, tmp_path):
    conn, aid = populated
    out = brief.write(conn, aid, tmp_path / "b.html")
    h = out.read_text()

    assert h.startswith("<!doctype html>")
    assert "<style>" in h, "CSS must be inlined"
    # A brief that fetches anything at open time breaks offline and on a locked-down
    # client laptop. The only permitted absolute URLs are the post links themselves.
    for bad in ("cdn.", "googleapis", "<script", "@import", "<link"):
        assert bad not in h.lower(), f"external/dynamic reference found: {bad}"


def test_states_owner_only_limits_for_competitors(populated, tmp_path):
    conn, aid = populated
    h = brief.write(conn, aid, tmp_path / "b.html").read_text()
    assert "cannot show" in h.lower()
    for metric in ("reach", "watch time", "impressions"):
        assert metric in h.lower(), f"must name {metric} as unobtainable"


def test_provenance_follows_the_adapter_not_the_label(populated, tmp_path):
    """An account labelled `client` whose export never ran is still scraped data.

    Claiming "built from the account's own analytics export" in that case puts a
    false sentence in a document the client pays for. Provenance is a claim about
    how the data arrived, so it must come from the run log.
    """
    conn, _ = populated
    cid = store.upsert_account(conn, platform="linkedin", handle="acme",
                               relationship="client", display_name="Acme")
    store.upsert_item(conn, account_id=cid, platform="linkedin",
                      platform_item_id="c1", kind="text",
                      published_at=int(time.time()) - DAY, body="hello", raw={})
    rid = store.start_run(conn, adapter="linkedin_posts", mode="delta",
                          account_id=cid)
    store.finish_run(conn, rid, status="ok", seen=1, new=1)
    conn.commit()

    h = brief.write(conn, cid, tmp_path / "c.html").read_text()
    assert "own analytics export" not in h.lower(), \
        "scraped data must not be described as a first-party export"
    assert "reach" in h.lower(), "the unobtainable-metrics caveat still applies"


def test_real_export_run_earns_the_first_party_claim(populated, tmp_path):
    conn, _ = populated
    cid = store.upsert_account(conn, platform="linkedin", handle="acme2",
                               relationship="client", display_name="Acme2")
    store.upsert_item(conn, account_id=cid, platform="linkedin",
                      platform_item_id="c2", kind="text",
                      published_at=int(time.time()) - DAY, body="hi", raw={})
    rid = store.start_run(conn, adapter="linkedin_export", mode="backfill",
                          account_id=cid)
    store.finish_run(conn, rid, status="ok", seen=1, new=1)
    conn.commit()

    h = brief.write(conn, cid, tmp_path / "c2.html").read_text()
    assert "own analytics export" in h.lower()


def test_views_appear_when_the_platform_reports_them(tmp_path):
    """A 1M-view reel was invisible: engagement counts interactions only."""
    conn = store.connect(tmp_path / "c.db")
    aid = store.upsert_account(conn, platform="instagram", handle="reels",
                               relationship="own")
    iid, _, _ = store.upsert_item(conn, account_id=aid, platform="instagram",
                                  platform_item_id="r1", kind="short_video",
                                  published_at=int(time.time()), body="a reel",
                                  raw={})
    store.record_metrics(conn, iid, reactions=100, comments=5, views=1_030_000)
    conn.commit()

    h = brief.write(conn, aid, tmp_path / "b.html").read_text()
    assert ">Views<" in h and "1.0M" in h
    assert "Views" in h, "top-posts table needs the column"


def test_views_column_hidden_when_platform_has_none(populated, tmp_path):
    conn, aid = populated
    h = brief.write(conn, aid, tmp_path / "b.html").read_text()
    assert ">Views<" not in h, "an empty column is worse than no column"


def test_top_posts_are_ranked_by_total_engagement(populated, tmp_path):
    conn, aid = populated
    h = brief.write(conn, aid, tmp_path / "b.html").read_text()
    top = h.index("gadgets")       # 900+80+40 = highest
    weak = h.index("another widget")  # 50+2+1 = lowest
    assert top < weak, "top post must appear before the weakest"


def test_format_table_uses_median(populated, tmp_path):
    conn, aid = populated
    data = brief.gather(conn, aid)
    s = brief.s_formats(data)
    assert "Median" in s.html
    assert "median" in s.html.lower()


def test_escapes_hostile_content(populated, tmp_path):
    """Post bodies are attacker-controlled text from a third party."""
    conn, aid = populated
    store.upsert_item(conn, account_id=aid, platform="linkedin",
                      platform_item_id="xss", kind="text",
                      published_at=int(time.time()),
                      body="<img src=x onerror=alert(1)>", raw={})
    conn.commit()
    h = brief.write(conn, aid, tmp_path / "b.html").read_text()
    assert "<img src=x" not in h
    assert "&lt;img" in h


def test_empty_account_does_not_crash(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    aid = store.upsert_account(conn, platform="linkedin", handle="empty",
                               relationship="competitor")
    h = brief.write(conn, aid, tmp_path / "b.html").read_text()
    assert "No content in range" in h


def test_since_filter_narrows_the_period(populated, tmp_path):
    conn, aid = populated
    recent = int(time.time()) - 25 * DAY
    h = brief.write(conn, aid, tmp_path / "b.html", since=recent).read_text()
    assert "a post about widgets" not in h, "items before `since` must be excluded"
    assert "carousel on process" in h


def test_analysis_section_appears_when_analysis_db_given(populated, tmp_path):
    conn, aid = populated
    from corpus.analyze import run as ar
    db = str(tmp_path / "a.db")
    ar.attach(conn, db)
    conn.execute(
        """INSERT INTO a.analysis_result (target_kind,target_id,template,prompt_hash,
             input_hash,output_text,model,provider,created_at)
           VALUES ('item',1,'hook-teardown','h1','i1','THE HOOK VERDICT','m','groq',?)""",
        (store.now(),))
    conn.commit()
    h = brief.write(conn, aid, tmp_path / "b.html", analysis_db=db).read_text()
    assert "THE HOOK VERDICT" in h
    assert "hook-teardown" in h


def test_no_analysis_db_omits_the_section(populated, tmp_path):
    conn, aid = populated
    h = brief.write(conn, aid, tmp_path / "b.html").read_text()
    assert "<h2>Analysis</h2>" not in h


def test_dark_mode_and_print_styles_present(populated, tmp_path):
    """It gets opened on a laptop and printed to PDF; both must look intentional."""
    conn, aid = populated
    h = brief.write(conn, aid, tmp_path / "b.html").read_text()
    assert "prefers-color-scheme:dark" in h.replace(" ", "")
    assert "@media print" in h


# ── outlier handling on the client-facing page ──────────────────────────────

def test_one_viral_post_does_not_flatten_the_weekday_chart(tmp_path):
    """A single 47.8k outlier pinned one day at 100% and every other day at 0%,
    on the most visual element of a paid deliverable."""
    conn = store.connect(tmp_path / "c.db")
    aid = store.upsert_account(conn, platform="instagram", handle="v",
                               relationship="client")
    base = 1735689600  # a Wednesday
    for i, (offset, r) in enumerate([(0, 80), (1, 90), (7, 70), (8, 85),
                                     (2, 47800)]):
        iid, _, _ = store.upsert_item(conn, account_id=aid, platform="instagram",
                                      platform_item_id=f"p{i}", kind="short_video",
                                      published_at=base + offset * DAY, raw={})
        store.record_metrics(conn, iid, reactions=r)
    conn.commit()

    html = brief.s_cadence(brief.gather(conn, aid)).html
    widths = [int(w) for w in __import__("re").findall(r"width:(\d+)%", html)]
    nonzero = [w for w in widths if w > 0]
    assert len(nonzero) >= 2, f"only {len(nonzero)} visible bars: {widths}"
    assert "thin" in html, "the single-post day must be marked"
    assert "anecdote" in html


def test_engagement_rate_hidden_below_a_credible_follower_count(tmp_path):
    """78 median on 378 followers is 20.77%: true, and unusable in a client doc."""
    conn = store.connect(tmp_path / "c.db")
    aid = store.upsert_account(conn, platform="instagram", handle="small",
                               relationship="client")
    iid, _, _ = store.upsert_item(conn, account_id=aid, platform="instagram",
                                  platform_item_id="p1", kind="image",
                                  published_at=int(time.time()), raw={})
    store.record_metrics(conn, iid, reactions=78)
    store.record_account_metrics(conn, aid, followers=378)
    conn.commit()
    html = brief.s_summary(brief.gather(conn, aid)).html
    # No percentage may be computed off a 378-follower base (78/378 would print
    # 20.63%). The phrase itself is now allowed to appear, because the brief
    # explains the omission rather than dropping the card silently.
    assert not re.search(r"\d+\.\d\d%", html), "a rate was computed anyway"
    assert "not shown" in html, "the omission must explain itself"

    conn.execute("DELETE FROM account_metric_snapshot")
    store.record_account_metrics(conn, aid, followers=25_000)
    conn.commit()
    html = brief.s_summary(brief.gather(conn, aid)).html
    assert "Engagement rate" in html and re.search(r"\d+\.\d\d%", html)


def test_weekdays_are_bucketed_on_the_audience_clock(tmp_path, monkeypatch):
    """Sunday 23:30 UTC is Monday 01:30 in Zagreb, and that is when it went up."""
    monkeypatch.setenv("CORPUS_TZ", "Europe/Zagreb")
    conn = store.connect(tmp_path / "c.db")
    aid = store.upsert_account(conn, platform="instagram", handle="tz",
                               relationship="client")
    sunday_2330_utc = 1735515000          # 2024-12-29 23:30 UTC, a Sunday
    for i in range(2):
        iid, _, _ = store.upsert_item(conn, account_id=aid, platform="instagram",
                                      platform_item_id=f"p{i}", kind="image",
                                      published_at=sunday_2330_utc + i * 7 * DAY,
                                      raw={})
        store.record_metrics(conn, iid, reactions=50)
    conn.commit()
    html = brief.s_cadence(brief.gather(conn, aid)).html
    rows = dict(re.findall(r"barlab'>(\w+)</div>.*?\((\d+)\)", html))
    assert rows["Mon"] == "2" and rows["Sun"] == "0", rows
    assert "Europe/Zagreb" in html


def test_benchmark_puts_the_subject_against_the_competitors_median(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    now = store.now()
    ids = []
    for h, rel, engs in (("client", "client", [10, 12, 14, 11, 12, 13]),
                         ("rival", "competitor", [100, 120, 90, 300, 95, 110]),
                         ("other", "competitor", [40, 50, 45, 60, 48, 52])):
        aid = store.upsert_account(conn, platform="instagram", handle=h,
                                   relationship=rel, display_name=h.title())
        for n, e in enumerate(engs):
            iid, _, _ = store.upsert_item(
                conn, account_id=aid, platform="instagram", platform_item_id=f"{h}{n}",
                kind="short_video" if n % 2 else "image", raw={}, body=f"{h} post {n}",
                published_at=now - (n + 1) * DAY, url=f"https://www.instagram.com/p/{h}{n}/")
            store.record_metrics(conn, iid, reactions=e)
        ids.append(aid)
    conn.commit()
    html = brief.render_benchmark([brief.gather(conn, i) for i in ids])
    assert "Where Client stands" in html
    assert "below them" in html                 # 11.5 typical vs 85 / 47.5
    assert "Rival: best posts" in html and "rival post 3" in html
    assert "client post" not in html.split("Where Client stands")[1], \
        "the subject's own posts belong in its own brief, not the rivals' list"
    assert "<script" not in html


def test_benchmark_never_mixes_platforms_in_one_table(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    ids = []
    for plat, h in (("linkedin", "me"), ("instagram", "them")):
        aid = store.upsert_account(conn, platform=plat, handle=h, relationship="client")
        iid, _, _ = store.upsert_item(conn, account_id=aid, platform=plat,
                                      platform_item_id=h, kind="text", raw={},
                                      published_at=store.now() - DAY)
        store.record_metrics(conn, iid, reactions=5)
        ids.append(aid)
    conn.commit()
    html = brief.render_benchmark([brief.gather(conn, i) for i in ids])
    assert "Nothing to compare" in html


def test_a_subject_with_a_handful_of_posts_gets_no_verdicts(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    ids = []
    for h, n_posts in (("me", 1), ("rival", 8)):
        aid = store.upsert_account(conn, platform="linkedin", handle=h,
                                   relationship="client", display_name=h)
        for n in range(n_posts):
            iid, _, _ = store.upsert_item(conn, account_id=aid, platform="linkedin",
                                          platform_item_id=f"{h}{n}", kind="text", raw={},
                                          published_at=store.now() - (n + 1) * DAY)
            store.record_metrics(conn, iid, reactions=20)
        ids.append(aid)
    conn.commit()
    html = brief.render_benchmark([brief.gather(conn, i) for i in ids])
    assert "too few to say" in html and "Where me stands" not in html
    assert "<h2>LinkedIn</h2>" in html


def test_brief_dates_are_on_the_audiences_clock(monkeypatch):
    """A sync at 00:40 in Portville on 23 Oct read "22 October (today)" in UTC."""
    from corpus import brief_i18n as I
    ts = 1792708800          # 2026-10-22 22:40 UTC = 2026-10-23 00:40 Zagreb
    monkeypatch.setenv("CORPUS_TZ", "Europe/Zagreb")
    assert I.date(ts, "hr") == "23. listopada 2026."
    assert I.date_num(ts) == "2026-10-23"
    assert "00:40" in I.stamp(ts) and "UTC" not in I.stamp(ts)
    monkeypatch.setenv("CORPUS_TZ", "UTC")
    assert I.date_num(ts) == "2026-10-22"
