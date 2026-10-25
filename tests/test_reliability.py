"""Unattended-run reliability: portable media paths, exit codes, the run lock,
SIGTERM, transcriber outages, vendor payload drift, backups, health."""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import store  # noqa: E402
from corpus.enrich import media as m  # noqa: E402
from corpus.enrich import run as er  # noqa: E402


def _item(conn, handle="acct", pid="p1", kind="image"):
    aid = store.upsert_account(conn, platform="instagram", handle=handle,
                               relationship="competitor")
    iid, _, _ = store.upsert_item(conn, account_id=aid, platform="instagram",
                                  platform_item_id=pid, kind=kind,
                                  published_at=1700000000, raw={})
    return aid, iid


def _cas(root: Path, blob: bytes, ext="jpg") -> Path:
    d = hashlib.sha256(blob).hexdigest()
    p = root / d[:2] / d[2:4] / f"{d}.{ext}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(blob)
    return p


# ── media paths ─────────────────────────────────────────────────────────────

def test_stored_paths_are_relative_and_resolve_against_the_current_root(tmp_path):
    root = tmp_path / "media"
    f = _cas(root, b"x")
    rel = m.to_stored(f, root)
    assert not Path(rel).is_absolute() and rel.count("/") == 2
    assert m.resolve(rel, root) == f
    # The same row read on another machine, under another root.
    other = tmp_path / "elsewhere"
    assert m.resolve(rel, other) == other / rel


@pytest.mark.parametrize("legacy", [
    "/Users/someone/.corpus/media/{t}",          # the Mac's default root
    "/tmp/user-corpus/media/{t}",             # a temp root from a test run
])
def test_a_legacy_absolute_path_is_rerooted_by_its_tail(tmp_path, legacy):
    root = tmp_path / "media"
    f = _cas(root, b"frame")
    tail = f.relative_to(root).as_posix()
    assert m.resolve(legacy.format(t=tail), root) == f
    assert m.resolve_existing(legacy.format(t=tail), root) == f


def test_fetch_images_writes_a_relative_path(tmp_path, monkeypatch):
    conn = store.connect(tmp_path / "t.db")
    _, iid = _item(conn)
    store.add_media(conn, iid, role="original", remote_url="http://x.invalid/a.jpg")
    monkeypatch.setattr(m, "download", lambda url, dest, **kw: (
        dest.write_bytes(b"jpegbytes"), dest)[1])
    er.fetch_images(conn, media_root=tmp_path / "media", workers=1)
    lp = conn.execute("SELECT local_path FROM media").fetchone()["local_path"]
    assert not Path(lp).is_absolute(), lp
    assert (tmp_path / "media" / lp).read_bytes() == b"jpegbytes"


def test_fix_paths_rewrites_legacy_rows_and_clears_dangling_ones(tmp_path):
    root = tmp_path / "media"
    conn = store.connect(tmp_path / "t.db")
    _, iid = _item(conn)
    here = _cas(root, b"present")
    gone_tail = "ab/cd/abcdef.jpg"
    rows = [f"/Users/someone/.corpus/media/{here.relative_to(root).as_posix()}",
            f"/tmp/user-corpus/media/{gone_tail}",
            here.relative_to(root).as_posix()]
    for i, lp in enumerate(rows):
        conn.execute("INSERT INTO media (item_id, role, seq, local_path, remote_url, "
                     "state, created_at) VALUES (?, 'slide', ?, ?, 'http://r', "
                     "'stored', 1)", (iid, i, lp))
    conn.commit()

    dry = store.fix_paths(conn, media_root=root)
    assert (dry["rewritten"], dry["dangling"], dry["ok"]) == (1, 1, 1)
    assert conn.execute("SELECT COUNT(*) FROM media WHERE local_path LIKE '/%'"
                        ).fetchone()[0] == 2, "a dry run changes nothing"

    store.fix_paths(conn, media_root=root, execute=True)
    got = conn.execute("SELECT local_path, state FROM media ORDER BY seq").fetchall()
    assert got[0]["local_path"] == here.relative_to(root).as_posix()
    assert got[1]["local_path"] is None and got[1]["state"] == "referenced", \
        "a dangling row must go back into the download queue"
    assert er.pending_images(conn)[0]["id"], "and enrich now offers it again"
    assert store.fix_paths(conn, media_root=root)["rewritten"] == 0, "idempotent"


def test_fix_paths_cli_is_a_dry_run_by_default(tmp_path, capsys):
    from corpus import cli
    db = tmp_path / "t.db"
    conn = store.connect(db)
    _, iid = _item(conn)
    conn.execute("INSERT INTO media (item_id, role, local_path, state, created_at) "
                 "VALUES (?, 'slide', '/tmp/user-corpus/media/ab/cd/abcd.jpg', "
                 "'stored', 1)", (iid,))
    conn.commit()
    assert cli.main(["--db", str(db), "fix-paths", "--media", str(tmp_path / "m")]) == 0
    assert "dry run" in capsys.readouterr().out
    assert conn.execute("SELECT local_path FROM media").fetchone()[0]
    assert cli.main(["--db", str(db), "fix-paths", "--media", str(tmp_path / "m"),
                     "--execute"]) == 0
    assert conn.execute("SELECT local_path FROM media").fetchone()[0] is None


def test_forget_unlinks_the_resolved_file_of_a_relative_row(tmp_path):
    root = tmp_path / "media"
    conn = store.connect(tmp_path / "t.db")
    _, iid = _item(conn, handle="gone")
    f = _cas(root, b"personal data")
    conn.execute("INSERT INTO media (item_id, role, local_path, sha256, state, "
                 "created_at) VALUES (?, 'slide', ?, 'x', 'stored', 1)",
                 (iid, f.relative_to(root).as_posix()))
    conn.commit()
    res = store.forget(conn, handle="gone", dry_run=False, media_root=root)
    assert str(f) in res["media_files"] and not f.exists()


