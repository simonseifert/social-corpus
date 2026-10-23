"""Keep marketing lenses off posts about grief, death, illness and personal crisis.

A real lens output told a client to "convert grief into authority" on a post about
their mother's death. Every lens is written to find a growth lever, so handed a
eulogy it finds one. Two guards, because each fails differently:

  1. this keyword pre-filter, which costs nothing and runs before any provider sees
     the text, so the post is never sent for a marketing read at all; and
  2. a rule in the system prompt (SENSITIVITY_RULE) for what the words miss, under
     which the model answers with the fixed SENTINEL instead of advice.

Either way the result is stored as status='personal' with the sentinel as its text,
so the brief can say "personal post, not assessed" instead of showing a gap.

The lists are English and Croatian. Words that also appear in ordinary health and
fitness marketing ("bolest", "cancer", "diagnosis") only count when the post is
also about someone close or about the author ("moja mama", "my dad", "I was
diagnosed"); words that only ever mean a death ("preminula", "sprovod",
"passed away") count on their own. Diacritics are folded first, because captions
are typed with and without them ("sućut" / "sucut") in roughly equal measure.
"""
from __future__ import annotations

import re
import unicodedata

SENTINEL = "PERSONAL: not assessed"

SENSITIVITY_RULE = (
    "SENSITIVE POSTS: if the content is about a death, grief, mourning, serious "
    "illness, a diagnosis, or a personal crisis (the author's or someone close to "
    "them), do not assess it as marketing. Give no growth, positioning, engagement, "
    "hook or conversion advice about it. Reply with exactly this line and nothing "
    f"else: {SENTINEL}"
    " (when a JSON object is required, reply {\"status\": \"" + SENTINEL + "\"})."
)

# Only ever mean a death or a funeral. Stems match inflections (preminul -> preminula,
# preminulo, preminulog).
_STRONG = (
    r"preminu\w*", r"sprovod\w*", r"pogreb\w*", r"sahran\w*", r"sucut\w*",
    r"pocivaj u miru", r"pokoj dusi", r"pocivala? u miru", r"vjecni pokoj",
    r"passed away", r"funeral\w*", r"condolence\w*", r"rest in peace",
    r"in loving memory", r"\bobituar\w*", r"miscarriage", r"pobacaj\w*",
)
# Case-sensitive: lower-case "rip" is a verb, and "rip curl" is a brand.
_STRONG_CASED = (r"\bRIP\b", r"\bR\.I\.P\.?")

# Common in health marketing too, so they need a personal marker as well.
_WEAK = (
    r"bolest\w*", r"bolesc\w*", r"bolesn\w*", r"\brak\b", r"\braka\b", r"\brakom\b",
    r"dijagnoz\w*", r"dijagnostic\w*", r"kemoterapij\w*", r"zracenj\w*",
    r"\btug[aeiu]\b", r"\btugom\b", r"tuguj\w*", r"\bumro\b", r"\bumrla\b",
    r"\bumrli\b", r"\bsmrt\w*", r"bolnic\w*", r"izgubi[ol]a?\b", r"izgubili\b",
    r"\bcancer\b", r"\btumou?r\w*", r"diagnos\w*", r"\bchemo\w*", r"\bgrief\b",
    r"\bgriev\w*", r"\bdied\b", r"\bdeath\b", r"\bterminal\b", r"\bhospice\b",
    r"\bhospital\w*", r"\bmourn\w*", r"\blost my\b", r"\bdepress\w*",
    r"\bsuicid\w*", r"\bsamoubojstv\w*", r"\bdepresij\w*",
)
_PERSONAL = (
    r"\bmy (mom|mum|mother|dad|father|grand\w+|wife|husband|son|daughter|brother|"
    r"sister|friend|partner|family|baby|child)\b",
    r"\bi (was|am|have been|got) (diagnosed|sick|ill)\b", r"\bmy (diagnosis|illness)\b",
    r"\b(mama|mamu|mame|majka|majku|majke|tata|tatu|tate|otac|oca|baka|baku|bake|"
    r"djed|djeda|supruga|suprugu|suprug|muz|zena|zenu|sin|sina|kci|kcer|brat|brata|"
    r"sestra|sestru|prijatelj\w*|obitelj\w*|dijete|djeteta)\b",
    r"\b(moj|moja|moje|mog|moga|mojoj|mome|mojem|moju|mi je|meni je|sam (bio|bila))\b",
)


_FAREWELL_RE = re.compile(r"\bzbogom\b|\bodlazis\w*|\bfarewell\b|\bgoodbye\b|"
                          r"\bdovidjenja\b|\bnedostajat\w*|\bwe will miss\b", re.I)
_MOURNING = ("🖤", "🕊", "🕯")


def _fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t.replace("đ", "dj").replace("Đ", "Dj")


_STRONG_RE = re.compile("|".join(_STRONG), re.I)
_STRONG_CASED_RE = re.compile("|".join(_STRONG_CASED))
_WEAK_RE = re.compile("|".join(_WEAK), re.I)
_PERSONAL_RE = re.compile("|".join(_PERSONAL), re.I)


def personal(text: str | None) -> str | None:
    """The matched phrase when `text` reads as a personal/grief post, else None."""
    if not text:
        return None
    t = _fold(text)
    m = _STRONG_RE.search(t) or _STRONG_CASED_RE.search(t)
    if m:
        return m.group(0)
    w = _WEAK_RE.search(t)
    if w and _PERSONAL_RE.search(t):
        return w.group(0)
    # "Zbogom rokeru! 🖤" (a client's bistro, 10 Oct 2026): a farewell under a black heart
    # or a dove is how a death is announced, and it was the client's top post,
    # showcased first. A farewell alone ("Dragi <name> ... ❤️", a coach leaving)
    # is not grief, so the mourning symbol is required.
    f = _FAREWELL_RE.search(t)
    if f and any(sym in text for sym in _MOURNING):
        return f.group(0)
    return None


def item_text(row: dict) -> str:
    return "\n".join(str(row.get(k) or "") for k in
                     ("title", "body", "transcript", "screen_text"))


def is_sentinel(output) -> bool:
    """True when the model itself declined under SENSITIVITY_RULE."""
    if isinstance(output, dict):
        return any(SENTINEL in str(v) for v in output.values())
    return SENTINEL.lower() in str(output or "")[:200].lower()
