"""On-device transcription via Apple SpeechAnalyzer, through the `yap` CLI.

Why this and not Whisper: it is free, fully local, and measured at roughly 30x
realtime on a 45s clip on an Apple Silicon laptop (short clips do worse -- model load dominates, so a
20s clip lands nearer 15x). Nothing leaves the machine and there is no per-minute bill.

Two hard constraints, both discovered by running it rather than reading about it:

  * `yap` has NO parallelism flag. One file per invocation. The fan-out here is ours.
  * The locale MUST be passed explicitly. A system locale like `en_US@rg=hrzzzz`
    (US English, Croatian regional formats) and SpeechAnalyzer rejects it outright
    with "The specified locale is not supported". Anything relying on the default
    locale fails on this machine.

Language coverage is the real limit: Apple ships ~30 locales and Croatian is not one
of them. Verified rejected: hr-HR, hr, sr-RS, sl-SI, pl-PL, cs-CZ. The entire Slavic
family is absent. de-DE and en-US work. Non-English-or-Western-European content needs
a paid fallback (Groq whisper-large-v3-turbo, ~$0.04/hour), which is deliberately not
wired up here -- add it when something actually needs it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LOCALE = "en-US"


class TranscribeError(RuntimeError):
    pass


class TranscribeUnavailable(TranscribeError):
    """The ENGINE failed, not the clip: server unreachable, connection refused, a
    timeout, a Groq 5xx or 429.

    Kept apart from content failures because enrich counts attempts per item and
    gives up after three. A whisper server that was down for a night used to burn
    one attempt on every pending reel, so three bad nights retired the whole
    backlog for good, with nothing wrong with any of the videos.
    """


# Self-hosted whisper.cpp at large-v3-turbo does a 90s reel in seconds on the GPU.
# The old 1800s ceiling meant one wedged request held an enrich worker for half
# an hour per item; five minutes is still generous for anything short-form.
def _selfhosted_timeout() -> int:
    import os
    try:
        return max(10, int(os.environ.get("WHISPER_TIMEOUT_S") or 300))
    except ValueError:
        return 300


def _is_outage(e: BaseException) -> bool:
    """Is this exception about reaching the engine rather than about the audio?"""
    import socket
    import urllib.error
    if isinstance(e, urllib.error.HTTPError):
        return e.code >= 500 or e.code == 429
    if isinstance(e, (urllib.error.URLError, ConnectionError, TimeoutError,
                      socket.timeout)):
        return True
    return False


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    text: str
    segments: list[Segment]
    locale: str
    suspect: str | None = None      # why the text was rejected, if it was

    @property
    def duration_s(self) -> float | None:
        return self.segments[-1].end if self.segments else None


# Whisper does not go quiet on music-only audio, it invents. These are its most
# common inventions, harvested from real output on the author's own reels: a reel
# captioned "Bro, I'm out here hustling 24/7" came back as "Субтитры создавал
# DimaTorzok" (a subtitle-credit string burned in from training data) and tagged
# Russian. Others returned "you", "Thank you.", "Oh".
#
# A fabricated transcript is worse than no transcript, because it flows into a lens
# and then into a client brief looking like a finding. Better to store nothing.
_HALLUCINATIONS = {
    "субтитры создавал dimatorzok", "субтитры делал dimatorzok",
    "продолжение следует...", "редактор субтитров а.синецкая",
    "thank you.", "thank you", "thanks for watching!", "you", "oh", "bye.",
    "музыка", "muzika", "music", "[music]", "applause", "subscribe",
    "please subscribe", "amara.org", "www.amara.org",
    "sottotitoli e revisione a cura di qtss", "sous-titrage société radio-canada",
    # South Slavic, from the self-hosted server: a 3 s pure tone sent as Croatian
    # came back "Hvala što pratite kanal." (2026-10-22). YouTube outros and subtitle
    # credits are what Whisper fills silence with in every language it knows.
    "hvala što pratite kanal", "hvala sto pratite kanal", "hvala na gledanju",
    "hvala što ste gledali", "hvala sto ste gledali", "hvala na pažnji",
    "pretplatite se na kanal", "pretplatite se", "hvala", "hvala vam", "doviđenja",
    "vidimo se", "glazba", "muzika", "hvala što ste pratili", "hvala na praćenju",
    "vielen dank fürs zuschauen", "danke fürs zuschauen", "dziękuję za uwagę",
    "děkuji za pozornost",
}

# Subtitle credits arrive with a name attached, so match the opening words.
_HALLUCINATION_PREFIXES = (
    "titlovi", "prijevod", "prevod", "prevela", "preveo", "obrada titlova",
    "untertitel", "napisy", "titulky", "субтитры", "sottotitoli", "sous-titres",
)

# Below this many characters per second of audio, the "transcript" is almost
# certainly an artifact rather than speech. Real speech runs 10-20 chars/sec.
_MIN_CHARS_PER_SEC = 0.7

# Background music with vocals passes every hallucination check: it is real speech,
# at a plausible rate, in a plausible language. It is just not the account owner
# talking. One stored analysis already attributed a Bob Marley lyric to the owner
# ("a 15-second clip of the owner muttering 'Oh, I can not explain'"), which is exactly
# the kind of confident nonsense that discredits a paid brief. Flagged rather than
# discarded: the audio is genuinely there, so the caller decides.
_LYRIC_MARKERS = (
    "don't get angry with me", "i never talk you no pain", "when you knocked on my door",
    "i can not explain", "i can't explain", "na na na", "oh oh oh", "yeah yeah yeah",
    "la la la", "baby baby",
)


def looks_like_lyrics(text: str, duration_s: float | None) -> bool:
    """Heuristic: short clip, sparse text, and a musical cadence or known lyric."""
    t = (text or "").strip().lower()
    if not t:
        return False
    if any(m in t for m in _LYRIC_MARKERS):
        return True
    words = t.split()
    if duration_s and duration_s >= 8 and len(words) < 12:
        # Very little said over a long clip is usually a track under b-roll.
        return True
    return False


def looks_hallucinated(text: str, duration_s: float | None) -> str | None:
    """Return a reason string when a transcript should be discarded, else None."""
    t = (text or "").strip()
    if not t:
        return None

    norm = t.lower().strip(" .!?…")
    if norm in _HALLUCINATIONS or t.lower() in _HALLUCINATIONS:
        return f"known artifact ({t[:40]!r})"
    if len(norm) < 80 and norm.startswith(_HALLUCINATION_PREFIXES):
        return f"subtitle credit ({t[:40]!r})"

    # A single short phrase repeated is the other classic failure shape.
    words = norm.split()
    if len(words) >= 6 and len(set(words)) <= 2:
        return "degenerate repetition"

    if duration_s and duration_s >= 5 and len(t) / duration_s < _MIN_CHARS_PER_SEC:
        return (f"{len(t)} chars for {duration_s:.0f}s of audio "
                f"({len(t)/duration_s:.2f} chars/s)")
    return None


# Groq returns language names ("Croatian"), yap returns BCP-47 ("en-US"). Storing
# both raw makes the column useless for filtering.
_LANG_NAMES = {
    "english": "en", "croatian": "hr", "serbian": "sr", "bosnian": "bs",
    "german": "de", "italian": "it", "spanish": "es", "french": "fr",
    "russian": "ru", "slovenian": "sl", "polish": "pl", "czech": "cs",
    "portuguese": "pt", "dutch": "nl", "turkish": "tr", "korean": "ko",
    "japanese": "ja", "chinese": "zh", "hungarian": "hu", "slovak": "sk",
    "macedonian": "mk", "albanian": "sq", "ukrainian": "uk", "arabic": "ar",
}


def normalize_lang(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().lower()
    if v in _LANG_NAMES:
        return _LANG_NAMES[v]
    return v.split("-")[0] if v not in ("auto",) else None


def available() -> bool:
    return shutil.which("yap") is not None


def _require_yap() -> str:
    path = shutil.which("yap")
    if not path:
        raise TranscribeError(
            "yap not found. Install with: brew install yap  "
            "(Apple SpeechAnalyzer CLI, macOS 26+)"
        )
    return path


def transcribe_file(path: str | Path, *, locale: str = DEFAULT_LOCALE,
                    timeout: int = 900) -> Transcript:
    """Transcribe one audio or video file.

    yap demuxes video itself, so there is no ffmpeg pre-extraction step. Feeding it
    the mp4 directly is both simpler and faster than extracting audio first.
    """
    yap = _require_yap()
    path = Path(path)
    if not path.exists():
        raise TranscribeError(f"no such file: {path}")

    proc = subprocess.run(
        [yap, "transcribe", "--locale", locale, "--json", str(path)],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if "locale is not supported" in err.lower():
            raise TranscribeError(
                f"locale {locale!r} unsupported by SpeechAnalyzer. Apple ships ~30 "
                f"locales and no Slavic languages; use a cloud fallback for these."
            )
        raise TranscribeError(f"yap failed ({proc.returncode}): {err[:400]}")

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise TranscribeError(f"yap returned non-JSON: {proc.stdout[:200]}") from e

    segs = [
        Segment(start=float(s.get("start", 0.0)), end=float(s.get("end", 0.0)),
                text=(s.get("text") or "").strip())
        for s in data.get("segments", [])
    ]
    segs = [s for s in segs if s.text]
    text = " ".join(s.text for s in segs).strip()
    dur = segs[-1].end if segs else None
    reason = looks_hallucinated(text, dur)
    return Transcript(
        text="" if reason else text,
        segments=[] if reason else segs,
        locale=normalize_lang((data.get("metadata") or {}).get("language")) or
               normalize_lang(locale),
        suspect=reason,
    )


# ── cloud fallback, for languages SpeechAnalyzer does not ship ──────────────

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"


def _env_key(name: str) -> str | None:
    import os
    if not os.environ.get(name):
        # providers.load() reads ~/.reach/keys.env, the shared key file.
        try:
            from .. import providers
            providers.load()
        except Exception:
            pass
    return os.environ.get(name) or None


def _whisper_servers() -> list[str]:
    """Self-hosted whisper.cpp servers, WHISPER_SERVER_URL comma-separated (the same
    setting `reach transcribe` uses)."""
    return [u.strip().rstrip("/") for u in (_env_key("WHISPER_SERVER_URL") or "").split(",")
            if u.strip()]


def _live_servers() -> list[str]:
    """The configured servers minus any that failed this process's ping."""
    return [u for u in _whisper_servers() if u not in _UNREACHABLE]


