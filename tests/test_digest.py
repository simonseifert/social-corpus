"""The weekly digest, against a synthetic corpus with known answers."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import cli, digest, notify, store  # noqa: E402

DAY = 86400


def _post(conn, aid, pid, days_ago, eng, *, now, body="a post", platform="instagram",
          views=None):
    iid, _, _ = store.upsert_item(
        conn, account_id=aid, platform=platform, platform_item_id=pid, kind="image",
        raw={}, body=body, published_at=now - int(days_ago * DAY),
        url=f"https://example.com/p/{pid}")
    conn.execute("""INSERT INTO metric_snapshot (item_id, captured_at, reactions,
                    comments, shares, views) VALUES (?,?,?,?,?,?)""",
                 (iid, now - int(days_ago * DAY) + 3600, eng, 0, 0, views))
    conn.execute("UPDATE item SET m_reactions=?, m_comments=0, m_shares=0, m_views=? "
                 "WHERE id=?", (eng, views, iid))
    return iid


def _snap(conn, iid, days_ago, reactions, *, now, views=None, comments=0):
    conn.execute("""INSERT INTO metric_snapshot (item_id, captured_at, reactions,
                    comments, shares, views) VALUES (?,?,?,?,0,?)""",
                 (iid, now - int(days_ago * DAY), reactions, comments, views))


def _account(conn, handle, rel="competitor", client_ref=None, scraped_days_ago=0.5,
             now=None):
    aid = store.upsert_account(conn, platform="instagram", handle=handle,
                               relationship=rel, client_ref=client_ref)
    conn.execute("UPDATE account SET last_scraped_at=? WHERE id=?",
                 (now - int(scraped_days_ago * DAY), aid))
    return aid


@pytest.fixture
def world(tmp_path):
    """One competitor with a history and a breakout this week, one client account
    with steady posting, follower history, and a commenter asking something."""
    conn = store.connect(tmp_path / "c.db")
    now = store.now()
    rival = _account(conn, "rival", now=now)
    for n in range(8):                                     # usual ~ 100
        _post(conn, rival, f"r{n}", 40 - n * 3, 90 + n * 3, now=now)
    hit = _post(conn, rival, "rhit", 2, 900, now=now, body="The one that flew\nmore")
    _post(conn, rival, "rnew", 1, 120, now=now)

    me = _account(conn, "acme", rel="client", client_ref="acme", now=now)
    old = []
    for n in range(6):
        old.append(_post(conn, me, f"m{n}", 60 - n * 5, 40, now=now))
    # an old post still climbing inside the window: 40 -> 40 before, 140 now
    _snap(conn, old[-1], 10, 40, now=now)
    _snap(conn, old[-1], 1, 140, now=now, views=None)
    _post(conn, me, "mnew", 3, 30, now=now)
    conn.execute("INSERT INTO account_metric_snapshot (account_id, captured_at, "
                 "followers) VALUES (?,?,?)", (me, now - 20 * DAY, 1000))
    conn.execute("INSERT INTO account_metric_snapshot (account_id, captured_at, "
                 "followers) VALUES (?,?,?)", (me, now - 1 * DAY, 1042))
    store.upsert_comment(conn, old[-1], platform_comment_id="c1",
                         body="How do you price this for a small gym?",
                         author_name="Some Stranger", published_at=now - 2 * DAY)
    store.upsert_comment(conn, old[-1], platform_comment_id="c2", body="great post",
                         author_name="Someone", published_at=now - 2 * DAY)
    store.upsert_comment(conn, old[-1], platform_comment_id="c3",
                         body="Did you ever try this with video?",
                         author_name="Old Commenter", published_at=now - 30 * DAY)
    conn.commit()
    return conn, now, {"rival": rival, "me": me, "hit": hit, "climber": old[-1]}


def test_new_posts_are_measured_against_the_accounts_own_usual(world):
    conn, now, ids = world
    d = digest.build(conn, now=now)
    by = {p["account"]: p for p in d["new_posts"]}
    assert by["rival (ig)"]["n"] == 2
    assert by["rival (ig)"]["usual"] == 100          # median of the 8 earlier posts
    assert by["rival (ig)"]["best_url"].endswith("/rhit")
    assert by["acme (ig)"]["n"] == 1 and by["acme (ig)"]["usual"] == 40


def test_movers_use_snapshot_deltas_inside_the_window_only(world):
    conn, now, ids = world
    d = digest.build(conn, now=now)
    assert [m["item_id"] for m in d["movers"]] == [ids["climber"]]
    assert d["movers"][0]["delta"] == 100
    # a post published inside the window is "new", not a mover, however it grew
    assert ids["hit"] not in [m["item_id"] for m in d["movers"]]


def test_a_counter_going_dark_is_not_a_negative_mover(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    now = store.now()
    a = _account(conn, "x", now=now)
    iid = _post(conn, a, "p", 30, 10, now=now, views=5000)
    _snap(conn, iid, 10, 10, now=now, views=5000)
    _snap(conn, iid, 1, 25, now=now, views=None)
    m = digest.movers(conn, [conn.execute("SELECT * FROM account").fetchone()],
                      now - 7 * DAY)
    assert m[0]["delta"] == 15 and m[0]["views_delta"] == 0


def test_follower_change_needs_a_reading_from_before_the_window(world):
    conn, now, ids = world
    d = digest.build(conn, now=now)
    assert d["followers"] == [{"account": "acme (ig)", "start": 1000, "end": 1042,
                               "delta": 42}]
    # first ever reading inside the window: no "+5000 this week"
    conn.execute("INSERT INTO account_metric_snapshot (account_id, captured_at, "
                 "followers) VALUES (?,?,?)", (ids["rival"], now - DAY, 5000))
    assert [f["account"] for f in digest.build(conn, now=now)["followers"]] == \
        ["acme (ig)"]


def test_breakouts_cover_every_relationship_with_an_absolute_floor(world, tmp_path):
    conn, now, ids = world
    d = digest.build(conn, now=now)
    assert [o["item_id"] for o in d["breakouts"]] == [ids["hit"]]
    # 3x usual on an account that gets 5 likes is noise, not a breakout
    small = _account(conn, "tiny", rel="own", now=now)
    for n in range(6):
        _post(conn, small, f"t{n}", 30 - n, 5, now=now)
    _post(conn, small, "tbig", 1, 40, now=now)
    assert all(o["handle"] != "tiny" for o in digest.build(conn, now=now)["breakouts"])


def test_questions_are_new_audience_questions_without_names(world):
    conn, now, _ = world
    d = digest.build(conn, now=now)
    assert [q["text"] for q in d["questions"]] == ["How do you price this for a "
                                                   "small gym?"]
    text = digest.render_text(d)
    assert "Some Stranger" not in text and "great post" not in text


def test_stale_accounts_are_twice_their_ttl(world):
    conn, now, ids = world
    assert digest.build(conn, now=now)["health"] == []
    # competitor TTL is 3 days: 5 days is late but not alarming, 7 is stale
    conn.execute("UPDATE account SET last_scraped_at=? WHERE id=?",
                 (now - 5 * DAY, ids["rival"]))
    assert digest.build(conn, now=now)["health"] == []
    conn.execute("UPDATE account SET last_scraped_at=? WHERE id=?",
                 (now - 7 * DAY, ids["rival"]))
    assert digest.build(conn, now=now)["health"] == ["rival (ig): not synced for 7d"]
    conn.execute("UPDATE account SET last_scraped_at=NULL WHERE id=?", (ids["me"],))
    assert "acme (ig): never synced" in digest.build(conn, now=now)["health"]


def test_failed_runs_in_the_window_are_health_problems(world):
    conn, now, ids = world
    rid = store.start_run(conn, adapter="instagram_public", mode="delta",
                          account_id=ids["rival"], started_at=now - DAY)
    store.finish_run(conn, rid, status="error", error="boom")
    assert "instagram_public: 1 failed run(s)" in digest.build(conn, now=now)["health"]


def test_client_ref_scopes_everything(world):
    conn, now, _ = world
    d = digest.build(conn, now=now, client_ref="acme")
    assert {p["account"] for p in d["new_posts"]} == {"acme (ig)"}
    assert d["breakouts"] == []           # the rival's breakout is out of scope
    d = digest.build(conn, now=now, client_ref="nobody")
    assert d["empty"] and d["breakouts"] == []


def test_text_is_whatsapp_shaped(world):
    conn, now, _ = world
    text = digest.render_text(digest.build(conn, now=now))
    assert len(text) <= digest.MAX_CHARS
    assert "|" not in text and "**" not in text and "#" not in text
    for ln in text.splitlines():
        if "http" in ln:
            assert ln.startswith("http") and " " not in ln   # links on their own line
    assert "BREAKOUTS" in text and "NEW POSTS" in text


def test_long_digests_shrink_sections_before_dropping_them(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    now = store.now()
    for k in range(30):
        a = _account(conn, f"account_with_a_long_name_{k}", now=now)
        _post(conn, a, f"p{k}", 1, 10, now=now)
    d = digest.build(conn, now=now)
    text = digest.render_text(d)
    assert len(text) <= digest.MAX_CHARS
    assert "NEW POSTS" in text and "more" in text


def test_nothing_changed_is_one_line(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    now = store.now()
    a = _account(conn, "quiet", now=now)
    _post(conn, a, "old", 60, 10, now=now)
    text = digest.render_text(digest.build(conn, now=now))
    assert text == ("corpus weekly: nothing changed in the last 7 days "
                    "(1 accounts tracked).")


def test_html_escapes_and_links(world):
    conn, now, _ = world
    conn.execute("UPDATE item SET body='<script>x</script>' WHERE platform_item_id="
                 "'rhit'")
    page = digest.render_html(digest.build(conn, now=now))
    assert "<script>x" not in page and "&lt;script&gt;" in page
    assert '<a href="https://example.com/p/rhit">' in page


def test_cli_weekly_writes_html_and_sends_text(world, tmp_path, monkeypatch, capsys):
    conn, now, _ = world
    db = conn.execute("PRAGMA database_list").fetchone()["file"]
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, **k: sent.append(text) or True)
    out = tmp_path / "w.html"
    assert cli.main(["--db", db, "weekly", "--send", "--out", str(out)]) == 0
    assert out.read_text().startswith("<!doctype html>")
    assert len(sent) == 1 and sent[0].startswith("corpus weekly")
    assert sent[0] in capsys.readouterr().out


def test_cli_weekly_send_failure_is_a_nonzero_exit(world, monkeypatch):
    conn, *_ = world
    db = conn.execute("PRAGMA database_list").fetchone()["file"]
    monkeypatch.setattr(notify, "send", lambda text, **k: False)
    assert cli.main(["--db", db, "weekly", "--send"]) == 1


def test_html_only_links_http_urls(world):
    """Post URLs are vendor data: a javascript: URL must not become a live link."""
    conn, now, _ = world
    conn.execute("UPDATE item SET url='javascript:alert(1)' WHERE platform_item_id="
                 "'rhit'")
    page = digest.render_html(digest.build(conn, now=now))
    assert 'href="javascript' not in page
    assert "javascript:alert(1)" in page           # still shown, as inert text