def test_forget_keeps_a_file_a_surviving_row_reaches_by_a_legacy_path(tmp_path):
    root = tmp_path / "media"
    conn = store.connect(tmp_path / "t.db")
    _, gone = _item(conn, handle="gone", pid="g")
    _, kept = _item(conn, handle="kept", pid="k")
    f = _cas(root, b"same black frame")
    rel = f.relative_to(root).as_posix()
    for iid, lp in ((gone, rel), (kept, f"/Users/someone/.corpus/media/{rel}")):
        conn.execute("INSERT INTO media (item_id, role, local_path, state, created_at) "
                     "VALUES (?, 'keyframe', ?, 'stored', 1)", (iid, lp))
    conn.commit()
    store.forget(conn, handle="gone", dry_run=False, media_root=root)
    assert f.exists(), "still shown by an account we keep"


def test_a_redo_removes_the_keyframes_it_replaces(tmp_path):
    root = tmp_path / "media"
    conn = store.connect(tmp_path / "t.db")
    _, iid = _item(conn, kind="short_video")
    er._store_frames(conn, iid, [(1, 0.0, b"old frame")], root)
    old = m.resolve(conn.execute("SELECT local_path FROM media").fetchone()[0], root)
    assert old.exists()
    er._store_frames(conn, iid, [(1, 0.0, b"new frame")], root)
    assert not old.exists(), "an unreferenced old keyframe must not stay on disk"


def test_a_replaced_keyframe_another_item_shares_survives(tmp_path):
    root = tmp_path / "media"
    conn = store.connect(tmp_path / "t.db")
    _, a = _item(conn, pid="a", kind="short_video")
    _, b = _item(conn, pid="b", kind="short_video")
    er._store_frames(conn, a, [(1, 0.0, b"black")], root)
    er._store_frames(conn, b, [(1, 0.0, b"black")], root)
    shared = m.resolve(conn.execute("SELECT local_path FROM media LIMIT 1").fetchone()[0],
                       root)
    er._store_frames(conn, a, [(1, 0.0, b"other")], root)
    assert shared.exists()


# ── transcriber outages ─────────────────────────────────────────────────────

from corpus.enrich import transcribe as tx  # noqa: E402


@pytest.fixture
def no_ambient(monkeypatch):
    monkeypatch.setattr(tx, "_whisper_servers", lambda: [])
    monkeypatch.setattr(tx, "_env_key", lambda name: None)
    monkeypatch.setattr(tx, "_UNREACHABLE", set())


def _video(conn, n=1):
    aid = store.upsert_account(conn, platform="instagram", handle="v",
                               relationship="competitor")
    ids = []
    for i in range(n):
        iid, _, _ = store.upsert_item(conn, account_id=aid, platform="instagram",
                                      platform_item_id=f"v{i}", kind="short_video",
                                      published_at=1700000000 + i, body="caption here",
                                      raw={})
        store.add_media(conn, iid, role="original", remote_url=f"http://x.invalid/{i}")
        ids.append(iid)
    conn.commit()
    return ids


def _fake_download(url, dest, **kw):
    dest.write_bytes(b"not really a video")
    return dest


@pytest.mark.parametrize("exc", [
    __import__("urllib.error").error.URLError(ConnectionRefusedError(111, "refused")),
    TimeoutError("timed out"),
    __import__("urllib.error").error.HTTPError("u", 503, "busy", {}, None),
    __import__("urllib.error").error.HTTPError("u", 429, "slow down", {}, None),
])
def test_an_engine_outage_is_unavailable_not_a_content_failure(exc, monkeypatch, tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")

    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(tx.urllib.request if hasattr(tx, "urllib") else
                        __import__("urllib.request").request, "urlopen", boom)
    with pytest.raises(tx.TranscribeUnavailable):
        tx._post_audio("http://gpu/inference", b"a", "a.opus", fields={}, headers={},
                       language=None, timeout=1)


def test_a_400_is_a_content_failure(monkeypatch):
    import urllib.error
    import urllib.request

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 400, "bad audio", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(tx.TranscribeError) as e:
        tx._post_audio("http://gpu/inference", b"a", "a.opus", fields={}, headers={},
                       language=None, timeout=1)
    assert not isinstance(e.value, tx.TranscribeUnavailable)


def test_a_transcriber_outage_does_not_spend_an_attempt(tmp_path, monkeypatch, no_ambient):
    conn = store.connect(tmp_path / "t.db")
    [iid] = _video(conn)
    monkeypatch.setattr(m, "download", _fake_download)
    monkeypatch.setattr(tx, "available", lambda: False)
    monkeypatch.setattr(tx, "whisper_available", lambda: True)

    def down(*a, **k):
        raise tx.TranscribeUnavailable("connection refused")
    monkeypatch.setattr(er.tx, "transcribe_auto", down)
    st = er.run(conn, media_root=tmp_path / "media", frames=0, workers=1)
    assert st.unavailable == 1 and st.failed == 0 and not st.ok
    att = conn.execute("SELECT enrich_attempts FROM item WHERE id=?", (iid,)).fetchone()[0]
    assert att == 0, "the server being down is not the clip's fault"
    assert conn.execute("SELECT status FROM scrape_run WHERE adapter='enrich'"
                        ).fetchone()[0] == "error"


def test_a_content_failure_still_counts(tmp_path, monkeypatch, no_ambient):
    conn = store.connect(tmp_path / "t.db")
    [iid] = _video(conn)
    monkeypatch.setattr(m, "download", _fake_download)
    monkeypatch.setattr(tx, "whisper_available", lambda: True)

    def bad(*a, **k):
        raise tx.TranscribeError("whisper returned garbage")
    monkeypatch.setattr(er.tx, "transcribe_auto", bad)
    st = er.run(conn, media_root=tmp_path / "media", frames=0, workers=1)
    assert st.failed == 1
    assert conn.execute("SELECT enrich_attempts FROM item WHERE id=?",
                        (iid,)).fetchone()[0] == 1


def test_repeated_outages_stop_the_run_instead_of_downloading_everything(
        tmp_path, monkeypatch, no_ambient):
    conn = store.connect(tmp_path / "t.db")
    _video(conn, n=8)
    downloads = []
    monkeypatch.setattr(m, "download", lambda u, d, **k: (downloads.append(u),
                                                          _fake_download(u, d))[1])
    monkeypatch.setattr(tx, "whisper_available", lambda: True)
    monkeypatch.setattr(er.tx, "transcribe_auto", lambda *a, **k: (_ for _ in ()).throw(
        tx.TranscribeUnavailable("down")))
    st = er.run(conn, media_root=tmp_path / "media", frames=0, workers=1)
    assert len(downloads) == er.OUTAGE_STOP_AFTER
    assert st.unavailable == 8 and any("stopped" in e for e in st.errors)


def test_a_youtube_bot_check_spends_no_attempt_and_stops_nothing(
        tmp_path, monkeypatch, no_ambient):
    """2026-10-23: after ~50 Shorts YouTube bot-checked this box and 67 Shorts each
    lost an attempt (three such nights retire them for good). It is the machine
    being refused, not the clip; and Instagram reels in the same run carry on."""
    conn = store.connect(tmp_path / "t.db")
    ids = _video(conn, n=5)
    monkeypatch.setattr(tx, "whisper_available", lambda: True)
    monkeypatch.setattr(m, "download", lambda u, d, **k: (_ for _ in ()).throw(
        m.SourceBlocked("yt-dlp: Sign in to confirm you're not a bot")))
    st = er.run(conn, media_root=tmp_path / "media", frames=0, workers=1)
    assert st.unavailable == 5 and st.failed == 0 and not st.ok
    assert not any("stopped" in e for e in st.errors), "not a transcriber outage"
    assert sum("source blocked" in e for e in st.errors) == 1
    att = [conn.execute("SELECT enrich_attempts FROM item WHERE id=?", (i,)).fetchone()[0]
           for i in ids]
    assert att == [0] * 5


def test_youtube_download_falls_back_to_the_android_client(monkeypatch, tmp_path):
    from types import SimpleNamespace
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if "youtube:player_client=android" in cmd:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"video")
            return SimpleNamespace(returncode=0, stderr="")
        return SimpleNamespace(returncode=1, stderr="ERROR: Sign in to confirm you're "
                                                    "not a bot. Use --cookies")
    monkeypatch.setattr(m.subprocess, "run", run)
    out = m._youtube("https://www.youtube.com/shorts/x", tmp_path / "v.mp4", 10)
    assert out.read_bytes() == b"video" and len(calls) == 2

    def blocked(cmd, **kw):
        return SimpleNamespace(returncode=1, stderr="ERROR: Sign in to confirm you're "
                                                    "not a bot.")
    monkeypatch.setattr(m.subprocess, "run", blocked)
    with pytest.raises(m.SourceBlocked):
        m._youtube("https://www.youtube.com/shorts/x", tmp_path / "w.mp4", 10)


