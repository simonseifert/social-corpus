"""The tesseract branch of enrich/ocr.py (Linux; macOS keeps Vision)."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus.enrich import ocr  # noqa: E402

TSV = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    "5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t92\tČovjek\n"
    "5\t1\t1\t1\t1\t2\t0\t0\t1\t1\t88\tće\n"
    "5\t1\t1\t1\t1\t3\t0\t0\t1\t1\t30\t|\n"
    "5\t1\t2\t1\t1\t1\t0\t0\t1\t1\t41\tTE\n"
)


def test_tsv_words_group_into_lines_and_low_confidence_drops():
    lines = ocr._tsv_lines(TSV, 60)
    assert [l.text for l in lines] == ["Čovjek će"]
    assert lines[0].confidence == pytest.approx(0.90)


def test_linux_uses_tesseract_not_vision(monkeypatch):
    monkeypatch.setattr(ocr.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ocr.shutil, "which",
                        lambda n: "/usr/bin/tesseract" if n == "tesseract" else None)
    assert ocr.available()
    monkeypatch.setattr(ocr, "_ensure_binary",
                        lambda: pytest.fail("Vision shim must not build on Linux"))
    monkeypatch.setattr(ocr, "_tesseract", lambda p, t: TSV)
    frames = ocr.read([Path(__file__)])
    assert frames[0].error is None and frames[0].text == "Čovjek će"


def test_no_tesseract_means_unavailable(monkeypatch):
    monkeypatch.setattr(ocr.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ocr.shutil, "which", lambda n: None)
    assert not ocr.available()


@pytest.mark.skipif(not (shutil.which("tesseract") and shutil.which("ffmpeg")
                         and sys.platform.startswith("linux")),
                    reason="needs tesseract + ffmpeg on Linux")
def test_real_caption_frame_keeps_croatian_diacritics(tmp_path):
    """A caption-styled frame: white text, dark outline, busy background."""
    font = subprocess.run(["fc-match", "-f", "%{file}", "DejaVu Sans:bold"],
                          capture_output=True, text=True).stdout
    if not font:
        pytest.skip("no DejaVu font")
    frame = tmp_path / "f.jpg"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
         "color=c=0x3a4450:size=480x854:duration=1", "-frames:v", "1", "-vf",
         "noise=alls=30:allf=t,"
         f"drawtext=fontfile={font}:text='Čovjek će đaku':fontsize=34:"
         "fontcolor=white:borderw=3:bordercolor=black:x=(w-tw)/2:y=h*0.6",
         str(frame)], check=True)
    text, stats = ocr.read_item_frames([frame])
    assert stats["failed"] == 0
    assert "Čovjek će đaku" in text, text


def test_a_frame_with_no_text_is_read_once_not_every_night(tmp_path, monkeypatch):
    """'' = read, nothing on screen. Re-reading those cost every nightly 70s."""
    from corpus import store
    from corpus.enrich import run
    conn = store.connect(tmp_path / "c.db")
    aid = store.upsert_account(conn, platform="instagram", handle="b",
                               relationship="competitor")
    iid, _, _ = store.upsert_item(conn, account_id=aid, platform="instagram",
                                  platform_item_id="p", kind="short_video",
                                  published_at=1700000000, raw={})
    frame = tmp_path / "f.jpg"
    frame.write_bytes(b"x")
    mid = store.add_media(conn, iid, role="keyframe")
    conn.execute("UPDATE media SET local_path=? WHERE id=?", (str(frame), mid))
    conn.commit()
    calls = []
    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "read_item_frames", lambda paths: calls.append(1) or ("", []))
    assert run.read_screen_text(conn)["considered"] == 1
    assert run.read_screen_text(conn)["considered"] == 0
    assert calls == [1]
