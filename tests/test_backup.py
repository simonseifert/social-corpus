"""Backup tests. A backup you have never restored is a hope, not a backup."""
import gzip, sqlite3, sys, time
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from corpus import backup, store  # noqa: E402


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "corpus.db"
    conn = store.connect(p)
    aid = store.upsert_account(conn, platform="linkedin", handle="x",
                               relationship="own")
    for i in range(5):
        iid, _, _ = store.upsert_item(conn, account_id=aid, platform="linkedin",
                                      platform_item_id=f"p{i}", kind="text",
                                      published_at=1700000000 + i,
                                      body=f"post {i}", raw={})
        store.record_metrics(conn, iid, reactions=i * 10)
    conn.commit()
    return p, conn


def test_snapshot_of_a_live_wal_database(db, tmp_path):
    """cp during WAL can tear; the online backup API cannot."""
    p, conn = db
    conn.execute("INSERT INTO account (platform, handle, relationship, first_seen_at) "
                 "VALUES ('x','uncommitted','own', 1)")   # open write txn
    snap = backup.snapshot(p, tmp_path / "b")
    assert snap.path.exists() and snap.bytes > 0
    assert backup.verify(snap.path)["ok"]


def test_verify_counts_real_rows(db, tmp_path):
    p, _ = db
    v = backup.verify(backup.snapshot(p, tmp_path / "b").path)
    assert v["integrity"] == "ok"
    assert v["counts"]["item"] == 5


def test_verify_is_schema_agnostic(tmp_path):
    """analysis.db has no `item` table; hardcoding one marked good backups corrupt."""
    other = tmp_path / "analysis.db"
    c = sqlite3.connect(other)
    c.execute("CREATE TABLE analysis_result (id INTEGER PRIMARY KEY, x TEXT)")
    c.execute("INSERT INTO analysis_result (x) VALUES ('y')")
    c.commit(); c.close()
    assert backup.verify(backup.snapshot(other, tmp_path / "b").path)["ok"]


def test_verify_rejects_an_empty_database(tmp_path):
    """Structurally fine and empty is the backup failure that looks like success."""
    empty = tmp_path / "empty.db"
    c = sqlite3.connect(empty)
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    c.commit(); c.close()
    assert not backup.verify(backup.snapshot(empty, tmp_path / "b").path)["ok"]


def test_verify_rejects_a_corrupt_snapshot(tmp_path):
    bad = tmp_path / "bad.db.gz"
    with gzip.open(bad, "wb") as f:
        f.write(b"SQLite format 3\x00" + b"\x00" * 400)
    v = backup.verify(bad)
    assert not v["ok"]


def test_restore_round_trip(db, tmp_path):
    """The whole point. Take a backup, destroy the original, get the data back."""
    p, conn = db
    snap = backup.snapshot(p, tmp_path / "b")
    conn.close()

    p.unlink()
    for sfx in ("-wal", "-shm"):
        Path(str(p) + sfx).unlink(missing_ok=True)

    backup.restore(snap.path, p)
    back = store.connect(p)
    assert back.execute("SELECT COUNT(*) c FROM item").fetchone()["c"] == 5
    assert back.execute("SELECT body FROM item WHERE platform_item_id='p3'"
                        ).fetchone()["body"] == "post 3"
    assert back.execute("SELECT COUNT(*) c FROM metric_snapshot").fetchone()["c"] == 5


def test_restore_refuses_to_clobber_without_force(db, tmp_path):
    p, _ = db
    snap = backup.snapshot(p, tmp_path / "b")
    with pytest.raises(FileExistsError):
        backup.restore(snap.path, p)


def test_restore_moves_the_old_database_aside(db, tmp_path):
    """You are least sure it is the right restore at the moment you most want one."""
    p, conn = db
    snap = backup.snapshot(p, tmp_path / "b")
    conn.close()
    backup.restore(snap.path, p, overwrite=True)
    aside = list(p.parent.glob("corpus.db.replaced-*"))
    assert aside, "the previous database must be preserved, not deleted"


def test_restore_refuses_an_unusable_snapshot(db, tmp_path):
    p, _ = db
    bad = tmp_path / "bad.db.gz"
    with gzip.open(bad, "wb") as f:
        f.write(b"nonsense")
    with pytest.raises(RuntimeError):
        backup.restore(bad, tmp_path / "dest.db")


def test_prune_keeps_the_newest(db, tmp_path):
    p, _ = db
    out = tmp_path / "b"
    for _ in range(4):
        backup.snapshot(p, out)
        time.sleep(1.05)          # stamps are second-granular
    removed = backup.prune(out, keep=2, stem="corpus")
    assert len(removed) == 2
    assert len(backup.list_snapshots(out, "corpus")) == 2


def test_run_reports_errors_rather_than_raising(tmp_path):
    res = backup.run([tmp_path / "nope.db"], tmp_path / "b")
    assert res["taken"] == [] and res["errors"] == []


def test_run_accepts_a_faithful_copy_of_a_database_that_is_still_empty(db, tmp_path):
    """analysis.db exists but is empty until the first `analyze --execute`. Its
    snapshot failed verification, so every nightly backup on a fresh install exited
    1 and pruned nothing. An empty copy of an empty source is a correct backup; an
    empty copy of a source WITH rows still fails (verify() above)."""
    p, _ = db
    empty = tmp_path / "analysis.db"
    c = sqlite3.connect(empty)
    c.execute("CREATE TABLE analysis_result (id INTEGER PRIMARY KEY)")
    c.commit(); c.close()
    for _ in range(2):          # second run takes the "unchanged" branch
        res = backup.run([p, empty], tmp_path / "b")
        assert res["errors"] == [], res
        assert all(v["ok"] for v in res["verified"])
        time.sleep(1.1)         # snapshot names are per second