def test_selfhosted_and_groq_both_down_is_an_outage(monkeypatch, tmp_path, no_ambient):
    f = tmp_path / "x.mp4"
    f.write_bytes(b"v")
    monkeypatch.setattr(tx, "_whisper_servers", lambda: ["http://gpu:8093"])
    monkeypatch.setattr(tx, "_env_key", lambda n: "k" if n == "GROQ_API_KEY" else None)
    monkeypatch.setattr(tx, "_post_whisper", lambda *a, **k: (_ for _ in ()).throw(
        tx.TranscribeUnavailable("refused")))
    with pytest.raises(tx.TranscribeUnavailable):
        tx.transcribe_whisper(f, language="hr")


def test_an_unreachable_server_is_pinged_once_and_skipped(monkeypatch, tmp_path, no_ambient):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                       # nothing listens here: connection refused
    monkeypatch.setattr(tx, "_whisper_servers", lambda: [f"http://127.0.0.1:{port}"])
    res = tx.ping_whisper(timeout=2)
    assert res[f"http://127.0.0.1:{port}"] is not None
    assert tx._live_servers() == [], "excluded from routing for this process"
    assert not tx.whisper_available()


def test_no_reachable_transcriber_fails_the_run_clearly(tmp_path, monkeypatch, no_ambient):
    conn = store.connect(tmp_path / "t.db")
    _video(conn)
    monkeypatch.setattr(tx, "available", lambda: False)
    monkeypatch.setattr(er, "fetch_images", lambda *a, **k: {"stored": 0, "expired": 0})
    st = er.run(conn, media_root=tmp_path / "media")
    assert not st.ok and any("no transcriber" in e for e in st.errors)


def test_the_selfhosted_timeout_is_minutes_not_half_an_hour(monkeypatch):
    monkeypatch.delenv("WHISPER_TIMEOUT_S", raising=False)
    assert tx._selfhosted_timeout() == 300
    monkeypatch.setenv("WHISPER_TIMEOUT_S", "120")
    assert tx._selfhosted_timeout() == 120


def test_post_whisper_leaves_no_temp_dir(monkeypatch, tmp_path):
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    f = tmp_path / "x.mp4"
    f.write_bytes(b"v")
    monkeypatch.setattr(m, "archive_audio", lambda src, dest: (dest.write_bytes(b"o"), dest)[1])
    monkeypatch.setattr(tx, "_post_audio", lambda *a, **k: tx.Transcript("hi", [], "en"))
    tx._post_whisper("http://gpu/inference", f, fields={}, headers={}, language=None,
                     timeout=1)
    assert not list(tmp_path.glob("corpus-tx-*")), "mkdtemp leaked one dir per reel"


def test_sweep_tmp_removes_only_old_corpus_dirs(tmp_path):
    import os
    import time
    from corpus import ops
    old = tmp_path / "corpus-tx-old"
    new = tmp_path / "corpus-media-new"
    other = tmp_path / "someone-else"
    for d in (old, new, other):
        d.mkdir()
    past = time.time() - 7 * 3600
    os.utime(old, (past, past))
    os.utime(other, (past, past))
    assert ops.sweep_tmp(tmp_dir=tmp_path) == 1
    assert not old.exists() and new.exists() and other.exists()


