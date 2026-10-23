"""Store behaviour tests.

These cover the properties the whole design leans on: idempotent re-sync,
change-only metrics, transcript never clobbered, and FTS staying in step with
late-arriving enrichment.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from corpus import store  # noqa: E402


def fresh(tmp_path):
    return store.connect(tmp_path / "t.db")


def _acct(conn, rel="competitor"):
    return store.upsert_account(conn, platform="linkedin", handle="test",
                                relationship=rel, url="https://x/in/test")


def test_resync_is_idempotent(tmp_path):
    conn = fresh(tmp_path)
    aid = _acct(conn)
    kw = dict(account_id=aid, platform="linkedin", platform_item_id="p1",
              kind="text", published_at=1700000000, body="hello", raw={"a": 1})

    _id, new, changed = store.upsert_item(conn, **kw)
    assert new and not changed

    _id2, new2, changed2 = store.upsert_item(conn, **kw)
    assert _id2 == _id
    assert not new2 and not changed2, "re-sync of unchanged data must be a no-op"
    assert conn.execute("SELECT COUNT(*) c FROM item").fetchone()["c"] == 1


def test_edit_is_detected(tmp_path):
    conn = fresh(tmp_path)
    aid = _acct(conn)
    kw = dict(account_id=aid, platform="linkedin", platform_item_id="p1",
              kind="text", published_at=1700000000, raw={})
    store.upsert_item(conn, body="before", **kw)
    _id, new, changed = store.upsert_item(conn, body="after", **kw)
    assert not new and changed
    row = conn.execute("SELECT body FROM item WHERE id=?", (_id,)).fetchone()
    assert row["body"] == "after"


def test_transcript_is_never_clobbered(tmp_path):
    """A metadata-only refresh must not wipe a transcript enrichment added later."""
    conn = fresh(tmp_path)
    aid = _acct(conn)
    kw = dict(account_id=aid, platform="linkedin", platform_item_id="v1",
              kind="short_video", published_at=1700000000, body="cap", raw={})

    iid, _, _ = store.upsert_item(conn, **kw)
    store.upsert_item(conn, transcript="spoken words", **kw)
    assert conn.execute("SELECT transcript FROM item WHERE id=?",
                        (iid,)).fetchone()["transcript"] == "spoken words"

    # re-fetch with no transcript field, as a metadata refresh would
    store.upsert_item(conn, **kw)
    assert conn.execute("SELECT transcript FROM item WHERE id=?",
                        (iid,)).fetchone()["transcript"] == "spoken words"


def test_metrics_are_change_only(tmp_path):
    conn = fresh(tmp_path)
    aid = _acct(conn)
    iid, _, _ = store.upsert_item(conn, account_id=aid, platform="linkedin",
                                  platform_item_id="p1", kind="text",
                                  published_at=1700000000, raw={})

    assert store.record_metrics(conn, iid, reactions=10, comments=2) is True
    assert store.record_metrics(conn, iid, reactions=10, comments=2) is False, \
        "identical metrics must not append a row"
    assert store.record_metrics(conn, iid, reactions=11, comments=2) is True

    n = conn.execute("SELECT COUNT(*) c FROM metric_snapshot WHERE item_id=?",
                     (iid,)).fetchone()["c"]
    assert n == 2

    cache = conn.execute("SELECT m_reactions FROM item WHERE id=?", (iid,)).fetchone()
    assert cache["m_reactions"] == 11, "sort cache must track the latest snapshot"


def test_fts_indexes_late_transcript(tmp_path):
    """The UPDATE trigger is the one that matters: enrichment fills transcript
    long after insert, and without it those rows stay unsearchable."""
    conn = fresh(tmp_path)
    aid = _acct(conn)
    kw = dict(account_id=aid, platform="linkedin", platform_item_id="v1",
              kind="short_video", published_at=1700000000, body="caption text",
              raw={})
    store.upsert_item(conn, **kw)
    assert len(store.search(conn, "unicorn")) == 0

    store.upsert_item(conn, transcript="a wild unicorn appears", **kw)
    hits = store.search(conn, "unicorn")
    assert len(hits) == 1, "transcript added after insert must become searchable"

    assert conn.execute(
        "INSERT INTO item_fts(item_fts) VALUES('integrity-check')"
    ) is not None


def test_relationship_is_constrained(tmp_path):
    conn = fresh(tmp_path)
    try:
        store.upsert_account(conn, platform="linkedin", handle="bad",
                             relationship="whatever")
    except Exception as e:
        assert "CHECK" in str(e).upper() or "constraint" in str(e).lower()
    else:
        raise AssertionError("relationship must reject unknown values")


def test_migrate_adds_missing_column(tmp_path):
    """A database created before a column existed must gain it on next connect."""
    db = tmp_path / "old.db"
    conn = store.connect(db)
    conn.close()

    import sqlite3
    raw = sqlite3.connect(db)
    raw.execute("DROP INDEX ix_item_work")  # partial index references the column
    raw.execute("ALTER TABLE item DROP COLUMN work_key")
    raw.commit()
    raw.close()

    conn = store.connect(db)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(item)")}
    assert "work_key" in cols


def test_health_reports_staleness(tmp_path):
    conn = fresh(tmp_path)
    rid = store.start_run(conn, adapter="linkedin_posts", mode="delta")
    store.finish_run(conn, rid, status="ok", seen=3, new=3)
    h = store.health(conn)
    assert h[0]["adapter"] == "linkedin_posts" and h[0]["status"] == "ok"
    assert h[0]["stale"] is False
    assert store.health(conn, stale_after_s=-1)[0]["stale"] is True


# ── erasure ─────────────────────────────────────────────────────────────────

def test_forget_is_a_dry_run_by_default(tmp_path):
    """The one operation with no undo defaults to showing, not doing."""
    conn = fresh(tmp_path)
    aid = _acct(conn, rel="competitor")
    store.upsert_item(conn, account_id=aid, platform="linkedin",
                      platform_item_id="p1", kind="text", published_at=1,
                      body="x", raw={})
    conn.commit()

    res = store.forget(conn, handle="test")
    assert res["dry_run"] and res["items"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM item").fetchone()["c"] == 1


def test_forget_removes_everything_including_derived_rows(tmp_path):
    conn = fresh(tmp_path)
    aid = _acct(conn, rel="competitor")
    iid, _, _ = store.upsert_item(conn, account_id=aid, platform="linkedin",
                                  platform_item_id="p1", kind="text",
                                  published_at=1, body="x", raw={"personal": "data"})
    store.record_metrics(conn, iid, reactions=5)
    store.add_media(conn, iid, role="original", remote_url="http://x")
    conn.commit()

    store.forget(conn, handle="test", dry_run=False)
    for table in ("account", "item", "media", "metric_snapshot"):
        assert conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] == 0, \
            f"{table} still holds data after erasure"


def test_forget_deletes_stored_files_not_just_rows(tmp_path):
    """Keyframes and stills are personal data on disk, not only in the database."""
    conn = fresh(tmp_path)
    aid = _acct(conn, rel="competitor")
    iid, _, _ = store.upsert_item(conn, account_id=aid, platform="linkedin",
                                  platform_item_id="p1", kind="short_video",
                                  published_at=1, raw={})
    f = tmp_path / "frame.jpg"
    f.write_bytes(b"pixels")
    conn.execute("INSERT INTO media (item_id, role, local_path, state, created_at) "
                 "VALUES (?, 'keyframe', ?, 'stored', 1)", (iid, str(f)))
    conn.commit()

    store.forget(conn, handle="test", dry_run=False)
    assert not f.exists(), "the stored image survived erasure"


def test_forget_refuses_to_erase_everything(tmp_path):
    conn = fresh(tmp_path)
    _acct(conn)
    with pytest.raises(ValueError):
        store.forget(conn, dry_run=False)


def test_forget_leaves_other_accounts_alone(tmp_path):
    conn = fresh(tmp_path)
    keep = store.upsert_account(conn, platform="linkedin", handle="keeper",
                                relationship="own")
    gone = store.upsert_account(conn, platform="linkedin", handle="gone",
                                relationship="competitor")
    for aid, pid in ((keep, "k1"), (gone, "g1")):
        store.upsert_item(conn, account_id=aid, platform="linkedin",
                          platform_item_id=pid, kind="text", published_at=1, raw={})
    conn.commit()

    store.forget(conn, handle="gone", dry_run=False)
    rows = conn.execute("SELECT handle FROM account").fetchall()
    assert [r["handle"] for r in rows] == ["keeper"]
    assert conn.execute("SELECT COUNT(*) c FROM item").fetchone()["c"] == 1


# ── FTS column migration ────────────────────────────────────────────────────

def test_adding_a_searchable_column_rebuilds_the_index(tmp_path):
    """The migration that broke a live database.

    DROP on an fts5 virtual table can leave its shadow tables behind. A fresh CREATE
    then adopts rows written under the OLD column count: the index reads fine, and
    the next write fails with "database disk image is malformed" -- which names the
    whole database rather than the index that is actually wrong.
    """
    import sqlite3
    db = tmp_path / "old.db"
    conn = store.connect(db)
    aid = store.upsert_account(conn, platform="linkedin", handle="a",
                               relationship="own")
    store.upsert_item(conn, account_id=aid, platform="linkedin",
                      platform_item_id="p1", kind="text", published_at=1,
                      body="findable words here", raw={})
    conn.commit()
    conn.close()

    # Rewind to the previous schema: a 3-column index, shadows and all.
    raw = sqlite3.connect(db)
    for trig in ("item_ai", "item_ad", "item_au"):
        raw.execute(f"DROP TRIGGER IF EXISTS {trig}")
    raw.execute("DROP TABLE item_fts")
    raw.execute("CREATE VIRTUAL TABLE item_fts USING fts5("
                "title, body, transcript, content='item', content_rowid='id')")
    raw.execute("INSERT INTO item_fts(item_fts) VALUES('rebuild')")
    raw.commit()
    raw.close()

    conn = store.connect(db)          # must notice the drift and rebuild
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(item_fts)")]
    assert "screen_text" in cols, "the index was not migrated"

    # The bug was silent until a write. This is the assertion that matters.
    conn.execute("UPDATE item SET screen_text='burned in caption' WHERE id=1")
    conn.commit()
    assert len(store.search(conn, "caption")) == 1
    assert len(store.search(conn, "findable")) == 1, "existing rows must survive"


def test_screen_text_is_searchable(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    aid = _acct(conn)
    store.upsert_item(conn, account_id=aid, platform="instagram",
                      platform_item_id="r1", kind="short_video", published_at=1,
                      body="a caption", screen_text="YOU WORRY A LOT", raw={})
    conn.commit()
    assert len(store.search(conn, "worry")) == 1, \
        "on-screen text must be searchable; on a music reel it is the whole message"


def test_search_highlight_delimiters_are_caller_supplied(tmp_path):
    """A caption containing a literal "[" is indistinguishable from a highlight
    marker once the snippet is a plain string. A terminal can live with that; an
    HTML renderer turning them into tags cannot."""
    conn = fresh(tmp_path)
    aid = _acct(conn)
    store.upsert_item(conn, account_id=aid, platform="linkedin",
                      platform_item_id="p1", kind="text", published_at=1,
                      body="see [the guide] for unicorn tips", raw={})
    conn.commit()
    hit = store.search(conn, "unicorn", open_mark="\x02", close_mark="\x03")[0]
    snip = hit["snip"]
    assert "\x02unicorn\x03" in snip
    assert "[the guide]" in snip, "a literal bracket must survive untouched"


def test_forget_works_on_an_account_that_has_been_synced(tmp_path):
    """Every sync writes a scrape_run row pointing at the account. Without unlinking
    it the DELETE hit a foreign key AFTER the files were gone: rows kept, files lost."""
    conn = fresh(tmp_path)
    aid = _acct(conn, rel="competitor")
    iid, _, _ = store.upsert_item(conn, account_id=aid, platform="linkedin",
                                  platform_item_id="p1", kind="text", published_at=1, raw={})
    f = tmp_path / "frame.jpg"
    f.write_bytes(b"pixels")
    conn.execute("INSERT INTO media (item_id, role, local_path, state, created_at) "
                 "VALUES (?, 'keyframe', ?, 'stored', 1)", (iid, str(f)))
    rid = store.start_run(conn, adapter="linkedin_posts", mode="delta", account_id=aid)
    store.finish_run(conn, rid, status="ok", seen=1, new=1)
    conn.commit()

    store.forget(conn, handle="test", dry_run=False)
    assert conn.execute("SELECT COUNT(*) c FROM account").fetchone()["c"] == 0
    assert not f.exists()
    run = conn.execute("SELECT account_id, status FROM scrape_run").fetchone()
    assert run["account_id"] is None and run["status"] == "ok", "provenance kept, unlinked"


def test_forget_keeps_a_file_another_account_still_uses(tmp_path):
    conn = fresh(tmp_path)
    keep = store.upsert_account(conn, platform="linkedin", handle="keeper",
                                relationship="own")
    gone = store.upsert_account(conn, platform="linkedin", handle="gone",
                                relationship="competitor")
    shared = tmp_path / "black.jpg"
    shared.write_bytes(b"\0")
    for aid, pid in ((keep, "k1"), (gone, "g1")):
        iid, _, _ = store.upsert_item(conn, account_id=aid, platform="linkedin",
                                      platform_item_id=pid, kind="text",
                                      published_at=1, raw={})
        conn.execute("INSERT INTO media (item_id, role, local_path, state, created_at) "
                     "VALUES (?, 'keyframe', ?, 'stored', 1)", (iid, str(shared)))
    conn.commit()

    store.forget(conn, handle="gone", dry_run=False)
    assert shared.exists(), "deleted a file the kept account still points at"


def test_forget_removes_kept_audio_and_thumbnails(tmp_path):
    import hashlib
    from corpus import brief
    conn = fresh(tmp_path)
    aid = _acct(conn, rel="competitor")
    iid, _, _ = store.upsert_item(conn, account_id=aid, platform="linkedin",
                                  platform_item_id="p1", kind="short_video",
                                  published_at=1, raw={})
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"pixels")
    conn.execute("INSERT INTO media (item_id, role, local_path, state, created_at) "
                 "VALUES (?, 'keyframe', ?, 'stored', 1)", (iid, str(frame)))
    conn.commit()
    media_root, thumbs = tmp_path / "media", tmp_path / "thumbs"
    audio = media_root / "audio" / (hashlib.sha256(str(iid).encode()).hexdigest()[:16] + ".opus")
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"opus")
    key = hashlib.sha256(f"{frame}\x1f{frame.stat().st_mtime_ns}\x1f{brief.THUMB_PX}"
                         .encode()).hexdigest()[:20]
    thumb = thumbs / f"{key}.jpg"
    thumbs.mkdir()
    thumb.write_bytes(b"small")

    store.forget(conn, handle="test", dry_run=False, media_root=media_root,
                 thumb_dir=thumbs)
    assert not audio.exists() and not thumb.exists() and not frame.exists()


def test_a_route_that_cannot_see_views_does_not_erase_them(tmp_path):
    """Client export reports views; the routine public sync does not. The sync used
    to write views=NULL and the views section vanished from the brief."""
    conn = fresh(tmp_path)
    aid = _acct(conn)
    iid, _, _ = store.upsert_item(conn, account_id=aid, platform="linkedin",
                                  platform_item_id="p1", kind="text", published_at=1, raw={})
    store.record_metrics(conn, iid, reactions=10, views=900)        # export
    store.record_metrics(conn, iid, reactions=12, comments=3,        # public route
                         carry=("views", "saves"))
    row = conn.execute("SELECT m_reactions, m_comments, m_views FROM item").fetchone()
    assert tuple(row) == (12, 3, 900)


def test_a_payload_with_no_counts_records_nothing(tmp_path):
    conn = fresh(tmp_path)
    aid = _acct(conn)
    iid, _, _ = store.upsert_item(conn, account_id=aid, platform="linkedin",
                                  platform_item_id="p1", kind="text", published_at=1, raw={})
    store.record_metrics(conn, iid, reactions=10)
    assert store.record_metrics(conn, iid) is False
    assert conn.execute("SELECT m_reactions FROM item").fetchone()[0] == 10


def test_a_count_the_owner_hides_is_blanked_not_kept(tmp_path):
    """Outside `carry`, None means measured-as-absent: a hidden like count must stop
    showing the old number."""
    conn = fresh(tmp_path)
    aid = _acct(conn)
    iid, _, _ = store.upsert_item(conn, account_id=aid, platform="instagram",
                                  platform_item_id="p1", kind="image", published_at=1, raw={})
    store.record_metrics(conn, iid, reactions=50, comments=4)
    store.record_metrics(conn, iid, reactions=None, comments=5, carry=("shares", "saves"))
    assert conn.execute("SELECT m_reactions FROM item").fetchone()[0] is None


# ── review fixes, 2026-10-22 ────────────────────────────────────────────────

def test_a_handle_differing_only_in_case_is_the_same_account(tmp_path):
    conn = fresh(tmp_path)
    a = store.upsert_account(conn, platform="linkedin", handle="alexrivera",
                             relationship="own")
    b = store.upsert_account(conn, platform="linkedin", handle="AlexRivera",
                             relationship="own")
    assert a == b
    assert conn.execute("SELECT COUNT(*) FROM account").fetchone()[0] == 1


def test_a_sync_whose_profile_spells_the_handle_differently_adds_no_account(tmp_path):
    """persist() used to upsert the vendor's spelling of the handle as an account."""
    from corpus import sync
    from corpus.adapters.base import Adapter, FetchResult, Profile, RawItem
    conn = fresh(tmp_path)
    aid = store.upsert_account(conn, platform="linkedin", handle="ana-k",
                               relationship="competitor")
    ad = Adapter(name="t", platform="linkedin", fetch=lambda *a, **k: None, relationships=("competitor",))
    res = FetchResult(items=[RawItem(platform_item_id="p1", kind="text",
                                     published_at=1700000000, raw={})],
                      profile=Profile(handle="Ana-K-renamed", display_name="Ana K",
                                      follower_count=120))
    sync.persist(conn, ad, aid, res)
    rows = conn.execute("SELECT id, handle, display_name, follower_count "
                        "FROM account").fetchall()
    assert [tuple(r) for r in rows] == [(aid, "ana-k", "Ana K", 120)]


