"""Breakout detection: against the account's own past, never its own peak."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import outliers, store  # noqa: E402

DAY = 86400


def _account(conn, handle="comp", rel="competitor", engs=(), last_days_ago=1):
    aid = store.upsert_account(conn, platform="instagram", handle=handle,
                               relationship=rel)
    now = store.now()
    ids = []
    for n, e in enumerate(engs):
        iid, _, _ = store.upsert_item(
            conn, account_id=aid, platform="instagram", platform_item_id=f"{handle}{n}",
            kind="image", raw={}, body=f"post {n}\nsecond line",
            published_at=now - (last_days_ago + len(engs) - 1 - n) * 3 * DAY, url=f"https://x/{n}")
        store.record_metrics(conn, iid, reactions=e)
        ids.append(iid)
    conn.commit()
    return aid, ids


def test_a_post_far_above_the_accounts_usual_is_found(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    _account(conn, engs=[50, 60, 55, 40, 70, 65, 400])
    got = outliers.find(conn, days=30)
    assert len(got) == 1
    assert got[0]["engagement"] == 400 and got[0]["usual"] == 57 and got[0]["x"] >= 7
    assert got[0]["opening"] == "post 6"


def test_a_steady_account_and_a_thin_history_raise_nothing(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    _account(conn, "steady", engs=[50, 60, 55, 40, 70, 65, 80])
    _account(conn, "new", engs=[5, 400])                   # no baseline yet
    assert outliers.find(conn, days=90) == []


def test_old_breakouts_and_own_accounts_are_out_of_scope_by_default(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    _account(conn, "old", engs=[50] * 6 + [900], last_days_ago=200)
    _account(conn, "me", rel="own", engs=[50] * 6 + [900])
    assert outliers.find(conn, days=14) == []
    assert [o["handle"] for o in outliers.find(conn, days=14, relationships=None)] == ["me"]


def test_item_ids_narrow_to_what_a_sync_just_added(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    _, ids = _account(conn, engs=[50] * 6 + [900, 800])
    got = outliers.find(conn, days=90, item_ids=[ids[-1]])
    assert [o["item_id"] for o in got] == [ids[-1]]