def test_old_thumbnails_are_pruned(tmp_path):
    import os
    import time
    from corpus import ops
    old, new = tmp_path / "a.jpg", tmp_path / "b.jpg"
    old.write_bytes(b"x"); new.write_bytes(b"y")
    past = time.time() - 61 * 86400
    os.utime(old, (past, past))
    assert ops.prune_thumbs(tmp_path) == 1
    assert not old.exists() and new.exists()


# ── exit codes, lock, SIGTERM ───────────────────────────────────────────────

from corpus import cli, ops  # noqa: E402


def test_enrich_exits_nonzero_when_items_fail(tmp_path, monkeypatch, no_ambient):
    db = tmp_path / "t.db"
    conn = store.connect(db)
    _video(conn)
    monkeypatch.setattr(m, "download", _fake_download)
    monkeypatch.setattr(tx, "whisper_available", lambda: True)
    monkeypatch.setattr(er.tx, "transcribe_auto", lambda *a, **k: (_ for _ in ()).throw(
        tx.TranscribeError("bad clip")))
    monkeypatch.setattr(ops, "prune_thumbs", lambda *a, **k: 0)
    assert cli.main(["--db", str(db), "enrich", "--media", str(tmp_path / "m"),
                     "--frames", "0", "--workers", "1"]) == 1


def test_enrich_exits_nonzero_with_no_transcriber(tmp_path, monkeypatch, no_ambient):
    db = tmp_path / "t.db"
    _video(store.connect(db))
    monkeypatch.setattr(tx, "available", lambda: False)
    monkeypatch.setattr(er, "fetch_images", lambda *a, **k: {"stored": 0, "expired": 0})
    monkeypatch.setattr(ops, "prune_thumbs", lambda *a, **k: 0)
    assert cli.main(["--db", str(db), "enrich", "--media", str(tmp_path / "m")]) == 1


def test_analyze_without_a_provider_exits_nonzero(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    conn = store.connect(db)
    _item(conn)
    conn.commit()
    from corpus import providers
    monkeypatch.setattr(providers, "available", lambda *a, **k: [])
    rc = cli.main(["--db", str(db), "--analysis-db", str(tmp_path / "a.db"),
                   "analyze", "content-audit", "--execute"])
    assert rc == 1


def test_a_second_writer_exits_75_instead_of_racing(tmp_path, capsys):
    db = tmp_path / "t.db"
    store.connect(db)
    with ops.run_lock(db, what="the nightly sync"):
        rc = cli.main(["--db", str(db), "fix-paths", "--execute"])
    assert rc == ops.EX_TEMPFAIL == 75
    err = capsys.readouterr().err
    assert "another corpus run" in err and "the nightly sync" in err


def test_reads_and_dry_runs_do_not_need_the_lock(tmp_path):
    db = tmp_path / "t.db"
    store.connect(db)
    with ops.run_lock(db):
        assert cli.main(["--db", str(db), "fix-paths"]) == 0
        assert cli.main(["--db", str(db), "adapters"]) == 0


def test_the_lock_is_released_after_a_run(tmp_path):
    db = tmp_path / "t.db"
    store.connect(db)
    assert cli.main(["--db", str(db), "fix-paths", "--execute"]) == 0
    with ops.run_lock(db):
        pass


def test_the_lock_lives_next_to_the_database(tmp_path):
    assert ops.lock_path(tmp_path / "x" / "corpus.db") == tmp_path / "x" / ".lock"


def test_a_lock_held_by_another_process_is_seen(tmp_path):
    import subprocess
    import textwrap
    db = tmp_path / "t.db"
    code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from corpus import ops
        with ops.run_lock({str(db)!r}, what="child"):
            print("held", flush=True)
            time.sleep(30)
    """)
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "held"
        with pytest.raises(ops.LockBusy, match="child"):
            with ops.run_lock(db):
                pass
    finally:
        p.kill()
        p.wait()
    with ops.run_lock(db):      # released by the kernel when the holder died
        pass


def test_sigterm_closes_open_runs_and_exits_143(tmp_path):
    """systemd stop = SIGTERM. The run row must end 'interrupted', not 'running'."""
    import signal
    import subprocess
    import textwrap
    db = tmp_path / "t.db"
    store.connect(db).close()
    code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from corpus import cli, store, sync

        def slow(conn, jobs, **kw):
            rid = store.start_run(conn, adapter="slow", mode="delta")
            try:
                print("running", flush=True)
                time.sleep(30)
            finally:
                store.finish_run(conn, rid, status="interrupted")
        cli.cmd_fix_paths = lambda args: slow(store.connect(args.db), [])
        cli.build_parser  # noqa
        p = cli.build_parser()
        sys.exit(cli.main(["--db", {str(db)!r}, "fix-paths", "--execute"]))
    """)
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "running"
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=10) == 143
    st = store.connect(db).execute("SELECT status FROM scrape_run").fetchone()[0]
    assert st == "interrupted"


def test_runs_left_running_by_a_dead_process_are_closed_on_the_next_run(tmp_path):
    db = tmp_path / "t.db"
    conn = store.connect(db)
    old = store.start_run(conn, adapter="linkedin_posts", mode="delta")
    recent = store.start_run(conn, adapter="linkedin_posts", mode="delta")
    conn.execute("UPDATE scrape_run SET started_at=? WHERE id=?",
                 (store.now() - 3 * 3600, old))
    conn.commit()
    assert cli.main(["--db", str(db), "fix-paths", "--execute"]) == 0
    got = dict(conn.execute("SELECT id, status FROM scrape_run").fetchall())
    assert got == {old: "interrupted", recent: "running"}


# ── vendor payload drift ────────────────────────────────────────────────────

from corpus import sync  # noqa: E402
from corpus.adapters.base import Adapter, FetchResult, RawItem  # noqa: E402


def _raw(i):
    return RawItem(platform_item_id=f"x{i}", kind="text", published_at=1700000000 + i,
                   raw={})