# Servers that failed the pre-flight ping in this process. Skipped for the rest of
# the run: a dead host costs a connect timeout per item otherwise, times the
# whole backlog.
_UNREACHABLE: set[str] = set()


def ping_whisper(timeout: float = 5.0) -> dict[str, str | None]:
    """Check every configured whisper server once. {url: None if reachable, else
    why not}. Down servers are excluded from routing for the rest of this process.

    A TCP connect, not an HTTP request: whisper.cpp's server handles one request
    at a time, so a GET while it is transcribing for someone else waits out the
    timeout and a busy server would read as a dead one. Accepting the connection
    is what "up" means here.
    """
    import socket
    from urllib.parse import urlsplit
    out: dict[str, str | None] = {}
    for url in _whisper_servers():
        u = urlsplit(url)
        port = u.port or (443 if u.scheme == "https" else 80)
        try:
            socket.create_connection((u.hostname, port), timeout=timeout).close()
            out[url] = None
        except Exception as e:  # noqa: BLE001 - any failure to connect is "down"
            out[url] = f"{type(e).__name__}: {e}"
    _UNREACHABLE.update(u for u, err in out.items() if err)
    _UNREACHABLE.difference_update(u for u, err in out.items() if not err)
    return out


def whisper_available() -> bool:
    """A Whisper engine is configured: self-hosted or Groq. Either covers what yap
    cannot (Slavic audio) and stands in for yap entirely off macOS."""
    return bool(_live_servers() or _env_key("GROQ_API_KEY"))


