"""The enrichment pass: transcripts and keyframes for stored items.

This is a separate layer on purpose. Fetching populates the corpus; enrichment is
optional, resumable, and can be run months later over a selected slice. Nothing in
the fetch path depends on it, and skipping it entirely leaves a perfectly usable
corpus of text and metrics.

Resumability is structural rather than a queue: an item needs enrichment iff it has
video media and no transcript. Kill the process at any point and the next run picks
up exactly where it stopped, because the work-list is derived from the data, not from
a job table that could drift out of sync with it.
"""
from __future__ import annotations

import hashlib
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, field
from pathlib import Path

from .. import store
from . import media as m
from . import ocr
from . import transcribe as tx

VIDEO_KINDS = ("short_video", "long_video")

# Roles that are still images and worth keeping a local copy of. Video originals are
# deliberately excluded: those get transcribed and keyframed, then discarded.
IMAGE_ROLES = ("slide", "poster", "thumbnail")

# How long an interrupt waits for in-flight downloads to land before
# giving up on them. Long enough for an HTTP response already on the wire,
# short enough that Ctrl-C still feels like Ctrl-C.
DRAIN_GRACE_S = 5.0

# Stills are pure HTTP latency with no CPU behind them, and they scale almost
# linearly: measured on 24 warm stills, 5.03s serial -> 1.01s at 8 -> 0.54s at 12.
# 8 is the conservative end of that curve; the CDN showed no contention at any point.
IMAGE_WORKERS = 8

# Video is a different animal and the measurements say so. Download parallelises 2x,
# but yap tops out at ~1.5x (SpeechAnalyzer lives in one shared system XPC service)
# and ffmpeg at ~1.4x, so the whole item pipeline measured 27.3s serial, 19.3s at 3
# workers (1.42x), and 32.3s at 4 -- worse than serial. Above that transcription time
# inflated 9s -> 46s. This is a real ceiling, not a guess, so it is enforced rather
# than documented and ignored.
MAX_VIDEO_WORKERS = 3

# How many times an item may fail transcription before it stops being offered.
# Without this a failure writes nothing at all, so `transcript IS NULL` still holds
# and the next run re-downloads and re-transcribes the same broken clip forever.
MAX_ENRICH_ATTEMPTS = 3

# Consecutive transcriber outages after which the rest of the run is skipped
# rather than downloaded and failed one by one.
OUTAGE_STOP_AFTER = 3


@dataclass
class EnrichStats:
    considered: int = 0
    transcribed: int = 0
    frames: int = 0
    expired: int = 0
    failed: int = 0
    skipped_no_media: int = 0
    images: int = 0
    # Items the transcriber could not take (server down, timeout, 429): not
    # counted as attempts, but still a failed run.
    unavailable: int = 0
    image_failed: int = 0
    no_transcriber: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.failed or self.unavailable or self.image_failed
                    or self.no_transcriber)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["errors"] = self.errors[:10]
        return d


_EN_WORDS = (" the ", " and ", " to ", " of ", " is ", " you ", " for ", " with ",
             " our ", " your ", " in ", " this ", " are ", " we ")


def _english(text: str) -> bool:
    low = f" {text.lower()} "
    return sum(1 for w in _EN_WORDS if w in low) >= 2


def account_languages(conn, account_ids) -> dict:
    """{account_id: language code} for accounts whose captions are mostly not English.

    The fallback for a reel whose own caption says nothing ("⚽ <player name>
    #<ClubTag>"). Whisper's own detection is not one: large-v3-turbo heard
    Croatian football commentary as English and returned a fluent translation
    ("A chance for <player>, but <player> didn't reach the top"), a football club
    account, 2026-10-23.
    The account's other captions are the better evidence.
    """
    out = {}
    for aid in account_ids:
        if aid is None:
            continue
        votes: dict = {}
        n = 0
        for (body,) in conn.execute(
                "SELECT body FROM item WHERE account_id=? AND body IS NOT NULL "
                "AND length(body) >= 12 ORDER BY published_at DESC LIMIT 60", (aid,)):
            code = tx.detect_language(body)
            if code is None and _english(body):
                code = "en"
            if code in (None, "auto"):
                continue        # emoji, names and hashtags: no vote either way
            n += 1
            votes[code] = votes.get(code, 0) + 1
        best = max(votes, key=votes.get) if votes else None
        if best and best != "en" and votes[best] * 2 > n:
            out[aid] = best
    return out