def _adapter(result):
    return Adapter(name="fake_route", platform="linkedin",
                   fetch=lambda *a, **k: result, relationships=("competitor",))


@pytest.mark.parametrize("raw,parsed,ok", [
    (10, 10, True), (10, 8, True),      # 20% is tolerated
    (10, 7, False),                      # 30% is a format change
    (4, 0, True),                        # too few records to call it
    (None, 3, True),                     # a route with no raw count
])
def test_a_run_that_drops_too_many_records_fails(tmp_path, raw, parsed, ok):
    conn = store.connect(tmp_path / "t.db")
    aid = store.upsert_account(conn, platform="linkedin", handle="a",
                               relationship="competitor")
    res = FetchResult(items=[_raw(i) for i in range(parsed)], raw_count=raw,
                      cost_usd=0.01)
    out = sync.run(conn, _adapter(res), account_id=aid, target="a")
    assert (out["status"] == "ok") is ok, out
    row = conn.execute("SELECT status, items_raw, items_seen, error FROM scrape_run"
                       ).fetchone()
    assert row["items_raw"] == raw and row["items_seen"] == parsed
    if not ok:
        assert row["status"] == "error" and "did not parse" in row["error"]
    assert conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] == parsed, \
        "what parsed was paid for and is kept either way"


def test_a_drifted_run_does_not_mark_the_account_fresh(tmp_path):
    """One of twelve Shorts parsed (@PortvilleTourism, 2026-10-23) and stamped the account
    synced, so the next sync skipped it for the whole TTL with 11 posts missing."""
    conn = store.connect(tmp_path / "t.db")
    aid = store.upsert_account(conn, platform="linkedin", handle="a",
                               relationship="competitor")
    res = FetchResult(items=[_raw(0)], raw_count=12, cost_usd=0.0)
    out = sync.run(conn, _adapter(res), account_id=aid, target="a")
    assert out["status"] == "error"
    assert "billed" not in out["error"], "a free route billed nothing"
    assert conn.execute("SELECT last_scraped_at FROM account").fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 1


def test_instagram_public_drops_an_unparseable_row_instead_of_losing_the_run(monkeypatch):
    import json as _json
    from corpus.adapters import instagram_public as ig
    rows = _json.loads((Path(__file__).parent / "fixtures" / "instagram_public.json")
                       .read_text())
    broken = dict(rows[0], createdAt=None, id="broken")

    class R:
        def __init__(self, b): self.b = b
        def read(self): return self.b
        def __enter__(self): return self
        def __exit__(self, *e): return False
    monkeypatch.setattr(ig.urllib.request, "urlopen",
                        lambda req, timeout=None: R(_json.dumps(rows + [broken]).encode()))
    r = ig.fetch("orbitlab", keys={"APIFY_TOKEN": "t"}, limit=50)
    assert r.raw_count == len(rows) + 1 and len(r.items) == len(rows)


def test_linkedin_posts_reports_its_raw_count(monkeypatch):
    from corpus.adapters import linkedin_posts as lp
    recs = [{"postedAt": None}] * 3
    monkeypatch.setattr(lp, "_call", lambda *a, **k: recs)
    r = lp.fetch("https://www.linkedin.com/in/someone/", keys={"APIFY_TOKEN": "t"},
                 since=1700000000, limit=10)
    assert r.raw_count == 3 and r.items == []


def test_youtube_counts_a_short_whose_page_failed_as_a_drop(monkeypatch):
    from corpus.adapters import youtube_shorts as Y
    flat = {"entries": [{"id": f"v{i}"} for i in range(5)], "uploader_id": "@c"}
    monkeypatch.setattr(Y, "_json", lambda args: flat)
    monkeypatch.setattr(Y, "_detail", lambda e: None if e["id"] in ("v1", "v2") else
                        RawItem(platform_item_id=e["id"], kind="short_video",
                                published_at=1700000000, raw={}))
    r = Y.fetch("https://www.youtube.com/@c", mode="backfill", limit=10)
    assert r.raw_count == 5 and len(r.items) == 3
    assert sync.drop_error(r), "40% of a 5-record batch is drift"


# ── backups ─────────────────────────────────────────────────────────────────

import time as _time  # noqa: E402

from corpus import backup  # noqa: E402


def _db(tmp_path, name="corpus.db", rows=3):
    p = tmp_path / name
    conn = store.connect(p)
    for i in range(rows):
        store.upsert_account(conn, platform="linkedin", handle=f"h{i}",
                             relationship="own")
    conn.commit()
    return p, conn


def test_an_unchanged_database_is_not_snapshotted_again(tmp_path):
    p, conn = _db(tmp_path)
    out = tmp_path / "b"
    first = backup.run([p], out)
    assert len(first["taken"]) == 1
    _time.sleep(1.05)
    again = backup.run([p], out)
    assert again["taken"] == [] and again["unchanged"] == first["taken"]
    assert again["verified"][0]["ok"], "the kept snapshot is still re-verified"
    assert len(backup.list_snapshots(out)) == 1
    store.upsert_account(conn, platform="linkedin", handle="new", relationship="own")
    conn.commit()
    _time.sleep(1.05)
    assert len(backup.run([p], out)["taken"]) == 1, "a real change is snapshotted"


def test_nothing_is_pruned_when_a_snapshot_fails_verification(tmp_path, monkeypatch):
    p, conn = _db(tmp_path)
    out = tmp_path / "b"
    for i in range(3):
        store.upsert_account(conn, platform="linkedin", handle=f"x{i}",
                             relationship="own")
        conn.commit()
        backup.run([p], out, keep=2)
        _time.sleep(1.05)
    assert len(backup.list_snapshots(out)) == 2
    store.upsert_account(conn, platform="linkedin", handle="y", relationship="own")
    conn.commit()
    monkeypatch.setattr(backup, "verify", lambda path: {
        "path": str(path), "integrity": "corrupt", "counts": {}, "ok": False})
    res = backup.run([p], out, keep=2)
    assert res["errors"] and res["pruned"] == [] and res.get("prune_skipped")
    assert len(backup.list_snapshots(out)) == 3, "every good snapshot is kept"
    assert backup.last_run(out)["ok"] is False


