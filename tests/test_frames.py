"""Frame-selection tests. The point of this module is NOT sending frames."""
import subprocess, sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from corpus.enrich import frames as F  # noqa: E402

HAS_FFMPEG = __import__("shutil").which("ffmpeg") is not None


def _frame(tmp, name, src):
    out = tmp / name
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src,
                    "-frames:v", "1", str(out)], check=True, capture_output=True)
    return out


# ── stage 1: the cheap gate that eliminates most items ─────────────────────

@pytest.mark.parametrize("item,expected,why", [
    ({"kind": "short_video", "body": "x" * 80, "transcript": "y" * 80},
     False, "both layers already carry the message"),
    ({"kind": "short_video", "body": "x" * 80, "transcript": "", "lang": "music"},
     True, "a music bed means the argument is on screen"),
    ({"kind": "short_video", "body": "", "transcript": ""},
     True, "nothing to read at all"),
    ({"kind": "short_video", "body": "x" * 80, "transcript": ""},
     True, "video with no transcript"),
    ({"kind": "text", "body": "x" * 80, "transcript": ""},
     False, "not visual content"),
    ({"kind": "carousel", "body": "", "transcript": ""},
     True, "a captionless carousel is only readable by looking"),
])
def test_needs_vision(item, expected, why):
    assert bool(F.needs_vision(item)) is expected, why


def test_skip_reason_is_always_given():
    n = F.needs_vision({"kind": "text", "body": "hi", "transcript": ""})
    assert n.reason and not n


# ── stage 2: near-duplicate collapse ───────────────────────────────────────

@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_identical_frames_collapse_to_one(tmp_path):
    a = _frame(tmp_path, "a.jpg", "color=c=blue:s=320x240")
    b = _frame(tmp_path, "b.jpg", "color=c=blue:s=320x240")
    assert len(F.dedupe_frames([a, b])) == 1


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_different_shots_are_kept(tmp_path):
    a = _frame(tmp_path, "a.jpg", "testsrc=s=320x240")
    b = _frame(tmp_path, "b.jpg", "color=c=black:s=320x240")
    assert len(F.dedupe_frames([a, b])) == 2


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_unreadable_frame_is_kept_not_dropped(tmp_path):
    """Unknown must never mean 'skip': a missed on-screen message is the failure."""
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    assert F.dedupe_frames([bad]) == [bad]


# ── stage 3: text detection ────────────────────────────────────────────────

@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_flat_colour_has_no_text(tmp_path):
    assert F.edge_density(_frame(tmp_path, "f.jpg", "color=c=gray:s=320x240")) < 0.02


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_structured_frame_reads_as_worth_looking_at(tmp_path):
    """testsrc scores 0.043; real keyframes run p25=0.047, p50=0.097."""
    assert F.likely_has_text(_frame(tmp_path, "t.jpg", "testsrc=s=320x240"))


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_threshold_sits_below_the_real_first_quartile(tmp_path):
    """Calibration guard: a flat field must fail, a structured one must pass."""
    flat = F.edge_density(_frame(tmp_path, "flat2.jpg", "color=c=gray:s=320x240"))
    busy = F.edge_density(_frame(tmp_path, "busy2.jpg", "testsrc=s=320x240"))
    assert flat < F.TEXT_EDGE_THRESHOLD <= busy


def test_missing_file_errs_toward_looking(tmp_path):
    assert F.likely_has_text(tmp_path / "nope.jpg") is True


# ── the plan ───────────────────────────────────────────────────────────────

def test_plan_skips_when_text_suffices():
    p = F.plan_for_item({"id": 1, "kind": "short_video",
                         "body": "x" * 80, "transcript": "y" * 80}, ["a.jpg"])
    assert p.frames == [] and p.skipped_reason and p.cost_estimate_usd == 0


def test_plan_caps_frames(tmp_path):
    if not HAS_FFMPEG:
        pytest.skip("needs ffmpeg")
    paths = [_frame(tmp_path, f"{i}.jpg", f"testsrc=s=320x240:d=1,hue=h={i*60}")
             for i in range(6)]
    p = F.plan_for_item({"id": 1, "kind": "short_video", "body": "",
                         "transcript": "", "lang": "music"}, paths, max_frames=3)
    assert len(p.frames) <= 3


def test_plan_with_no_frames_says_so():
    p = F.plan_for_item({"id": 1, "kind": "short_video", "body": "",
                         "transcript": "", "lang": "music"}, [])
    assert p.skipped_reason == "no frames extracted"


def test_summarise_reports_the_reduction():
    plans = [F.VisionPlan(item_id=1, considered=6,
                          frames=[F.FrameChoice(Path("a"), "r")]),
             F.VisionPlan(item_id=2, considered=6, skipped_reason="text suffices")]
    s = F.summarise(plans)
    assert s["frames_considered"] == 12 and s["frames_selected"] == 1
    assert s["reduction"] == "92%"
