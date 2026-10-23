"""Comment storage.

Comments are third-party content and cost real money per row, so the properties
that matter are: never duplicate on re-fetch, never leak into post counts, and
never survive erasure.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import store  # noqa: E402


def _acct_item(conn, m_comments=3):
    aid = store.upsert_account(conn, platform="linkedin", handle="t",
                               relationship="own")
    iid, _, _ = store.upsert_item(conn, account_id=aid, platform="linkedin",
                                  platform_item_id="p1", kind="text",
                                  published_at=1700000000, body="post", raw={})
    store.record_metrics(conn, iid, comments=m_comments)
    conn.commit()
    return aid, iid


def test_refetching_a_thread_does_not_duplicate(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    _, iid = _acct_item(conn)
    for _ in range(2):
        store.upsert_comment(conn, iid, platform_comment_id="c1",
                             body="hello", author_name="A")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) c FROM comment").fetchone()["c"] == 1


def test_edited_comment_body_is_refreshed(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    _, iid = _acct_item(conn)
    store.upsert_comment(conn, iid, platform_comment_id="c1", body="before")
    store.upsert_comment(conn, iid, platform_comment_id="c1", body="after",
                         reactions=5)
    conn.commit()
    r = conn.execute("SELECT body, reactions FROM comment").fetchone()
    assert r["body"] == "after" and r["reactions"] == 5


def test_comments_never_count_as_posts(tmp_path):
    """The whole reason they live in their own table."""
    conn = store.connect(tmp_path / "c.db")
    aid, iid = _acct_item(conn)
    for i in range(5):
        store.upsert_comment(conn, iid, platform_comment_id=f"c{i}", body="x")
    conn.commit()
    n = conn.execute("SELECT COUNT(*) c FROM item WHERE account_id=?",
                     (aid,)).fetchone()["c"]
    assert n == 1, "comments leaked into the item table"


def test_author_replies_are_excluded_by_default(tmp_path):
    """A creator answering their own thread is the subject talking, not audience."""
    conn = store.connect(tmp_path / "c.db")
    aid, iid = _acct_item(conn)
    store.upsert_comment(conn, iid, platform_comment_id="a", body="audience")
    store.upsert_comment(conn, iid, platform_comment_id="b", body="thanks!",
                         is_author_reply=True)
    conn.commit()
    assert len(store.comments_for_account(conn, aid)) == 1
    assert len(store.comments_for_account(conn, aid,
                                          include_author_replies=True)) == 2


def test_worklist_skips_threads_already_fetched(tmp_path):
    """Re-paying for a thread we already hold is the expensive failure mode."""
    conn = store.connect(tmp_path / "c.db")
    aid, iid = _acct_item(conn, m_comments=2)
    assert len(store.items_missing_comments(conn, aid)) == 1
    for i in range(2):
        store.upsert_comment(conn, iid, platform_comment_id=f"c{i}", body="x")
    conn.commit()
    assert store.items_missing_comments(conn, aid) == [], \
        "a fully-fetched thread was offered for re-fetch"


def test_worklist_ignores_posts_with_no_comments(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    aid, _ = _acct_item(conn, m_comments=0)
    assert store.items_missing_comments(conn, aid) == []


def test_erasure_removes_comments(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    aid, iid = _acct_item(conn)
    store.upsert_comment(conn, iid, platform_comment_id="c1", body="personal data")
    conn.commit()

    assert store.forget(conn, handle="t")["comments"] == 1, \
        "the dry run must disclose how many comments would be erased"
    store.forget(conn, handle="t", dry_run=False)
    assert conn.execute("SELECT COUNT(*) c FROM comment").fetchone()["c"] == 0


def test_is_new_is_accurate_across_duplicate_rows_in_one_batch(tmp_path):
    """conn.total_changes is cumulative for the connection, so using it to mean
    "did this insert" reported every row as new. The vendor sends duplicates
    inside a single batch, so the count overstated what actually landed."""
    conn = store.connect(tmp_path / "c.db")
    _, iid = _acct_item(conn)
    _, first = store.upsert_comment(conn, iid, platform_comment_id="c1", body="x")
    _, again = store.upsert_comment(conn, iid, platform_comment_id="c1", body="x")
    _, other = store.upsert_comment(conn, iid, platform_comment_id="c2", body="y")
    assert (first, again, other) == (True, False, True)


def test_a_capped_thread_is_not_offered_forever(tmp_path):
    """The money bug: 189 comments billed to store 46.

    A 185-comment post from which we fetched the 25 the cap allows looks 160 short
    against the raw count, so every subsequent run re-fetches and re-pays for the
    same 25 rows.
    """
    conn = store.connect(tmp_path / "c.db")
    aid, iid = _acct_item(conn, m_comments=185)
    for i in range(25):
        store.upsert_comment(conn, iid, platform_comment_id=f"c{i}", body="x")
    conn.commit()

    assert store.items_missing_comments(conn, aid, per_post=25) == [], \
        "a thread already fetched to the cap was offered for re-billing"
    # Raising the cap legitimately reopens it.
    assert len(store.items_missing_comments(conn, aid, per_post=50)) == 1


def test_a_thread_that_returns_less_than_its_count_is_not_retried_forever(tmp_path):
    """Instagram's count includes replies we do not request, so "have < count"
    is never satisfied and the post would be re-billed on every run."""
    conn = store.connect(tmp_path / "c.db")
    aid, iid = _acct_item(conn, m_comments=9)
    for i in range(5):                       # vendor returned only 5 of the 9
        store.upsert_comment(conn, iid, platform_comment_id=f"c{i}", body="x")
    store.mark_comments_fetched(conn, iid)
    conn.commit()
    assert store.items_missing_comments(conn, aid, per_post=25) == []


def test_a_post_that_gained_comments_is_revisited(tmp_path):
    """The watermark must not freeze a live thread out of future fetches.

    Timestamps are set explicitly: a real run marks both within the same second,
    which would make a > comparison flaky rather than wrong.
    """
    conn = store.connect(tmp_path / "c.db")
    aid, iid = _acct_item(conn, m_comments=5)
    store.upsert_comment(conn, iid, platform_comment_id="c0", body="x")
    conn.execute("UPDATE item SET metrics_at=1000, comments_at=1000 WHERE id=?",
                 (iid,))
    conn.commit()
    assert store.items_missing_comments(conn, aid, per_post=25) == [], \
        "a thread fetched after its last metric update was offered again"

    conn.execute("UPDATE item SET m_comments=40, metrics_at=2000 WHERE id=?", (iid,))
    conn.commit()
    assert len(store.items_missing_comments(conn, aid, per_post=25)) == 1, \
        "a thread that grew since the last fetch must be revisited"


def test_likes_moving_does_not_rebuy_a_short_thread(tmp_path):
    """Keyed on metrics_at, a 9-comment thread that returned 5 rows was bought
    again every time its reaction count ticked up."""
    conn = store.connect(tmp_path / "c.db")
    aid, iid = _acct_item(conn, m_comments=9)
    for i in range(5):
        store.upsert_comment(conn, iid, platform_comment_id=f"c{i}", body="x")
    store.mark_comments_fetched(conn, iid)
    conn.execute("UPDATE item SET comments_at = comments_at - 10 WHERE id=?", (iid,))
    store.record_metrics(conn, iid, reactions=500, comments=9)
    conn.commit()
    assert store.items_missing_comments(conn, aid, per_post=25) == []

    store.record_metrics(conn, iid, reactions=510, comments=12)
    conn.commit()
    assert len(store.items_missing_comments(conn, aid, per_post=25)) == 1