def pending(conn, *, account_id: int | None = None, limit: int | None = None,
            redo: bool = False) -> list[dict]:
    """Items that still want a transcript, newest first.

    Newest first because recent content is what a client audit talks about; a
    half-finished backfill is then still useful rather than an arbitrary slice.
    """
    where = ["i.kind IN (%s)" % ",".join("?" * len(VIDEO_KINDS)), "i.deleted_at IS NULL"]
    args: list = list(VIDEO_KINDS)
    if not redo:
        # A video whose transcript was REJECTED (music only, hallucinated output)
        # is indistinguishable from one never attempted if both store NULL, so it
        # gets re-downloaded and re-transcribed on every run, forever. The lang
        # sentinel records "we looked, there was no speech".
        where.append("(i.transcript IS NULL OR i.transcript = '')")
        where.append("COALESCE(i.lang, '') <> 'none'")
        where.append("COALESCE(i.enrich_attempts, 0) < ?")
        args.append(MAX_ENRICH_ATTEMPTS)
    if account_id is not None:
        where.append("i.account_id = ?")
        args.append(account_id)

    # state <> 'failed' is the same filter pending_images already has, and its
    # absence here made the ExpiredURL handler inert: the handler marks the original
    # failed, this subquery handed the same dead URL straight back, it 403'd, and the
    # loop repeated every run. IG video URLs die at 32-36h and enrich is a separate
    # command from sync, so that window is reached routinely. Excluding the failed row
    # also means a re-sync's freshly minted URL (a new media row) is the one picked up.
    sql = f"""
        SELECT i.id, i.account_id, i.platform, i.platform_item_id, i.kind, i.url,
               i.duration_s, i.body, i.lang,
               (SELECT remote_url FROM media
                 WHERE item_id = i.id AND role = 'original' AND state <> 'failed'
                 ORDER BY seq LIMIT 1) AS video_url
          FROM item i
         WHERE {' AND '.join(where)}
         ORDER BY i.published_at DESC
    """
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]


def _store_frames(conn, item_id: int, frames, media_root: Path) -> int:
    """Persist keyframes content-addressed, replacing any previous set.

    Takes frames as (seq, t_offset_s, bytes), not as paths: the worker's temp
    workspace is deleted the moment its `with` block exits, and this runs later, on
    the writer thread. Reading the bytes out before the workspace closes is exactly
    what lets the download-and-decode half run concurrently.
    """
    old = conn.execute(
        "SELECT local_path, sha256 FROM media WHERE item_id=? AND role='keyframe' "
        "AND local_path IS NOT NULL", (item_id,)).fetchall()
    conn.execute("DELETE FROM media WHERE item_id=? AND role='keyframe'", (item_id,))
    n = 0
    for seq, t_offset_s, blob in frames:
        digest = hashlib.sha256(blob).hexdigest()
        dest = media_root / digest[:2] / digest[2:4] / f"{digest}.{m.FRAME_EXT}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            dest.write_bytes(blob)
        conn.execute(
            """INSERT INTO media (item_id, role, seq, mime, local_path, sha256,
                                  t_offset_s, state, created_at)
               VALUES (?,'keyframe',?,?,?,?,?, 'stored', ?)""",
            (item_id, seq, f"image/{m.FRAME_EXT}", m.to_stored(dest, media_root),
             digest, t_offset_s, store.now()),
        )
        n += 1
    _unlink_unreferenced(conn, old, media_root)
    return n


def _unlink_unreferenced(conn, rows, media_root: Path) -> int:
    """Delete the files of replaced media rows that no remaining row points at.

    A --redo re-keyframes an item and its old frames used to stay on disk forever,
    unreferenced. Files are content-addressed and can be shared (two identical
    black frames on two items), so a file goes only when no row still carries its
    digest.
    """
    n = 0
    for r in rows:
        if r["sha256"] and conn.execute(
                "SELECT 1 FROM media WHERE sha256=? LIMIT 1", (r["sha256"],)).fetchone():
            continue
        p = m.resolve(r["local_path"], media_root)
        try:
            if p is not None and p.exists():
                p.unlink()
                n += 1
        except OSError:
            pass
    return n