def test_restore_goes_to_the_database_the_snapshot_belongs_to(tmp_path, capsys):
    from corpus import cli
    p, conn = _db(tmp_path)
    a = tmp_path / "analysis.db"
    import sqlite3
    c = sqlite3.connect(a)
    c.execute("CREATE TABLE analysis_result (id INTEGER PRIMARY KEY)")
    c.execute("INSERT INTO analysis_result DEFAULT VALUES")
    c.commit(); c.close()
    out = tmp_path / "b"
    backup.run([p, a], out)
    snap = next(s.path for s in backup.list_snapshots(out) if s.path.name.startswith("analysis-"))
    conn.close()
    rc = cli.main(["--db", str(p), "--analysis-db", str(a), "backup", "--restore",
                   str(snap), "--force"])
    assert rc == 0
    assert list(tmp_path.glob("analysis.db.replaced-*")), "analysis.db was the target"
    assert not list(tmp_path.glob("corpus.db.replaced-*")), "corpus.db untouched"


def test_restore_refuses_a_snapshot_of_another_database(tmp_path):
    p, _ = _db(tmp_path)
    out = tmp_path / "b"
    other, _c = _db(tmp_path, name="other.db")
    backup.run([other], out)
    snap = backup.list_snapshots(out)[0].path
    with pytest.raises(ValueError, match="not a snapshot of corpus"):
        backup.restore(snap, p, overwrite=True, expect_stem="corpus")
    from corpus import cli
    assert cli.main(["--db", str(p), "--analysis-db", str(tmp_path / "a.db"),
                     "backup", "--restore", str(snap), "--force"]) == 2


def test_backups_default_to_beside_the_database(tmp_path):
    from corpus import cli
    p, _ = _db(tmp_path)
    assert cli.main(["--db", str(p), "--analysis-db", str(tmp_path / "a.db"),
                     "backup"]) == 0
    assert backup.list_snapshots(tmp_path / "backups")


# ── health ──────────────────────────────────────────────────────────────────

from corpus import health  # noqa: E402


def _acct_run(conn, handle, rel="competitor", status="ok", age_s=0, adapter="linkedin_posts",
              error=None, **kw):
    aid = store.upsert_account(conn, platform="linkedin", handle=handle, relationship=rel)
    r = store.start_run(conn, adapter=adapter, mode="delta", account_id=aid)
    store.finish_run(conn, r, status=status, error=error, **kw)
    conn.execute("UPDATE scrape_run SET started_at=?, finished_at=? WHERE id=?",
                 (store.now() - age_s, store.now() - age_s, r))
    conn.commit()
    return aid


def _levels(checks):
    return {c.subject: c.level for c in checks}


def test_staleness_is_relative_to_each_accounts_ttl(tmp_path):
    from corpus import freshness
    conn = store.connect(tmp_path / "t.db")
    ttl = freshness.ttl_for("competitor")
    _acct_run(conn, "fresh", age_s=ttl)            # inside 2x ttl
    _acct_run(conn, "stale", age_s=2 * ttl + 3600)
    lv = _levels(health.accounts(conn))
    assert lv == {"linkedin/fresh": "ok", "linkedin/stale": "red"}


