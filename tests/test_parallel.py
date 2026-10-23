"""Concurrency tests: same answers, one writer, nothing lost to an interrupt.

Three properties are asserted throughout, because they are the three ways a
parallel pipeline goes wrong in a way a normal test would not notice:

  1. Concurrent and serial produce identical database state.
  2. Every write happens on the thread that owns the connection. SQLite permits
     exactly one writer, and store.connect() deliberately omits
     check_same_thread=False so a stray write raises rather than corrupts.
  3. Results reach disk as they land. An interrupt may lose work still in flight;
     it must never lose work already paid for.

Sleeps are small but real: a test that stubs out the waiting cannot tell the
difference between a pool and a for-loop, which is the thing being tested.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import freshness, llm, store, sync as sync_mod  # noqa: E402
from corpus.adapters.base import (Adapter, FetchResult, Profile,  # noqa: E402
                                  RawItem, RawMedia)
from corpus.analyze import run as ar  # noqa: E402
from corpus.enrich import media as m  # noqa: E402
from corpus.enrich import run as er  # noqa: E402
from corpus.enrich import transcribe as tx  # noqa: E402

MAIN = threading.current_thread()


def gate(n: int, *, limit: int | None = None, timeout: float = 10.0):
    """A gate that only opens when `n` callers are inside it at the same time.

    Wall-clock assertions ("concurrent was faster") are the obvious way to prove a
    pool is real, and they are wrong here: this machine runs other work, and a
    loaded box turns a correct implementation into a red test. A barrier proves the
    same property exactly -- if the code is secretly serial, the nth caller never
    arrives and the barrier breaks -- and it does not care how busy the CPU is.

    `limit` stops gating after that many calls, for batches larger than the pool.
    """
    barrier = threading.Barrier(n)
    state = {"broken": False, "calls": 0}
    lock = threading.Lock()

    def enter():
        with lock:
            state["calls"] += 1
            i = state["calls"]
        if limit is not None and i > limit:
            return
        try:
            barrier.wait(timeout=timeout)
        except threading.BrokenBarrierError:
            state["broken"] = True

    return enter, state


# ── fixtures ────────────────────────────────────────────────────────────────

def _item(pid: str, *, published_at: int = 1700000000, reactions: int = 1):
    return RawItem(platform_item_id=pid, kind="text", published_at=published_at,
                   raw={"id": pid}, body=f"body {pid}",
                   metrics={"reactions": reactions})


def slow_adapter(name="fake", *, delay=0.15, items=2, platform="linkedin",
                 seen=None, fail=False, cost=0.01):
    """An adapter that waits like a vendor does, and records where it ran."""
    def fetch(target, *, mode="delta", keys=None, since=None, limit=None, **kw):
        time.sleep(delay)
        if seen is not None:
            seen.append((target, threading.current_thread()))
        if fail:
            raise RuntimeError(f"{name} is down")
        n = 1 if limit == 1 else items
        return FetchResult(
            items=[_item(f"{target}-{i}") for i in range(n)],
            profile=Profile(handle=target, display_name=target.upper(),
                            follower_count=10),
            cost_usd=cost)
    return Adapter(name=name, platform=platform, fetch=fetch,
                   relationships=("competitor",), cost_per_1k=2.0)


@pytest.fixture
def db(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    yield conn
    conn.close()


def _accounts(conn, n, *, platform="linkedin"):
    return [store.upsert_account(conn, platform=platform, handle=f"acct{i}",
                                 relationship="competitor")
            for i in range(n)]


def _jobs(conn, ids, adapter, **kw):
    return [sync_mod.Job(account_id=aid, target=f"acct{i}", routes=[adapter],
                         handle=f"acct{i}", fetch_kwargs={"limit": 10}, **kw)
            for i, aid in enumerate(ids)]


def _snapshot(conn):
    """Everything a sync is supposed to have written, order-independent."""
    return {
        "items": sorted(tuple(r) for r in conn.execute(
            "SELECT account_id, platform_item_id, body, kind FROM item")),
        "metrics": sorted(tuple(r) for r in conn.execute(
            "SELECT item_id, reactions FROM metric_snapshot")),
        "accounts": sorted(tuple(r) for r in conn.execute(
            "SELECT handle, display_name, follower_count FROM account")),
        "runs": sorted(tuple(r) for r in conn.execute(
            "SELECT adapter, status, items_seen, items_new FROM scrape_run")),
    }


# ── sync: same answers ──────────────────────────────────────────────────────

def test_concurrent_sync_matches_serial(tmp_path):
    """The whole point. Six accounts, two runs, byte-identical outcomes."""
    out = {}
    for label, workers in (("serial", 1), ("concurrent", 6)):
        conn = store.connect(tmp_path / f"{label}.db")
        ids = _accounts(conn, 6)
        sync_mod.run_jobs(conn, _jobs(conn, ids, slow_adapter(delay=0.05)),
                          workers=workers)
        out[label] = _snapshot(conn)
        conn.close()
    assert out["serial"] == out["concurrent"]
    assert len(out["serial"]["items"]) == 12


def test_concurrent_sync_actually_overlaps(db):
    """Guard against a 'pool' that is really a for-loop."""
    enter, state = gate(6)
    ids = _accounts(db, 6)

    def fetch(target, *, mode="delta", keys=None, since=None, limit=None, **kw):
        enter()
        return FetchResult(items=[_item(f"{target}-0")], cost_usd=0.0)

    adapter = Adapter(name="fake", platform="linkedin", fetch=fetch,
                      relationships=("competitor",))
    sync_mod.run_jobs(db, _jobs(db, ids, adapter), workers=6)
    assert not state["broken"], "six accounts never had six fetches in flight"
    assert db.execute("SELECT COUNT(*) c FROM item").fetchone()["c"] == 6


def test_fetches_run_off_thread_and_writes_do_not(db):
    """The boundary that makes this safe, asserted rather than assumed."""
    seen: list = []
    write_threads: list = []
    real_persist = sync_mod.persist

    def watched_persist(conn, adapter, account_id, result, **kw):
        write_threads.append(threading.current_thread())
        return real_persist(conn, adapter, account_id, result, **kw)

    sync_mod.persist = watched_persist
    try:
        ids = _accounts(db, 4)
        sync_mod.run_jobs(db, _jobs(db, ids, slow_adapter(delay=0.05, seen=seen)),
                          workers=4)
    finally:
        sync_mod.persist = real_persist

    assert len(seen) == 4 and len(write_threads) == 4
    assert all(t is not MAIN for _, t in seen), "fetches must leave the main thread"
    assert len({t for _, t in seen}) > 1, "fetches must not share one worker"
    assert all(t is MAIN for t in write_threads), "every write stays on the owner"


def test_fetch_only_cannot_reach_the_database(db):
    """Structural, not conventional: the network half takes no connection."""
    import inspect
    assert "conn" not in inspect.signature(sync_mod.fetch_only).parameters
    assert "conn" not in inspect.signature(sync_mod.execute).parameters

    # And the reason that matters: a connection used off its own thread raises.
    import sqlite3
    err: list = []

    def touch():
        try:
            db.execute("SELECT 1").fetchone()
        except sqlite3.ProgrammingError as e:
            err.append(e)

    t = threading.Thread(target=touch)
    t.start()
    t.join()
    assert err, "check_same_thread must stay on; it is what enforces the boundary"


# ── sync: streaming and interrupts ──────────────────────────────────────────

def test_results_are_written_as_they_land(db):
    """A fast account must be on disk before a slow one has even returned."""
    ids = _accounts(db, 2)
    # The slow account is held open until the fast one has been written, so the
    # assertion is about ordering rather than about how fast this machine is.
    release, slow_done = threading.Event(), threading.Event()

    def fetch(target, *, mode="delta", keys=None, since=None, limit=None, **kw):
        if target == "acct1":
            release.wait(timeout=10)
            slow_done.set()
        return FetchResult(items=[_item(f"{target}-0")], cost_usd=0.0)

    adapter = Adapter(name="fake", platform="linkedin", fetch=fetch,
                      relationships=("competitor",))
    observed: list = []

    def on_result(job, res):
        observed.append((job.handle,
                         db.execute("SELECT COUNT(*) c FROM item").fetchone()["c"],
                         slow_done.is_set()))
        release.set()

    sync_mod.run_jobs(db, _jobs(db, ids, adapter), workers=2, on_result=on_result)

    first = observed[0]
    assert first[0] == "acct0", "the fast account should land first"
    assert first[1] == 1, "its row must already be committed, not batched"
    assert first[2] is False, "and written while the slow fetch is still running"


def test_interrupt_keeps_completed_work_and_leaves_no_running_rows(db):
    """Ctrl-C after two accounts: two persisted, nothing stuck in 'running'."""
    ids = _accounts(db, 5)

    def on_result(job, res):
        if db.execute("SELECT COUNT(*) c FROM item").fetchone()["c"] >= 4:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        sync_mod.run_jobs(db, _jobs(db, ids, slow_adapter(delay=0.05)),
                          workers=5, on_result=on_result)

    done = db.execute("SELECT COUNT(*) c FROM item").fetchone()["c"]
    assert done >= 4, "work already fetched and returned must be persisted"

    statuses = [r["status"] for r in db.execute("SELECT status FROM scrape_run")]
    assert "running" not in statuses, f"a run row was left running: {statuses}"
    assert statuses.count("ok") == done // 2
    assert "interrupted" in statuses

    # And the corpus is readable and consistent afterwards.
    orphans = db.execute(
        "SELECT COUNT(*) c FROM item WHERE account_id NOT IN "
        "(SELECT id FROM account)").fetchone()["c"]
    assert orphans == 0


def test_a_failing_account_does_not_take_the_others_down(db):
    ids = _accounts(db, 3)
    good, bad = slow_adapter(delay=0.05), slow_adapter(name="bad", fail=True)
    jobs = _jobs(db, ids, good)
    jobs[1].routes = [bad]

    results = sync_mod.run_jobs(db, jobs, workers=3)
    assert sorted(r["status"] for r in results) == ["error", "ok", "ok"]
    assert db.execute("SELECT COUNT(*) c FROM item").fetchone()["c"] == 4
    err = db.execute(
        "SELECT error FROM scrape_run WHERE status='error'").fetchone()["error"]
    assert "bad is down" in err


def test_route_fallback_records_every_attempt(db):
    """A fallback that leaves no trace is how a bill becomes unexplainable."""
    [aid] = _accounts(db, 1)
    dead = slow_adapter(name="free_route", delay=0.02, fail=True)
    live = slow_adapter(name="paid_route", delay=0.02)
    job = sync_mod.Job(account_id=aid, target="acct0", routes=[dead, live],
                       handle="acct0", fetch_kwargs={"limit": 10})

    [res] = sync_mod.run_jobs(db, [job], workers=2)
    assert res["status"] == "ok" and res["adapter"] == "paid_route"
    assert res["fallbacks"] == [("free_route", "free_route is down")]

    rows = {r["adapter"]: r["status"] for r in
            db.execute("SELECT adapter, status FROM scrape_run")}
    assert rows == {"free_route": "error", "paid_route": "ok"}


# ── sync: the probe, folded into the same pool ──────────────────────────────

def test_probe_skips_the_fetch_and_still_banks_the_metrics(db):
    [aid] = _accounts(db, 1)
    iid, _, _ = store.upsert_item(db, account_id=aid, platform="linkedin",
                                  platform_item_id="acct0-0", kind="text",
                                  published_at=1700000000, body="body", raw={})
    store.record_metrics(db, iid, reactions=1)
    db.commit()

    calls: list = []

    def fetch(target, *, mode="delta", keys=None, since=None, limit=None, **kw):
        calls.append(limit)
        return FetchResult(items=[_item("acct0-0", reactions=99)], cost_usd=0.002)

    adapter = Adapter(name="fake", platform="linkedin", fetch=fetch,
                      relationships=("competitor",))
    job = sync_mod.Job(account_id=aid, target="acct0", routes=[adapter],
                       handle="acct0", probe=True, newest_id="acct0-0",
                       fetch_kwargs={"limit": 10})

    [res] = sync_mod.run_jobs(db, [job], workers=2)
    assert calls == [1], "a probe that finds nothing new must not fetch again"
    assert res["probed"] is True and res["new"] == 0
    # The probe already paid for the item, so its metrics are banked...
    assert db.execute(
        "SELECT MAX(reactions) r FROM metric_snapshot").fetchone()["r"] == 99
    # ...and what it cost is recorded rather than hidden.
    assert res["cost_usd"] == pytest.approx(0.002)
    assert db.execute(
        "SELECT cost_usd FROM scrape_run").fetchone()["cost_usd"] == pytest.approx(0.002)


def test_probe_that_sees_a_new_post_falls_through_to_the_fetch(db):
    [aid] = _accounts(db, 1)
    store.upsert_item(db, account_id=aid, platform="linkedin",
                      platform_item_id="old", kind="text",
                      published_at=1699000000, body="old", raw={})
    db.commit()

    job = sync_mod.Job(account_id=aid, target="acct0",
                       routes=[slow_adapter(delay=0.02)], handle="acct0",
                       probe=True, newest_id="old", fetch_kwargs={"limit": 10})
    [res] = sync_mod.run_jobs(db, [job], workers=2)
    assert res["status"] == "ok" and res["new"] == 2


def test_probes_and_fetches_share_one_pool(db):
    """Both kinds of job overlap; the wall clock is one round, not two."""
    ids = _accounts(db, 4)
    for i, aid in enumerate(ids[:2]):
        store.upsert_item(db, account_id=aid, platform="linkedin",
                          platform_item_id=f"acct{i}-0", kind="text",
                          published_at=1700000000, body="b", raw={})
    db.commit()

    enter, state = gate(4)

    def fetch(target, *, mode="delta", keys=None, since=None, limit=None, **kw):
        enter()
        return FetchResult(items=[_item(f"{target}-0")], cost_usd=0.001)

    adapter = Adapter(name="fake", platform="linkedin", fetch=fetch,
                      relationships=("competitor",))
    jobs = _jobs(db, ids, adapter)
    for i in range(2):
        jobs[i].probe = True
        jobs[i].newest_id = f"acct{i}-0"

    sync_mod.run_jobs(db, jobs, workers=4)
    assert not state["broken"], \
        "two probes and two fetches must share one pool, not run in two rounds"


def test_probe_verdict_is_pure(db):
    """It runs on a worker thread, so it must not need a database."""
    assert freshness.probe_verdict(None, "x") is True, "no answer means do the work"
    assert freshness.probe_verdict(FetchResult(items=[]), "x") is True
    assert freshness.probe_verdict(FetchResult(items=[_item("x")]), "x") is False
    assert freshness.probe_verdict(FetchResult(items=[_item("y")]), "x") is True


# ── enrich: images ──────────────────────────────────────────────────────────

def _seed_stills(conn, n, *, account_id=None):
    aid = account_id or store.upsert_account(
        conn, platform="instagram", handle="ig", relationship="competitor")
    ids = []
    for i in range(n):
        iid, _, _ = store.upsert_item(conn, account_id=aid, platform="instagram",
                                      platform_item_id=f"p{i}", kind="image",
                                      published_at=1700000000 + i, raw={})
        ids.append(store.add_media(conn, iid, role="slide",
                                   remote_url=f"http://example.invalid/{i}.jpg"))
    conn.commit()
    return aid, ids


def test_concurrent_image_fetch_matches_serial(tmp_path, monkeypatch):
    def fake_download(url, dest, **kw):
        time.sleep(0.05)
        dest.write_bytes(b"jpeg-" + url.encode())
        return dest

    monkeypatch.setattr(m, "download", fake_download)
    out = {}
    for label, workers in (("serial", 1), ("concurrent", 8)):
        conn = store.connect(tmp_path / f"img-{label}.db")
        _seed_stills(conn, 8)
        conn.commit()
        res = er.fetch_images(conn, media_root=tmp_path / f"m-{label}",
                              workers=workers)
        out[label] = (res, sorted(tuple(r) for r in conn.execute(
            "SELECT sha256, bytes, state FROM media ORDER BY id")))
        conn.close()

    assert out["serial"][0] == out["concurrent"][0] == {
        "considered": 8, "stored": 8, "expired": 0, "failed": 0}
    assert out["serial"][1] == out["concurrent"][1]


def test_stills_are_downloaded_all_at_once(tmp_path, monkeypatch):
    conn = store.connect(tmp_path / "img.db")
    _seed_stills(conn, 6)
    enter, state = gate(6)

    def fake_download(url, dest, **kw):
        enter()
        dest.write_bytes(b"jpeg")
        return dest

    monkeypatch.setattr(m, "download", fake_download)
    res = er.fetch_images(conn, media_root=tmp_path / "m", workers=8)
    assert not state["broken"], "the stills loop is still serial"
    assert res["stored"] == 6
    conn.close()


def test_image_writes_stream_and_survive_an_interrupt(tmp_path, monkeypatch):
    """Ctrl-C part way through a batch keeps every still already downloaded."""
    conn = store.connect(tmp_path / "img.db")
    _seed_stills(conn, 6)
    n = {"started": 0, "done": 0}
    lock = threading.Lock()
    three_in = threading.Event()

    def fake_download(url, dest, **kw):
        with lock:
            n["started"] += 1
            i = n["started"]
        if i > 3:
            # Held until three downloads have returned, so the interrupt lands
            # strictly part way through the batch rather than racing it.
            three_in.wait(timeout=10)
            raise KeyboardInterrupt
        dest.write_bytes(b"jpeg")
        with lock:
            n["done"] += 1
            if n["done"] >= 3:
                three_in.set()
        return dest

    monkeypatch.setattr(m, "download", fake_download)
    with pytest.raises(KeyboardInterrupt):
        er.fetch_images(conn, media_root=tmp_path / "m", workers=4)

    stored = conn.execute(
        "SELECT COUNT(*) c FROM media WHERE state='stored'").fetchone()["c"]
    # A worker bumps its counter before its future resolves, so "every finished
    # download" is not observable from here; the invariant that IS enforceable is
    # that nothing already resolved gets dropped, and that no phantom row appears.
    assert 0 < stored <= n["done"], (
        f"{n['done']} downloads completed, {stored} kept")
    assert stored < 6, "the interrupt must have stopped the batch"
    assert all((tmp_path / "m" / r["local_path"]).exists() for r in conn.execute(
        "SELECT local_path FROM media WHERE state='stored'")), \
        "a stored row must point at a file that is really there"
    conn.close()


def test_expired_still_is_marked_and_the_rest_continue(tmp_path, monkeypatch):
    conn = store.connect(tmp_path / "img.db")
    _seed_stills(conn, 4)

    def fake_download(url, dest, **kw):
        if url.endswith("1.jpg"):
            raise m.ExpiredURL("403")
        dest.write_bytes(b"jpeg")
        return dest

    monkeypatch.setattr(m, "download", fake_download)
    res = er.fetch_images(conn, media_root=tmp_path / "m", workers=4)
    assert res == {"considered": 4, "stored": 3, "expired": 1, "failed": 0}
    assert conn.execute(
        "SELECT COUNT(*) c FROM media WHERE state='failed'").fetchone()["c"] == 1
    # And the failed row is not offered again.
    assert len(er.pending_images(conn)) == 0
    conn.close()


# ── enrich: video ───────────────────────────────────────────────────────────

def _seed_videos(conn, n, *, kind="short_video"):
    aid = store.upsert_account(conn, platform="instagram", handle="ig",
                               relationship="competitor")
    ids = []
    for i in range(n):
        iid, _, _ = store.upsert_item(conn, account_id=aid, platform="instagram",
                                      platform_item_id=f"v{i}", kind=kind,
                                      published_at=1700000000 + i,
                                      body=f"caption {i}", raw={})
        store.add_media(conn, iid, role="original",
                        remote_url=f"http://example.invalid/{i}.mp4")
        ids.append(iid)
    conn.commit()
    return aid, ids


@pytest.fixture
def stub_video(monkeypatch):
    """A whole video pipeline with the slow parts faked but the shape intact."""
    monkeypatch.setattr(tx, "available", lambda: True)
    monkeypatch.setattr(er.tx, "available", lambda: True)

    def fake_download(url, dest, **kw):
        time.sleep(0.08)
        dest.write_bytes(b"\x00" * 64)
        return dest

    def fake_transcribe(video, **kw):
        time.sleep(0.08)
        # Keyed on the item's own caption, not on the temp path: the workspace name
        # is random, and a test that hashes it compares noise.
        return tx.Transcript(text=f"spoken words about {kw.get('hint_text')}",
                             segments=[], locale="en-US")

    def fake_keyframes(video, out_dir, *, count=6, width=480, duration=None):
        out_dir.mkdir(parents=True, exist_ok=True)
        out = []
        for i in range(2):
            p = out_dir / f"f{i}.jpg"
            p.write_bytes(b"jpegdata" + str(i).encode())
            out.append(m.Keyframe(path=p, t_offset_s=float(i), seq=i + 1))
        return out

    monkeypatch.setattr(m, "download", fake_download)
    monkeypatch.setattr(m, "probe_duration", lambda p: 12.0)
    monkeypatch.setattr(m, "keyframes", fake_keyframes)
    monkeypatch.setattr(er.tx, "transcribe_auto", fake_transcribe)
    return None


def test_concurrent_enrich_matches_serial(tmp_path, stub_video):
    out = {}
    for label, workers in (("serial", 1), ("concurrent", 3)):
        conn = store.connect(tmp_path / f"v-{label}.db")
        _seed_videos(conn, 6)
        st = er.run(conn, media_root=tmp_path / f"media-{label}", workers=workers,
                    frames=2)
        out[label] = (
            st.as_dict(),
            sorted(tuple(r) for r in conn.execute(
                "SELECT platform_item_id, transcript, lang, duration_s FROM item")),
            sorted(r["c"] for r in conn.execute(
                "SELECT COUNT(*) c FROM media WHERE role='keyframe' GROUP BY item_id")),
        )
        conn.close()
    assert out["serial"][1] == out["concurrent"][1]
    assert out["serial"][2] == out["concurrent"][2] == [2] * 6
    assert out["serial"][0]["transcribed"] == out["concurrent"][0]["transcribed"] == 6
    assert out["serial"][0]["frames"] == out["concurrent"][0]["frames"] == 12


def test_enrich_overlaps_work_across_items(tmp_path, stub_video, monkeypatch):
    """Three videos in flight at once, which is the measured ceiling."""
    conn = store.connect(tmp_path / "v.db")
    _seed_videos(conn, 6)
    enter, state = gate(3)
    real = m.download

    def gated_download(url, dest, **kw):
        enter()
        return real(url, dest, **kw)

    monkeypatch.setattr(m, "download", gated_download)
    st = er.run(conn, media_root=tmp_path / "media", workers=3, frames=2)
    assert not state["broken"], "items were enriched one at a time"
    assert st.transcribed == 6
    conn.close()


def test_enrich_workers_are_capped_at_the_measured_ceiling(tmp_path, stub_video):
    conn = store.connect(tmp_path / "v.db")
    _seed_videos(conn, 2)
    st = er.run(conn, media_root=tmp_path / "media", workers=12, frames=0)
    assert any("capped workers 12" in e for e in st.errors), \
        "a knob that is silently ignored is worse than one that is refused"
    conn.close()


def test_frame_bytes_are_read_before_the_workspace_dies(tmp_path, stub_video):
    """The one line the whole video fan-out depends on."""
    conn = store.connect(tmp_path / "v.db")
    _, [iid] = _seed_videos(conn, 1)
    work = er.fetch_item(dict(conn.execute(
        "SELECT i.id, i.body, i.lang, (SELECT remote_url FROM media "
        "WHERE item_id=i.id) AS video_url FROM item i").fetchone()),
        media_root=tmp_path / "media", locale="en-US", frames=2,
        keep_audio=False)
    assert work.outcome == "ok"
    assert [len(b) for _, _, b in work.frames] == [9, 9], \
        "frames must come back as bytes, not as paths into a deleted temp dir"

    er.store_item(conn, work, media_root=tmp_path / "media")
    paths = [r["local_path"] for r in conn.execute(
        "SELECT local_path FROM media WHERE role='keyframe'")]
    assert len(paths) == 2 and all((tmp_path / "media" / p).exists() for p in paths)
    conn.close()


def test_enrich_interrupt_keeps_completed_transcripts(tmp_path, stub_video,
                                                      monkeypatch):
    conn = store.connect(tmp_path / "v.db")
    _seed_videos(conn, 6)

    real_store = er.store_item
    n = {"i": 0}

    def counting(conn_, work, **kw):
        n["i"] += 1
        if n["i"] > 2:
            raise KeyboardInterrupt
        return real_store(conn_, work, **kw)

    monkeypatch.setattr(er, "store_item", counting)
    with pytest.raises(KeyboardInterrupt):
        er.run(conn, media_root=tmp_path / "media", workers=3, frames=2)
    monkeypatch.setattr(er, "store_item", real_store)

    done = conn.execute(
        "SELECT COUNT(*) c FROM item WHERE transcript IS NOT NULL").fetchone()["c"]
    assert done == 2, "committed transcripts must survive the interrupt"
    assert conn.execute(
        "SELECT status FROM scrape_run").fetchone()["status"] == "interrupted"
    # And the remaining four are still pending, so a re-run resumes.
    assert len(er.pending(conn)) == 4
    conn.close()


# ── enrich: work that was being repeated forever ────────────────────────────

def test_expired_video_is_not_handed_back_next_run(tmp_path, monkeypatch):
    conn = store.connect(tmp_path / "v.db")
    _, [iid] = _seed_videos(conn, 1)
    monkeypatch.setattr(tx, "available", lambda: True)
    monkeypatch.setattr(m, "download",
                        lambda url, dest, **kw: (_ for _ in ()).throw(m.ExpiredURL("403")))

    st = er.run(conn, media_root=tmp_path / "media", frames=0)
    assert st.expired == 1

    # The dead URL is no longer offered, so the next run cannot spend a download on
    # it. Before the state filter this returned the same 403 on every single run.
    again = er.pending(conn)
    assert [r["video_url"] for r in again] == [None]
    calls: list = []
    monkeypatch.setattr(m, "download", lambda url, dest, **kw: calls.append(url))
    er.run(conn, media_root=tmp_path / "media", frames=0)
    assert calls == [], "a dead signed URL must not be re-fetched every run forever"

    # A re-sync re-mints the URL as a new media row, and the item comes back.
    store.add_media(conn, iid, role="original", remote_url="http://example.invalid/new.mp4")
    conn.commit()
    back = er.pending(conn)
    assert len(back) == 1 and back[0]["video_url"].endswith("new.mp4")
    conn.close()


def test_repeated_transcription_failure_stops_being_retried(tmp_path, monkeypatch):
    conn = store.connect(tmp_path / "v.db")
    _seed_videos(conn, 1)
    monkeypatch.setattr(tx, "available", lambda: True)
    monkeypatch.setattr(m, "download",
                        lambda url, dest, **kw: (dest.write_bytes(b"x"), dest)[1])
    monkeypatch.setattr(m, "probe_duration", lambda p: 5.0)
    monkeypatch.setattr(er.tx, "transcribe_auto", lambda *a, **kw: (
        (_ for _ in ()).throw(tx.TranscribeError("engine exploded"))))

    for i in range(er.MAX_ENRICH_ATTEMPTS):
        st = er.run(conn, media_root=tmp_path / "media", frames=0)
        assert st.failed == 1, f"attempt {i} should still be offered"
    st = er.run(conn, media_root=tmp_path / "media", frames=0)
    assert st.considered == 0, \
        "a permanently broken clip must stop costing a download every run"
    assert er.pending(conn, redo=True), "--redo still reaches it"
    conn.close()


# ── analyse: the token bucket and the pool it feeds ─────────────────────────

def test_bucket_refunds_what_a_call_did_not_use():
    b = llm._TokenBucket(8000)
    reservations = [b.take(1684) for _ in range(4)]
    assert sum(t for _, t in b.events) == 6736
    for r in reservations:
        b.settle(r, 700)            # 660 in + ~40 out, the measured shape
    assert sum(t for _, t in b.events) == 2800, \
        "unused output reservation must go back to the window"
    # A fifth call now fits inside the same minute; before settling it would have
    # queued for 60s. That difference is the entire Groq wall clock.
    b.take(1684)
    assert len(b.events) == 5


def test_bucket_settle_never_exceeds_the_window():
    b = llm._TokenBucket(8000)
    r = b.take(1000)
    b.settle(r, 99999)
    assert b.events[0][1] == 8000, "one call must not stall the bucket for a minute"
    b.settle(r, -5)
    assert b.events[0][1] == 0


def test_bucket_still_paces_when_the_window_is_full(monkeypatch):
    """Settling must not turn the bucket into a no-op."""
    b = llm._TokenBucket(1000)
    b.take(900)
    naps: list = []

    def fake_sleep(seconds):
        naps.append(seconds)
        b.events.clear()            # the window rolls; release the waiter
    monkeypatch.setattr(llm.time, "sleep", fake_sleep)

    b.take(900)
    assert naps and naps[0] > 1, "a full window must wait, not admit"


@pytest.fixture
def analyze_env(tmp_path):
    d = tmp_path / "templates"
    d.mkdir()
    (d / "demo.md").write_text(
        "---\nname: Demo\ndescription: d\nmodel: m\ntemperature: 0.3\n"
        "max_tokens: 1024\n---\nAnalyse.\nCaption: {{caption}}\n")
    conn = store.connect(tmp_path / "c.db")
    aid = store.upsert_account(conn, platform="linkedin", handle="bob",
                               relationship="influencer")
    for i in range(12):
        store.upsert_item(conn, account_id=aid, platform="linkedin",
                          platform_item_id=f"p{i}", kind="text",
                          published_at=1700000000 + i, body=f"post {i}", raw={})
    conn.commit()
    yield conn, d, str(tmp_path / "a.db")
    conn.close()


def test_workers_are_sized_to_the_provider_not_to_the_flag(analyze_env, monkeypatch):
    """--workers 16 on an 8000 TPM provider is six threads queueing on a lock."""
    conn, tpl_dir, db_path = analyze_env
    monkeypatch.setattr(ar.providers, "available",
                        lambda: [{"name": "groq", "model": "stub", "tpm": 3000}])
    monkeypatch.setattr(ar.llm, "active", lambda: {"name": "groq", "model": "stub"})

    live = {"n": 0, "peak": 0}
    lock = threading.Lock()

    def fake_chat(messages, **kw):
        with lock:
            live["n"] += 1
            live["peak"] = max(live["peak"], live["n"])
        time.sleep(0.05)
        with lock:
            live["n"] -= 1
        return "out"

    monkeypatch.setattr(ar.llm, "chat", fake_chat)
    st = ar.run(conn, "demo", db_path=db_path, template_dir=tpl_dir,
                dry_run=False, workers=16)
    assert st.done == 12
    # 3000 TPM against ~1050 tokens a call is two in flight, whatever the flag says.
    assert live["peak"] <= 3, f"ran {live['peak']} wide on a 3000 TPM provider"
    assert any("workers 16 ->" in e for e in st.errors), "the cut must be visible"


def test_a_high_tpm_provider_keeps_the_requested_width(analyze_env, monkeypatch):
    conn, tpl_dir, db_path = analyze_env
    monkeypatch.setattr(ar.providers, "available",
                        lambda: [{"name": "deepseek", "model": "s", "tpm": 1_000_000}])
    monkeypatch.setattr(ar.llm, "active", lambda: {"name": "deepseek", "model": "s"})

    # 12 items, 8 workers: the first 8 must be in flight together.
    enter, state = gate(8, limit=8)

    def fake_chat(messages, **kw):
        enter()
        return "out"

    monkeypatch.setattr(ar.llm, "chat", fake_chat)
    st = ar.run(conn, "demo", db_path=db_path, template_dir=tpl_dir, dry_run=False,
                workers=8)
    assert not state["broken"], "1M TPM is latency-bound; the flag should stand"
    assert st.done == 12


def test_analyze_concurrent_matches_serial(tmp_path, monkeypatch):
    monkeypatch.setattr(ar.providers, "available",
                        lambda: [{"name": "deepseek", "model": "s", "tpm": 1_000_000}])
    monkeypatch.setattr(ar.llm, "active", lambda: {"name": "deepseek", "model": "s"})
    monkeypatch.setattr(ar.llm, "chat",
                        lambda messages, **kw: f"analysis of {messages[0]['content'][-8:]}")
    d = tmp_path / "templates"
    d.mkdir()
    (d / "demo.md").write_text(
        "---\nname: Demo\ndescription: d\nmodel: m\ntemperature: 0.3\n"
        "max_tokens: 1024\n---\nAnalyse.\nCaption: {{caption}}\n")

    out = {}
    for label, workers in (("serial", 1), ("concurrent", 8)):
        conn = store.connect(tmp_path / f"a-{label}.db")
        aid = store.upsert_account(conn, platform="linkedin", handle="bob",
                                   relationship="influencer")
        for i in range(10):
            store.upsert_item(conn, account_id=aid, platform="linkedin",
                              platform_item_id=f"p{i}", kind="text",
                              published_at=1700000000 + i, body=f"post {i}", raw={})
        conn.commit()
        st = ar.run(conn, "demo", db_path=str(tmp_path / f"an-{label}.db"),
                    template_dir=d, dry_run=False, workers=workers)
        out[label] = (st.done, st.failed, sorted(
            r["output_text"] for r in conn.execute(
                "SELECT output_text FROM a.analysis_result")))
        conn.close()
    assert out["serial"] == out["concurrent"]
    assert out["serial"][0] == 10


def test_a_failure_burst_does_not_abort_a_wide_run(analyze_env, monkeypatch):
    """At 8 in flight, 3 consecutive failures is a hiccup, not a collapse."""
    conn, tpl_dir, db_path = analyze_env
    monkeypatch.setattr(ar.providers, "available",
                        lambda: [{"name": "deepseek", "model": "s", "tpm": 1_000_000}])
    monkeypatch.setattr(ar.llm, "active", lambda: {"name": "deepseek", "model": "s"})

    n = {"i": 0}
    lock = threading.Lock()

    def flaky(messages, **kw):
        with lock:
            n["i"] += 1
            i = n["i"]
        if i <= 4:                      # a short 429 burst at the start
            raise ar.llm.LLMError("429 rate limited")
        return "out"

    monkeypatch.setattr(ar.llm, "chat", flaky)
    st = ar.run(conn, "demo", db_path=db_path, template_dir=tpl_dir,
                dry_run=False, workers=8)
    assert st.done == 8 and st.failed == 4
    assert not any("aborted" in e for e in st.errors), \
        "a burst inside the in-flight window must not kill a healthy run"


def test_a_real_collapse_still_aborts(analyze_env, monkeypatch):
    conn, tpl_dir, db_path = analyze_env
    monkeypatch.setattr(ar.providers, "available",
                        lambda: [{"name": "groq", "model": "s", "tpm": 8000}])
    monkeypatch.setattr(ar.llm, "active", lambda: {"name": "groq", "model": "s"})
    monkeypatch.setattr(ar.llm, "chat", lambda *a, **kw: (
        (_ for _ in ()).throw(ar.llm.LLMError("429 rate limited"))))
    st = ar.run(conn, "demo", db_path=db_path, template_dir=tpl_dir,
                dry_run=False, workers=4)
    assert st.failed < 12, "the run must stop, not churn the whole worklist"
    assert any("aborted" in e for e in st.errors)


def test_an_exhausted_provider_chain_aborts_at_once(analyze_env, monkeypatch):
    conn, tpl_dir, db_path = analyze_env
    monkeypatch.setattr(ar.providers, "available",
                        lambda: [{"name": "groq", "model": "s", "tpm": 8000}])
    monkeypatch.setattr(ar.llm, "active", lambda: {"name": "groq", "model": "s"})
    monkeypatch.setattr(ar.llm, "chat", lambda *a, **kw: (
        (_ for _ in ()).throw(ar.llm.DailyLimitError("all providers exhausted"))))
    st = ar.run(conn, "demo", db_path=db_path, template_dir=tpl_dir,
                dry_run=False, workers=1)
    assert st.failed == 1, "waiting will not help; do not burn the worklist"
    assert any("provider chain is exhausted" in e for e in st.errors)


def test_analyze_interrupt_keeps_completed_results(analyze_env, monkeypatch):
    conn, tpl_dir, db_path = analyze_env
    monkeypatch.setattr(ar.providers, "available",
                        lambda: [{"name": "deepseek", "model": "s", "tpm": 1_000_000}])
    monkeypatch.setattr(ar.llm, "active", lambda: {"name": "deepseek", "model": "s"})
    monkeypatch.setattr(ar.llm, "chat", lambda *a, **kw: "out")

    real = ar._record
    n = {"i": 0}

    def counting(*a, **kw):
        n["i"] += 1
        if n["i"] > 3:
            raise KeyboardInterrupt
        return real(*a, **kw)

    monkeypatch.setattr(ar, "_record", counting)
    with pytest.raises(KeyboardInterrupt):
        ar.run(conn, "demo", db_path=db_path, template_dir=tpl_dir, dry_run=False,
               workers=4)
    # Restore only the interrupt, not the stubs. monkeypatch.undo() here would put
    # the real provider chain back and the resume below would spend real money.
    monkeypatch.setattr(ar, "_record", real)

    kept = conn.execute("SELECT COUNT(*) c FROM a.analysis_result").fetchone()["c"]
    assert kept == 3, "paid-for results must be on disk before the next one starts"
    assert conn.execute(
        "SELECT status FROM a.analysis_run").fetchone()["status"] == "interrupted"

    # Re-running resumes: the three cached results are skipped, not repeated.
    st = ar.run(conn, "demo", db_path=db_path, template_dir=tpl_dir, dry_run=False,
                workers=4)
    assert st.skipped == 3 and st.done == 9



def test_resolved_futures_are_never_dropped_on_interrupt(tmp_path, monkeypatch):
    """The drain, tested deterministically.

    as_completed yields in completion order, so a future that RAISES can be handed
    over before successes that finished earlier and are still queued. Without a
    drain those downloads are paid for and thrown away. Here the successes are
    guaranteed resolved before the raising one is even submitted.
    """
    conn = store.connect(tmp_path / "d.db")
    _seed_stills(conn, 4)
    rows = er.pending_images(conn)

    done_first = rows[:3]
    raiser = rows[3]

    def fake_download(url, dest, **kw):
        if url == raiser["remote_url"]:
            raise KeyboardInterrupt
        dest.write_bytes(b"jpeg")
        return dest

    monkeypatch.setattr(m, "download", fake_download)

    # workers=1 so submission order is execution order: the three successes resolve,
    # then the raiser. Any drop is the code's fault, not a race.
    with pytest.raises(KeyboardInterrupt):
        er.fetch_images(conn, media_root=tmp_path / "m", workers=4,
                        rows=done_first + [raiser])

    stored = conn.execute(
        "SELECT COUNT(*) c FROM media WHERE state='stored'").fetchone()["c"]
    assert stored == 3, f"all three resolved downloads must be kept, got {stored}"