def test_client_ref_can_be_cleared_but_is_kept_when_not_given(tmp_path):
    conn = fresh(tmp_path)
    kw = dict(platform="linkedin", handle="c", relationship="client")
    aid = store.upsert_account(conn, client_ref="acme", **kw)
    store.upsert_account(conn, **kw)
    ref = lambda: conn.execute("SELECT client_ref FROM account WHERE id=?",  # noqa: E731
                               (aid,)).fetchone()[0]
    assert ref() == "acme"
    store.upsert_account(conn, client_ref="", **kw)
    assert ref() is None


def _collab(tmp_path):
    """One IG collab post seen on two tracked accounts' feeds."""
    from corpus import sync
    from corpus.adapters.base import Adapter, FetchResult, RawItem
    conn = fresh(tmp_path)
    a = store.upsert_account(conn, platform="instagram", handle="a",
                             relationship="competitor")
    b = store.upsert_account(conn, platform="instagram", handle="b",
                             relationship="competitor")
    ad = Adapter(name="t", platform="instagram", fetch=lambda *x, **k: None, relationships=("competitor",))
    post = lambda pid: RawItem(platform_item_id=pid, kind="image",  # noqa: E731
                               published_at=1700000000, raw={}, body="collab")
    sync.persist(conn, ad, a, FetchResult(items=[post("shared"), post("a-only")]))
    sync.persist(conn, ad, b, FetchResult(items=[post("shared")]))
    return conn, a, b


def test_a_collab_post_counts_for_both_co_authors(tmp_path):
    from corpus import brief, freshness
    conn, a, b = _collab(tmp_path)
    assert [i["platform_item_id"] for i in brief.gather(conn, b)["items"]] == ["shared"]
    assert len(brief.gather(conn, a)["items"]) == 2
    assert freshness.newest_stored(conn, b)[0] == "shared"


def test_forgetting_the_owner_of_a_collab_post_keeps_it_for_the_co_author(tmp_path):
    conn, a, b = _collab(tmp_path)
    out = store.forget(conn, account_id=a, dry_run=False)
    assert out["items"] == 1                      # only a-only
    left = conn.execute("SELECT platform_item_id, account_id FROM item").fetchall()
    assert [tuple(r) for r in left] == [("shared", b)]
    assert conn.execute("SELECT COUNT(*) FROM item_account").fetchone()[0] == 0
