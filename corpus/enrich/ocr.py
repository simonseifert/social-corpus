"""Reading the text that is burned into a video, not spoken in it.

A large share of short-form content carries its whole argument on screen over a music
bed. The transcript for those is empty or an artefact, so the analysis lens has
nothing to work with and correctly says so -- which is accurate and useless.

The obvious fix is to send keyframes to a vision model. This does not do that, because
macOS already ships a text recogniser (Vision.framework) that is free, offline, needs
no model download, and reads reel captions well. Measured on the author's own frames: about
0.12s per frame, and it read Croatian burned-in text correctly ("MOZES ZA", "VAZE 80%")
despite Vision listing no Croatian, because the recogniser is Latin-script.

That inverts the economics the frame-selection module was designed around. Selecting
frames carefully mattered when each one cost a vision call; now OCR is free and the
selection exists to keep the OUTPUT clean rather than to control spend. A vision model
is still the right tool for "what is happening in this image", which OCR cannot answer
-- so this reads the words, and anything beyond words stays a separate, paid decision.

Linux has no Vision, so there it shells out to tesseract (eng+hrv traineddata) with the
same output contract. Tesseract is a document OCR and reads burned-in captions badly
as-is: white text with a dark outline over a moving picture is the opposite of a
scanned page. Measured on 2026-10-22 against real Short frames and a caption-styled
frame carrying every Croatian diacritic, at the 480px keyframe width corpus stores:

  raw frame, psm 11       "é€e, daku" / "Čovjek." / "reci"   (ć, đ lost)
  2x + white-isolated     "Čovjek će đaku"                    (all diacritics right)

so each frame is read twice, as-is and with bright pixels isolated (captions are
white or yellow), and the lines from both are pooled; merge() already folds the
duplicates. Words below 60 confidence are dropped first: t-shirt prints and screen
furniture come back as "TE", "19", "|" at 60-80, which the two-word minimum in merge()
then removes. When tesseract is absent `available()` is False and OCR is skipped.
"""
from __future__ import annotations

import csv
import io
import json
import os
import platform
import tempfile
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

_SRC = Path(__file__).with_name("vendor") / "ocr.swift"
# Built once into the user's cache rather than the repo: the repo is shared, and a
# compiled arm64 binary is not portable to the machine that clones it.
_BIN = Path("~/.corpus/bin/ocr").expanduser()

# Vision returns per-line confidence in 0..1. Below this the "text" is usually a
# logo, a watermark edge, or compression noise shaped like letters.
MIN_CONFIDENCE = 0.45

# Frames are sampled at fixed intervals, so a caption animating in gets captured
# half-drawn: "The Em" and "Emotional out." are the same line at different moments.
# Merging keeps the longest version of any line another line is a prefix of.
_WORD = re.compile(r"[^\W\d_]{2,}", re.UNICODE)


class OCRError(RuntimeError):
    pass


@dataclass
class Line:
    text: str
    confidence: float


@dataclass
class FrameText:
    path: Path
    lines: list[Line] = field(default_factory=list)
    error: str | None = None

    @property
    def text(self) -> str:
        return "\n".join(l.text for l in self.lines)


# Tesseract word confidence is 0-100 and runs high on shapes that are not text.
TESS_MIN_WORD_CONF = 60
TESS_LANGS = os.environ.get("CORPUS_OCR_LANGS", "hrv+eng")


def _darwin() -> bool:
    return platform.system() == "Darwin"


def available() -> bool:
    """True when on-device OCR can run here."""
    if not _darwin():
        return shutil.which("tesseract") is not None
    return _BIN.exists() or shutil.which("swiftc") is not None


def _ensure_binary() -> Path:
    """Compile the shim on first use, then reuse it.

    Rebuilt when the source is newer than the binary so an edit to the Swift is not
    silently ignored.
    """
    if _BIN.exists() and _BIN.stat().st_mtime >= _SRC.stat().st_mtime:
        return _BIN
    if platform.system() != "Darwin":
        raise OCRError("on-device OCR needs macOS (Vision.framework)")
    swiftc = shutil.which("swiftc")
    if not swiftc:
        raise OCRError("swiftc not found; install the Xcode command line tools")

    _BIN.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([swiftc, "-O", str(_SRC), "-o", str(_BIN)],
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not _BIN.exists():
        raise OCRError(f"could not build the OCR shim: {proc.stderr[:300]}")
    return _BIN


def _tsv_lines(tsv: str, min_word_conf: float) -> list[Line]:
    """Group tesseract's word rows into lines; line confidence is the mean of the
    words kept, scaled to Vision's 0..1."""
    lines: dict[tuple, list[tuple[str, float]]] = {}
    for r in csv.DictReader(io.StringIO(tsv), delimiter="\t",
                            quoting=csv.QUOTE_NONE):
        if r.get("level") != "5" or not (r.get("text") or "").strip():
            continue
        try:
            conf = float(r["conf"])
        except (TypeError, ValueError):
            continue
        if conf < min_word_conf:
            continue
        key = (r["page_num"], r["block_num"], r["par_num"], r["line_num"])
        lines.setdefault(key, []).append((r["text"].strip(), conf))
    return [Line(" ".join(w for w, _ in ws), sum(c for _, c in ws) / len(ws) / 100)
            for ws in lines.values()]


def _isolate_bright(src: Path, dst: Path, timeout: int) -> bool:
    """2x upscale and keep only bright pixels, as black on white."""
    if not shutil.which("ffmpeg"):
        return False
    proc = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-vf",
         "scale=iw*2:-2:flags=lanczos,format=gray,"
         "lutyuv=y='if(gt(val,190),0,255)'", str(dst)],
        capture_output=True, timeout=timeout)
    return proc.returncode == 0 and dst.exists()