def pending_images(conn, *, account_id: int | None = None,
                   limit: int | None = None) -> list[dict]:
    """Vendor-hosted stills we have a URL for but no local copy.

    Both platforms sign their image URLs and expire them, so an un-downloaded
    reference is a broken image in whatever document quotes it later. LinkedIn
    carousel slides and post images live here, which is why a LinkedIn post had no
    picture in the brief despite the vendor returning one.
    """
    # role='original' is the post's own media, which is an image on an image or
    # carousel post and an mp4 playlist on a video. Selecting it by item kind rather
    # than by sniffing the URL keeps this true on both platforms; on a video the
    # poster and keyframe roles already supply the picture. Without this, 49 of
    # a client's 58 LinkedIn posts rendered with no image at all.
    where = ["m.local_path IS NULL", "m.remote_url IS NOT NULL",
             "m.state <> 'failed'",
             "(m.role IN (%s) OR (m.role = 'original' AND i.kind IN "
             "('image', 'carousel')))" % ",".join("?" * len(IMAGE_ROLES))]
    args: list = list(IMAGE_ROLES)
    if account_id is not None:
        where.append("i.account_id = ?")
        args.append(account_id)
    sql = f"""SELECT m.id, m.item_id, m.role, m.seq, m.remote_url
                FROM media m JOIN item i ON i.id = m.item_id
               WHERE {' AND '.join(where)}
               ORDER BY m.item_id DESC, m.seq"""
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]


def _download_still(row: dict) -> tuple[dict, bytes | None, Exception | None]:
    """Fetch one still into memory. Worker half: no conn, no writes, never raises."""
    try:
        with tempfile.TemporaryDirectory(prefix="corpus-img-") as td:
            tmp = Path(td) / "i"
            m.download(row["remote_url"], tmp)
            return row, tmp.read_bytes(), None
    except m.MediaError as e:     # ExpiredURL is a subclass; the writer sorts them
        return row, None, e


def fetch_images(conn, *, media_root, account_id=None, limit=None,
                 workers: int = IMAGE_WORKERS, rows=None) -> dict:
    """Download and content-address stills. Idempotent; safe to re-run.

    Best effort-to-payoff change in the pipeline: pure HTTP latency, no CPU, runs at
    the head of every enrich invocation, and measured 5.03s -> 0.54s across 24 warm
    stills (9.3x at 12 workers) with no contention at any width. The database side is
    one UPDATE per still and stays on the calling thread.
    """
    media_root = Path(media_root).expanduser()
    rows = (pending_images(conn, account_id=account_id, limit=limit)
            if rows is None else rows)
    got = expired = failed = 0

    def write(row, blob, err):
        """Owner thread only."""
        nonlocal got, expired, failed
        if err is not None:
            if isinstance(err, m.ExpiredURL):
                conn.execute("UPDATE media SET state='failed' WHERE id=?", (row["id"],))
                conn.commit()
                expired += 1
            else:
                failed += 1
            return
        digest = hashlib.sha256(blob).hexdigest()
        dest = media_root / digest[:2] / digest[2:4] / f"{digest}.jpg"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            dest.write_bytes(blob)
        conn.execute(
            "UPDATE media SET local_path=?, sha256=?, bytes=?, state='stored' WHERE id=?",
            (m.to_stored(dest, media_root), digest, len(blob), row["id"]))
        # Commit per still, not once at the end: an interrupt part way through 254
        # downloads has to keep the ones already fetched.
        conn.commit()
        got += 1

    if workers <= 1 or len(rows) <= 1:
        for r in rows:
            write(*_download_still(r))
    else:
        pool = ThreadPoolExecutor(max_workers=min(workers, len(rows)))
        futures = [pool.submit(_download_still, r) for r in rows]
        try:
            for f in as_completed(futures):
                write(*f.result())
        except BaseException:
            # as_completed yields in completion order, so a future that RAISES can be
            # handed over while successes are still in flight. Those downloads are
            # already paid for and land milliseconds later, so a snapshot of
            # f.done() right here writes nothing. Give the in-flight work a bounded
            # grace period, then persist whatever finished.
            #
            # Bounded, not unbounded: this path runs on Ctrl-C, and a second Ctrl-C
            # during the grace period should still get out.
            done, _ = wait(futures, timeout=DRAIN_GRACE_S)
            for f in done:
                if f.cancelled() or f.exception() is not None:
                    continue
                try:
                    write(*f.result())
                except Exception:
                    pass
            raise
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    conn.commit()
    return {"considered": len(rows), "stored": got, "expired": expired,
            "failed": failed}