def _post_whisper(url: str, path: Path, *, fields: dict, headers: dict,
                  language: str | None, timeout: int) -> Transcript:
    """One OpenAI-shaped verbose_json transcription. Groq and whisper.cpp's server
    answer in the same shape, so one parser serves both."""
    # Send audio, not video. Whisper's upload cap is 25 MB, but the size that
    # matters is the one going up the uplink: a real 9.49 MB clip becomes 0.136 MB
    # of opus (70x) in 0.33s, and the Groq round trip drops 1.23s -> 0.55s.
    # TemporaryDirectory, not mkdtemp: the opus copy used to be left behind on every
    # call, one /tmp/corpus-tx-* directory per transcribed reel, forever.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="corpus-tx-") as td:
        src = path
        try:
            from . import media as _m
            src = _m.archive_audio(path, Path(td) / "a.opus")
        except Exception:
            # ffmpeg missing or the encode failed. The raw file still works below
            # the 25 MB cap, so degrade rather than refuse.
            src = path
            if path.stat().st_size > 24 * 1024 * 1024:
                raise TranscribeError(
                    f"{path.name} is {path.stat().st_size / 1e6:.0f} MB, over the "
                    f"25 MB upload cap, and could not be converted to opus")
        audio = src.read_bytes()
        name = src.name
    return _post_audio(url, audio, name, fields=fields, headers=headers,
                       language=language, timeout=timeout)


