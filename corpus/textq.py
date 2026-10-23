"""Is this machine transcript worth showing to a lens (or a reader) at all?

Whisper does not fail loudly. Handed music, silence or noise it returns fluent junk:
"Music Music Music Music", "Thank you. Thank you. Thank you.", a chorus transcribed
eight times over, or "Субтитры создавал DimaTorzok" on a Croatian reel. A lens given
that text analyses it earnestly, and a brief quotes it as what the creator said.

`assess()` returns a verdict and a reason:

  ok       looks like speech
  music    lyrics or a music bed: a phrase repeated as a chorus, or Whisper's own
           [Music] / ♪ markers. Real content, but not the speaker talking.
  garbled  junk: one token on repeat, almost no word-like tokens, or a known
           Whisper hallucination line.

`garbled(text)` is the yes/no the brief and the lens builder call (the brief imports
it by that exact name). It is True for both junk and music, because neither should
be presented as what the account owner said.

No dictionary: the corpus is Croatian and English and the heuristics must not need a
word list per language. "Word-like" means mostly letters with at least one vowel,
counting syllabic r so Croatian "prst", "vrt" and "krv" pass.
"""
from __future__ import annotations

import re
from collections import Counter

_TOKEN = re.compile(r"[^\s]+")
_LETTERS = re.compile(r"[^\W\d_]+", re.UNICODE)
_VOWELS = set("aeiouyrAEIOUYRáéíóúàèìòùäëïöüåæøœ" "аеиоуыэюяёАЕИОУЫЭЮЯЁ")

# Lines Whisper emits over silence or music, verbatim, in the languages seen so far.
_HALLUCINATIONS = (
    "thank you", "thanks for watching", "thank you for watching", "subtitles by",
    "субтитры", "продолжение следует", "hvala na gledanju", "hvala što ste gledali",
    "please subscribe", "like and subscribe", "you", "bye", "music",
    # Croatian/Japanese closers seen over music beds, 2026-10-23 (three local
    # business accounts): "thanks for following (the channel)".
    "hvala što pratite", "hvala sto pratite", "hvala što pratite kanal",
    "hvala što pratite moj kanal", "ご視聴ありがとうございました",
)
# Phrases that only ever appear as a hallucinated subtitle credit.
_CREDITS = ("субтитры", "dimatorzok", "amara.org", "редактор субтитров", "корректор")
_SENTENCE = re.compile(r"[.!?\n。]+")
_MUSIC_MARK = re.compile(r"\[(music|glazba|muzika|applause)\]|\((music|glazba)\)|♪|♫",
                         re.I)


def _tokens(text: str) -> list[str]:
    return [t.strip(".,!?;:\"'()[]-–—…").lower() for t in _TOKEN.findall(text)]


def _wordlike(tok: str) -> bool:
    if not tok or len(tok) > 24:
        return False
    letters = "".join(_LETTERS.findall(tok))
    if len(letters) < max(1, len(tok) * 0.7):
        return False
    return any(c in _VOWELS for c in letters)


def _max_run(toks: list[str]) -> int:
    best = run = 1 if toks else 0
    for a, b in zip(toks, toks[1:]):
        run = run + 1 if a == b and a else 1
        best = max(best, run)
    return best


def _chorus(text: str) -> bool:
    """A phrase of 3+ words that recurs 3+ times and covers a good share of the
    text. Lyrics repeat; people making a point almost never say the same five words
    three times in a minute."""
    toks = [t for t in _tokens(text) if t]
    if len(toks) < 12:
        return False
    for n in (5, 4, 3):
        grams = Counter(tuple(toks[i:i + n]) for i in range(len(toks) - n + 1))
        gram, count = grams.most_common(1)[0]
        if count >= 3 and count * n / len(toks) >= 0.3:
            return True
    lines = [l.strip().lower() for l in re.split(r"[\n.!?]+", text) if l.strip()]
    if len(lines) >= 4:
        top = Counter(lines).most_common(1)[0][1]
        if top >= 3 and top / len(lines) >= 0.4:
            return True
    return False


def assess(text: str | None) -> tuple[str, str]:
    """(verdict, reason). verdict is 'ok', 'music' or 'garbled'."""
    t = (text or "").strip()
    if not t:
        return "ok", ""
    if _MUSIC_MARK.search(t):
        return "music", "music markers in the transcript"

    toks = [x for x in _tokens(t) if x]
    if not toks:
        return "garbled", "no words"
    joined = " ".join(toks)
    if joined in _HALLUCINATIONS or (
            len(toks) <= 12 and all(" ".join(toks[i:i + 2]) in _HALLUCINATIONS
                                    or toks[i] in _HALLUCINATIONS
                                    for i in range(len(toks)))):
        return "garbled", "a known Whisper hallucination over silence or music"
    if any(h in joined for h in _CREDITS):
        return "garbled", "a known Whisper hallucination over silence or music"
    # Every sentence a known closer ("Hvala što pratite kanal." twice over a
    # music bed): the per-token test above cannot see across the seam.
    parts = [" ".join(x for x in _tokens(p) if x) for p in _SENTENCE.split(t)]
    parts = [p for p in parts if p]
    if parts and all(p in _HALLUCINATIONS for p in parts):
        return "garbled", "a known Whisper hallucination over silence or music"

    if len(toks) >= 4 and _max_run(toks) >= 4:
        return "garbled", f"'{toks[0] if _max_run(toks) == len(toks) else 'a word'}' repeated"
    if len(toks) >= 8:
        uniq = len(set(toks)) / len(toks)
        if uniq < 0.2:
            return "garbled", f"only {len(set(toks))} distinct words in {len(toks)}"
    word_ratio = sum(1 for x in toks if _wordlike(x)) / len(toks)
    if len(toks) >= 5 and word_ratio < 0.6:
        return "garbled", f"only {word_ratio:.0%} of tokens look like words"
    if _chorus(t):
        return "music", "a repeated chorus, probably lyrics"
    return "ok", ""


def garbled(text: str) -> bool:
    """True when the transcript should not be presented as what was said."""
    return assess(text)[0] != "ok"


def reason(text: str) -> str:
    """Why `garbled` said so, empty when it did not."""
    return assess(text)[1]