@dataclass
class ItemWork:
    """What a worker produced for one item, in a form that outlives its temp dir.

    Frames are (seq, t_offset_s, bytes). Carrying paths instead would be a
    use-after-free: VideoWorkspace deletes the whole directory on exit, and the
    point of the split is that the writer runs after that.
    """
    row: dict
    outcome: str                 # no_media | expired | failed:... | ok
    transcript: object = None
    duration_s: float | None = None
    frames: list = field(default_factory=list)


def _stored_lang(row: dict) -> str | None:
    """The item's stored language, when it is one. "music" and "none" are enrich's
    own sentinels (handed to whisper as a language they failed the clip), and on a
    --redo the stored value is the previous transcription's guess, which is what is
    being redone: a football club's reel transcribed as English stayed English."""
    v = row.get("lang")
    if row.get("redo") or not v or v in ("music", "none", "auto"):
        return None
    return v


def fetch_item(row: dict, *, media_root: Path, locale: str, frames: int,
               keep_audio: bool, allow_cloud: bool = True,
               language: str | None = None, stop=None) -> ItemWork:
    """Download, transcribe and keyframe one item. Worker half: no conn.

    Everything expensive lives here. The measured split for a mixed batch is 18-33%
    network and the rest ffmpeg + SpeechAnalyzer, which is why the pool this runs in
    is capped at 3: the network part scales 2x but yap only 1.5x and ffmpeg 1.4x.
    """
    url = row.get("video_url")
    if not url:
        return ItemWork(row, "no_media")
    if stop is not None and stop.is_set():
        # The transcriber went away mid-run; downloading the rest of the backlog
        # only to fail each one is minutes of bandwidth for nothing.
        return ItemWork(row, "unavailable:skipped, transcriber unreachable")

    try:
        with m.VideoWorkspace(url) as ws:
            # The caption is the only pre-transcription language signal, and
            # getting this wrong yields invented text rather than an error.
            # A silent clip (no audio stream: common on Instagram) is "no speech",
            # not a failure: whisper answered 500, Groq 400, and each such reel
            # burned its three attempts on every run (field test, 2026-10-23).
            if m.has_audio(ws.video) is False:
                transcript = tx.Transcript(text="", segments=[], locale=None)
            else:
                transcript = tx.transcribe_auto(
                    ws.video, locale=locale, allow_cloud=allow_cloud,
                    language=language or _stored_lang(row),
                    hint_text=row.get('body'),
                    fallback_lang=row.get('account_lang'))
            # One ffprobe, not two: keyframes() needs the duration anyway and used
            # to compute its own, so every item paid for the same 65ms spawn twice.
            dur = m.probe_duration(ws.video)
            kfs = m.keyframes(ws.video, Path(ws.video).parent / "frames",
                              count=frames, duration=dur) if frames else []

            if keep_audio:
                digest = hashlib.sha256(str(row["id"]).encode()).hexdigest()
                audio_dest = media_root / "audio" / f"{digest[:16]}.opus"
                try:
                    m.archive_audio(ws.video, audio_dest)
                except m.MediaError:
                    pass  # insurance, not a requirement

            # Read the frame bytes out BEFORE the workspace closes. This single line
            # is what makes the stage parallelisable at all.
            blobs = [(kf.seq, kf.t_offset_s, kf.path.read_bytes()) for kf in kfs]

    except m.ExpiredURL:
        return ItemWork(row, "expired")
    except m.SourceBlocked as e:
        return ItemWork(row, f"blocked:{e}")
    except tx.TranscribeUnavailable as e:
        # The engine was down, not the clip broken: see store_item.
        return ItemWork(row, f"unavailable:{e}")
    except (m.MediaError, tx.TranscribeError, OSError) as e:
        return ItemWork(row, f"failed:{type(e).__name__}:{e}")

    return ItemWork(row, "ok", transcript=transcript, duration_s=dur, frames=blobs)


