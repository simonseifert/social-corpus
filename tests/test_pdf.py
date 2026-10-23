"""PDF export's browser lookup, and a real render where a Chromium exists."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import brief, store  # noqa: E402


def _no_system_browser(monkeypatch):
    monkeypatch.setattr(brief, "CHROME_PATHS", ())
    monkeypatch.setattr("shutil.which", lambda name: None)


def test_corpus_chrome_override_wins(tmp_path, monkeypatch):
    fake = tmp_path / "my-chrome"
    fake.write_text("")
    monkeypatch.setenv("CORPUS_CHROME", str(fake))
    assert brief._chrome() == str(fake)


def test_newest_playwright_chromium_is_found(tmp_path, monkeypatch):
    """A headless Linux server has no system Chrome, only Playwright's builds."""
    _no_system_browser(monkeypatch)
    monkeypatch.delenv("CORPUS_CHROME", raising=False)
    for rev in ("1223", "1243", "1234"):
        p = tmp_path / f"chromium-{rev}" / "chrome-linux64" / "chrome"
        p.parent.mkdir(parents=True)
        p.write_text("")
    (tmp_path / "chromium_headless_shell-9999").mkdir()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    assert brief._chrome() == str(
        tmp_path / "chromium-1243" / "chrome-linux64" / "chrome")


def test_nothing_found_is_none(tmp_path, monkeypatch):
    _no_system_browser(monkeypatch)
    monkeypatch.delenv("CORPUS_CHROME", raising=False)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    assert brief._chrome() is None


@pytest.mark.skipif(brief._chrome() is None, reason="no Chromium on this machine")
def test_a_brief_renders_to_pdf(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    aid = store.upsert_account(conn, platform="linkedin", handle="bob",
                               relationship="competitor", display_name="Bob")
    for i in range(3):
        store.upsert_item(conn, account_id=aid, platform="linkedin",
                          platform_item_id=f"p{i}", kind="text",
                          published_at=1700000000 + i * 86400,
                          body=f"Post {i}: čćđšž", raw={})
    conn.commit()
    html, pdf = brief.write_pdf(conn, aid, tmp_path / "brief.html")
    assert pdf.read_bytes()[:5] == b"%PDF-" and pdf.stat().st_size > 5000
