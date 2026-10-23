"""Which provider may see a payload, decided before anything is sent.

Until October 2026 every lens sent its prompt to the head of one provider chain, and
the head was DeepSeek: client captions, client video transcripts and audience
comments went to a hosted API that stores requests in China and may train on them,
with no DPA behind it. The chain was ordered by throughput, and nothing in the code
asked whose text it was.

So the question is now asked per payload, and it has two answers:

  PRIVATE  the account is own/client (consented data we hold on their behalf), a
           collab post is shared with such an account, or the payload carries
           audience comments (third parties who agreed to nothing). Goes only to
           providers.PRIVATE_PROVIDERS. None reachable -> the item is SKIPPED with a
           message; it is never handed to the public chain instead.
  PUBLIC   competitor/influencer post text only. Normal public chain.

Commenter names are pseudonymised whatever the destination: a lens never needs to
know who asked a question, only what they asked, and the local model being the
usual destination is not a reason to keep a habit that leaks the day it is not.
"""
from __future__ import annotations

import re

from .. import providers

PRIVATE = "private"
PUBLIC = "public"

PRIVATE_RELATIONSHIPS = ("own", "client")


def classify(*, relationship: str | None, has_comments: bool = False,
             shared_with_private: bool = False,
             private_context: bool = False) -> str:
    """PRIVATE or PUBLIC. Unknown relationship counts as private: failing closed
    costs one skipped item, failing open sends a client's text to the wrong place.

    shared_with_private: the payload holds an item owned by, or collab-linked to, an
    own/client account. private_context: free text the operator added (--context).
    """
    if has_comments or shared_with_private or private_context:
        return PRIVATE
    if relationship in ("competitor", "influencer"):
        return PUBLIC
    return PRIVATE


def touches_private(conn, item_ids) -> bool:
    """Does any of these items belong to, or is it shared with, an own/client
    account? A competitor's dossier can carry a collab post with a client, and
    that post is the client's content too."""
    ids = [int(i) for i in item_ids]
    if not ids:
        return False
    marks = ",".join("?" * len(ids))
    rels = ",".join(f"'{r}'" for r in PRIVATE_RELATIONSHIPS)
    return conn.execute(
        f"""SELECT 1 FROM item i JOIN account a ON a.id = i.account_id
             WHERE i.id IN ({marks}) AND a.relationship IN ({rels})
            UNION ALL
            SELECT 1 FROM item_account ia JOIN account a ON a.id = ia.account_id
             WHERE ia.item_id IN ({marks}) AND a.relationship IN ({rels})
            LIMIT 1""", ids + ids).fetchone() is not None


def chain_for(privacy: str) -> list[dict]:
    """The providers a payload of this class may reach, in order. May be empty."""
    if privacy == PRIVATE:
        return providers.private_chain()
    return providers.available()


def skip_reason(privacy: str) -> str:
    allowed = ",".join(providers.PRIVATE_PROVIDERS) or "(none)"
    return (f"private payload and no private provider is reachable "
            f"(CORPUS_PRIVATE_PROVIDERS={allowed}); start Ollama or set "
            f"MISTRAL_API_KEY. Not sent anywhere.")


def _label(n: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA. Spreadsheet columns, so it never runs out."""
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


_MENTION = re.compile(r"(?<![\w.])@[\w.]{2,30}")


class Pseudonyms:
    """Stable 'Commenter A/B/...' labels in order of first appearance.

    Stable for a given input, so the same comments render the same prompt and the
    analysis cache still hits. Names are also scrubbed from comment BODIES: people
    address each other by name ("@ana_k exactly this", "Marko, you're right").
    """

    def __init__(self):
        self.map: dict[str, str] = {}

    def name(self, real: str | None) -> str:
        key = (real or "").strip().lower()
        if not key:
            return "A commenter"
        if key not in self.map:
            self.map[key] = f"Commenter {_label(len(self.map))}"
        return self.map[key]

    def scrub(self, text: str | None) -> str:
        t = _MENTION.sub("@someone", text or "")
        # Longest first, so "Ana Kovač" is replaced before "Ana" can split it.
        for real in sorted(self.map, key=len, reverse=True):
            if len(real) < 3:
                continue
            t = re.sub(rf"(?<!\w){re.escape(real)}(?!\w)", self.map[real], t,
                       flags=re.I)
        return t


def pseudonymise(comments: list[dict]) -> list[dict]:
    """Copies of `comments` with author_name replaced and bodies scrubbed.

    Two passes, so a name that appears in an earlier body but only authors a later
    comment is still caught.
    """
    ps = Pseudonyms()
    for c in comments:
        ps.name(c.get("author_name"))
    out = []
    for c in comments:
        d = dict(c)
        d["author_name"] = ps.name(c.get("author_name"))
        d["body"] = ps.scrub(c.get("body"))
        out.append(d)
    return out