def store_item(conn, work: ItemWork, *, media_root: Path) -> str:
    """Write one item's enrichment. Owner thread only; SQLite has one writer."""
    row = work.row
    if work.outcome == "no_media":
        return "no_media"

    if work.outcome == "expired":
        # The signed URL died. Re-running the fetch adapter re-mints a fresh window,
        # so flag it and move on rather than burning retries against a dead link.
        conn.execute(
            "UPDATE media SET state='failed' WHERE item_id=? AND role='original'",
            (row["id"],),
        )
        conn.commit()
        return "expired"

    if work.outcome.startswith(("unavailable", "blocked")):
        # NOT an attempt. (blocked: the platform refused this machine, e.g. a
        # YouTube bot check; the clip is fine and is retried next run.) A night with the whisper server down used to cost every
        # pending reel one of its three attempts, and three such nights retired the
        # whole backlog permanently with nothing wrong with any video.
        return work.outcome

    if work.outcome.startswith("failed"):
        # Count the attempt. Without this the failure writes nothing, transcript
        # stays NULL, and the item is re-downloaded and re-transcribed on every
        # future run with the same result.
        conn.execute(
            "UPDATE item SET enrich_attempts = COALESCE(enrich_attempts,0) + 1 "
            "WHERE id=?", (row["id"],))
        conn.commit()
        return work.outcome

    transcript, dur = work.transcript, work.duration_s
    n_frames = _store_frames(conn, row["id"], work.frames, media_root) \
        if work.frames else 0

    # Writing transcript through the normal UPDATE path is what keeps FTS in step:
    # the item_au trigger reindexes the row, so a transcript added long after the
    # item was fetched becomes searchable immediately.
    conn.execute(
        "UPDATE item SET transcript=?, duration_s=COALESCE(duration_s, ?), "
        "lang=? WHERE id=?",
        (transcript.text or None, dur,
         # "none" marks a deliberate no-speech outcome so pending() can skip it.
         ("music" if (transcript.text
                      and tx.looks_like_lyrics(transcript.text, dur))
          else transcript.locale) if transcript.text else "none",
         row["id"]),
    )
    conn.commit()
    return f"ok:{n_frames}"


def pending_ocr(conn, *, account_id: int | None = None,
                limit: int | None = None, redo: bool = False) -> list[dict]:
    """Items with stored keyframes but no screen text read off them yet."""
    where = ["m.role = 'keyframe'", "m.local_path IS NOT NULL"]
    args: list = []
    if not redo:
        # NULL = never read; '' = read, nothing on screen. Re-reading the '' ones
        # cost the first server nightly 70s of tesseract for zero new text, every
        # night, forever.
        where.append("i.screen_text IS NULL")
    if account_id is not None:
        where.append("i.account_id = ?")
        args.append(account_id)
    sql = f"""SELECT i.id, GROUP_CONCAT(m.local_path, '\n') paths
                FROM item i JOIN media m ON m.item_id = i.id
               WHERE {' AND '.join(where)}
               GROUP BY i.id ORDER BY i.published_at DESC"""
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]


