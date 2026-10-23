"""What a client actually sees: data age, lenses, the account summary, comments,
rates, Croatian, CSV, compare and the dashboard overview.

Each test here comes from a finding in an audit of real briefs sent (or about to be
sent) to Croatian small-business clients from an August sync, in October.
"""
import csv
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import brief, brief_i18n, outliers, store, web  # noqa: E402

DAY = 86400


def _acct(conn, handle, *, rel="client", platform="instagram", name=None,
          synced_ago=1, posts=(), client_ref=None, followers=None):
    """posts: (days_before_sync, body, reactions[, kind[, transcript]])."""
    aid = store.upsert_account(conn, platform=platform, handle=handle, relationship=rel,
                               display_name=name or handle.title(),
                               client_ref=client_ref)
    sync = store.now() - synced_ago * DAY
    ids = []
    for n, p in enumerate(posts):
        before, body, rx = p[:3]
        kind = p[3] if len(p) > 3 else "image"
        tx = p[4] if len(p) > 4 else None
        iid, _, _ = store.upsert_item(
            conn, account_id=aid, platform=platform, platform_item_id=f"{handle}{n}",
            kind=kind, published_at=sync - before * DAY, body=body, transcript=tx,
            url=f"https://example.com/{handle}/{n}", raw={})
        store.record_metrics(conn, iid, reactions=rx)
        ids.append(iid)
    # The capture time is what the brief anchors on; pin both sources of it.
    conn.execute("UPDATE account SET last_scraped_at=? WHERE id=?", (sync, aid))
    conn.execute(f"UPDATE item SET metrics_at=? WHERE {store.account_items('')}",
                 (sync, aid, aid))
    if followers:
        store.record_account_metrics(conn, aid, followers=followers)
    conn.commit()
    return aid, ids


def _lens(conn, db, item_id, template, text, *, at=None, kind="item", js=None):
    from corpus.analyze import run as ar
    ar.attach(conn, db)
    conn.execute(
        """INSERT INTO a.analysis_result (target_kind,target_id,template,prompt_hash,
             input_hash,output_text,output_json,model,provider,created_at)
           VALUES (?,?,?,?,?,?,?,'m','p',?)""",
        (kind, item_id, template, f"h{time.time_ns()}", "i", text, js,
         at or store.now()))
    conn.commit()


# ── 1. data age and the window ──────────────────────────────────────────────

