"""Retention: third-party comments on studied accounts age out; client data never."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import cli, retention, store  # noqa: E402

NOW = 1_800_000_000
DAY = 86400


def _seed(tmp_path):
    db = tmp_path / "c.db"
    conn = store.connect(db)
    items = {}
    for handle, rel in (("rival", "competitor"), ("acme", "client"),
                        ("star", "influencer")):
        aid = store.upsert_account(conn, platform="instagram", handle=handle,
                                   relationship=rel)
        iid, *_ = store.upsert_item(conn, account_id=aid, platform="instagram",
                                    platform_item_id=handle, kind="image",
                                    published_at=NOW - 900 * DAY, raw={})
        items[handle] = (aid, iid)
    rows = [  # (item, id, published_at, fetched_at)
        ("rival", "old", NOW - 500 * DAY, NOW - 499 * DAY),
        ("rival", "nodate", None, NOW - 400 * DAY),
        ("rival", "recent-raw", NOW - 200 * DAY, NOW - 150 * DAY),
        ("rival", "fresh", NOW - 10 * DAY, NOW - 5 * DAY),
        ("star", "old", NOW - 600 * DAY, NOW - 600 * DAY),
        ("acme", "old", NOW - 900 * DAY, NOW - 900 * DAY),
    ]
    for h, cid, pub, fet in rows:
        conn.execute("INSERT INTO comment (item_id, platform_comment_id, published_at,"
                     " body, author_name, fetched_at, raw_json) VALUES (?,?,?,?,?,?,?)",
                     (items[h][1], cid, pub, "text", "Someone", fet, '{"x":1}'))
    conn.commit()
    return conn, items, db


def _left(conn):
    return {(r["h"], r["cid"]): r["raw_json"] for r in conn.execute(
        "SELECT a.handle h, c.platform_comment_id cid, c.raw_json FROM comment c "
        "JOIN item i ON i.id=c.item_id JOIN account a ON a.id=i.account_id")}


def test_dry_run_counts_and_changes_nothing(tmp_path):
    conn, _, _ = _seed(tmp_path)
    r = retention.apply(conn, months=12, now=NOW)
    assert (r["delete"], r["blank_raw"]) == (3, 1)
    assert len(_left(conn)) == 6


def test_execute_ages_out_studied_comments_only(tmp_path):
    conn, _, _ = _seed(tmp_path)
    retention.apply(conn, months=12, now=NOW, execute=True)
    left = _left(conn)
    assert set(left) == {("rival", "recent-raw"), ("rival", "fresh"), ("acme", "old")}
    assert left[("rival", "recent-raw")] is None, "raw payload blanked sooner"
    assert left[("rival", "fresh")] is not None
    assert left[("acme", "old")] is not None, "client data is never touched"


def test_a_collab_shared_with_a_client_is_left_alone(tmp_path):
    conn, items, _ = _seed(tmp_path)
    conn.execute("INSERT INTO item_account VALUES (?,?)",
                 (items["rival"][1], items["acme"][0]))
    conn.commit()
    r = retention.apply(conn, months=12, now=NOW, execute=True)
    assert r["delete"] == 1          # only the influencer's
    assert ("rival", "old") in _left(conn)


def test_cli_is_a_dry_run_by_default(tmp_path, capsys):
    conn, _, db = _seed(tmp_path)
    conn.close()
    assert cli.main(["--db", str(db), "retention"]) == 0
    out = capsys.readouterr().out
    assert "would delete" in out and "dry run" in out
    assert len(_left(store.connect(db))) == 6
