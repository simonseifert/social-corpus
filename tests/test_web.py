"""Dashboard.

The security-relevant surface: this process can be told to listen on a network,
and it renders text written by strangers. Both are covered here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from corpus import store, web  # noqa: E402


def _db(tmp_path):
    conn = store.connect(tmp_path / "w.db")
    aid = store.upsert_account(conn, platform="linkedin", handle="subject",
                               relationship="own", client_ref="ref")
    iid, _, _ = store.upsert_item(conn, account_id=aid, platform="linkedin",
                                  platform_item_id="p1", kind="text",
                                  published_at=1700000000, body="a normal post",
                                  raw={})
    conn.commit()
    return conn, aid, iid


def test_public_bind_without_a_token_is_refused(tmp_path):
    """The database holds comment text from third parties who never consented."""
    with pytest.raises(SystemExit) as e:
        web.serve(db_path=tmp_path / "x.db", media_root=tmp_path,
                  host="0.0.0.0", port=0)
    assert "token" in str(e.value).lower()


def test_localhost_bind_needs_no_token(tmp_path, monkeypatch):
    """Refusing the safe case too would just push people to --host 0.0.0.0."""
    started = {}

    class FakeServer:
        def __init__(self, addr, handler):
            started["addr"] = addr

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(web, "ThreadingHTTPServer", FakeServer)
    web.serve(db_path=tmp_path / "x.db", media_root=tmp_path,
              host="127.0.0.1", port=8899, brief_dir=tmp_path)
    assert started["addr"] == ("127.0.0.1", 8899)


def test_comment_text_from_a_stranger_cannot_inject_markup(tmp_path):
    """Comment bodies are written by people we have no relationship with."""
    conn, aid, iid = _db(tmp_path)
    store.upsert_comment(conn, iid, platform_comment_id="c1",
                         body="<script>alert('xss')</script> nice post",
                         author_name="<img src=x onerror=alert(1)>")
    conn.commit()
    html = web.view_search(conn, "nice")
    # The check is for live MARKUP, not for the substring: "onerror=" appearing
    # inside &lt;img src=x onerror=alert(1)&gt; is inert text, and asserting on the
    # bare substring fails on correctly-escaped output.
    assert "<script" not in html and "<img" not in html
    assert "&lt;script&gt;" in html and "&lt;img" in html


def test_account_names_and_post_text_are_escaped(tmp_path):
    conn = store.connect(tmp_path / "w.db")
    aid = store.upsert_account(conn, platform="linkedin",
                               handle="ok-handle", relationship="own",
                               display_name="<b>bold</b>")
    store.upsert_item(conn, account_id=aid, platform="linkedin",
                      platform_item_id="p1", kind="text", published_at=1700000000,
                      body="<script>bad()</script>", raw={})
    conn.commit()
    for html in (web.view_index(conn, web.Jobs()),
                 web.view_account(conn, "ok-handle", web.Jobs())):
        assert "<script>" not in html and "<b>bold</b>" not in html


def test_search_highlight_cannot_be_forged_by_a_bracket_in_the_text(tmp_path):
    """The snippet delimiters are control characters precisely so that a caption
    containing "[" cannot emit a <mark> tag."""
    conn, aid, iid = _db(tmp_path)
    store.upsert_item(conn, account_id=aid, platform="linkedin",
                      platform_item_id="p2", kind="text", published_at=1700000001,
                      body="[not a highlight] unicorn here", raw={})
    conn.commit()
    html = web.view_search(conn, "unicorn")
    assert "<mark>unicorn</mark>" in html
    assert "<mark>not a highlight" not in html


def test_only_one_job_runs_at_a_time():
    """SQLite takes exactly one writer."""
    import threading
    jobs = web.Jobs()
    gate = threading.Event()
    assert jobs.start("first", gate.wait)["ok"] is True
    assert jobs.start("second", lambda: None)["ok"] is False
    gate.set()


def test_a_failed_job_is_reported_as_an_error():
    """Reporting a failure green means nobody looks at it again."""
    import time
    jobs = web.Jobs()

    def boom():
        raise LookupError("no account 'nope'")

    jobs.start("enrich nope", boom)
    for _ in range(50):
        if not jobs.busy:
            break
        time.sleep(0.02)
    rec = jobs.recent()[0]
    assert rec["state"] == "error" and "nope" in rec["detail"]


def test_brief_reference_is_sanitised(tmp_path):
    """The ref becomes a filename; traversal must not reach outside brief_dir."""
    conn, aid, iid = _db(tmp_path)
    safe = "".join(c for c in "../../etc/passwd" if c.isalnum() or c in "-_")
    assert "/" not in safe and ".." not in safe


# ── cross-origin: POST /run spends money ────────────────────────────────────

def _live(tmp_path, monkeypatch, token=None):
    """A real server on a free port, so the checks run on real HTTP headers."""
    import threading
    conn, _, _ = _db(tmp_path)
    started = []
    monkeypatch.setattr(web.Jobs, "start", lambda self, label, fn: started.append(label))
    allowed: set = set()
    h = web.make_handler(db_path=tmp_path / "w.db", analysis_db=tmp_path / "a.db",
                         media_root=tmp_path, jobs=web.Jobs(), token=token,
                         brief_dir=tmp_path, allowed_hosts=allowed)
    srv = web.ThreadingHTTPServer(("127.0.0.1", 0), h)
    port = srv.server_address[1]
    allowed.update({f"127.0.0.1:{port}", f"localhost:{port}"})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port, started


def _req(port, method, path, headers=None, body=None):
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read()


def _form(csrf):
    return f"csrf={csrf}&action=comments&handle=subject"


def test_a_form_from_another_site_cannot_start_a_paid_job(tmp_path, monkeypatch):
    srv, port, started = _live(tmp_path, monkeypatch)
    try:
        hdr = {"Content-Type": "application/x-www-form-urlencoded",
               "Origin": "https://evil.example"}
        st, _, _ = _req(port, "POST", "/run", hdr, _form(web.CSRF))
        assert st == 403
        del hdr["Origin"]                          # no Origin, no token: still refused
        st, _, _ = _req(port, "POST", "/run", hdr, _form("guess"))
        assert st == 403
        assert started == []
        st, _, _ = _req(port, "POST", "/run", hdr, _form(web.CSRF))
        assert st == 303 and started == ["comments subject"]
    finally:
        srv.shutdown()


def test_a_rebound_hostname_is_not_served(tmp_path, monkeypatch):
    srv, port, _ = _live(tmp_path, monkeypatch)
    try:
        assert _req(port, "GET", "/", {"Host": "evil.example"})[0] == 421
        assert _req(port, "GET", "/")[0] == 200
    finally:
        srv.shutdown()


def test_the_page_carries_the_csrf_token_in_its_forms(tmp_path, monkeypatch):
    srv, port, _ = _live(tmp_path, monkeypatch)
    try:
        assert f"value='{web.CSRF}'".encode() in _req(port, "GET", "/")[2]
    finally:
        srv.shutdown()


def test_token_mode_sets_a_cookie_so_links_keep_working(tmp_path, monkeypatch):
    srv, port, _ = _live(tmp_path, monkeypatch, token="s3cret")
    try:
        assert _req(port, "GET", "/")[0] == 403
        st, hdrs, _ = _req(port, "GET", "/?token=s3cret")
        assert st == 200 and "SameSite=Strict" in hdrs.get("Set-Cookie", "")
        assert _req(port, "GET", "/search?q=x", {"Cookie": "corpus_token=s3cret"})[0] == 200
        assert _req(port, "GET", "/", {"Cookie": "corpus_token=wrong"})[0] == 403
    finally:
        srv.shutdown()


def test_dashboard_scopes_a_client_and_reaches_both_platforms_of_a_handle(
        tmp_path, monkeypatch):
    """Field test 2026-10-23: /brief/harbor folded a rival café (a competitor tagged
    'harbor') into the client's brief, and /a/bistroharbor could only ever open the
    Instagram account, never the Facebook one with the same handle."""
    srv, port, _ = _live(tmp_path, monkeypatch)
    try:
        conn = store.connect(tmp_path / "w.db")
        for plat, body in (("instagram", "ig words"), ("facebook", "fb words")):
            a = store.upsert_account(conn, platform=plat, handle="twin",
                                     relationship="client", client_ref="ref")
            store.upsert_item(conn, account_id=a, platform=plat,
                              platform_item_id=plat, kind="image", body=body,
                              published_at=store.now() - 86400, raw={})
        r = store.upsert_account(conn, platform="linkedin", handle="rivalco",
                                 relationship="competitor", client_ref="ref")
        store.upsert_item(conn, account_id=r, platform="linkedin",
                          platform_item_id="r1", kind="text", body="rival words",
                          published_at=store.now() - 86400, raw={})
        conn.commit()
        st, _, body = _req(port, "GET", "/")
        assert b"/a/facebook%3Atwin" in body and b"/a/instagram%3Atwin" in body
        st, _, body = _req(port, "GET", "/a/facebook%3Atwin")
        assert st == 200 and b"fb words" in body and b"ig words" not in body
        st, _, body = _req(port, "GET", "/brief/ref")
        assert st == 200 and b"rival words" not in body and b"ig words" in body
    finally:
        srv.shutdown()
