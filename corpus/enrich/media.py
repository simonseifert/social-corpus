"""Fetch media, pull keyframes, then throw the video away.

The storage policy is extract-and-discard, the same one ig-saves already runs: once
you hold the transcript plus a handful of keyframes, re-fetching the original adds
almost nothing -- a vision model consumes frames, not H.264. 5,000 reels kept as
transcript + frames + poster is roughly 1.1 GB; kept as 720p video it is about 47 GB.

The other thing shaping this module is CDN expiry. Instagram signs its media URLs and
they die: measured across 157 fresh URLs, video windows ran 32.0-35.9h (mean 33.8h)
and images 104.0-107.9h (mean 105.9h). You cannot extend them client-side -- stripping
or tampering with the `oh`/`oe` params returns 403.

What that does NOT mean is that enrichment has to run in the same pass as the fetch.
Re-requesting the item's metadata re-mints a full window regardless of how old the
post is (a 2019 post returned a window of now + 106.7h). So a stale URL is a cheap
re-resolve, not lost data. That is why `remote_url` is treated as a cache here and a
403 marks the row for re-resolution instead of failing the item.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}

FRAME_WIDTH = 480

# JPEG, not WebP. The Homebrew ffmpeg on this machine ships without a webp encoder
# ("Automatic encoder selection failed ... codec webp is probably disabled"), and
# ig-saves already writes .jpg frames, so this keeps the two pipelines consistent.
# mjpeg quality is -q:v on a 2..31 scale where LOWER is better; 4 is visually clean
# at 480px and lands around 30-45 KB a frame.
FRAME_EXT = "jpg"
FRAME_QSCALE = 4
DEFAULT_FRAMES = 6


class MediaError(RuntimeError):
    pass


# ── where stored files live ─────────────────────────────────────────────────
#
# media.local_path used to hold absolute paths: ~/.corpus/media/ab/cd/... expanded to
# an absolute home path on a Mac, and 75 rows under /tmp/<user>-corpus/media from a test run that pointed
# at a temp root and is long gone. Neither survives the corpus moving machines, and
# a dangling path is worse than none: the row claims a stored file, so nothing ever
# fetches the image again and the brief silently shows no picture. Paths are now
# stored RELATIVE to the media root and resolved on read, through these two helpers
# and nowhere else.

LEGACY_MARKER = "/.corpus/media/"


def media_root(root: str | Path | None = None) -> Path:
    """The media root: an explicit one, else $CORPUS_MEDIA, else ~/.corpus/media.
    Read at call time so a test (or the dashboard) setting the env var wins."""
    return Path(root or os.environ.get("CORPUS_MEDIA") or "~/.corpus/media").expanduser()


def to_stored(path: str | Path, root: str | Path | None = None) -> str:
    """What to write into media.local_path: relative to the root when inside it."""
    p, r = Path(path), media_root(root)
    try:
        return p.relative_to(r).as_posix()
    except ValueError:
        return str(p)


def _legacy_tail(p: Path) -> str | None:
    """The media-root-relative part of a legacy absolute path, if recognisable.

    Two shapes: anything under a `.corpus/media/` directory (the default root on
    every machine), and a content-addressed tail `ab/cd/abcd....ext`, which is how
    every keyframe and still is laid out whatever root it was written under.
    """
    s = p.as_posix()
    if LEGACY_MARKER in s:
        return s.split(LEGACY_MARKER, 1)[1]
    parts = p.parts
    if len(parts) >= 3:
        a, b, name = parts[-3], parts[-2], parts[-1]
        if len(a) == 2 and len(b) == 2 and name[:2] == a and name[2:4] == b:
            return f"{a}/{b}/{name}"
    return None


def resolve(stored: str | Path | None, root: str | Path | None = None) -> Path | None:
    """Where the file for a media.local_path value is on THIS machine, or None.

    Relative values join the current root. A legacy absolute value is re-rooted by
    its recognisable tail; the original absolute path is the fallback only when the
    re-rooted one does not exist and it does. Returns a path that may not exist:
    callers that need the file check, callers that delete it do not have to.
    """
    if not stored:
        return None
    p = Path(stored)
    r = media_root(root)
    if not p.is_absolute():
        return r / p
    tail = _legacy_tail(p)
    if tail is None:
        return p
    cand = r / tail
    if cand.exists() or not p.exists():
        return cand
    return p


def resolve_existing(stored, root=None) -> Path | None:
    """resolve(), but None when the file is not there: the read-side helper."""
    p = resolve(stored, root)
    return p if p is not None and p.exists() else None


class ExpiredURL(MediaError):
    """The signed CDN URL is dead. Re-resolve from the platform id, do not retry."""


@dataclass
class Keyframe:
    path: Path
    t_offset_s: float
    seq: int


def _tool(name: str) -> str:
    # launchd strips /opt/homebrew/bin from PATH, so resolve absolutely and fail with
    # something readable rather than a bare FileNotFoundError inside a scheduled run.
    p = shutil.which(name)
    if not p:
        raise MediaError(f"{name} not found on PATH (brew install {name})")
    return p


class SourceBlocked(MediaError):
    """The platform refused this machine, not this clip ("Sign in to confirm you're
    not a bot"). Not the clip's fault, so it must not use up an enrich attempt."""


# The web client first; the android client second. After ~50 Shorts in one night
# YouTube answered every web request with a bot check, and some Shorts (all of
# one tourist board's) are "not available" to the web client at all; the android client
# downloaded both, 2026-10-23.
_YT_CLIENTS = ([], ["--extractor-args", "youtube:player_client=android"])


def _youtube(url: str, dest: Path, timeout: int) -> Path:
    """A YouTube page URL is not a file: yt-dlp resolves and fetches it. The
    smallest MP4 with sound is plenty for a transcript and six keyframes."""
    from ..adapters.youtube_shorts import ytdlp
    msgs = []
    for extra in _YT_CLIENTS:
        proc = subprocess.run(
            [ytdlp(), "--no-warnings", "--no-playlist", "-q", "--force-overwrites",
             *extra,
             # Separate streams merged by ffmpeg: without a JS runtime YouTube offers
             # yt-dlp no single file with sound (format 18 is gone).
             "-f", "bv*[height<=720][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=720]+ba/b",
             "--merge-output-format", "mp4", "-o", str(dest), url],
            capture_output=True, text=True, timeout=max(timeout, 300))
        if proc.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            return dest
        err = (proc.stderr or "").strip().splitlines()
        msgs.append(err[-1] if err else f"exit {proc.returncode}")
    msg = msgs[-1]
    if any("not a bot" in x.lower() or "sign in to confirm" in x.lower() for x in msgs):
        raise SourceBlocked(f"yt-dlp: {msgs[0][:160]}")
    if "unavailable" in msg.lower() or "private" in msg.lower():
        raise ExpiredURL(f"yt-dlp: {msg}")
    raise MediaError(f"yt-dlp: {msg}")


# ── what download() may fetch ───────────────────────────────────────────────
#
# remote_url is vendor data, and urlopen opens whatever it is handed: a file:///
# URL in a scraped payload would copy a local file into the media store, and from
# there into a client brief; an http://127.0.0.1:... or 169.254.169.254 one would
# make this box request its own services (SSRF). So: http(s) only, and every host,
# including each redirect hop, must resolve to public addresses only.
#
# The address check is an allowlist (ip.is_global), not a denylist: a denylist of
# is_private/is_loopback/... let NAT64 (64:ff9b::/96), 6to4 and Teredo wrappings of
# internal IPv4 addresses through. And the connection goes to the address that
# was checked: the socket is opened by _pinned_connect, which resolves, refuses
# any non-public answer and connects to that same answer, so a DNS-rebinding host
# cannot pass the check with one address and be fetched at another. TLS still
# verifies against the hostname (SNI and the certificate check use conn.host).

class UnsafeURL(MediaError):
    """A media URL that may not be fetched (scheme or destination)."""


def _resolve(host: str, port: int) -> list[str]:
    import socket
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, port,
                                                         type=socket.SOCK_STREAM)})