def _post_audio(url: str, audio: bytes, name: str, *, fields: dict, headers: dict,
                language: str | None, timeout: int) -> Transcript:
    import json as _json
    import mimetypes
    import urllib.request
    import uuid

    boundary = uuid.uuid4().hex
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    fields = {**fields, "response_format": "verbose_json"}
    if language:
        fields["language"] = language
    parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
             f"{v}\r\n".encode() for k, v in fields.items()]
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{name}\"\r\nContent-Type: {mime}\r\n\r\n".encode()
        + audio + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())

    # The User-Agent is load-bearing, not decoration. Groq sits behind Cloudflare,
    # which rejects urllib's default "Python-urllib/3.x" with a 403 (error 1010).
    req = urllib.request.Request(
        url, data=b"".join(parts),
        headers={**headers, "User-Agent": "corpus-cli/0.1 python-urllib",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = _json.loads(r.read())
    except Exception as e:
        kind = TranscribeUnavailable if _is_outage(e) else TranscribeError
        raise kind(f"whisper transcription failed at {url}: {e}") from e

    segs = [Segment(start=float(s.get("start", 0.0)), end=float(s.get("end", 0.0)),
                    text=(s.get("text") or "").strip())
            for s in data.get("segments", [])]
    segs = [s for s in segs if s.text]
    text = (data.get("text") or " ".join(s.text for s in segs)).strip()
    dur = data.get("duration") or (segs[-1].end if segs else None)

    # verbose_json exposes no_speech_prob per segment. When every segment is
    # confidently non-speech, whatever came back is an artifact of music.
    probs = [s.get("no_speech_prob") for s in data.get("segments", [])
             if s.get("no_speech_prob") is not None]
    reason = looks_hallucinated(text, dur)
    if not reason and probs and min(probs) > 0.6:
        reason = f"no speech detected (min no_speech_prob {min(probs):.2f})"

    return Transcript(
        text="" if reason else text,
        segments=[] if reason else segs,
        locale=normalize_lang(data.get("language")) or normalize_lang(language),
        suspect=reason,
    )


def transcribe_selfhosted(path: str | Path, *, language: str | None = None,
                          timeout: int | None = None) -> Transcript:
    """whisper.cpp server (large-v3-turbo on a local GPU box): $0, nothing leaves the
    network, and unlike yap it speaks Croatian. Tries each configured server.

    Raises TranscribeUnavailable when every server failed for an outage reason (or
    none is configured/reachable), so the caller does not blame the clip.
    """
    path = Path(path)
    if not path.exists():
        raise TranscribeError(f"no such file: {path}")
    timeout = timeout or _selfhosted_timeout()
    errors: list[TranscribeError] = []
    for url in _live_servers():
        try:
            return _post_whisper(f"{url}/inference", path, fields={}, headers={},
                                 language=language or "auto", timeout=timeout)
        except TranscribeError as e:
            errors.append(e)
    raise _combine(errors, "WHISPER_SERVER_URL not set or no server reachable")


def _combine(errors: list, empty: str) -> TranscribeError:
    """One error out of several engines' failures. It is an outage only if every
    engine had one: a single content failure means the clip itself is the problem."""
    if not errors:
        return TranscribeUnavailable(empty)
    kind = (TranscribeUnavailable
            if all(isinstance(e, TranscribeUnavailable) for e in errors)
            else TranscribeError)
    return kind("; ".join(str(e) for e in errors))


# Whisper covers 99 languages against SpeechAnalyzer's ~30, including every Slavic
# one. At $0.04/hour this is not a cost decision, it is a coverage decision: without
# it, a Croatian reel yields no transcript at all, and the author's own best-performing
# Instagram content is partly Croatian.
def transcribe_cloud(path: str | Path, *, language: str | None = None,
                     api_key: str | None = None, timeout: int = 600) -> Transcript:
    path = Path(path)
    if not path.exists():
        raise TranscribeError(f"no such file: {path}")
    key = api_key or _env_key("GROQ_API_KEY")
    if not key:
        # Configuration, not content: no clip should lose an attempt over it.
        raise TranscribeUnavailable("GROQ_API_KEY not set; needed for non-English audio")
    return _post_whisper(GROQ_URL, path, fields={"model": GROQ_MODEL},
                         headers={"Authorization": f"Bearer {key}"},
                         language=language, timeout=timeout)


def transcribe_whisper(path: str | Path, *, language: str | None = None,
                       allow_cloud: bool = True) -> Transcript:
    """Self-hosted first, Groq as the fallback. `allow_cloud=False` still allows the
    self-hosted server: it is not the cloud."""
    try:
        return transcribe_selfhosted(path, language=language)
    except TranscribeError as local_err:
        if not allow_cloud:
            raise
        try:
            return transcribe_cloud(path, language=language)
        except TranscribeError as e:
            raise _combine([local_err, e], "") from e


# Detecting the language BEFORE transcribing is not an optimisation, it is a
# correctness requirement. Handed Croatian audio under an en-US locale,
# SpeechAnalyzer does not fail and does not return silence: it returns confident
# English that was never said. A real reel whose caption reads "U pocetku biznisa
# naradit ces se za besplatno" transcribed as "I can fix the ghost line, I'll be
# there. You want to be?" -- fluent, plausible, and entirely invented. Any fallback
# that keys off empty output will never fire on that, and the garbage flows straight
# into the corpus and then into a client deliverable.
#
# Diacritics are the cheapest reliable signal, backed by stopwords for text that has
# been stripped of them.
_LATIN_EXTRA = set("čćžšđČĆŽŠĐąćęłńóśźżáéíóúñõãâêôàèìòùäöüß")
_STOPWORDS = {
    "hr": {" je ", " se ", " na ", " za ", " sam ", " smo ", " ali ", " nije ",
           " kako ", " sto ", " što ", " koji ", " svoje ", " ovo ", " biti "},
    "de": {" der ", " die ", " das ", " und ", " ist ", " nicht ", " mit ", " ein "},
    "es": {" que ", " los ", " las ", " para ", " con ", " una ", " por "},
    "it": {" che ", " non ", " per ", " con ", " una ", " sono ", " della "},
}


def detect_language(text: str | None, *, default: str | None = None) -> str | None:
    """Guess a language code from caption text. None means "looks like English".

    Deliberately crude and biased toward flagging non-English: a false positive costs
    $0.0007 of Whisper, a false negative costs a fabricated transcript.
    """
    if not text or len(text.strip()) < 12:
        return default
    low = f" {text.lower()} "

    scores = {code: sum(1 for w in words if w in low)
              for code, words in _STOPWORDS.items()}
    best = max(scores, key=scores.get)
    if scores[best] >= 2:
        return best

    if sum(1 for ch in text if ch in _LATIN_EXTRA) >= 2:
        return best if scores[best] else "auto"
    return default


def transcribe_auto(path: str | Path, *, locale: str = DEFAULT_LOCALE,
                    allow_cloud: bool = True, language: str | None = None,
                    hint_text: str | None = None,
                    fallback_lang: str | None = None) -> Transcript:
    """Transcribe with the right engine for the language.

    `hint_text` should be the caption. It is the only pre-transcription signal
    available, and it is what decides whether the local engine is even applicable.
    """
    # fallback_lang: the account's usual caption language, for a caption that is
    # only emoji, names and hashtags.
    lang = language or detect_language(hint_text, default=fallback_lang)
    if lang == "auto" and fallback_lang:
        # "not English, not sure what": the account's language is a far better
        # guess than whisper's, which heard a Croatian park video as Korean.
        lang = fallback_lang
    whisper_lang = None if lang in (None, "auto") else lang

    if lang and lang.lower() not in ("en", "en-us", "en-gb"):
        if not allow_cloud and not _live_servers():
            raise TranscribeUnavailable(
                f"content looks like {lang!r}; SpeechAnalyzer ships no Slavic locales "
                f"and would return invented English. Set WHISPER_SERVER_URL or "
                f"enable the cloud fallback."
            )
        return transcribe_whisper(path, language=whisper_lang, allow_cloud=allow_cloud)

    try:
        local = transcribe_file(path, locale=locale)
    except TranscribeError:
        # No yap (Linux, older macOS) or it failed: Whisper is the engine then.
        return transcribe_whisper(path, allow_cloud=allow_cloud)

    if local.text:
        return local

    from . import media as _m
    if (_m.probe_duration(Path(path)) or 0) >= 2.0:
        try:
            return transcribe_whisper(path, allow_cloud=allow_cloud)
        except TranscribeError:
            return local
    return local