def read_screen_text(conn, *, account_id=None, limit=None, redo=False,
                     media_root=None) -> dict:
    """Read burned-in text off stored keyframes.

    Separate from enrich_item because it needs no network and no video: the frames
    are already on disk, so this can be re-run over the whole corpus for the price of
    some CPU. On a music-bed reel this is the only place the post's argument exists.
    """
    if not ocr.available():
        return {"considered": 0, "read": 0, "skipped": "on-device OCR needs macOS"}

    rows = pending_ocr(conn, account_id=account_id, limit=limit, redo=redo)
    read = chars = 0
    error = None
    for r in rows:
        paths = [str(f) for f in (m.resolve_existing(p, media_root)
                                  for p in (r["paths"] or "").split("\n") if p)
                 if f is not None]
        if not paths:
            continue
        try:
            text, _ = ocr.read_item_frames(paths)
        except ocr.OCRError as e:
            # A broken toolchain fails every item, so stop early, but say so: this
            # used to break silently and report "read=0" as if nothing needed it.
            error = f"OCR failed, stopped: {e}"
            break
        conn.execute("UPDATE item SET screen_text=? WHERE id=?", (text or "", r["id"]))
        conn.commit()
        if not text:
            continue
        read += 1
        chars += len(text)
    out = {"considered": len(rows), "read": read, "chars": chars}
    if error:
        out["error"] = error
    return out


def enrich_item(conn, row: dict, *, media_root: Path, locale: str,
                frames: int, keep_audio: bool, allow_cloud: bool = True,
                language: str | None = None) -> str:
    """Fetch + store one item serially. Returns an outcome string; never raises."""
    work = fetch_item(row, media_root=media_root, locale=locale, frames=frames,
                      keep_audio=keep_audio, allow_cloud=allow_cloud,
                      language=language)
    return store_item(conn, work, media_root=media_root)