def _public(addr: str) -> bool:
    import ipaddress
    ip = ipaddress.ip_address(addr.split("%", 1)[0])
    if ip.version == 6:
        if ip in ipaddress.ip_network("64:ff9b::/96") or \
                ip in ipaddress.ip_network("64:ff9b:1::/48"):
            return False                # NAT64: an IPv4 address in disguise
        inner = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if inner is not None:
            ip = inner
        elif ip not in ipaddress.ip_network("2000::/3"):
            return False                # only global unicast; refuses ::a00:1 (IPv4-compatible)
    return ip.is_global and not (ip.is_multicast or ip.is_reserved)


def check_url(url: str) -> None:
    """Raise UnsafeURL unless `url` is http(s) to a host with only public addresses."""
    from urllib.parse import urlsplit
    u = urlsplit(url or "")
    if u.scheme not in ("http", "https"):
        raise UnsafeURL(f"refused media URL scheme {u.scheme or '(none)'!r}")
    if not u.hostname:
        raise UnsafeURL("refused media URL without a host")
    try:
        addrs = _resolve(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except OSError as e:
        raise MediaError(f"cannot resolve {u.hostname}: {e}") from e
    bad = [a for a in addrs if not _public(a)]
    if not addrs or bad:
        raise UnsafeURL(f"refused media host {u.hostname}: resolves to a non-public "
                        f"address ({', '.join(bad) or 'none'})")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """Re-check every hop: a public CDN URL may 302 to http://10.0.0.1/."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _pinned_connect(address, timeout=None, source_address=None, **kw):
    """socket.create_connection, but only to addresses that passed _public."""
    import socket
    host, port = address
    addrs = _resolve(host, port)
    bad = [a for a in addrs if not _public(a)]
    if not addrs or bad:
        raise UnsafeURL(f"refused media host {host}: resolves to a non-public "
                        f"address ({', '.join(bad) or 'none'})")
    err = None
    for a in addrs:
        try:
            return socket.create_connection((a, port), timeout, source_address)
        except OSError as e:
            err = e
    raise err  # type: ignore[misc]


class _PinnedHTTP(urllib.request.HTTPHandler):
    def http_open(self, req):
        import http.client

        class Conn(http.client.HTTPConnection):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self._create_connection = _pinned_connect
        return self.do_open(Conn, req)


class _PinnedHTTPS(urllib.request.HTTPSHandler):
    def https_open(self, req):
        import http.client

        class Conn(http.client.HTTPSConnection):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self._create_connection = _pinned_connect
        return self.do_open(Conn, req, context=self._context)


def _open(req, timeout):
    """urlopen through the hop-checking redirect handler and the pinned
    connections (tests replace this)."""
    # ProxyHandler({}): an http(s)_proxy in the environment would make the proxy,
    # not _pinned_connect, resolve the media host, which bypasses the check.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _CheckedRedirects,
                                       _PinnedHTTP, _PinnedHTTPS).open(req, timeout=timeout)


def download(url: str, dest: Path, *, timeout: int = 120) -> Path:
    if re.match(r"https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", url or ""):
        return _youtube(url, dest, timeout)
    check_url(url)
    req = urllib.request.Request(url, headers=UA)
    try:
        with _open(req, timeout) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    except UnsafeURL:
        raise
    except urllib.error.HTTPError as e:
        # 403 on a signed CDN URL means the signature expired or was tampered with.
        # 404 usually means the post was deleted. Both are "re-resolve", not "retry".
        if e.code in (403, 404, 410):
            raise ExpiredURL(f"{e.code} for signed URL (re-resolve from platform id)") from e
        raise MediaError(f"HTTP {e.code} fetching media") from e
    except Exception as e:
        raise MediaError(f"fetch failed: {e}") from e
    if dest.stat().st_size == 0:
        raise MediaError("fetched 0 bytes")
    with open(dest, "rb") as f:
        head = f.read(16)
    if head.startswith(b"#EXTM3U"):
        # An HLS playlist, not a video: LinkedIn's video URLs from HarvestAPI are
        # dms.licdn.com/playlist/... . Stored as-is, whisper answered 500 to the
        # playlist text and the clip burned its attempts (a ferry company's page, 2026-10-23).
        return _hls(url, dest, timeout)
    return dest


# Hosts whose HLS playlists corpus fetches. Only the one actually seen: LinkedIn
# video from HarvestAPI (dms.licdn.com/playlist/...). The playlist is vendor data,
# and ffmpeg follows whatever URLs it lists, so an unknown host is refused outright.
HLS_HOSTS = ("dms.licdn.com",)


def _hls_allowed(url: str) -> bool:
    from urllib.parse import urlsplit
    u = urlsplit(url or "")
    return u.scheme == "https" and (u.hostname or "").lower() in HLS_HOSTS


def _hls(url: str, dest: Path, timeout: int) -> Path:
    """Fetch an HLS stream into an MP4 with ffmpeg (it picks the best variant).

    Locked down because ffmpeg follows the URLs inside a vendor-supplied playlist:
    an allowlisted https host to start from, and -protocol_whitelist so nothing the
    playlist names can be read over file:, plain http or any other protocol.
    """
    if not _hls_allowed(url):
        raise MediaError(f"HLS refused: not an https URL on {', '.join(HLS_HOSTS)}")
    tmp = dest.with_name(dest.stem + ".hls.mp4")
    guard = ["-protocol_whitelist", "https,tls,tcp,crypto"]
    # LinkedIn's fMP4 segments sit behind extensionless URLs, which ffmpeg 7's HLS
    # demuxer rejects as "Empty segment". -extension_picky 0 only skips that
    # extension-vs-format check on the segments; what may be opened is still bound
    # by the protocol whitelist above. (-allowed_extensions ALL is not needed and is
    # not used.) Older ffmpeg has no -extension_picky and gets the guarded plain run.
    for relax in (["-extension_picky", "0"], []):
        proc = subprocess.run(
            [_tool("ffmpeg"), "-v", "error", "-y", *guard, *relax, "-i", url,
             "-c", "copy", str(tmp)],
            capture_output=True, text=True, timeout=max(timeout, 300))
        if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            break
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        err = (proc.stderr or "").strip().splitlines()
        msg = err[-1] if err else f"exit {proc.returncode}"
        if "403" in msg or "404" in msg or "410" in msg:
            raise ExpiredURL(f"HLS: {msg}")
        raise MediaError(f"HLS fetch failed: {msg}")
    tmp.replace(dest)
    return dest


def has_audio(path: Path) -> bool | None:
    """Whether the file carries an audio stream; None when ffprobe cannot tell."""
    try:
        out = subprocess.run(
            [_tool("ffprobe"), "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return bool(out.stdout.strip())


def probe_duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            [_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return float(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    except Exception:
        return None


def keyframes(video: Path, out_dir: Path, *, count: int = DEFAULT_FRAMES,
              width: int = FRAME_WIDTH, duration: float | None = None) -> list[Keyframe]:
    """Sample `count` frames evenly across the video.

    Even sampling rather than scene detection: for short-form the interesting frames
    are the hook, the mid-roll and the CTA, and those land predictably by position.
    Scene detection costs more and returns a variable number of frames, which makes
    downstream prompts inconsistent.

    Pass `duration` when the caller already probed it. Every ffprobe is a 65ms
    process spawn and the enrich path was paying for two per item.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = duration if duration else probe_duration(video)
    if not dur or dur <= 0:
        raise MediaError("could not determine duration")

    n = max(1, min(count, int(dur) or 1))
    fps = n / dur
    pattern = out_dir / f"f%02d.{FRAME_EXT}"
    proc = subprocess.run(
        [_tool("ffmpeg"), "-nostdin", "-v", "error", "-y", "-i", str(video),
         "-vf", f"fps={fps},scale={width}:-1",
         "-vsync", "vfr", "-q:v", str(FRAME_QSCALE), str(pattern)],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        raise MediaError(f"ffmpeg keyframes failed: {(proc.stderr or '')[:300]}")

    frames = sorted(out_dir.glob(f"f*.{FRAME_EXT}"))
    step = dur / max(1, len(frames))
    return [Keyframe(path=f, t_offset_s=round(i * step, 2), seq=i + 1)
            for i, f in enumerate(frames)]


def poster(video: Path, dest: Path, *, at_s: float = 0.0, width: int = FRAME_WIDTH) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [_tool("ffmpeg"), "-nostdin", "-v", "error", "-y", "-ss", str(at_s),
         "-i", str(video), "-frames:v", "1", "-vf", f"scale={width}:-1", str(dest)],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0 or not dest.exists():
        raise MediaError(f"poster extraction failed: {(proc.stderr or '')[:200]}")
    return dest


def archive_audio(video: Path, dest: Path) -> Path:
    """Keep a tiny opus copy as re-transcription insurance.

    opus 24k mono is ~10.6 MB/hour, so 5,000 minute-long reels is under a gigabyte --
    inside the free tier of any object store. It also sidesteps Groq's 25 MB upload
    cap (2.4 hours per file at this bitrate, versus 13 minutes for 16-bit WAV) if a
    cloud fallback is ever needed for a language SpeechAnalyzer cannot handle.

    Do not reach for FLAC here: measured on 16 kHz mono speech it came out LARGER
    than WAV.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [_tool("ffmpeg"), "-nostdin", "-v", "error", "-y", "-i", str(video),
         "-vn", "-ac", "1", "-c:a", "libopus", "-b:a", "24k", str(dest)],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0 or not dest.exists():
        raise MediaError(f"opus encode failed: {(proc.stderr or '')[:200]}")
    return dest


class VideoWorkspace:
    """Download a video to a temp dir, work on it, and guarantee the mp4 is gone.

    A context manager rather than a helper function because the whole storage policy
    depends on the original never surviving the block, including on exception.
    """

    def __init__(self, url: str):
        self.url = url
        self._tmp: tempfile.TemporaryDirectory | None = None
        self.video: Path | None = None

    def __enter__(self) -> "VideoWorkspace":
        self._tmp = tempfile.TemporaryDirectory(prefix="corpus-media-")
        self.video = download(self.url, Path(self._tmp.name) / "src.mp4")
        return self

    def __exit__(self, *exc) -> None:
        if self._tmp:
            self._tmp.cleanup()
        self.video = None