def test_a_failure_after_the_last_success_is_red_with_one_clean_line(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    aid = _acct_run(conn, "x", age_s=3600)
    r = store.start_run(conn, adapter="linkedin_posts", mode="delta", account_id=aid)
    store.finish_run(conn, r, status="error",
                     error="RuntimeError: apify 402 on actor: out of credit\n"
                           "Traceback (most recent call last):\n  File ...")
    [c] = health.accounts(conn)
    assert c.level == "red" and "out of credit" in c.message
    assert "Traceback" not in c.message


def test_a_route_that_fell_through_does_not_redden_the_account(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    aid = _acct_run(conn, "x", age_s=0)
    fb = store.start_run(conn, adapter="instagram_free", mode="delta", account_id=aid)
    store.finish_run(conn, fb, status="error", error="blocked")
    conn.execute("UPDATE scrape_run SET finished_at=finished_at-5 WHERE id=?", (fb,))
    conn.commit()
    assert health.accounts(conn)[0].level == "ok"


def test_orphans_drift_and_given_up_items_are_reported(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    old = store.start_run(conn, adapter="linkedin_posts", mode="delta")
    conn.execute("UPDATE scrape_run SET started_at=? WHERE id=?",
                 (store.now() - 3 * 3600, old))
    r = store.start_run(conn, adapter="instagram_public", mode="delta")
    store.finish_run(conn, r, status="error", seen=6, raw=10)
    _, iid = _item(conn, kind="short_video")
    conn.execute("UPDATE item SET enrich_attempts=3 WHERE id=?", (iid,))
    conn.commit()
    lv = {(c.check, c.subject): c.level for c in health.runs(conn)}
    assert lv[("runs", "orphaned")] == "red"
    assert lv[("drift", "instagram_public")] == "red"
    assert lv[("enrich", "gave up")] == "warn"


def test_a_failed_enrich_run_is_red(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    r = store.start_run(conn, adapter="enrich", mode="enrich")
    store.finish_run(conn, r, status="error", error="no transcriber reachable")
    [c] = [c for c in health.runs(conn) if c.subject == "enrich"]
    assert c.level == "red" and "no transcriber" in c.message


def test_backup_age_and_verification(tmp_path):
    p, _ = _db(tmp_path)
    out = tmp_path / "b"
    assert _levels(health.backups(out)) == {str(out): "red"}, "never backed up"
    backup.run([p], out)
    assert set(_levels(health.backups(out)).values()) == {"ok"}
    later = store.now() + 27 * 3600
    assert _levels(health.backups(out, now=later))["age"] == "red"


def test_whisper_down_is_red_only_when_nothing_else_can_transcribe():
    down = lambda: {"http://gpu:8093": "URLError: refused"}   # noqa: E731
    assert health.transcription(ping=down, yap=False, groq=False)[0].level == "red"
    assert health.transcription(ping=down, yap=False, groq=True)[0].level == "warn"
    assert health.transcription(ping=lambda: {"http://gpu:8093": None},
                                yap=False, groq=False)[0].level == "ok"


def test_spend_totals_this_month(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    for cost, age in ((0.5, 0), (0.25, 0), (9.0, 40 * 86400)):
        r = store.start_run(conn, adapter="linkedin_posts", mode="delta")
        store.finish_run(conn, r, status="ok", cost_usd=cost)
        conn.execute("UPDATE scrape_run SET started_at=? WHERE id=?",
                     (store.now() - age, r))
    conn.commit()
    msg = health.spend(conn)[0].message
    assert "$0.75" in msg


def test_spend_names_the_route_that_spent_it(tmp_path):
    """"Apify fetches and comments" was printed for ScrapeCreators and HarvestAPI
    spend too; the line has to say which balance went down."""
    conn = store.connect(tmp_path / "t.db")
    for ad, cost in (("scrapecreators_instagram", 0.03), ("linkedin_harvest", 0.012),
                     ("youtube_shorts", 0.0)):
        r = store.start_run(conn, adapter=ad, mode="delta")
        store.finish_run(conn, r, status="ok", cost_usd=cost)
    conn.commit()
    msg = health.spend(conn)[0].message
    assert "scrapecreators_instagram $0.030" in msg and "linkedin_harvest $0.012" in msg
    assert "Apify" not in msg and "youtube_shorts" not in msg


def test_adapters_table_keeps_long_names_apart(tmp_path, capsys):
    from corpus import cli
    assert cli.main(["--db", str(tmp_path / "c.db"), "adapters"]) == 0
    out = capsys.readouterr().out
    assert "scrapecreators_instagraminstagram" not in out
    assert "scrapecreators_instagram  instagram" in out


def test_health_json_and_exit_code(tmp_path, capsys, monkeypatch):
    import json as _json
    from corpus import cli
    monkeypatch.setattr(health, "transcription", lambda **kw: [])
    p, conn = _db(tmp_path)
    rc = cli.main(["--db", str(p), "health", "--json"])
    data = _json.loads(capsys.readouterr().out)
    assert rc == 1 and data["ok"] is False, "no backup yet is red"
    assert {"level", "check", "subject", "message"} <= set(data["checks"][0])


def test_an_interrupted_cli_exits_without_waiting_for_stuck_workers(tmp_path):
    """A worker blocked in a 300s network call must not hold the process open after
    Ctrl-C / SIGTERM: under systemd that stop becomes a SIGKILL."""
    import subprocess, sys, time
    script = tmp_path / "run.py"
    script.write_text(f"""
import sys, threading, time
sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
from corpus import cli
def fake_dispatch(args):
    threading.Thread(target=time.sleep, args=(60,)).start()   # non-daemon, stuck
    return 130
cli._dispatch = fake_dispatch
sys.argv = ["corpus", "--db", {str(tmp_path / 'c.db')!r}, "health"]
sys.exit(cli.main())
""")
    t = time.time()
    p = subprocess.run([sys.executable, str(script)], timeout=30)
    assert p.returncode == 130
    assert time.time() - t < 10, "waited for the stuck worker"


def test_a_damaged_database_fails_loudly_and_can_still_be_restored(tmp_path, capsys):
    """A truncated corpus must never read as an empty one. A 0-byte file opened as a
    brand-new database (health ok, next sync re-buys everything); a cut-off one
    ended in a raw traceback. Both now name the problem and the restore path, and
    `backup --restore` still runs over the damaged file."""
    from corpus import cli
    good = tmp_path / "corpus.db"
    conn = store.connect(good)
    store.upsert_account(conn, platform="instagram", handle="a", relationship="client")
    conn.commit()
    conn.close()
    A = ["--analysis-db", str(tmp_path / "analysis.db")]
    assert cli.main(["--db", str(good), *A, "backup"]) == 0
    snap = sorted((tmp_path / "backups").glob("corpus-*.db.gz"))[-1]
    capsys.readouterr()

    data = good.read_bytes()
    for size in (0, 100):
        bad = tmp_path / "corpus.db"
        bad.write_bytes(data[:size])
        assert cli.main(["--db", str(bad), *A, "health"]) == 1
        err = capsys.readouterr().err
        assert "damaged" in err and "--restore" in err
        assert bad.stat().st_size == size, "a damaged file must not be rewritten"
        if size == 0:
            # sqlite would have created a fresh schema here
            with pytest.raises(store.DamagedDatabase):
                store.connect(bad)

    assert cli.main(["--db", str(bad), *A, "backup", "--restore", str(snap),
                     "--force"]) == 0
    conn = store.connect(bad)
    assert conn.execute("SELECT handle FROM account").fetchone()[0] == "a"


@pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None, reason="no ffmpeg")
def test_a_clip_with_no_audio_stream_is_no_speech_not_a_failure(
        tmp_path, monkeypatch, no_ambient):
    """Three Instagram reels with no audio track: whisper 500, Groq 400, an attempt
    burned on every run until they were retired as failures (2026-10-23)."""
    import subprocess
    conn = store.connect(tmp_path / "t.db")
    [iid] = _video(conn)
    src = tmp_path / "silent.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=160x120:rate=5:duration=2", "-pix_fmt", "yuv420p",
                    str(src)], check=True, capture_output=True)

    def dl(url, dest, **kw):
        dest.write_bytes(src.read_bytes())
        return dest
    monkeypatch.setattr(m, "download", dl)
    monkeypatch.setattr(tx, "whisper_available", lambda: True)
    monkeypatch.setattr(er.tx, "transcribe_auto", lambda *a, **k: pytest.fail(
        "a clip with no audio was sent to a transcriber"))
    st = er.run(conn, media_root=tmp_path / "media", frames=0, workers=1)
    assert st.failed == 0 and st.transcribed == 1
    row = conn.execute("SELECT lang, enrich_attempts FROM item WHERE id=?",
                       (iid,)).fetchone()
    assert tuple(row) == ("none", 0)


def test_sentinels_and_redone_guesses_are_never_handed_to_whisper():
    assert er._stored_lang({"lang": "music"}) is None
    assert er._stored_lang({"lang": "none"}) is None
    assert er._stored_lang({"lang": "en", "redo": True}) is None
    assert er._stored_lang({"lang": "hr"}) == "hr"


def test_a_non_english_caption_of_unknown_language_takes_the_accounts(monkeypatch,
                                                                      tmp_path):
    seen = {}
    monkeypatch.setattr(tx, "transcribe_whisper",
                        lambda p, *, language=None, **k: seen.update(language=language)
                        or tx.Transcript("x", [], language))
    tx.transcribe_auto(tmp_path / "x.mp4", fallback_lang="hr",
                       hint_text="Riječki parkovi – zelene oaze, šetnice i čempresi")
    assert tx.detect_language("Riječki parkovi – zelene oaze, šetnice i čempresi") == "auto"
    assert seen["language"] == "hr"


def test_a_profile_the_vendor_says_is_gone_does_not_fall_through(tmp_path):
    """A nonexistent Instagram handle went from ScrapeCreators ("Profile is
    unavailable", 0 credits) on to Apify, Bright Data (billed error records) and
    the throttled free route. A definitive answer ends the walk."""
    from corpus.adapters.base import TargetGone
    calls = []

    def gone(*a, **k):
        calls.append("first")
        raise TargetGone("scrapecreators: @nobody: Profile is unavailable")

    def paid(*a, **k):
        calls.append("second")
        return FetchResult(items=[])
    routes = [Adapter(name="a", platform="instagram", fetch=gone,
                      relationships=("competitor",)),
              Adapter(name="b", platform="instagram", fetch=paid,
                      relationships=("competitor",))]
    job = sync.execute(sync.Job(account_id=1, target="nobody", routes=routes))
    assert calls == ["first"] and job.attempts[-1].gone


def test_harvest_company_not_found_is_definitive(monkeypatch):
    from corpus.adapters import linkedin_harvest as H
    from corpus.adapters.base import TargetGone
    import io, json as _json

    monkeypatch.setattr(H.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(
        _json.dumps({"error": ["Company not found: harbor-ferries-typo"]}).encode()))
    with pytest.raises(TargetGone):
        H._get("/linkedin/company-posts", {"company": "x"}, "k")


# ── a page that exists but has never posted (2026-10-23..25: nightly red 3x) ──

def _empty_routes(*specs):
    """specs: (name, apify_actor, behaviour) with behaviour 'empty' | 'error'."""
    from corpus.adapters.base import EmptyProfile
    calls = []

    def mk(name, how):
        def fetch(*a, **k):
            calls.append(name)
            if how == "empty":
                raise EmptyProfile(f"{name}: no posts")
            raise RuntimeError(f"{name}: 403 outstanding invoices")
        return fetch
    return calls, [Adapter(name=n, platform="linkedin", fetch=mk(n, how), apify_actor=act,
                           relationships=("influencer",)) for n, act, how in specs]


def test_two_vendors_agreeing_on_no_posts_is_a_checked_empty_account(tmp_path):
    from corpus import freshness, health
    conn = store.connect(tmp_path / "t.db")
    aid = store.upsert_account(conn, platform="linkedin", handle="new-page",
                               relationship="influencer")
    calls, routes = _empty_routes(("brightdata_linkedin", "", "empty"),
                                  ("linkedin_harvest", "", "empty"),
                                  ("linkedin_posts", "harvestapi~linkedin-profile-posts", "empty"))
    job = sync.execute(sync.Job(account_id=aid, target="x", routes=routes, mode="backfill"))
    assert calls == ["brightdata_linkedin", "linkedin_harvest"]   # stops at the quorum
    out = sync.record(conn, job)
    assert out["status"] == "ok" and out["new"] == 0
    assert conn.execute("SELECT COUNT(*) FROM scrape_run WHERE status='error'").fetchone()[0] == 0
    assert freshness.plan(conn)[0].action == "skip"
    chk = health.accounts(conn)[0]
    assert chk.level == "ok" and "no posts yet" in chk.message
    # still green past the normal 2x ttl, because it is only rechecked weekly...
    later = store.now() + 6 * 86400
    assert health.accounts(conn, now=later)[0].level == "ok"
    # ...and due again after a week
    conn.execute("UPDATE account SET last_scraped_at=? WHERE id=?",
                 (store.now() - freshness.EMPTY_RECHECK - 60, aid))
    assert freshness.plan(conn)[0].action == "backfill"


def test_one_vendor_saying_empty_is_still_a_failure(tmp_path):
    _, routes = _empty_routes(("brightdata_linkedin", "", "empty"),
                              ("linkedin_harvest", "", "error"))
    job = sync.execute(sync.Job(account_id=1, target="x", routes=routes))
    assert not job.attempts[-1].ok


def test_the_same_vendor_twice_is_not_a_second_opinion(tmp_path):
    _, routes = _empty_routes(("linkedin_harvest", "", "empty"),
                              ("linkedin_posts", "harvestapi~linkedin-profile-posts", "empty"))
    job = sync.execute(sync.Job(account_id=1, target="x", routes=routes))
    assert not job.attempts[-1].ok


def test_brightdata_dead_page_without_activities_means_empty_not_broken():
    from corpus.adapters import brightdata as B
    from corpus.adapters.base import EmptyProfile
    with pytest.raises(EmptyProfile):
        B._raise_if_only_errors([], [{"error_code": "dead_page",
                                      "error": "Activities are not found"}], "u")
    with pytest.raises(RuntimeError) as e:
        B._raise_if_only_errors([], [{"error_code": "dead_page", "error": "Page not found"}], "u")
    assert not isinstance(e.value, EmptyProfile)
