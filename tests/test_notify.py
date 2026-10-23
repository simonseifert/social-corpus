"""The alert channel: delivery, fallback config, and never leaking the key.

A local HTTP server stands in for the WhatsApp bridge, so the real request shape
(path, JSON body, X-API-Key header) is exercised without a message being sent.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import cli, notify  # noqa: E402

KEY = "sekret-key-0123456789"


@pytest.fixture
def bridge():
    got = []

    class H(BaseHTTPRequestHandler):
        reply = {"success": True}

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            got.append({"path": self.path, "headers": self.headers,  # case-insensitive, as on the wire
                        "body": self.rfile.read(n)})
            body = json.dumps(H.reply).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}", got, H
    srv.shutdown()
    srv.server_close()


def _config(tmp_path, url, **extra):
    env = tmp_path / "bridge.env"
    env.write_text(f"OTHER=1\nexport API_KEY='{KEY}'\n")
    cfg = tmp_path / "notify.json"
    cfg.write_text(json.dumps({"rest_url": url + "/api/send", "rest_env": str(env),
                               "recipient": "10000000000@s.whatsapp.net", **extra}))
    return cfg


def test_send_posts_recipient_and_message_with_the_key_header(tmp_path, bridge,
                                                              monkeypatch, capsys):
    url, got, _ = bridge
    monkeypatch.setenv("CORPUS_NOTIFY_CONFIG", str(_config(tmp_path, url)))
    assert notify.send("hello from corpus") is True
    assert len(got) == 1
    req = got[0]
    assert req["path"] == "/api/send"
    assert req["headers"]["X-API-Key"] == KEY
    assert json.loads(req["body"]) == {"recipient": "10000000000@s.whatsapp.net",
                                       "message": "hello from corpus"}
    out = capsys.readouterr()
    assert KEY not in out.out + out.err


def test_bridge_saying_success_false_is_not_delivery(tmp_path, bridge, monkeypatch):
    url, _, handler = bridge
    handler.reply = {"success": False, "message": "not logged in"}
    monkeypatch.setenv("CORPUS_NOTIFY_CONFIG", str(_config(tmp_path, url)))
    assert notify.send("x") is False


def test_unreachable_bridge_returns_false_and_never_prints_the_key(tmp_path,
                                                                   monkeypatch, capsys):
    monkeypatch.setenv("CORPUS_NOTIFY_CONFIG",
                       str(_config(tmp_path, "http://127.0.0.1:9")))
    assert notify.send("x", timeout=2) is False
    out = capsys.readouterr()
    assert KEY not in out.out + out.err
    assert "whatsapp failed" in out.err


def test_ntfy_is_a_second_channel(tmp_path, bridge, monkeypatch):
    url, got, _ = bridge
    cfg = _config(tmp_path, "http://127.0.0.1:9", ntfy_url=url + "/corpus-alerts")
    monkeypatch.setenv("CORPUS_NOTIFY_CONFIG", str(cfg))
    # WhatsApp is down, ntfy still takes it: the alert gets out.
    assert notify.send("disk full", title="corpus: x failed", timeout=2) is True
    assert got[0]["path"] == "/corpus-alerts"
    assert got[0]["body"] == b"disk full"
    assert got[0]["headers"]["Title"] == "corpus: x failed"


def test_falls_back_to_the_shared_bridge_config(tmp_path, bridge, monkeypatch):
    url, got, _ = bridge
    monkeypatch.delenv("CORPUS_NOTIFY_CONFIG", raising=False)
    monkeypatch.setattr(notify, "CONFIG", tmp_path / "missing" / "notify.json")
    monkeypatch.setattr(notify, "FALLBACK", _config(tmp_path, url))
    assert notify.send("via shared config") is True
    assert json.loads(got[0]["body"])["message"] == "via shared config"


def test_corpus_config_wins_over_the_shared_one(tmp_path, monkeypatch):
    monkeypatch.delenv("CORPUS_NOTIFY_CONFIG", raising=False)
    mine = tmp_path / "mine.json"
    mine.write_text(json.dumps({"recipient": "mine"}))
    shared = tmp_path / "notify-bridge.json"
    shared.write_text(json.dumps({"recipient": "shared"}))
    monkeypatch.setattr(notify, "CONFIG", mine)
    monkeypatch.setattr(notify, "FALLBACK", shared)
    assert notify.load_config()["recipient"] == "mine"


def test_no_config_at_all_is_false_not_an_exception(monkeypatch, tmp_path):
    monkeypatch.setenv("CORPUS_NOTIFY_CONFIG", str(tmp_path / "nope.json"))
    assert notify.send("x") is False


def test_scrub_removes_secrets_but_keeps_ordinary_log_lines():
    assert notify.scrub("APIFY_TOKEN=apify_api_abc123") == "APIFY_TOKEN=***"
    assert notify.scrub("GET /x?token=abc&n=1") == "GET /x?token=***&n=1"
    assert "abc.def" not in notify.scrub("Authorization: Bearer abc.def")
    assert "sk-" not in notify.scrub("DEEPSEEK_API_KEY: sk-abcdefghijklmnopq123")
    assert notify.scrub('{"api_key": "abc123", "n": 1}') == '{"api_key": "***", "n": 1}'
    assert notify.scrub("{'token': 'abc'}") == "{'token': '***'}"
    assert notify.scrub('"password": 12345,') == '"password": ***,'
    assert notify.scrub("GET https://bob:hunter2@host/x") == "GET https://***@host/x"
    assert "dXNlcjpwYXNz" not in notify.scrub("Authorization: Basic dXNlcjpwYXNz")
    assert "ghp_abc" not in notify.scrub("authorization: token ghp_abc")
    for keep in ("keyframes: 6", "tokens: 1200", "enrich: 3 transcribed",
                 "/home/user/src/corpus/deploy/corpus-nightly.sh"):
        assert notify.scrub(keep) == keep


def _fake_journalctl(tmp_path, lines):
    exe = tmp_path / "journalctl"
    exe.write_text("#!/bin/sh\necho \"$@\" > " + str(tmp_path / "argv") + "\n"
                   + "".join(f"echo '{ln}'\n" for ln in lines))
    exe.chmod(0o755)
    return exe


def _fake_systemctl(tmp_path):
    exe = tmp_path / "systemctl"
    exe.write_text("#!/bin/sh\necho Result=exit-code\necho ExecMainStatus=1\n")
    exe.chmod(0o755)
    return exe


def _which(**exes):
    return lambda name: str(exes[name]) if name in exes else None


def test_unit_report_carries_status_and_the_scrubbed_journal_tail(tmp_path,
                                                                  monkeypatch):
    exe = _fake_journalctl(tmp_path, ["step 1 ok", "GROQ_API_KEY=gsk_live_999",
                                      "Traceback: boom " + "x" * 400])
    monkeypatch.setattr(notify.shutil, "which",
                        _which(journalctl=exe, systemctl=_fake_systemctl(tmp_path)))
    monkeypatch.setenv("CORPUS_NOTIFY_CONFIG", str(tmp_path / "none.json"))
    text = notify.unit_failure_text("corpus-nightly.service")
    assert text.startswith("corpus: corpus-nightly.service failed on ")
    assert "(result=exit-code, exit status 1)" in text.splitlines()[0]
    assert "step 1 ok" in text and "boom" in text
    assert "gsk_live_999" not in text
    assert max(len(ln) for ln in text.splitlines()) <= notify.JOURNAL_LINE_CHARS
    argv = (tmp_path / "argv").read_text().split()
    assert argv[:5] == ["--user", "-u", "corpus-nightly.service", "-n", "15"]
    assert "--no-pager" in argv


def test_status_only_mode_sends_no_log_lines(tmp_path, monkeypatch):
    exe = _fake_journalctl(tmp_path, ["something private in a log line"])
    monkeypatch.setattr(notify.shutil, "which",
                        _which(journalctl=exe, systemctl=_fake_systemctl(tmp_path)))
    cfg = tmp_path / "n.json"
    cfg.write_text(json.dumps({"unit_detail": "status"}))
    monkeypatch.setenv("CORPUS_NOTIFY_CONFIG", str(cfg))
    text = notify.unit_failure_text("corpus-nightly.service")
    assert "exit status 1" in text
    assert "private" not in text
    assert not (tmp_path / "argv").exists()       # the journal was never read


def test_unit_report_without_journalctl_still_says_what_failed(monkeypatch):
    monkeypatch.setattr(notify.shutil, "which", lambda n: None)
    text = notify.unit_failure_text("corpus-weekly.service")
    assert "corpus-weekly.service failed" in text


def test_cli_notify_unit_dry_run_prints_and_sends_nothing(tmp_path, monkeypatch,
                                                          capsys):
    exe = _fake_journalctl(tmp_path, ["sync: 1 failure(s)"])
    monkeypatch.setattr(notify.shutil, "which", _which(journalctl=exe))
    monkeypatch.setenv("CORPUS_NOTIFY_CONFIG", str(tmp_path / "none.json"))
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(a) or True)
    assert cli.main(["notify", "--unit", "corpus-nightly.service", "--dry-run"]) == 0
    assert "sync: 1 failure(s)" in capsys.readouterr().out
    assert sent == []


def test_cli_notify_exit_code_follows_delivery(monkeypatch):
    monkeypatch.setattr(notify, "send", lambda *a, **k: False)
    assert cli.main(["notify", "hello"]) == 1
    monkeypatch.setattr(notify, "send", lambda *a, **k: True)
    assert cli.main(["notify", "hello"]) == 0