def _tesseract(path: Path, timeout: int) -> str:
    proc = subprocess.run(["tesseract", str(path), "stdout", "-l", TESS_LANGS,
                           "--psm", "11", "tsv"],
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise OCRError((proc.stderr or "tesseract failed")[:200])
    return proc.stdout


def _read_tesseract(paths: list[Path], *, min_confidence: float,
                    timeout: int) -> list[FrameText]:
    if not shutil.which("tesseract"):
        raise OCRError("tesseract not found; apt install tesseract-ocr "
                       "tesseract-ocr-hrv")
    out = []
    with tempfile.TemporaryDirectory(prefix="corpus-ocr-") as tmp:
        for p in paths:
            if not p.exists():
                out.append(FrameText(p, error="missing"))
                continue
            try:
                lines = _tsv_lines(_tesseract(p, timeout), TESS_MIN_WORD_CONF)
                bright = Path(tmp) / (p.stem + ".png")
                if _isolate_bright(p, bright, timeout):
                    lines += _tsv_lines(_tesseract(bright, timeout),
                                        TESS_MIN_WORD_CONF)
            except (OCRError, subprocess.TimeoutExpired) as e:
                out.append(FrameText(p, error=str(e) or "timeout"))
                continue
            out.append(FrameText(p, lines=[l for l in lines
                                           if l.confidence >= min_confidence]))
    return out


def read(paths, *, min_confidence: float = MIN_CONFIDENCE,
         batch: int = 64, timeout: int = 300) -> list[FrameText]:
    """OCR a batch of images. Never raises for one bad frame."""
    paths = [Path(p) for p in paths]
    if not paths:
        return []
    if not _darwin():
        return _read_tesseract(paths, min_confidence=min_confidence,
                               timeout=min(timeout, 60))
    binary = _ensure_binary()

    out: list[FrameText] = []
    for i in range(0, len(paths), batch):
        chunk = [str(p) for p in paths[i:i + batch]]
        try:
            proc = subprocess.run([str(binary), *chunk], capture_output=True,
                                  text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            out += [FrameText(Path(p), error="timeout") for p in chunk]
            continue

        seen = set()
        for raw in proc.stdout.splitlines():
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                continue
            p = Path(d.get("path", ""))
            seen.add(str(p))
            if d.get("error"):
                out.append(FrameText(p, error=d["error"]))
                continue
            lines = [Line(l["text"].strip(), float(l.get("confidence", 0)))
                     for l in d.get("lines", [])
                     if l.get("text", "").strip()
                     and float(l.get("confidence", 0)) >= min_confidence]
            out.append(FrameText(p, lines=lines))
        # A frame the binary never reported on is a failure, not an absence.
        for p in chunk:
            if p not in seen:
                out.append(FrameText(Path(p), error="no output"))
    return out


def merge(frames: list[FrameText], *, min_words: int = 2) -> str:
    """Fold per-frame lines into one readable block.

    Two things to undo. Frames sampled across a video repeat the same caption, so
    duplicates collapse. And a caption animating in appears truncated in earlier
    frames, so a line that is a prefix of a longer one is dropped in favour of the
    longer one.
    """
    seen: list[str] = []
    for f in frames:
        for line in f.lines:
            t = " ".join(line.text.split())
            if not t or len(_WORD.findall(t)) < min_words:
                continue
            low = t.lower()
            replaced = False
            for i, existing in enumerate(seen):
                e = existing.lower()
                if low == e:
                    replaced = True
                    break
                if low.startswith(e) or e in low:
                    # The longer rendering of the same line wins.
                    seen[i] = t if len(t) > len(existing) else existing
                    replaced = True
                    break
                if e.startswith(low):
                    replaced = True
                    break
            if not replaced:
                seen.append(t)
    return "\n".join(seen)


def read_item_frames(paths, **kw) -> tuple[str, dict]:
    """OCR one item's keyframes and return (merged text, stats)."""
    frames = read(paths, **kw)
    ok = [f for f in frames if f.error is None]
    text = merge(ok)
    return text, {
        "frames": len(frames),
        "failed": len(frames) - len(ok),
        "lines": sum(len(f.lines) for f in ok),
        "chars": len(text),
    }


if __name__ == "__main__":       # tiny manual probe: python -m corpus.enrich.ocr img...
    txt, stats = read_item_frames(sys.argv[1:])
    print(json.dumps(stats))
    print(txt)