def run(conn, *, media_root: str | Path = "~/.corpus/media",
        account_id: int | None = None, limit: int | None = None,
        locale: str = tx.DEFAULT_LOCALE, workers: int = MAX_VIDEO_WORKERS,
        image_workers: int = IMAGE_WORKERS,
        frames: int = m.DEFAULT_FRAMES, keep_audio: bool = False,
        redo: bool = False, dry_run: bool = False,
        allow_cloud: bool = True, language: str | None = None) -> EnrichStats:
    """Enrich pending items.

    `workers` fans out fetch_item across items and is capped at MAX_VIDEO_WORKERS.
    The cap is measured, not cautious: 27.3s serial -> 19.3s at 3 (1.42x) -> 32.3s
    at 4, which is worse than serial, because yap and ffmpeg are both already using
    the cores. Downloading is the part that actually parallelises, and at 3 workers
    there are enough of them in flight to cover the CPU stages.

    `image_workers` is separate and much higher because stills are pure network.
    """
    media_root = Path(media_root).expanduser()
    media_root.mkdir(parents=True, exist_ok=True)

    rows = pending(conn, account_id=account_id, limit=limit, redo=redo)
    langs = account_languages(conn, {r.get("account_id") for r in rows})
    for r in rows:
        r["account_lang"] = langs.get(r.get("account_id"))
        r["redo"] = redo
    stats = EnrichStats(considered=len(rows))

    if dry_run:
        with_media = sum(1 for r in rows if r.get("video_url"))
        stats.skipped_no_media = len(rows) - with_media
        # Local transcription has no per-item cost; the honest estimate is time.
        stats.errors.append(
            f"dry-run: {with_media} item(s) would be transcribed locally at $0, "
            f"roughly {with_media * 3}s at ~30x realtime for a 60s clip; "
            f"plus {len(pending_images(conn, account_id=account_id))} "
            f"still image(s) to download"
        )
        return stats

    if workers > MAX_VIDEO_WORKERS:
        # Say so rather than silently obeying. A knob that quietly makes things
        # slower is how someone concludes fan-out does not help.
        stats.errors.append(
            f"capped workers {workers} -> {MAX_VIDEO_WORKERS}: measured 1.42x at 3 "
            f"and 0.85x at 4, transcription time inflates 9s -> 46s above that")
        workers = MAX_VIDEO_WORKERS

    from .. import ops
    ops.sweep_tmp()

    run_id = store.start_run(conn, adapter="enrich", mode="enrich",
                             account_id=account_id)
    # Stills first: cheap, fast, and what makes a post visible in a brief.
    img = fetch_images(conn, media_root=media_root, account_id=account_id,
                       workers=image_workers)
    stats.images = img["stored"]
    stats.expired += img["expired"]
    if img.get("failed"):
        stats.image_failed = img["failed"]
        stats.errors.append(f"{img['failed']} still image(s) failed to download "
                            f"(network or server error, will retry next run)")

    # Ping the self-hosted server(s) once, up front. A dead host otherwise costs a
    # connect timeout per item, and each of those used to count against the item.
    if rows:
        for url, why in tx.ping_whisper().items():
            if why:
                stats.errors.append(f"whisper server {url} unreachable ({why}); "
                                    f"skipped for this run")

    # Only now: stills need no transcriber, and returning before fetch_images is
    # how a Linux box with no yap used to enrich nothing at all. Whisper (self-hosted
    # or Groq) is a full transcriber on its own.
    if rows and not tx.available() and not tx.whisper_available():
        stats.no_transcriber = True
        stats.errors.append("no transcriber reachable: install yap (macOS 26+), or "
                            "set WHISPER_SERVER_URL / GROQ_API_KEY in ~/.reach/keys.env"
                            " (and check the whisper server is up)")
        store.finish_run(conn, run_id, status="error", seen=len(rows), new=0,
                         error=stats.errors[-1])
        return stats

    stop = threading.Event()
    outages = 0

    def tally(work: ItemWork) -> None:
        """Write and count one result. Owner thread only."""
        nonlocal outages
        outcome = store_item(conn, work, media_root=media_root)
        if outcome == "no_media":
            stats.skipped_no_media += 1
        elif outcome == "expired":
            stats.expired += 1
        elif outcome.startswith("ok:"):
            stats.transcribed += 1
            stats.frames += int(outcome.split(":")[1])
            outages = 0
        elif outcome.startswith("blocked"):
            # Counted as missing, but not as a transcriber outage: the whisper
            # server is fine, and stopping would also skip every Instagram reel.
            stats.unavailable += 1
            if not any(e.startswith("source blocked") for e in stats.errors):
                stats.errors.append(f"source blocked (retried next run, no attempt "
                                    f"used): {outcome[8:200]}")
        elif outcome.startswith("unavailable"):
            stats.unavailable += 1
            outages += 1
            if outages >= OUTAGE_STOP_AFTER and not stop.is_set():
                stop.set()
                stats.errors.append(
                    f"transcriber unavailable {outages} times in a row; stopped "
                    f"(remaining items keep their attempts): {outcome[12:200]}")
            elif not stop.is_set():
                stats.errors.append(f"item {work.row['id']}: {outcome[:200]}")
        else:
            stats.failed += 1
            stats.errors.append(f"item {work.row['id']}: {outcome}")

    kw = dict(media_root=media_root, locale=locale, frames=frames,
              keep_audio=keep_audio, allow_cloud=allow_cloud, language=language,
              stop=stop)
    try:
        if workers <= 1 or len(rows) <= 1:
            for row in rows:
                tally(fetch_item(row, **kw))
        else:
            pool = ThreadPoolExecutor(max_workers=min(workers, len(rows)))
            try:
                futs = [pool.submit(fetch_item, row, **kw) for row in rows]
                # as_completed, and each result written the moment it lands: an
                # interrupt keeps every transcript already produced.
                for f in as_completed(futs):
                    tally(f.result())
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        # 'ok' only when nothing failed. Recording 'ok' over a run where every item
        # failed is how a dead transcriber stayed invisible for days.
        bad = stats.failed + stats.unavailable + stats.image_failed
        store.finish_run(conn, run_id, status="error" if bad else "ok",
                         seen=stats.considered, changed=stats.transcribed,
                         error=(f"{stats.failed} failed, {stats.unavailable} "
                                f"transcriber outage(s), {stats.image_failed} image "
                                f"download(s) failed; first: "
                                f"{(stats.errors or [''])[0][:300]}") if bad else None)
    except BaseException as e:
        # BaseException so Ctrl-C closes the run row too. A run left 'running'
        # forever is indistinguishable from one still in progress.
        store.finish_run(conn, run_id,
                         status="interrupted" if isinstance(e, KeyboardInterrupt)
                         else "error",
                         seen=stats.considered, changed=stats.transcribed,
                         error=None if isinstance(e, KeyboardInterrupt) else str(e))
        raise
    return stats