def test_every_brief_says_when_its_data_was_captured(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    aid, _ = _acct(conn, "fresh", synced_ago=2, posts=[(1, "hi", 5)])
    h = brief.write(conn, aid, tmp_path / "b.html").read_text()
    assert "Data captured" in h and "(2 days ago)" in h
    assert "Stale data" not in h


def test_stale_data_is_flagged_in_red_at_the_top(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    aid, _ = _acct(conn, "old", synced_ago=78, posts=[(1, "hi", 5)])
    h = brief.write(conn, aid, tmp_path / "b.html").read_text()
    assert "(78 days ago)" in h
    top = h.split("<h2>")[0]
    assert "class='stale'" in top and "78 days old" in top


def test_the_window_counts_back_from_the_last_sync_not_from_now(tmp_path):
    """Synced 100 days ago; a post 50 days before that sync is inside "last 60
    days" of the data even though it is 150 days before today."""
    conn = store.connect(tmp_path / "c.db")
    aid, _ = _acct(conn, "anch", synced_ago=100,
                   posts=[(50, "inside the window", 9), (80, "outside it", 9)])
    h = brief.write(conn, aid, tmp_path / "b.html", days=60).read_text()
    assert "inside the window" in h and "outside it" not in h
    assert "the last sync, not up to today" in h
    assert "1 older post(s) are outside this period" in h


# ── 2. lenses ───────────────────────────────────────────────────────────────

def test_client_lenses_come_from_the_templates_dir_never_personal(tmp_path):
    d = tmp_path / "templates"
    (d / "personal").mkdir(parents=True)
    for slug in ("what-works", "content-audit", "zeta-lens", "pillar-tag"):
        (d / f"{slug}.md").write_text("---\nname: X\n---\nbody")
    (d / "creator-only.md").write_text("---\nname: Y\naudience: personal\n---\nbody")
    (d / "personal" / "triage.md").write_text("---\nname: T\n---\nbody")
    assert brief.client_lenses(d) == ["content-audit", "what-works", "zeta-lens"]


def test_the_real_templates_dir_has_no_personal_lens_in_the_allowlist():
    got = brief.client_lenses()
    assert got[:2] == ["content-audit", "what-works"]
    for bad in ("triage", "steal-format", "pillar-tag", "script-outline"):
        assert bad not in got


def test_every_client_lens_shows_per_post_in_order_and_personal_ones_never(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    aid, (iid,) = _acct(conn, "lensy", posts=[(1, "a post", 5)])
    db = str(tmp_path / "a.db")
    now = store.now()
    _lens(conn, db, iid, "what-works", "WORKS OLD", at=now - 100)
    _lens(conn, db, iid, "what-works", "WORKS NEW", at=now - 50)
    _lens(conn, db, iid, "content-audit", "AUDIT TEXT", at=now - 90)
    _lens(conn, db, iid, "triage", "TRIAGE VERDICT: skip", at=now)   # newest of all
    h = brief.write(conn, aid, tmp_path / "b.html", analysis_db=db).read_text()
    assert "TRIAGE" not in h, "a personal lens reached a client brief"
    assert "WORKS NEW" in h and "WORKS OLD" not in h
    assert h.index("AUDIT TEXT") < h.index("WORKS NEW"), "fixed order, not newest"

    only = brief.write(conn, aid, tmp_path / "t.html", analysis_db=db,
                       template="triage").read_text()
    assert "TRIAGE VERDICT" in only and "AUDIT TEXT" not in only


def test_a_personal_post_is_not_assessed_by_any_lens(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    aid, (iid,) = _acct(conn, "grief", posts=[(1, "my mum passed away", 5)])
    db = str(tmp_path / "a.db")
    _lens(conn, db, iid, "content-audit", "PERSONAL: bereavement post")
    _lens(conn, db, iid, "what-works", "Strong hook, weak CTA")
    h = brief.write(conn, aid, tmp_path / "b.html", analysis_db=db).read_text()
    assert "Personal post, not assessed" in h
    assert "PERSONAL:" not in h and "weak CTA" not in h


# ── 3. account summary ──────────────────────────────────────────────────────

SUMMARY = {"summary": "You post rarely but your carousels **work**.",
           "priorities": [{"title": f"Priority {n}", "why": f"because {n}",
                           "action": f"do {n}"} for n in (1, 2, 3)],
           "plan": [{"week": w, "idea": f"Idea {w}", "format": "carousel",
                     "why": f"reason {w}"} for w in (1, 2, 3, 4)]}


def test_the_account_summary_renders_first(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    aid, _ = _acct(conn, "summ", posts=[(1, "a post", 5)])
    db = str(tmp_path / "a.db")
    _lens(conn, db, aid, "account-summary", None, kind="account",
          js=json.dumps(SUMMARY))
    h = brief.write(conn, aid, tmp_path / "b.html", analysis_db=db).read_text()
    first_h2 = h.split("<h2>")[1]
    assert first_h2.startswith("Summary and priorities")
    assert "<strong>work</strong>" in first_h2
    for s in ("Priority 3", "do 2", "Idea 4", "reason 1", "The next four weeks"):
        assert s in first_h2


def test_no_or_malformed_summary_renders_nothing(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    aid, _ = _acct(conn, "nosumm", posts=[(1, "a post", 5)])
    db = str(tmp_path / "a.db")
    h = brief.write(conn, aid, tmp_path / "b.html", analysis_db=db).read_text()
    assert "Summary and priorities" not in h
    _lens(conn, db, aid, "account-summary", None, kind="account", js="[1, 2")
    h = brief.write(conn, aid, tmp_path / "b.html", analysis_db=db).read_text()
    assert "Summary and priorities" not in h


# ── 4. transcripts ──────────────────────────────────────────────────────────

def test_transcripts_are_labelled_and_garbled_ones_hidden(tmp_path, monkeypatch):
    conn = store.connect(tmp_path / "c.db")
    aid, _ = _acct(conn, "vid", posts=[
        (1, "clean reel", 5, "short_video", "we talk about pricing today"),
        (2, "noisy reel", 5, "short_video", "scubido cher car linte yos todas")])
    monkeypatch.setattr(brief, "garbled", lambda t: "scubido" in t)
    h = brief.write(conn, aid, tmp_path / "b.html").read_text()
    assert "Machine transcript, may contain errors" in h
    assert "we talk about pricing today" in h
    assert "scubido" not in h


# ── 5/6. posting rate ───────────────────────────────────────────────────────

def test_a_single_post_does_not_read_one_a_week(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    aid, _ = _acct(conn, "one", posts=[(3, "lonely", 5)])
    d = brief.gather(conn, aid, days=365)
    assert brief._per_week(d, brief._own(d["items"])) < 0.5
    h = brief.render(d)
    assert "1.0/wk" not in h


def test_rate_divides_by_the_window_when_history_covers_it(tmp_path):
    """52 posts in a year read 1/wk, even if they all came in the last month: the
    old first-to-last span put that at ~12/wk."""
    conn = store.connect(tmp_path / "c.db")
    posts = [(400, "ancient", 1)] + [(1 + n % 28, f"p{n}", 1) for n in range(52)]
    aid, _ = _acct(conn, "rate", posts=posts)
    d = brief.gather(conn, aid, days=365)
    assert brief._per_week(d, brief._own(d["items"])) == pytest.approx(1.0, abs=0.01)


def test_rate_uses_first_held_post_when_history_is_shorter_than_the_window(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    aid, _ = _acct(conn, "short", posts=[(n, f"p{n}", 1) for n in range(0, 70, 7)])
    d = brief.gather(conn, aid, days=365)
    # 10 posts over the 63 days since the first one we hold: ~1.1/wk, not 0.2.
    assert brief._per_week(d, brief._own(d["items"])) == pytest.approx(10 / 9, rel=0.01)


# ── 7. comments ─────────────────────────────────────────────────────────────

def _comment(conn, item_id, body, who, rx=0, n=[0]):
    n[0] += 1
    store.upsert_comment(conn, item_id, platform_comment_id=f"c{n[0]}", body=body,
                         author_name=who, reactions=rx, published_at=store.now())


def test_comments_drop_spam_and_our_own_accounts(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    _acct(conn, "alex_rivera", rel="own", name="Alex Rivera")
    aid, (i1, i2) = _acct(conn, "client", posts=[(1, "Opening line one\nmore", 5),
                                                  (2, "Second opening", 5)])
    _comment(conn, i1, "we love u bro, so proud", "alex_rivera", rx=4)
    _comment(conn, i2, "again from me", "alex_rivera", rx=1)
    _comment(conn, i1, "Maybe you can try @kateinsmm? They're pro at this", "spam1", 3)
    _comment(conn, i1, "SEND IT ON 👉@HEAVEN_OF_TRAVEL_", "spam2", 2)
    _comment(conn, i1, "@friend_two jel lijepse tu ili Portville?", "friend", 1)
    _comment(conn, i2, "Koliko košta jedan paket?", "kupac", 2)
    conn.commit()
    h = brief.s_audience(brief.gather(conn, aid)).html
    assert "so proud" not in h and "alex_rivera" not in h
    assert "kateinsmm" not in h and "HEAVEN" not in h
    assert "Koliko košta jedan paket?" in h
    assert "Questions asked" in h and "Unanswered" not in h
    assert "Opening line one" in h, "each quote names the post it answers"


@pytest.mark.parametrize("text", ["jel radite subotom", "Možete li poslati cjenik",
                                  "imate li termin u petak"])
def test_croatian_questions_without_a_question_mark(text):
    assert brief._is_question(text)


def _gap(body):
    items = [{"body": body, "is_repost": 0}] + [
        {"body": "just a normal post about nothing", "is_repost": 0}] * 11
    html = brief.s_gaps({"items": items}).html
    return {lab: html.split(lab)[1][:60] for lab, _, _ in brief._GAP_TOPICS}


def test_croatian_calls_to_action_and_prices_are_counted():
    assert "1 of 12" in _gap("Rezervirajte termin, link u opisu")["A clear next step"]
    assert "1 of 12" in _gap("Pošalji poruku za ponudu")["A clear next step"]
    assert "1 of 12" in _gap("Cijena paketa je 49 €")["Pricing named"]
    assert "1 of 12" in _gap("porast prodaje od 40 % u godinu dana")["Proof and numbers"]


@pytest.mark.parametrize("body,label", [
    ("100% agree with this", "Proof and numbers"),
    ("I was in charge of the team", "Pricing named"),
    ("First, congrats to everyone", "How it works"),
    ("apply this to your life", "A clear next step"),
    ("novi proizvod je stigao", "A client result shown"),
])
def test_over_matching_patterns_are_tightened(body, label):
    assert "0 of 12" in _gap(body)[label]


# ── 10. title, voice, limits ────────────────────────────────────────────────

def test_title_is_the_name_before_the_tagline(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    aid, _ = _acct(conn, "nk", name="Jordan Novak | Helping teams sell better",
                   posts=[(1, "x", 1)])
    h = brief.write(conn, aid, tmp_path / "b.html").read_text()
    assert "<h1>Jordan Novak</h1>" in h and "Helping teams" not in h.split("</h1>")[0]


def test_combined_title_defaults_to_the_clients_name(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    a1, _ = _acct(conn, "nk", name="Jordan Novak | tagline", posts=[(1, "x", 1)])
    a2, _ = _acct(conn, "nkh", platform="linkedin", name="Jordan Novak Perić",
                  posts=[(1, "y", 1)])
    html, _ = brief.write_combined(conn, [a1, a2], tmp_path / "c")
    assert "<h1>Jordan Novak Perić</h1>" in html.read_text()


def test_limits_ask_a_client_for_their_export_and_tell_competitors_no(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    c, _ = _acct(conn, "cl", rel="client", posts=[(1, "x", 1)])
    r, _ = _acct(conn, "rv", rel="competitor", posts=[(1, "x", 1)])
    ch = brief.write(conn, c, tmp_path / "c.html").read_text()
    rh = brief.write(conn, r, tmp_path / "r.html").read_text()
    assert "Instagram insights export" in ch and "not obtainable" not in ch
    assert "not obtainable" in rh and "insights export" not in rh
    # The chrome talks to a client as "you" and about a competitor as "they".
    assert "written by you" in ch and "written by this account" in rh


# ── 11. Croatian ────────────────────────────────────────────────────────────

def _everything(tmp_path):
    """One account with every section populated, for the language tests."""
    conn = store.connect(tmp_path / "c.db")
    posts = [(n * 9 + 1, f"objava {n} link u opisu\ndrugi red", 10 + n * 7,
              ("short_video", "carousel", "image")[n % 3],
              "ovo je transkript videa" if n % 3 == 0 else None) for n in range(14)]
    aid, ids = _acct(conn, "klijent", posts=posts, followers=500,
                     name="Klijent | slogan")
    store.upsert_item(conn, account_id=aid, platform="instagram", platform_item_id="rp",
                      kind="image", published_at=store.now() - 5 * DAY, body="tuđe",
                      raw={}, is_repost=True, author_name="Netko")
    for n in range(4):
        _comment(conn, ids[n], f"Koliko košta paket broj {n}?", f"osoba{n % 2}", rx=n)
    db = str(tmp_path / "a.db")
    _lens(conn, db, aid, "account-summary", None, kind="account",
          js=json.dumps(SUMMARY))
    _lens(conn, db, ids[0], "content-audit", "Nalaz.")
    _lens(conn, db, ids[1], "what-works", "PERSONAL: obitelj")
    _lens(conn, db, ids[2], "hook-teardown", "Udica.")
    _lens(conn, db, ids[3], "cta-teardown", "Poziv.")
    _lens(conn, db, ids[4], "positioning", "Pozicija.")
    conn.commit()
    return conn, aid, db


def test_croatian_brief_has_no_english_chrome_left(tmp_path):
    conn, aid, db = _everything(tmp_path)
    comp, _ = _acct(conn, "rival", rel="competitor", posts=[(1, "x", 70), (2, "y", 9)])
    brief_i18n.MISSING.clear()
    h = brief.write(conn, aid, tmp_path / "b.html", analysis_db=db, lang="hr",
                    days=365).read_text()
    brief.write(conn, comp, tmp_path / "r.html", lang="hr", days=365)
    brief.write_combined(conn, [aid, comp], tmp_path / "c", analysis_db=db, lang="hr")
    brief.write_benchmark(conn, [aid, comp], tmp_path / "bm", lang="hr", days=90)
    brief.write_benchmark(conn, [aid, comp], tmp_path / "bm0", lang="hr")
    assert brief_i18n.MISSING == set(), sorted(brief_i18n.MISSING)
    assert '<html lang="hr">' in h
    assert "Podaci prikupljeni" in h and "Svaka objava" in h
    assert " Data captured" not in h and "Every post" not in h


def test_english_stays_the_default(tmp_path):
    conn, aid, db = _everything(tmp_path)
    h = brief.write(conn, aid, tmp_path / "b.html", analysis_db=db).read_text()
    assert '<html lang="en">' in h and "Every post" in h


def test_croatian_dates():
    ts = 1785931200          # 2026-08-05 12:00 UTC
    assert brief_i18n.date(ts, "hr") == "5. kolovoza 2026."
    assert brief_i18n.date(ts, "en") == "05 Aug 2026"
    assert brief_i18n.ago(78, "hr") == "prije 78 dana"


# ── 12. CSV ─────────────────────────────────────────────────────────────────

def test_csv_lists_every_post_next_to_the_html(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    aid, _ = _acct(conn, "jordan.novak", posts=[(1, "=HYPERLINK(evil)", 30),
                                               (2, "ordinary", 10), (3, "x", 20)])
    out = brief.write(conn, aid, brief._html_path(tmp_path / "jordan.novak-audit"),
                      csv=True)
    assert out.name == "jordan.novak-audit.html", "a dot in the handle ate the name"
    rows = list(csv.DictReader(out.with_suffix(".csv").open(encoding="utf-8-sig")))
    assert [r["opening"] for r in rows] == ["'=HYPERLINK(evil)", "ordinary", "x"]
    assert rows[0]["x_usual"] == "1.50" and rows[0]["engagement"] == "30"
    assert set(brief.CSV_COLUMNS) <= set(rows[0])


# ── 8. compare ──────────────────────────────────────────────────────────────

def _bench_db(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    ig, _ = _acct(conn, "me_ig", client_ref="me", posts=[(n, "a", 10) for n in range(6)])
    li, _ = _acct(conn, "me_li", platform="linkedin", client_ref="me",
                  posts=[(n, "b", 10) for n in range(6)])
    r_ig, _ = _acct(conn, "rival_ig", rel="competitor",
                    posts=[(n, "c", 50) for n in range(6)])
    r_li, _ = _acct(conn, "rival_li", rel="competitor", platform="linkedin",
                    posts=[(n, "d", 5) for n in range(6)])
    quiet, _ = _acct(conn, "quiet", rel="competitor", posts=[(400, "old", 5)])
    yt, _ = _acct(conn, "tuber", rel="competitor", platform="youtube",
                  posts=[(1, "e", 5)])
    return conn, (ig, li, r_ig, r_li, quiet, yt)


def test_compare_names_who_was_skipped_and_why(tmp_path):
    conn, (ig, li, r_ig, r_li, quiet, yt) = _bench_db(tmp_path)
    h = brief.render_benchmark(
        [brief.gather(conn, a, days=90) for a in (ig, r_ig, quiet, yt)])
    assert "Not compared:" in h
    assert "Quiet, Instagram (no posts in the period)" in h
    assert "Tuber, YouTube (the subject is not on YouTube)" in h
    assert "against 1 account(s)" in h, "only accounts actually compared count"
    table = h.split("<h2>Instagram</h2>")[1].split("</table>")[0]
    assert "Quiet" not in table, "zero-post rows are hidden"


def test_compare_a_client_matches_each_platform_with_its_rivals(tmp_path):
    conn, (ig, li, r_ig, r_li, quiet, yt) = _bench_db(tmp_path)
    h = brief.render_benchmark([brief.gather(conn, a) for a in (ig, li, r_ig, r_li)],
                               subject_ids=[ig, li])
    ig_t = h.split("<h2>Instagram</h2>")[1].split("</table>")[0]
    li_t = h.split("<h2>LinkedIn</h2>")[1].split("</table>")[0]
    assert "Me_Ig" in ig_t and "Rival_Ig" in ig_t and "Rival_Li" not in ig_t
    assert "Me_Li" in li_t and "Rival_Li" in li_t and "Rival_Ig" not in li_t
    assert "against 2 account(s)" in h


def test_compare_cli_accepts_a_client_ref(tmp_path, capsys):
    from corpus import cli
    conn, _ = _bench_db(tmp_path)
    conn.close()
    rc = cli.main(["--db", str(tmp_path / "c.db"), "compare", "--client", "me",
                   "rival_ig", "rival_li", "tuber", "--out", str(tmp_path / "cmp")])
    assert rc == 0
    h = (tmp_path / "cmp.html").read_text()
    assert "<h2>Instagram</h2>" in h and "<h2>LinkedIn</h2>" in h
    assert "skipped tuber" in capsys.readouterr().out


# ── 9. outliers ─────────────────────────────────────────────────────────────

def test_outliers_need_an_absolute_floor_not_just_a_multiple(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    _acct(conn, "tiny", rel="competitor", synced_ago=0,
          posts=[(1, "20 likes", 20)] + [(n, "x", 3) for n in range(3, 10)])
    assert outliers.find(conn, days=30) == []
    assert len(outliers.find(conn, days=30, min_eng=10)) == 1


def test_outliers_without_competitors_says_how_to_add_one(tmp_path, capsys):
    from corpus import cli
    conn = store.connect(tmp_path / "c.db")
    _acct(conn, "me", rel="own", posts=[(1, "x", 1)])
    conn.close()
    assert cli.main(["--db", str(tmp_path / "c.db"), "outliers"]) == 0
    out = capsys.readouterr().out
    assert "no competitors are tracked" in out
    assert "corpus add <profile-url> --relationship competitor" in out


def test_outlier_lines_show_the_year_when_it_is_not_this_year():
    from corpus import cli
    o = {"handle": "h", "platform": "instagram", "x": 4.0, "engagement": 80,
         "usual": 20, "kind": "image", "opening": "x", "url": "",
         "published_at": int(time.mktime((2024, 6, 10, 12, 0, 0, 0, 0, -1)))}
    assert "2024" in cli._outlier_line(o)


# ── 13. dashboard overview ──────────────────────────────────────────────────

def test_overview_shows_data_age_outliers_and_a_compare_link(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    _acct(conn, "stale_one", synced_ago=40, client_ref="acme", posts=[(1, "x", 1)])
    _acct(conn, "rival", rel="competitor", synced_ago=1,
          posts=[(1, "breakout post", 400)] + [(n, "x", 50) for n in range(3, 10)])
    h = web.view_overview(conn)
    assert "<span class='stale'>40d</span>" in h
    assert "<span class='fresh'>1d</span>" in h
    assert "breakout post" in h
    assert "href='/compare/acme'" in h


def test_compare_route_serves_and_keeps_host_protection(tmp_path, monkeypatch):
    import threading
    import http.client
    conn = store.connect(tmp_path / "w.db")
    _acct(conn, "me_ig", client_ref="acme", posts=[(n, "a", 10) for n in range(6)])
    _acct(conn, "rv", rel="competitor", posts=[(n, "b", 30) for n in range(6)])
    conn.close()
    allowed: set = set()
    h = web.make_handler(db_path=tmp_path / "w.db", analysis_db=tmp_path / "a.db",
                         media_root=tmp_path, jobs=web.Jobs(), token=None,
                         brief_dir=tmp_path / "briefs", allowed_hosts=allowed)
    srv = web.ThreadingHTTPServer(("127.0.0.1", 0), h)
    port = srv.server_address[1]
    allowed.update({f"127.0.0.1:{port}"})
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def get(path, host=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", path, headers={"Host": host} if host else {})
        r = c.getresponse()
        return r.status, r.read().decode()
    try:
        st, body = get("/compare/acme")
        assert st == 200 and "<h2>Instagram</h2>" in body and "Rv" in body
        assert get("/compare/acme", host="evil.example")[0] == 421
        assert get("/compare/..%2Fetc")[0] == 400
        st, body = get("/")
        assert st == 200 and "Overview" in body
    finally:
        srv.shutdown()


def test_a_grief_post_is_counted_but_never_showcased(tmp_path):
    """A bereavement post sat at #1 of Top posts, transcript quoted, in a client brief."""
    from corpus import brief, store
    conn = store.connect(tmp_path / "c.db")
    aid = store.upsert_account(conn, platform="instagram", handle="biz",
                               relationship="client")
    now = store.now()
    texts = ["My mum passed away last week. Thank you all for the kind words."] + \
            [f"New menu item {n}: try our tuna steak" for n in range(9)]
    for n, body in enumerate(texts):
        iid, _, _ = store.upsert_item(conn, account_id=aid, platform="instagram",
                                      platform_item_id=f"p{n}", kind="image", raw={},
                                      body=body, published_at=now - (n + 1) * 86400)
        store.record_metrics(conn, iid, reactions=900 if n == 0 else 20 + n)
    conn.commit()
    data = brief.gather(conn, aid)
    assert "passed away" not in brief.s_top(data).html
    assert "passed away" not in brief.s_hooks(data).html
    assert brief.s_summary(data).html            # still counted in the totals
    assert len(brief._own(data["items"])) == 10
    # ...and the "Best post" tile agrees with the table under it (29, not 900).
    tile = brief.s_summary(data).html.split("Best post", 1)[1][:80]
    assert ">29<" in tile and ">900<" not in tile, tile


def test_croatian_numbers_use_a_decimal_comma():
    assert brief_i18n.num("5.45%", "hr") == "5,45%"
    assert brief_i18n.num("1,234.5", "hr") == "1.234,5"
    assert brief_i18n.num("5.45%", "en") == "5.45%"


def test_a_clients_tagged_competitors_are_rivals_not_subjects(tmp_path, capsys,
                                                              monkeypatch):
    """Field test 2026-10-23: competitors added with the client's --client-ref were
    benchmarked as the client and folded into the client's brief numbers."""
    from corpus import cli
    conn = store.connect(tmp_path / "c.db")
    _acct(conn, "harbor", client_ref="m", posts=[(n, "a", 10) for n in range(6)])
    _acct(conn, "cafeportville", rel="competitor", client_ref="m",
          posts=[(n, "rival post", 50) for n in range(6)])
    conn.close()
    db = ["--db", str(tmp_path / "c.db"), "--analysis-db", str(tmp_path / "a.db")]
    assert cli.main([*db, "compare", "--client", "m", "--out", str(tmp_path / "cmp")]) == 0
    h = (tmp_path / "cmp.html").read_text()
    assert "against 1 account(s)" in h and "Cafeportville" in h

    monkeypatch.setattr(cli, "DEFAULT_BRIEFS", str(tmp_path))
    capsys.readouterr()
    assert cli.main([*db, "brief", "all", "--client-ref", "m"]) == 0
    assert "left out 1 competitor" in capsys.readouterr().out
    h = (tmp_path / "m-content-audit.html").read_text()
    assert "rival post" not in h
    assert cli.main([*db, "brief", "all", "--client-ref", "m", "--lang", "hr"]) == 0
    assert (tmp_path / "m-content-audit-hr.html").exists(), "hr must not overwrite en"
    assert cli.main([*db, "brief", "all", "--client-ref", "m",
                     "--relationship", "competitor"]) == 0
    assert "rival post" in (tmp_path / "m-competitor-content-audit.html").read_text()


def test_a_handle_on_two_platforms_gets_two_brief_files(tmp_path, monkeypatch):
    from corpus import cli
    conn = store.connect(tmp_path / "c.db")
    _acct(conn, "harbor", posts=[(1, "ig post", 10)])
    _acct(conn, "harbor", platform="facebook", posts=[(1, "fb post", 10)])
    conn.close()
    monkeypatch.setattr(cli, "DEFAULT_BRIEFS", str(tmp_path))
    db = ["--db", str(tmp_path / "c.db"), "--analysis-db", str(tmp_path / "a.db")]
    assert cli.main([*db, "brief", "instagram:harbor"]) == 0
    assert cli.main([*db, "brief", "facebook:harbor"]) == 0
    assert "ig post" in (tmp_path / "instagram-harbor-content-audit.html").read_text()
    assert "fb post" in (tmp_path / "facebook-harbor-content-audit.html").read_text()


def test_a_business_on_two_platforms_is_named_once_in_its_benchmark(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    ig, _ = _acct(conn, "bistroharbor", name="Bistro Harbor", client_ref="m",
                  posts=[(n, "a", 10) for n in range(3)])
    fb, _ = _acct(conn, "bistroharbor", platform="facebook", name="bistroharbor",
                  client_ref="m", posts=[(n, "b", 10) for n in range(3)])
    r, _ = _acct(conn, "rival", rel="competitor", posts=[(n, "c", 5) for n in range(3)])
    h = brief.render_benchmark([brief.gather(conn, a) for a in (ig, fb, r)],
                               subject_ids=[ig, fb])
    assert "Bistro Harbor: competitor benchmark" in h
    assert "bistroharbor + Bistro Harbor" not in h


def test_an_account_gone_quiet_is_named_in_the_summary(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    quiet, _ = _acct(conn, "quietco", posts=[(190, "old", 30), (250, "older", 20)])
    busy, _ = _acct(conn, "busyco", posts=[(3, "new", 30)])
    for lang, want in (("en", "No new post since"), ("hr", "Nema nove objave od")):
        h = brief.s_summary(brief.gather(conn, quiet, lang=lang)).html
        assert want in h and "190 " in h
    assert "No new post since" not in brief.s_summary(brief.gather(conn, busy)).html


def test_a_collab_another_account_wrote_is_not_the_clients_own(tmp_path):
    """Field test: a cleaning company's collab post on a restaurant's feed was
    reported as the restaurant's writing (and its weakest opening)."""
    from corpus import brief, outliers, store
    conn = store.connect(tmp_path / "c.db")
    aid = store.upsert_account(conn, platform="instagram", handle="bistro",
                               relationship="client")
    now = store.now()
    for n in range(8):
        iid, _, _ = store.upsert_item(conn, account_id=aid, platform="instagram",
                                      platform_item_id=f"p{n}", kind="image", raw={},
                                      body=f"Our dish {n}", author_handle="bistro",
                                      published_at=now - (n + 2) * 86400)
        store.record_metrics(conn, iid, reactions=60)
    iid, _, _ = store.upsert_item(conn, account_id=aid, platform="instagram",
                                  platform_item_id="collab", kind="image", raw={},
                                  body="Sparkling clean kitchens by us",
                                  author_handle="cleaningco", published_at=now - 86400)
    store.record_metrics(conn, iid, reactions=900)
    conn.commit()
    data = brief.gather(conn, aid)
    assert all(i["platform_item_id"] != "collab" for i in brief._own(data["items"]))
    assert "Sparkling clean" not in brief.s_top(data).html
    assert "Sparkling clean" in brief.s_reposts(data).html
    assert outliers.find(conn, days=30, relationships=None) == []
