"""Client brief: a self-contained HTML report from the corpus.

This is the deliverable, which is why it is the first surface rather than a
dashboard. Everything is inlined (no CDN, no external CSS, no fonts) so the file can
be emailed, opened offline, or printed to PDF without breaking.

It states its own provenance. A brief built from scraped public data says so, and it
says plainly which metrics are structurally unavailable, because the alternative is a
client assuming the reach number is missing rather than impossible. Reach,
impressions, watch time and follows-from-post exist only inside the account owner's
own analytics; no scraper produces them for a third party, and any tool claiming
otherwise is fabricating.
"""
from __future__ import annotations

import base64
import html
import json
import hashlib
import subprocess
import mimetypes
import re
import statistics
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import store
from . import brief_i18n as i18n
from .brief_i18n import t as _t

# The analysis side ships the text-quality check. Until it is merged (or on an
# install without it) nothing is treated as garbled, which is the old behaviour.
try:
    from .textq import garbled
except ImportError:
    garbled = lambda text: False  # noqa: E731

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# A brief covers the twelve months before the account's last sync unless asked
# otherwise. All-time numbers mixed a 2022 back-catalogue into "how often do you
# post": a client who posted weekly in 2023 and twice this year read 0.2/wk and
# both halves of that were wrong.
DEFAULT_WINDOW_DAYS = 365
# Older than this and the brief says so in red at the top. Real case: briefs built
# in October from an August sync, with nothing on the page saying the numbers were
# eleven weeks old.
STALE_DAYS = 14
# The shortest span a posting rate is divided by. One post the day before a sync
# otherwise reads as 7 a week.
MIN_RATE_DAYS = 28

# Metrics that only the account owner can ever see. Named explicitly so the brief can
# tell a client why a column is absent instead of leaving a gap.
OWNER_ONLY = ("impressions", "reach", "watch time", "completion rate",
              "follows from post", "profile visits", "saves (Instagram)")


@dataclass
class Section:
    title: str
    html: str


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


# Only these schemes may reach an href. Post and profile URLs come from scraped vendor
# payloads, so they are attacker-influenceable: `javascript:alert(1)` in a
# companyLinkedinUrl rendered as a live link before this existed.
_SAFE_SCHEMES = ("http://", "https://", "mailto:")


def _href(url) -> str | None:
    """Escaped URL if the scheme is safe, else None so the caller omits the link."""
    if not url:
        return None
    u = str(url).strip()
    if not u.lower().startswith(_SAFE_SCHEMES):
        return None
    return html.escape(u, quote=True)


def _md(text: str) -> str:
    """Render the small markdown subset the lenses actually emit.

    Escape first, then re-introduce markup, so post text remains inert while
    **bold**, headings and bullets stop showing up as literal asterisks in a
    client-facing document. Anything richer than this belongs in a real renderer,
    but a full markdown dependency for three constructs is not worth the surface.
    """
    out, lines = [], _esc(text or "").split("\n")
    in_list = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        if stripped.startswith("|"):
            continue  # pipe tables from a lens are noise in a narrow column
        if re.match(r"^#{1,6}\s", stripped):
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f"<h4>{re.sub(r'^#{1,6}\\s*', '', stripped)}</h4>")
            continue
        if re.match(r"^[-*\u2022\u00b7\u25aa]\s+", stripped):
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{re.sub(r'^[-*•·▪]\s+', '', stripped)}</li>")
            continue
        if in_list:
            out.append("</ul>"); in_list = False
        out.append(f"<p>{stripped}</p>")
    if in_list:
        out.append("</ul>")

    body = "".join(out)
    body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", body)
    body = re.sub(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])", r"<em>\1</em>", body)
    body = re.sub(r"`([^`\n]+?)`", r"<code>\1</code>", body)
    return body


def _fmt(n) -> str:
    if n is None:
        return "-"
    n = int(n)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


# ── lenses ─────────────────────────────────────────────────────────────────

# Client-facing lenses in the order a reader meets them on each post: what the post
# was for and whether it worked first, the repeatable pattern second, the mechanics
# after. Anything else in templates/ that is client-safe follows alphabetically.
LENS_ORDER = ("content-audit", "what-works", "hook-teardown", "cta-teardown",
              "positioning")
# Top-level templates that are nonetheless written for the creator ("which of the
# creator's pillars", "adapted to the creator's voice"). In a client's document they read as
# someone else's notes left in the file.
CREATOR_LENSES = frozenset({"pillar-tag", "script-outline"})
ACCOUNT_SUMMARY = "account-summary"
# The analysis agent's marker for a post it declined to assess (a bereavement, a
# family post). Shown as a plain note, never as the raw sentinel.
PERSONAL = "PERSONAL:"


def _template_dir() -> Path:
    from .analyze.templates import TEMPLATE_DIR
    return Path(TEMPLATE_DIR)


def client_lenses(directory: str | Path | None = None) -> list[str]:
    """The lenses a client brief shows by default, in display order.

    Derived from the templates directory rather than hard-coded, so a new client
    lens shows up without touching this file, and anything under templates/personal
    can never reach a client: the brief used to show whichever result was newest per
    post, and triage ("act / reference / skip") is a creator's verdict on someone
    else's work. A template can also opt out with `audience: personal` in its
    frontmatter.
    """
    d = Path(directory) if directory else _template_dir()
    personal = {p.stem for p in (d / "personal").glob("*.md")}
    ok = []
    for p in sorted(d.glob("*.md")):
        if p.stem in personal or p.stem in CREATOR_LENSES or p.stem == ACCOUNT_SUMMARY:
            continue
        head = p.read_text(encoding="utf-8", errors="replace")[:600].lower()
        if re.search(r"^audience:\s*['\"]?(personal|creator)", head, re.M):
            continue
        ok.append(p.stem)
    rank = {s: n for n, s in enumerate(LENS_ORDER)}
    return sorted(ok, key=lambda s: (rank.get(s, len(LENS_ORDER)), s))


def _lens_names() -> dict[str, str]:
    try:
        from .analyze.templates import load_all
        return {k: v.name for k, v in load_all(_template_dir()).items()}
    except Exception:
        return {}


def _account_summary(conn, account_id: int) -> dict | None:
    """The account-level read (exec summary, three priorities, four-week plan).

    Validated rather than trusted: it is model output, and a half-shaped object
    rendered as-is put "None" and stray brackets in front of the client.
    """
    r = conn.execute(
        """SELECT output_json, output_text FROM a.analysis_result
            WHERE target_kind='account' AND target_id=? AND template=?
            ORDER BY created_at DESC, id DESC LIMIT 1""",
        (account_id, ACCOUNT_SUMMARY)).fetchone()
    if not r:
        return None
    try:
        d = json.loads(r["output_json"] or r["output_text"] or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    summary = str(d.get("summary") or "").strip()
    pri = [p for p in (d.get("priorities") or []) if isinstance(p, dict)
           and str(p.get("title") or "").strip()][:3]
    plan = [w for w in (d.get("plan") or []) if isinstance(w, dict)
            and str(w.get("idea") or "").strip()][:4]
    if not (summary or pri or plan):
        return None
    return {"summary": summary, "priorities": pri, "plan": plan}


def _anchor(conn, acct: dict) -> int | None:
    """When this account's data was last captured: its last sync, or the newest
    metric reading if that is later (a comments fetch refreshes counts too)."""
    newest = conn.execute(
        f"SELECT MAX(metrics_at) FROM item WHERE {store.account_items('')}",
        (acct["id"], acct["id"])).fetchone()[0]
    got = [v for v in (acct.get("last_scraped_at"), newest) if v]
    return max(got) if got else None


def gather(conn, account_id: int, *, since: int | None = None,
           days: int | None = None, analysis_db: str | None = None,
           template: str | None = None, lang: str = "en") -> dict:
    """Everything one account's section of a brief needs.

    `days` is counted back from the account's LAST SYNC, not from now. Counting from
    now made a brief built from stale data quietly lose its newest posts off the
    front of the window, and two accounts synced weeks apart were compared over
    different periods without anyone being told. An explicit `since` (a timestamp)
    still wins, for callers that mean a calendar date.
    """
    acct = dict(conn.execute("SELECT * FROM account WHERE id=?", (account_id,)).fetchone())
    anchor = _anchor(conn, acct)
    if since is None and days:
        since = (anchor or store.now()) - int(days) * 86400

    # Its own posts plus collab posts stored under a co-author (store.account_items).
    where, args = [store.account_items(""), "deleted_at IS NULL"], [account_id, account_id]
    if since:
        where.append("published_at >= ?")
        args.append(int(since))
    items = [dict(r) for r in conn.execute(
        f"SELECT * FROM item WHERE {' AND '.join(where)} ORDER BY published_at DESC",
        args)]
    # A collab post another account wrote (Instagram's co-author posts) sits on this
    # feed as an original, not a repost. Field test: a cleaning company's post on a
    # restaurant's feed landed under "written by you" and "your weakest openings",
    # and its content-audit gave the restaurant the cleaning company's advice.
    for it in items:
        it["_foreign"] = foreign_author(it, acct)

    # What the store holds outside the window, so the page can say what it left out
    # and the posting rate knows whether the history even reaches back that far.
    hist = conn.execute(
        f"""SELECT MIN(published_at) first,
                   SUM(CASE WHEN COALESCE(is_repost,0)=0 AND published_at < ?
                            THEN 1 ELSE 0 END) older
              FROM item WHERE {store.account_items("")} AND deleted_at IS NULL""",
        (int(since or 0), account_id, account_id)).fetchone()

    followers = conn.execute(
        """SELECT followers FROM account_metric_snapshot WHERE account_id=?
           ORDER BY captured_at DESC LIMIT 1""", (account_id,)).fetchone()

    # Which adapter last succeeded for this account decides what the brief may claim.
    last_ok = conn.execute(
        """SELECT adapter FROM scrape_run WHERE account_id=? AND status='ok'
            ORDER BY started_at DESC LIMIT 1""", (account_id,)).fetchone()
    adapter = last_ok["adapter"] if last_ok else None

    analyses: dict[int, list[dict]] = {}
    summary = None
    if analysis_db:
        from .analyze import run as ar
        ar.attach(conn, analysis_db)
        # Every lens per post, newest result of each. Keeping only the newest row
        # per POST made the lens on show a matter of which batch ran last, so two
        # thirds of what was paid for never reached the page.
        wanted = [template] if template else client_lenses()
        rank = {s: n for n, s in enumerate(wanted)}
        seen: set[tuple[int, str]] = set()
        # Scoped to this account's items. Unscoped, every account in a combined
        # brief loaded the entire analysis table, which is O(accounts x total rows):
        # invisible at 125 analyses, real at a 50-account roster with 20k of them.
        for r in conn.execute(
            f"""SELECT target_id, template, output_text, output_json
                 FROM a.analysis_result WHERE target_kind='item'
                  AND target_id IN (SELECT id FROM main.item
                                     WHERE {store.account_items("")})
                ORDER BY created_at DESC, id DESC""", (account_id, account_id)):
            key = (r["target_id"], r["template"])
            if r["template"] not in rank or key in seen:
                continue
            seen.add(key)
            analyses.setdefault(r["target_id"], []).append(dict(r))
        for results in analyses.values():
            results.sort(key=lambda x: rank[x["template"]])
        summary = _account_summary(conn, account_id)

    # Media for the detail cards: a poster or the first keyframe, whichever exists.
    by_item: dict[int, dict] = {}
    for m in conn.execute(
            f"""SELECT item_id, role, local_path, remote_url, seq FROM media
                WHERE item_id IN (SELECT id FROM item WHERE {store.account_items("")})
                ORDER BY item_id,
                         -- A locally stored file beats a nicer-sounding role that
                         -- only has a remote URL: the remote one is a signed CDN
                         -- link that expires and cannot be embedded at all.
                         (local_path IS NULL),
                         CASE role WHEN 'poster' THEN 0 WHEN 'thumbnail' THEN 1
                                   WHEN 'keyframe' THEN 2 ELSE 3 END, seq""",
            (account_id, account_id)):
        by_item.setdefault(m["item_id"], dict(m))

    # Audience comments. Author replies come along, because "do they answer" is a
    # finding, but they are tagged so they never get counted as audience voice.
    # The post's opening line comes along so each quote can say what it answered.
    cmts = [dict(r) for r in conn.execute(
        f"""SELECT c.body, c.author_name, c.reactions, c.published_at,
                  c.is_author_reply, c.item_id, i.url item_url,
                  COALESCE(i.body, i.title, '') item_text
             FROM comment c JOIN item i ON i.id = c.item_id
            WHERE {store.account_items()} AND COALESCE(i.is_repost,0) = 0
              AND i.deleted_at IS NULL {"AND i.published_at >= ?" if since else ""}
            ORDER BY COALESCE(c.reactions,0) DESC""",
        (account_id, account_id) + ((int(since),) if since else ()))]

    # Who is "us": the subject itself and every account marked own. The owner's "we
    # love u bro" with four likes headed the "what got agreement" list on a
    # client's brief, as if it were the audience speaking.
    us = {str(v).strip().lower() for v in (acct.get("handle"), acct.get("display_name"))
          if v}
    for r in conn.execute("SELECT handle, display_name FROM account "
                          "WHERE relationship='own'"):
        us |= {str(v).strip().lower() for v in (r["handle"], r["display_name"]) if v}

    return {
        "account": acct,
        "items": items,
        "comments": cmts,
        "thumbs": by_item,
        "followers": followers["followers"] if followers else None,
        "analyses": analyses,
        "summary": summary,
        "adapter": adapter,
        # Only a first-party export carries impressions and reach. Every other route,
        # including one pointed at an account you own, sees exactly what a stranger sees.
        "via_consented_export": bool(adapter and "export" in adapter),
        "anchor": anchor,
        "since": since,
        "days": days if since and days else None,
        "history_start": hist["first"],
        "older": int(hist["older"] or 0) if since else 0,
        "us": us,
        "lang": i18n.lang_of(lang),
    }


def _engagement(it: dict) -> int:
    """Interactions only. Views are reach, not engagement, and mixing them makes a
    video look a thousand times more engaging than a text post."""
    return sum(int(it.get(k) or 0) for k in ("m_reactions", "m_comments", "m_shares"))


def foreign_author(item: dict, account: dict) -> bool:
    """True when the stored author is a different account than the one whose feed
    this is. Unknown authors (most routes leave it blank on originals) count as own."""
    a = (item.get("author_handle") or "").strip().lstrip("@").lower()
    return bool(a) and not item.get("is_repost") and a != (account.get("handle") or "").lower()


def _own(items: list[dict]) -> list[dict]:
    """Posts this account actually wrote.

    A repost is somebody else's writing on your feed. Counting it as yours inflates
    every number in the brief and points the analysis at a stranger. Real example:
    21 LinkedIn items on one account, 20 of them reposts, and the "top post" at 49
    reactions was written by someone else entirely.
    """
    return [i for i in items if not i.get("is_repost") and not i.get("_foreign")]


def _personal_ids(data: dict) -> set:
    """Posts about grief, illness or a personal crisis: by the same keyword check
    the lenses use, or because a lens declined them. Counted like any post, but
    never held up as an example: a bereavement post sat at #1 of "Top posts" with
    its transcript quoted under "What your videos say" in a client brief."""
    if "_personal" not in data:
        from .analyze import sensitivity
        ids = {it["id"] for it in data["items"]
               if sensitivity.personal(sensitivity.item_text(it))}
        for iid, results in (data.get("analyses") or {}).items():
            if any(_is_personal(r) for r in results):
                ids.add(iid)
        data["_personal"] = ids
    return data["_personal"]


def _best_shown(data: dict, items: list, engs: list) -> int:
    skip = _personal_ids(data)
    shown = [_engagement(i) for i in items if i["id"] not in skip]
    return max(shown) if shown else (max(engs) if engs else 0)


def _showcase(data: dict) -> list[dict]:
    """Own posts that may be quoted as examples (top posts, openings, videos)."""
    skip = _personal_ids(data)
    return [i for i in _own(data["items"]) if i["id"] not in skip]


def _reposts(items: list[dict]) -> list[dict]:
    """Somebody else's writing on this feed: reposts, and collabs they authored."""
    return [i for i in items if i.get("is_repost") or i.get("_foreign")]


def _has_views(items: list[dict]) -> bool:
    return any(it.get("m_views") for it in items)


def display_name(acct: dict) -> str:
    """The name a client recognises. Instagram display names carry a tagline
    ("Jordan Novak | Helping teams sell better"), which made the tagline the
    title of the client's own brief."""
    name = (acct.get("display_name") or "").split("|")[0].strip()
    return name or acct.get("handle") or ""


def _you(data: dict) -> bool:
    """Is the reader the subject? Own and client briefs talk to "you"; a
    competitor brief talks about "them". Mixing the two in one document read as
    if two people had written it."""
    return (data.get("account") or {}).get("relationship") in ("own", "client")


def _v(data: dict, you: str, they: str, **kw) -> str:
    return _t(data, you if _you(data) else they, **kw)


def _per_week(data: dict, items: list[dict]) -> float:
    """Posts per week over the period the brief covers.

    Divided by the length of the window (ending at the last sync), or, when the
    stored history does not reach back that far, by the time from the first stored
    post to the last sync. Never by first-to-last post: that span made one post
    read 1.0/wk and two posts a day apart read 7/wk.
    """
    if not items:
        return 0.0
    end = data.get("anchor") or store.now()
    first_in = min(i["published_at"] for i in items)
    since, first_held = data.get("since"), data.get("history_start")
    start = since if since and first_held is not None and first_held <= since \
        else first_in
    span = max(MIN_RATE_DAYS, (end - start) / 86400)
    return len(items) / (span / 7)


def _rate_fmt(data, per_week: float) -> str:
    """Below one a week, per month reads as a number; "0.1/wk" reads as a typo."""
    if per_week >= 1 or per_week == 0:
        return _t(data, "{n}/wk", n=i18n.num(f"{per_week:.1f}", data.get("lang")))
    return _t(data, "{n}/mo", n=i18n.num(f"{per_week * 52 / 12:.1f}", data.get("lang")))


def _period(data: dict) -> str:
    """What the numbers cover, said once in plain words."""
    days, anchor = data.get("days"), data.get("anchor")
    if not days or not anchor:
        return _t(data, "every stored post")
    when = i18n.date(anchor, data.get("lang"))
    if days == 365:
        return _t(data, "the 12 months to {d}", d=when)
    return _t(data, "the {n} days to {d}", n=days, d=when)


# ── sections ────────────────────────────────────────────────────────────────

def _cards(rows) -> str:
    return "<div class='cards'>" + "".join(
        f"<div class='card'><div class='k'>{_esc(k)}</div>"
        f"<div class='v'>{_esc(v)}</div>"
        f"<div class='hint'>{_esc(h)}</div></div>" for k, v, h in rows
    ) + "</div>"


QUIET_DAYS = 90


def s_summary(data: dict) -> Section:
    all_items = data["items"]
    items = _own(all_items)
    shared = _reposts(all_items)
    title = _t(data, "Summary")
    if not items:
        note = (_t(data, "No original posts in range. {n} repost(s) of other people's "
                         "content were found and are listed separately.", n=len(shared))
                if shared else _t(data, "No content in range."))
        return Section(title, f"<p class='muted'>{_esc(note)}</p>")

    engs = [_engagement(i) for i in items]
    per_week = _per_week(data, items)
    followers = data["followers"]

    # Split rather than combined, and every card says what it means. "Cadence" and
    # "median" are jargon to a client; a number nobody can interpret is decoration.
    tot_r = sum(int(i.get("m_reactions") or 0) for i in items)
    tot_c = sum(int(i.get("m_comments") or 0) for i in items)
    tot_s = sum(int(i.get("m_shares") or 0) for i in items)

    cards = [
        (_t(data, "Original posts"), str(len(items)),
         _v(data, "written by you", "written by this account")),
        (_t(data, "Reactions"), _fmt(tot_r), _t(data, "likes and other reactions")),
        (_t(data, "Comments"), _fmt(tot_c), _t(data, "replies left on the posts")),
    ]
    if tot_s:
        cards.append((_t(data, "Shares"), _fmt(tot_s), _t(data, "reposts by other people")))
    if _has_views(items):
        cards.append((_t(data, "Views"),
                      _fmt(sum(int(i.get("m_views") or 0) for i in items)),
                      _t(data, "times the video was played")))
    cards += [
        (_t(data, "Typical post"), _fmt(int(statistics.median(engs)) if engs else 0),
         _t(data, "the middle post, not the average")),
        # The best post that may be shown: a death notice (a client's 3.2k
        # "Zbogom rokeru! 🖤") is kept out of Top posts, and a tile reading 3.2k
        # above a table topped by 185 looked like a wrong number.
        (_t(data, "Best post"), _fmt(_best_shown(data, items, engs)),
         _t(data, "highest single post")),
        (_t(data, "Posting rate"), _rate_fmt(data, per_week),
         _t(data, "over {p}", p=_period(data))),
    ]
    note = None
    if followers:
        cards.append((_t(data, "Followers"), _fmt(followers), _t(data, "at last check")))
        if followers >= 1000:
            rate = statistics.median(engs) / followers * 100
            cards.append((_t(data, "Engagement rate"), i18n.num(f"{rate:.2f}%", data.get("lang")),
                          _t(data, "typical post vs follower count")))
        else:
            # Below ~1000 followers the ratio stops describing the audience: one
            # reel that travels to non-followers puts it in four figures, which
            # looks like a rendering fault. Say so rather than dropping the card
            # silently and leaving the reader to wonder what is missing.
            note = _t(data, "Engagement rate is not shown: at {n} followers a single "
                            "post that reaches beyond the audience distorts the "
                            "percentage past the point of meaning.", n=_fmt(followers))

    body = _cards(cards)
    if note:
        body += f"<p class='muted'>{_esc(note)}</p>"
    # An account that has gone quiet reads like a slow one from the cards alone:
    # remisens-hotel-group's 0.3/mo hid that nothing had been posted for six
    # months before the sync (field test, 2026-10-23).
    newest = max((i.get("published_at") or 0) for i in items)
    end = data.get("anchor") or store.now()
    if newest and end - newest > QUIET_DAYS * 86400:
        body += "<p class='note'>" + _esc(_t(
            data, "No new post since {d}, {n} days before the last sync.",
            d=i18n.date(newest, data.get("lang")), n=(end - newest) // 86400)) + "</p>"
    if shared:
        body += "<p class='muted'>" + _esc(_t(
            data, "Excludes {n} repost(s) of other people's posts, which are shown "
                  "separately below. Reposts are amplification, not authorship, and "
                  "their engagement belongs to the original author.",
            n=len(shared))) + "</p>"
    return Section(title, body)


def _kind(data, kind: str) -> str:
    return _t(data, (kind or "").replace("_", " "))


def s_top(data: dict, n: int = 10) -> Section:
    title = _t(data, "Top posts")
    items = sorted(_showcase(data), key=_engagement, reverse=True)[:n]
    if not items:
        return Section(title, "")
    own = _own(data["items"])
    show_views = _has_views(own)
    # Instagram actors report no shares at all, so the column was a row of dashes
    # across the whole table. An empty column reads as missing data rather than as
    # a metric the platform does not expose.
    show_shares = any(i.get("m_shares") for i in own)
    rows = []
    for it in items:
        when = i18n.date_num(it["published_at"], data.get("lang"))
        text = _clip(it.get("body") or it.get("title") or "", 150)
        # A blank cell reads as broken export rather than as "this post had no
        # caption", which on a carousel is the finding worth reporting.
        cell = (_esc(text) if text.strip() else
                f"<span class='muted'>{_esc(_t(data, 'no caption'))}</span>")
        href = _href(it.get("url"))
        link = f"<a href='{href}'>{_esc(_t(data, 'open'))}</a>" if href else ""
        rows.append(
            f"<tr>{_thumb_cell(data, it)}"
            f"<td class='nowrap'>{when}</td><td>{_esc(_kind(data, it['kind']))}</td>"
            f"<td class='num'>{_fmt(it.get('m_reactions'))}</td>"
            f"<td class='num'>{_fmt(it.get('m_comments'))}</td>"
            + (f"<td class='num'>{_fmt(it.get('m_shares'))}</td>" if show_shares else "")
            + (f"<td class='num'>{_fmt(it.get('m_views'))}</td>" if show_views else "")
            + f"<td class='num strong'>{_fmt(_engagement(it))}</td>"
            f"<td>{cell}</td><td>{link}</td></tr>")
    th = lambda s, num=True: (f"<th class='num'>{_esc(_t(data, s))}</th>" if num
                              else f"<th>{_esc(_t(data, s))}</th>")
    return Section(title, (
        "<table><thead><tr><th></th>" + th("Date", False) + th("Type", False)
        + th("Reactions") + th("Comments")
        + (th("Shares") if show_shares else "")
        + (th("Views") if show_views else "")
        + th("Total") + th("Excerpt", False) + "<th></th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"))


def s_formats(data: dict) -> Section:
    title = _t(data, "What formats work")
    items = _own(data["items"])
    if not items:
        return Section(title, "")
    by: dict[str, list[int]] = {}
    for it in items:
        by.setdefault(it["kind"], []).append(_engagement(it))

    # Share of the account's own baseline, which is the decision-relevant number:
    # a format at 40% of output and 0.6x is the finding, and a bare median never
    # shows it.
    baseline = statistics.median([_engagement(i) for i in items]) or 0
    thin = False
    rows = []
    for kind, engs in sorted(by.items(), key=lambda kv: -statistics.median(kv[1])):
        med = statistics.median(engs)
        mult = (i18n.num(f"{med / baseline:.1f}", data.get("lang")) + "\u00d7"
                if baseline else "\u2013")
        # Below three posts a median is a coin flip, so it is marked rather than
        # quietly presented next to formats with real support.
        mark = ""
        if len(engs) < 3:
            mark, thin = " <span class='thin'>&#8224;</span>", True
        rows.append(
            f"<tr><td>{_esc(_kind(data, kind))}{mark}</td>"
            f"<td class='num'>{len(engs)}</td>"
            f"<td class='num'>{_fmt(int(med))}</td>"
            f"<td class='num'>{mult}</td>"
            f"<td class='num'>{_fmt(max(engs))}</td></tr>")

    note = "<p class='muted'>" + _v(
        data,
        "Median rather than mean: one viral post otherwise makes a format look "
        "reliable when it was a single outlier. &ldquo;vs typical&rdquo; compares "
        "each format against your own middle post, so 1.0&times; is par for you, "
        "not for the platform.",
        "Median rather than mean: one viral post otherwise makes a format look "
        "reliable when it was a single outlier. &ldquo;vs typical&rdquo; compares "
        "each format against this account's own middle post, so 1.0&times; is par "
        "for them, not for the platform.") + "</p>"
    if thin:
        note += "<p class='muted'>&#8224; " + _t(
            data, "Fewer than three posts. The median of two posts is not a pattern; "
                  "treat these rows as unproven rather than as a recommendation."
        ) + "</p>"
    th = lambda s, num=True: (f"<th class='num'>{_esc(_t(data, s))}</th>" if num
                              else f"<th>{_esc(_t(data, s))}</th>")
    return Section(title, (
        "<table><thead><tr>" + th("Format", False) + th("Posts")
        + th("Median engagement") + th("vs typical") + th("Best")
        + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>" + note))


def _phrases(*words: str) -> re.Pattern:
    """Whole-word, case-insensitive phrase matcher. A trailing * allows an ending
    (Croatian inflects: cijena, cijene, cijenu). Whole words because substrings
    misfired: "roi" matched the Croatian "proizvod", so every product post counted
    as showing a client result."""
    parts = []
    for w in words:
        stem, star = (w[:-1], True) if w.endswith("*") else (w, False)
        p = r"(?<!\w)" + re.escape(stem)
        p += r"\w*" if star else (r"(?!\w)" if stem[-1:].isalnum() else "")
        parts.append(p)
    return re.compile("|".join(parts), re.I)


# Commercially load-bearing subjects. The persuasive finding in a content audit is
# almost always a zero: an account posting daily that has never once mentioned price
# or shown a result does not have a cadence problem. Kept as plain phrase lists
# rather than an LLM pass so the count is reproducible and the reader can check it.
# English and Croatian side by side: the clients write in Croatian, and an
# English-only list reported "never" for a CTA they used in every post.
#
# Tightened after an audit of real briefs: a bare "%" matched "100%" in "100%
# agree", "apply" matched "apply this to your life", "charge" matched "in charge
# of", and "first," matched every "First, congrats".
_GAP_TOPICS = [
    ("Pricing named", _phrases(
        "pricing", "how much", "per month", "per hour", "retainer", "costs you",
        "price is", "i charge", "we charge", "charge per", "budget", "invoice",
        "deposit",
        "cijena", "cijene", "cijenu", "cijenom", "koliko košta", "koliko je koštalo",
        "po satu", "mjesečno", "budžet", "popust*", "akcija", "ponuda vrijedi"),
     re.compile(r"[$€£]\s?\d|\d\s?(?:€|eur\b|eura\b|kn\b|kuna\b)", re.I)),
    ("A client result shown", _phrases(
        "case study", "before and after", "testimonial", "roi", "we grew", "i grew",
        "helped them", "their revenue", "for a client", "client went",
        "results speak", "went from",
        "studija slučaja", "prije i poslije", "za klijenta", "naš klijent",
        "našem klijentu", "klijent je", "klijentica je", "povećali smo",
        "udvostručili smo", "iskustvo klijenata", "recenzij*"), None),
    ("How it works", _phrases(
        "step 1", "step one", "here's how", "heres how", "how i ", "how we ",
        "the process", "framework", "walkthrough", "tutorial",
        "korak 1", "prvi korak", "evo kako", "kako smo ", "proces", "vodič",
        "upute"), None),
    ("Proof and numbers", _phrases(
        "revenue", "doubled", "tripled", "increased", "generated", "saved us",
        "x more", "figures", "per week", "in 30 days",
        "prihod*", "udvostruč*", "utrostruč*", "povećan*", "zaradil*", "ušted*"),
     # A percentage, except 100%: "100% agree" and "100% natural" are idioms,
     # and a real "+100%" result almost always comes with "doubled" or similar.
     re.compile(r"(?<![\d.,])(?!100\s?%)\d{1,4}(?:[.,]\d+)?\s?%|\b\d+x\b", re.I)),
    ("A clear next step", _phrases(
        "dm me", "book a", "link in bio", "get in touch", "work with me",
        "apply now", "apply here", "apply today", "sign up", "comment below",
        "comment the word", "reach out", "let's talk", "send me a",
        "link u opisu", "link u biu", "link u bio", "javi se", "javite se",
        "pošalji poruku", "pošaljite poruku", "pošalji mi", "piši mi", "pišite nam",
        "rezerviraj*", "prijavi se", "prijavite se", "komentiraj*",
        "napiši u komentar*", "kontaktiraj*", "nazovi*", "naruči*"), None),
    ("Founder story", _phrases(
        "i started", "when i was", "my journey", "years ago", "i failed", "i quit",
        "growing up", "back then", "i used to",
        "kad sam bio", "kad sam bila", "kad sam počeo", "kad sam počela",
        "počeo sam", "počela sam", "moj put", "moja priča", "odrastao", "odrasla"),
     re.compile(r"prije \d+ godin", re.I)),
]


def _gap_hit(blob: str, words: re.Pattern, extra: re.Pattern | None) -> bool:
    return bool(words.search(blob) or (extra and extra.search(blob)))


def s_gaps(data: dict) -> Section:
    """What they never talk about.

    Deliberately reports absence, not a score. Every count links to a definition the
    reader can audit, because "0 of 42" is only persuasive if the matching rule is
    visible.
    """
    title = _v(data, "What you never post about", "What they never post about")
    items = _own(data["items"])
    if len(items) < 10:
        return Section(title, "")

    blobs = []
    for it in items:
        blobs.append(" ".join(str(it.get(f) or "") for f in
                              ("title", "body", "transcript", "screen_text")).lower())

    rows = []
    for label, words, extra in _GAP_TOPICS:
        n = sum(1 for b in blobs if _gap_hit(b, words, extra))
        pct = n / len(blobs) * 100
        cls = "gap-zero" if n == 0 else ("gap-low" if pct < 15 else "")
        verdict = ("never" if n == 0 else "rarely" if pct < 15 else
                   "sometimes" if pct < 40 else "often")
        rows.append(
            f"<tr class='{cls}'><td>{_esc(_t(data, label))}</td>"
            f"<td class='num'>{_esc(_t(data, '{n} of {m}', n=n, m=len(blobs)))}</td>"
            f"<td class='num'>{pct:.0f}%</td><td>{_esc(_t(data, verdict))}</td></tr>")

    th = lambda s, num=True: (f"<th class='num'>{_esc(_t(data, s))}</th>" if num
                              else f"<th>{_esc(_t(data, s))}</th>")
    return Section(title, (
        "<p class='muted'>" + _t(
            data, "Six subjects that tend to decide whether an audience becomes a "
                  "customer, counted across every original post including video "
                  "transcripts and on-screen text. A zero here is usually more "
                  "actionable than any engagement number on the page.") + "</p>"
        "<table><thead><tr>" + th("Subject", False) + th("Posts") + th("Share")
        + th("How often", False) + "</tr></thead><tbody>"
        + "".join(rows) + "</tbody></table>"
        "<p class='muted'>" + _t(
            data, "Counted by matching the phrases people actually use for each "
                  "subject, in English and Croatian, not by interpretation, so every "
                  "number is reproducible and can be checked against the post list "
                  "below. The phrases are deliberately narrow: merely saying "
                  "&ldquo;client&rdquo; is not counted as showing a client result. "
                  "It will therefore undercount a subject discussed in unusual "
                  "words.") + "</p>"))


def _tz():
    """The audience's clock for day-of-week buckets: $CORPUS_TZ (an IANA name such
    as Europe/Zagreb), else this machine's zone. UTC filed every post made after
    midnight Croatian time under the previous day, so Monday's best slot showed up
    as Sunday."""
    import os
    from datetime import datetime
    name = os.environ.get("CORPUS_TZ")
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


def _local(ts: int):
    from datetime import datetime
    return datetime.fromtimestamp(ts, _tz())


def _tz_label() -> str:
    tz = _tz()
    return getattr(tz, "key", None) or str(tz)


def s_cadence(data: dict) -> Section:
    title = _v(data, "When you post", "When they post")
    items = _own(data["items"])
    if not items:
        return Section(title, "")
    by_day: dict[int, list[int]] = {}
    for it in items:
        d = _local(it["published_at"]).weekday()
        by_day.setdefault(d, []).append(_engagement(it))

    # Scale to the best day that has enough posts to mean anything. Scaling to the
    # raw maximum lets a single viral post pin one day at 100% and flatten every
    # other bar to zero, which is exactly the situation a client hires you in.
    solid = [statistics.median(v) for v in by_day.values() if len(v) >= 2]
    peak = max(solid or [statistics.median(v) for v in by_day.values()] or [1]) or 1

    bars = []
    one = _esc(_t(data, "1 post"))
    for i, name in enumerate(i18n.DAYS[i18n.lang_of(data)]):
        engs = by_day.get(i, [])
        med = statistics.median(engs) if engs else 0
        # Cap rather than overflow: a thin day above the scale still reads as
        # off-the-chart without blowing the layout out.
        pct = min(100, int(med / peak * 100)) if peak else 0
        thin = " thin" if 0 < len(engs) < 2 else ""
        note = f" <span class='muted'>&#8226; {one}</span>" if thin else ""
        bars.append(
            f"<div class='barrow'><div class='barlab'>{name}</div>"
            f"<div class='bartrack'><div class='bar{thin}' style='width:{pct}%'>"
            f"</div></div>"
            f"<div class='barval'>{_fmt(int(med))} <span class='muted'>"
            f"({len(engs)})</span>{note}</div></div>")
    return Section(title, "<div class='bars'>" + "".join(bars) + "</div>"
                   "<p class='muted'>" + _t(
                       data, "Days in {tz} time. Median engagement by weekday, post "
                             "count in brackets. Days with a single post are hatched "
                             "and excluded from the scale, because one post is an "
                             "anecdote rather than a pattern.",
                       tz=html.escape(_tz_label())) + "</p>")


def s_reposts(data: dict, n: int = 8) -> Section:
    """What this account amplifies, credited to whoever wrote it."""
    title = _v(data, "What you amplify", "What they amplify")
    shared = sorted(_reposts(data["items"]), key=_engagement, reverse=True)[:n]
    if not shared:
        return Section(title, "")
    rows = []
    for it in shared:
        when = i18n.date_num(it["published_at"], data.get("lang"))
        text = _clip(it.get("body") or it.get("title") or "", 130)
        cell = (_esc(text) if text.strip() else
                f"<span class='muted'>{_esc(_t(data, 'no caption'))}</span>")
        who = it.get("author_name") or _t(data, "unknown")
        href = _href(it.get("url"))
        link = f"<a href='{href}'>{_esc(_t(data, 'open'))}</a>" if href else ""
        rows.append(
            f"<tr><td class='nowrap'>{when}</td><td>{_esc(who)}</td>"
            f"<td class='num'>{_fmt(_engagement(it))}</td>"
            f"<td>{cell}</td><td>{link}</td></tr>")
    return Section(title, (
        f"<table><thead><tr><th>{_esc(_t(data, 'Date'))}</th>"
        f"<th>{_esc(_t(data, 'Original author'))}</th>"
        f"<th class='num'>{_esc(_t(data, 'Engagement'))}</th>"
        f"<th>{_esc(_t(data, 'Excerpt'))}</th><th></th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        "<p class='muted'>" + _v(
            data,
            "Reposts of other people's content. The engagement belongs to the original "
            "author, so none of it is counted in the figures above. Who you choose to "
            "amplify is still a positioning signal.",
            "Reposts of other people's content. The engagement belongs to the original "
            "author, so none of it is counted in the figures above. Who someone "
            "chooses to amplify is still a positioning signal.") + "</p>"))


def _date_text(v) -> str:
    """LinkedIn dates arrive as {'month','year','text'}, not strings.

    Rendering the dict stringified put {'month': 'Apr', 'year': 2026, ...} straight
    into a client document. Any nested vendor object needs an explicit projection;
    str() on a dict is never a display value.

    Returns ESCAPED HTML: the text is vendor data ("<script>" in startDate.text
    was rendered live into the brief until 2026-10-23), and every caller inlines it.
    """
    if not v:
        return ""
    if isinstance(v, str):
        return _esc(v)
    if isinstance(v, dict):
        if v.get("text"):
            return _esc(v["text"])
        parts = [str(v[k]) for k in ("month", "year") if v.get(k)]
        return _esc(" ".join(parts))
    return ""


def _date_range(start, end, *, duration: str | None = None) -> str:
    """Escaped HTML: both ends come from _date_text (escaped); the separators and
    &middot; are the only markup, added after escaping."""
    a, b = _date_text(start), _date_text(end) or _esc("Present")
    span = f"{a} - {b}" if a else b
    return f"{span} &middot; {_esc(duration)}" if duration else span


def s_experience(data: dict) -> Section:
    """Career history and education, which is what makes positioning checkable."""
    raw = data["account"].get("raw_json")
    if not raw:
        return Section("Experience", "")
    try:
        d = json.loads(raw)
    except Exception:
        return Section("Experience", "")

    exp = d.get("experience") or []
    edu = d.get("education") or []
    if not exp and not edu:
        return Section("Experience", "")

    out = []
    if exp:
        rows = []
        for e in exp:
            where = e.get("companyName") or e.get("company") or ""
            href = _href(e.get("companyLinkedinUrl"))
            where_html = (f"<a href='{href}'>{_esc(where)}</a>" if href
                          else _esc(where))
            bits = [x for x in (e.get("employmentType"), e.get("workplaceType"),
                                e.get("location")) if x]
            desc = (e.get("description") or "").strip()
            # A LinkedIn "what I did" can run 300 words of client case studies and
            # eat a whole page. Collapse the long ones: the reader is scanning a
            # career, not reading a CV.
            if len(desc) > 420:
                head, tail = desc[:400].rsplit("\n", 1)[0] or desc[:400], desc
                desc_html = (_md(head) +
                             f"<details class='more'><summary>{_esc(_t(data, 'more'))}</summary>"
                             f"{_md(tail[len(head):])}</details>")
            else:
                desc_html = _md(desc) if desc else ""
            rows.append(
                f"<tr><td><strong>{_esc(e.get('position') or '')}</strong><br>"
                f"<span class='muted'>{where_html}</span></td>"
                f"<td class='nowrap'>{_date_range(e.get('startDate'), e.get('endDate'), duration=e.get('duration'))}"
                + (f"<br><span class='muted'>"
                   f"{' &middot; '.join(_esc(b) for b in bits)}</span>"
                   if bits else "") +
                f"</td><td class='exdesc'>{desc_html}</td></tr>")
        out.append(
            f"<table class='exp'><thead><tr><th>{_esc(_t(data, 'Role'))}</th>"
            f"<th>{_esc(_t(data, 'When'))}</th>"
            f"<th>{_esc(_v(data, 'What you did', 'What they did'))}</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")

    if edu:
        rows = []
        for e in edu:
            school = e.get("schoolName") or ""
            href = _href(e.get("schoolLinkedinUrl"))
            school_html = (f"<a href='{href}'>{_esc(school)}</a>" if href
                           else _esc(school))
            what = " &middot; ".join(_esc(x) for x in
                                    (e.get("degree"), e.get("fieldOfStudy")) if x)
            when = (_esc(e["period"]) if e.get("period") else
                    _date_range(e.get("startDate"), e.get("endDate")))
            rows.append(f"<tr><td><strong>{school_html}</strong></td>"
                        f"<td class='nowrap'>{when}</td><td>{what}</td></tr>")
        out.append(f"<h3 class='sub3'>{_esc(_t(data, 'Education'))}</h3>"
                   f"<table class='exp'><thead><tr><th>{_esc(_t(data, 'School'))}</th>"
                   f"<th>{_esc(_t(data, 'When'))}</th>"
                   f"<th>{_esc(_t(data, 'Degree'))}</th></tr></thead>"
                   f"<tbody>{''.join(rows)}</tbody></table>")

    chips = []
    for label, key in (("awards", "honorsAndAwards"), ("skills", "skills"),
                       ("certifications", "certifications"),
                       ("languages", "languages"), ("courses", "courses")):
        v = d.get(key) or []
        if v:
            chips.append(f"<span class='chip'><b>{len(v)}</b> "
                         f"{_esc(_t(data, label))}</span>")
    if chips:
        out.append(f"<div class='chips' style='margin-top:.9rem'>{''.join(chips)}</div>")

    return Section(_t(data, "Experience & education"), "".join(out))


# Lens output goes into a document a client pays for, so it needs a gate. Real
# example from a shipped brief: "the caption body reached me empty... Resend the
# caption as plain text, word-for-word... Then I can point to the exact line." A
# static PDF instructing the reader to send text back to a model is the moment the
# document announces itself as unreviewed AI output.
_FOURTH_WALL = re.compile(
    r"(?im)^.*\b("
    r"resend|send me|paste (?:it|the|them)|share the (?:caption|text|file)|"
    r"if you (?:can )?(?:provide|share|send)|"
    r"(?:i|I)(?:'| a)m (?:an? )?(?:AI|language model|assistant)|"
    r"as an AI|reached me empty|was not provided to me|"
    r"i (?:cannot|can't) (?:see|access|read) (?:the|this|it)|"
    r"paste it (?:here|below)|let me know if"
    r")\b.*$")


def clean_lens_output(text: str) -> str:
    """Drop sentences that address the reader as if this were a chat.

    Removes whole lines rather than trying to rewrite them: a half-edited sentence
    reads worse than a missing one, and the surrounding analysis stands on its own.
    """
    if not text:
        return text
    lines = text.split("\n")
    kept = [ln for ln in lines if not _FOURTH_WALL.match(ln)]
    out = "\n".join(kept).strip()

    if len(kept) == len(lines):
        # Nothing was stripped, so nothing to second-guess. A short analysis is a
        # short analysis; the length rule below is about salvage, not about quality.
        return out

    # The gate ate most of it, meaning the lens spent its answer talking to the
    # reader. An omitted block beats a hollow one.
    return out if len(out) >= max(40, len(text) * 0.25) else ""


def _is_personal(r: dict) -> bool:
    return (r.get("output_text") or r.get("output_json") or "").lstrip() \
        .upper().startswith(PERSONAL)


def _lenses_html(data: dict, results: list[dict]) -> str:
    """Every lens on one post, in LENS_ORDER, or the personal-post note.

    One PERSONAL verdict covers the whole post: if the analysis declined to assess
    a bereavement post under one lens, the other lenses' "strong hook, weak CTA" on
    the same post is exactly what must not reach the client.
    """
    if not results:
        return ""
    if any(_is_personal(r) for r in results):
        return (f"<div class='pan personal'><p class='muted'>"
                f"{_esc(_t(data, 'Personal post, not assessed.'))}</p></div>")
    names = data.get("_lens_names")
    if names is None:
        names = data["_lens_names"] = _lens_names()
    out = []
    for r in results:
        text = clean_lens_output(r["output_text"] or r["output_json"] or "")
        if not text:
            continue
        slug = r["template"]
        label = _t(data, names.get(slug) or slug.replace("-", " ").title())
        out.append(f"<div class='pan' data-lens='{_esc(slug)}'><div class='panhead'>"
                   f"<span class='tag'>{_esc(label)}</span></div>"
                   f"<div class='abody'>{_md(text)}</div></div>")
    return "".join(out)


def s_account_summary(data: dict) -> Section:
    """The account-level read, first in the document because it is the part a
    client acts on: what is going on, the three things to change, and a month of
    posts to make. Absent entirely when no summary has been run."""
    sm = data.get("summary")
    title = _t(data, "Summary and priorities")
    if not sm:
        return Section(title, "")
    out = []
    if sm.get("summary"):
        out.append(f"<div class='execsum'>{_md(clean_lens_output(sm['summary']))}</div>")
    if sm.get("priorities"):
        out.append(f"<h3 class='sub'>{_esc(_t(data, 'Three priorities'))}</h3>"
                   "<ol class='prio'>")
        for p in sm["priorities"]:
            why, act = str(p.get("why") or "").strip(), str(p.get("action") or "").strip()
            out.append(
                f"<li><b>{_esc(p.get('title'))}</b>"
                + (f"<p><span class='lbl'>{_esc(_t(data, 'Why'))}</span> {_esc(why)}</p>"
                   if why else "")
                + (f"<p><span class='lbl'>{_esc(_t(data, 'Do this'))}</span> "
                   f"{_esc(act)}</p>" if act else "")
                + "</li>")
        out.append("</ol>")
    if sm.get("plan"):
        rows = []
        for n, w in enumerate(sm["plan"], 1):
            wk = w.get("week") if isinstance(w.get("week"), int) else n
            rows.append(f"<tr><td class='num'>{wk}</td><td>{_esc(w.get('idea'))}</td>"
                        f"<td>{_esc(w.get('format') or '')}</td>"
                        f"<td class='muted'>{_esc(w.get('why') or '')}</td></tr>")
        out.append(
            f"<h3 class='sub'>{_esc(_t(data, 'The next four weeks'))}</h3>"
            f"<table><thead><tr><th class='num'>{_esc(_t(data, 'Week'))}</th>"
            f"<th>{_esc(_t(data, 'Post idea'))}</th><th>{_esc(_t(data, 'Format'))}</th>"
            f"<th>{_esc(_t(data, 'Why'))}</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")
    return Section(title, "".join(out))


# Where downscaled copies live. Embedding originals put one client's brief at 23MB,
# which is past what most mail servers accept; at 200px the same document is a few
# hundred KB and looks identical at the size it is actually displayed.
THUMB_CACHE = Path("~/.corpus/thumbs").expanduser()
THUMB_PX = 200


def _small(path: Path, px: int = THUMB_PX) -> Path | None:
    """Return a downscaled copy, generating and caching it on first use.

    ffmpeg rather than a Python imaging library: it is already a hard dependency of
    the video path, so this adds no install step. Cached by source digest and size,
    so regenerating a brief re-encodes nothing.
    """
    try:
        key = hashlib.sha256(
            f"{path}\x1f{path.stat().st_mtime_ns}\x1f{px}".encode()).hexdigest()[:20]
    except OSError:
        return None
    dest = THUMB_CACHE / f"{key}.jpg"
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part.jpg")
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(path),
             "-vf", f"scale='min({px},iw)':-2", "-q:v", "6", "-frames:v", "1",
             str(tmp)],
            check=True, capture_output=True, timeout=30)
        tmp.replace(dest)
        return dest
    except (subprocess.SubprocessError, OSError):
        tmp.unlink(missing_ok=True)
        return None


def _thumb_uri(rec: dict | None, max_bytes: int = 260_000) -> str | None:
    """Inline a stored still as a small data: URI.

    The brief has to survive being emailed and opened offline, so nothing may be
    fetched at open time. Only LOCAL files are embedded: a remote CDN URL would be
    dead within about 34 hours for video and would render as a broken image in the
    client's inbox, which is worse than no image.
    """
    if not rec or not rec.get("local_path"):
        return None
    from .enrich.media import resolve_existing   # stored paths are media-root-relative
    p = resolve_existing(rec["local_path"])
    if p is None:
        return None
    small = _small(p)
    if small is not None and small.stat().st_size <= max_bytes:
        p = small
    elif p.stat().st_size > max_bytes:
        return None          # no downscale available and the original is too big
    mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"


def _thumb_cell(data: dict, it: dict) -> str:
    """A small preview for a table row.

    A row that says "image" and shows none reads as a failed export. Where no file
    was stored the cell says so rather than sitting blank, because on a post the
    scraper could not reach, that absence is itself information.
    """
    uri = _thumb_uri((data.get("thumbs") or {}).get(it["id"]))
    if uri:
        return f"<td class='thumbcell'><img class='tthumb' src='{uri}' alt=''></td>"
    return "<td class='thumbcell'><span class='nothumb'></span></td>"


def _transcript(it: dict) -> str:
    """The stored transcript, or "" when it is not fit to show. Whisper on a noisy
    reel produces confident nonsense, and quoting it in a client document reads as
    the client having said it."""
    tx = (it.get("transcript") or "").strip()
    if not tx or it.get("lang") == "music" or garbled(tx):
        return ""
    return tx


def s_all_posts(data: dict, template: str | None = None) -> Section:
    """Every original post: one compact row, expanding to the full breakdown.

    Collapsed by default because a hundred fully-expanded posts is not a document
    anyone reads, it is a wall. The row carries enough to scan for the one you want
    (date, type, the opening line, the numbers); clicking gives caption, transcript
    and analysis. Native <details> so it works in a browser, needs no JavaScript,
    and survives being emailed.

    `template` is accepted for compatibility; the lens choice is made in gather().
    """
    title = _t(data, "Every post")
    items = sorted(_own(data["items"]), key=lambda i: i["published_at"], reverse=True)
    if not items:
        return Section(title, "")

    lang = data.get("lang")
    analyses = data.get("analyses") or {}
    thumbs = data.get("thumbs") or {}
    blocks = []
    for it in items:
        when = i18n.date(it["published_at"], lang)
        uri = _thumb_uri(thumbs.get(it["id"]))
        kind = _kind(data, it["kind"])
        thumb = (f"<img class='pthumb' src='{uri}' alt=''>" if uri
                 else f"<span class='pthumb empty'>{_esc(kind[:4])}</span>")

        first_line = (it.get("body") or it.get("title") or "").strip().split("\n")[0]
        headline = _clip(first_line, 96) or _t(data, "({kind}, no caption)", kind=kind)

        nums = []
        for short, key in (("R", "m_reactions"), ("C", "m_comments"),
                           ("S", "m_shares"), ("V", "m_views")):
            v = it.get(key)
            if v:
                nums.append(f"<span class='n'><b>{_fmt(v)}</b>{_t(data, short)}</span>")

        chips = []
        for label, one, key in (("reactions", "reaction", "m_reactions"),
                                ("comments", "comment", "m_comments"),
                                ("shares", "share", "m_shares"),
                                ("views", "view", "m_views")):
            v = it.get(key)
            if v:
                chips.append(f"<span class='chip'><b>{_fmt(v)}</b> "
                             f"{_esc(_t(data, one if v == 1 else label))}</span>")
        total = _engagement(it)
        if total:
            chips.append(f"<span class='chip total'><b>{_fmt(total)}</b> "
                         f"{_esc(_t(data, 'total interactions'))}</span>")
        if not chips:
            chips.append(f"<span class='chip'>"
                         f"{_esc(_t(data, 'no engagement recorded'))}</span>")

        body = (it.get("body") or it.get("title") or "").strip()
        tx = _transcript(it)
        href = _href(it.get("url"))
        plat = PLATFORM_NAME.get(it["platform"], it["platform"])
        link = (f"<a href='{href}'>{_esc(_t(data, 'open on {p}', p=plat))} &rarr;</a>"
                if href else "")
        meta = [when, kind]
        if it.get("duration_s"):
            meta.append(f"{int(it['duration_s'])}s")
        if it.get("lang") and it["lang"] not in ("none",):
            meta.append(_t(data, "music bed") if it["lang"] == "music" else it["lang"])

        an = _lenses_html(data, analyses.get(it["id"], []))

        big = (f"<img class='pbig' src='{uri}' alt=''>" if uri else "")
        no_cap = _t(data, "This post has no caption. On a carousel or image post the "
                          "caption carries the argument, so there is nothing for a "
                          "reader to respond to.")
        blocks.append(
            f"<details class='post'>"
            f"<summary>{thumb}"
            f"<span class='pdate'>{when}</span>"
            f"<span class='phead'>{_esc(headline)}</span>"
            f"<span class='pnums'>{''.join(nums)}</span></summary>"
            f"<div class='pdetail'>{big}<div class='pbody'>"
            f"<div class='pmeta'>{' &middot; '.join(_esc(m) for m in meta)}"
            f"{' &middot; ' + link if link else ''}</div>"
            f"<div class='chips'>{''.join(chips)}</div>"
            + (f"<p class='ptext'>{_esc(body)}</p>" if body else
               f"<p class='muted'>{_esc(no_cap)}</p>")
            + (f"<div class='ptx'><div class='ptxh'>"
               f"{_esc(_t(data, 'Machine transcript, may contain errors'))}</div>"
               f"<p>{_esc(tx)}</p></div>" if tx else "")
            + an + "</div></div></details>")

    return Section(title, (
        "<p class='muted'>" + _esc(_t(
            data, "{n} original posts, newest first. Click any row for the full "
                  "breakdown.", n=len(items))) + "</p>"
        + "".join(blocks)))


def _adds_beyond_audio(screen_text: str, transcript: str) -> bool:
    """Is the on-screen text saying anything the audio did not?

    Most talking-head videos burn in auto-captions, so OCR returns fragments of the
    words already spoken -- one client's read "than ever. they can", which is noise beside
    a full transcript. On a music-only reel the same field IS the message. Distinguish
    them by word overlap rather than by guessing from the file.
    """
    words = {w for w in re.findall(r"[a-z']{3,}", screen_text.lower())}
    if not words:
        return False
    spoken = {w for w in re.findall(r"[a-z']{3,}", (transcript or "").lower())}
    if not spoken:
        return True
    return len(words - spoken) / len(words) > 0.5


def _clip(text: str, limit: int) -> str:
    """Truncate on a word boundary.

    A raw slice produced "And Ban" out of Bangkok and "for a k" mid-word, which reads
    as a rendering fault rather than an excerpt.
    """
    text = (text or "").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:-") + "\u2026"


def _first_line(it: dict) -> str:
    """The opening line, which on every one of these platforms is the whole bet.

    Prefer the caption; fall back to the spoken opening for a video whose caption is
    just a title. Truncated at a sentence rather than a character count so the table
    does not cut mid-word.
    """
    for src in (it.get("body"), _transcript(it)):
        text = (src or "").strip()
        if not text:
            continue
        line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        line = _clip(line, 110)
        if line:
            return line
    return ""


def s_hooks(data: dict) -> Section:
    """Openings that worked next to openings that did not.

    The one comparison a content person acts on directly. It is deliberately not a
    score: with this few posts a ranked "hook rating" would be inventing precision,
    so the brief shows the actual lines and lets the reader see the pattern.
    """
    title = _v(data, "How you open", "How they open")
    posts = [i for i in _showcase(data) if _first_line(i)]
    if len(posts) < 8:
        return Section(title, "")

    posts.sort(key=_engagement, reverse=True)
    n = max(3, min(5, len(posts) // 4))
    best, worst = posts[:n], posts[-n:]

    def rows(items):
        return "".join(
            f"<tr><td>{_esc(_first_line(i))}</td>"
            f"<td class='num'>{_fmt(_engagement(i))}</td></tr>"
            for i in items)

    head = (f"<table class='tbl'><thead><tr><th>{_esc(_t(data, 'First line'))}</th>"
            f"<th class='num'>{_esc(_t(data, 'Total'))}</th></tr></thead>")
    return Section(title, (
        "<p class='muted'>" + _esc(_v(
            data,
            "The first line is what decides whether the rest gets read. These are the "
            "openings of your {n} best and {n} weakest posts, by total interactions. "
            "Look for what the top group does that the bottom group does not.",
            "The first line is what decides whether the rest gets read. These are the "
            "openings of their {n} best and {n} weakest posts, by total interactions. "
            "Look for what the top group does that the bottom group does not.",
            n=n)) + "</p>"
        "<div class='hookcols'>"
        f"<div><h3 class='sub'>{_esc(_v(data, 'Your best openings', 'Their best openings'))}</h3>"
        f"{head}<tbody>{rows(best)}</tbody></table></div>"
        f"<div><h3 class='sub'>{_esc(_v(data, 'Your weakest openings', 'Their weakest openings'))}</h3>"
        f"{head}<tbody>{rows(worst)}</tbody></table></div>"
        "</div>"))


# A question in a comment is a content brief the audience wrote for you.
#
# A bare interrogative opener is NOT enough: "when", "what" and "who" open ordinary
# narrative comments just as often ("What a show, well done!", "when grades mean
# nothing in Croatia"), and both of those were printed as questions the audience
# wanted answered. So a question mark is required, except for these phrases, which
# are interrogative wherever they appear and are commonly typed without punctuation.
_ASKS = ("can you", "could you", "do you", "did you", "are you", "any tips",
         "any advice", "would you", "should i", "how do ", "how did ", "how much",
         "how many", "what do you", "what would you", "where can i", "what tools",
         "is there a", "is it possible",
         # Croatian, the clients' audience. Same rule: openers that are questions
         # wherever they start a comment, typed without the "?" as often as with.
         "možeš li", "možete li", "mozes li", "mozete li", "imaš li", "imate li",
         "imas li", "jel ", "je li ", "da li ", "ima li ", "koliko košta",
         "koliko kosta", "koliko ste", "koliko si", "gdje mogu", "gdje se može",
         "kako mogu", "kako ste ", "što preporuč", "koji alat", "koje alate",
         "šta misliš", "što misliš", "kad će", "kada će")


# A third-party plug riding on the post: an @mention plus a promotion word. Real
# example, quoted on a client's brief as audience voice: "Maybe you can try
# @kateinsmm? They're pro at this 🔥". A mention alone is ordinary (tagging a
# friend), and so is "pro" alone; together they are an advert.
_MENTION = re.compile(r"@[\w.]{2,}")
_PROMO = re.compile(r"\b(pro|promo|promotion|promote|dm|dms|check (?:out|my|his|her|"
                    r"their|them)|send it|follow|collab|boost|grow your|followers|"
                    r"giveaway|discount|link in (?:my )?bio)\b", re.I)


def _is_spam(text: str) -> bool:
    t = text or ""
    return bool(_MENTION.search(t) and _PROMO.search(t))


# Mirrors comments.DEFAULT_PER_POST. Stated on the page because every count in the
# section is bounded by it, and an unqualified "93 comments" against a post showing
# 185 reads as a data error.
COMMENT_CAP = 25


def _is_question(text: str) -> bool:
    t = (text or "").strip().lower()
    if len(t) < 12 or len(t.split()) < 3:
        return False
    if "?" in t:
        return True
    # Anchored, not a substring search. These phrases are interrogative when they
    # OPEN a comment and ordinary mid-sentence: "It's wild how many people focus on
    # tactics" is a statement, and it shipped as a question the audience asked.
    return t.startswith(_ASKS)


def s_audience(data: dict) -> Section:
    """What the audience said back.

    Everything here is quoted, never summarised: the value of a comment section to
    a content strategy is the audience's own words, and a paraphrase loses the
    vocabulary that made it useful. No sentiment score -- scoring tone from a few
    dozen comments would be inventing a number, and the competitors that print one
    do not publish a rubric for it.
    """
    title = _t(data, "What the audience says back")
    cmts = data.get("comments") or []
    if not cmts:
        return Section(title, "")

    us = data.get("us") or set()
    # Not audience: the author's own replies, comments by the subject or by our own
    # accounts (a friend's "so proud" is not market feedback), and plugs.
    audience = [c for c in cmts if not c.get("is_author_reply")
                and (c.get("author_name") or "").strip().lower() not in us
                and not _is_spam(c.get("body") or "")]
    if not audience:
        return Section(title, "")

    questions = [c for c in audience if _is_question(c.get("body") or "")]

    # Distinct people, and the ones who keep coming back. A reply-rate metric was
    # here and has been removed: replies TO comments are not fetched (scrapeReplies
    # is off by default, and it is a separate paid event), so an author answering a
    # commenter is invisible by construction. The card read "17% of threads joined"
    # and would have told a client they ignore their audience when the truth is we
    # never looked.
    by_person: dict[str, set] = {}
    for c in audience:
        who = (c.get("author_name") or "").strip().lower()
        if who:
            by_person.setdefault(who, set()).add(c["item_id"])
    regulars = {w: posts for w, posts in by_person.items() if len(posts) > 1}

    cards = [
        (_t(data, "Comments read"), _fmt(len(audience)),
         _t(data, "audience comments captured")),
        (_t(data, "Questions asked"), _fmt(len(questions)),
         _v(data, "each one is a post your audience already wants",
            "each one is a post they already want")),
        (_t(data, "People"), _fmt(len(by_person)), _t(data, "distinct commenters")),
    ]
    if regulars:
        cards.append((_t(data, "Regulars"), _fmt(len(regulars)),
                      _t(data, "commented on more than one post")))
    body = _cards(cards)

    def quotes(rows, limit):
        out = []
        for c in rows[:limit]:
            who = _esc(c.get("author_name") or _t(data, "someone"))
            rx = c.get("reactions") or 0
            href = _href(c.get("item_url"))
            # Which post it answered: a quote with no context ("Objasnio", "tako
            # je") means nothing, and "on the post" made the reader click to find
            # out.
            opening = _clip(((c.get("item_text") or "").strip().splitlines() or [""])[0],
                            60)
            label = (_t(data, "on “{p}”", p=opening) if opening
                     else _t(data, "on the post"))
            link = (f" <a href='{href}'>{_esc(label)}</a>" if href
                    else f" <span>{_esc(label)}</span>" if opening else "")
            likes = _t(data, "1 like") if rx == 1 else _t(data, "{n} likes", n=_fmt(rx))
            meta = f"{who}{f' &middot; {_esc(likes)}' if rx else ''}{link}"
            out.append(f"<blockquote class='cq'><p>{_esc(_clip(c.get('body'), 300))}</p>"
                       f"<cite>{meta}</cite></blockquote>")
        return "".join(out)

    if questions:
        body += (f"<h3 class='sub'>{_esc(_t(data, 'Questions asked'))}</h3>"
                 "<p class='muted'>" + _esc(_v(
                     data,
                     "Questions are the cheapest content ideas available: your "
                     "audience has already said what it wants. Replies are not "
                     "collected, so some of these may already have an answer under "
                     "them.",
                     "Questions are the cheapest content ideas available: the "
                     "audience has already said what it wants. Replies are not "
                     "collected, so some of these may already have an answer under "
                     "them.")) + "</p>" + quotes(questions, 12))

    loud = [c for c in audience if (c.get("reactions") or 0) > 0]
    if loud:
        body += (f"<h3 class='sub'>{_esc(_t(data, 'What got agreement'))}</h3>"
                 "<p class='muted'>" + _esc(_t(
                     data, "Comments other readers liked, so these are the opinions "
                           "the wider audience endorsed, not just one person's.")) +
                 "</p>" + quotes(loud, 8))

    if regulars:
        top = sorted(regulars.items(), key=lambda kv: -len(kv[1]))[:8]
        names = ", ".join(f"{_esc(w)} ({len(posts)})" for w, posts in top)
        body += (f"<h3 class='sub'>{_esc(_t(data, 'Who keeps coming back'))}</h3>"
                 "<p class='muted'>" + _esc(_v(
                     data,
                     "{n} of {m} commenters showed up on more than one post. These are "
                     "your warmest audience and the cheapest people to talk to "
                     "directly.",
                     "{n} of {m} commenters showed up on more than one post. These are "
                     "the warmest audience on the account and the cheapest people to "
                     "talk to directly.", n=len(regulars), m=len(by_person))) +
                 f"</p><p>{names}</p>")

    body += "<p class='muted'>" + _esc(_t(
        data, "Up to {cap} comments were read per post, newest first, and replies to "
              "comments are not collected. Your own accounts' comments and "
              "promotional plugs are left out. Counts here are therefore a floor, not "
              "the full thread.", cap=COMMENT_CAP)) + "</p>"
    return Section(title, body)


def s_video(data: dict) -> Section:
    """What the videos actually say.

    This is the section no competing tool can produce. LinkedIn scrapers return no
    transcript at all, and the one actor that does costs $0.42 a video with two users
    in its lifetime; the audit tools in this space cap out at text posts. So a
    creator's video back-catalogue is, to everyone else, a list of thumbnails.

    Two layers, because they answer different questions: what was SAID, and what was
    written ON SCREEN over a music bed.
    """
    title = _v(data, "What your videos say", "What their videos say")
    vids = [i for i in _showcase(data)
            if _transcript(i)
            or _adds_beyond_audio((i.get("screen_text") or "").strip(),
                                  i.get("transcript") or "")]
    if not vids:
        return Section(title, "")

    lang_ui = data.get("lang")
    vids.sort(key=_engagement, reverse=True)
    thumbs = data.get("thumbs") or {}
    blocks = []
    for it in vids[:12]:
        when = i18n.date(it["published_at"], lang_ui)
        uri = _thumb_uri(thumbs.get(it["id"]))
        img = f"<img class='vthumb' src='{uri}' alt=''>" if uri else ""
        dur = f"{int(it['duration_s'])}s" if it.get("duration_s") else ""
        lang = it.get("lang")
        langtag = (f"<span class='chip'>{_esc(lang)}</span>"
                   if lang and lang not in ("none", "en") else "")
        href = _href(it.get("url"))

        parts = []
        tx = _transcript(it)
        if tx:
            parts.append(f"<div class='vlayer'><div class='vlabel'>"
                         f"{_esc(_t(data, 'Spoken (machine transcript, may contain errors)'))}"
                         f"</div><p>{_esc(_clip(tx, 600))}</p></div>")
        sc = (it.get("screen_text") or "").strip()
        if sc and _adds_beyond_audio(sc, it.get("transcript") or ""):
            parts.append(f"<div class='vlayer'><div class='vlabel'>"
                         f"{_esc(_t(data, 'On screen'))}</div>"
                         f"<p>{_esc(_clip(sc, 400))}</p></div>")
        cap = (it.get("body") or "").strip()
        if cap:
            parts.append(f"<div class='vlayer'><div class='vlabel'>"
                         f"{_esc(_t(data, 'Caption'))}</div>"
                         f"<p>{_esc(_clip(cap, 260))}</p></div>")

        openlink = f"<a href='{href}'>{_esc(_t(data, 'open'))}</a>" if href else ""
        blocks.append(
            f"<div class='vid'>{img}<div class='vbody'>"
            f"<div class='pmeta'>{when}{' &middot; ' + dur if dur else ''}"
            f" &middot; {_esc(_t(data, '{n} interactions', n=_fmt(_engagement(it))))}"
            + (f" &middot; {openlink}" if openlink else "")
            + f" {langtag}</div>"
            + "".join(parts) + "</div></div>")

    return Section(title, (
        "<p class='muted'>" + _esc(_t(
            data, "{n} video(s) with readable content. Spoken audio is transcribed by "
                  "machine and can mishear names and slang; on-screen text is read "
                  "off the frames. Most analytics tools cannot see either, which is "
                  "why a video back-catalogue usually shows up as a row of "
                  "thumbnails.", n=len(vids))) + "</p>" + "".join(blocks)))


# Where the owner-only numbers live, by platform, for the "send us your export"
# line on an own or client brief.
_EXPORT_NAME = {"instagram": "Instagram insights export",
                "linkedin": "LinkedIn analytics export",
                "youtube": "YouTube Studio export"}


def s_limits(data: dict, platforms: list[str] | None = None,
             relationship: str | None = None) -> Section:
    """What the brief cannot show, and for the client what would fix that.

    Relationship-aware: for a competitor the owner-only metrics are simply not
    obtainable, and saying so is the point. For a client or our own account they
    ARE obtainable, from the account's own export, and telling a client "no tool
    can get this" about their own reach is both false and a missed ask.
    """
    # Provenance is a claim about HOW the data arrived, so it has to be read off the
    # run log, not off relationship. relationship is intent: an account marked "own"
    # whose export never landed is still served by the paid public scraper, and
    # saying otherwise puts a false sentence in a document a client pays for.
    title = _t(data, "What this can and cannot show")
    consented = data.get("via_consented_export", False)
    rel = relationship or (data.get("account") or {}).get("relationship")
    plats = platforms or [(data.get("account") or {}).get("platform")]
    if consented:
        body = "<p>" + _esc(_t(data, "This brief is built from the account's own "
                                     "analytics export, so it includes owner-only "
                                     "metrics.")) + "</p>"
        return Section(title, body)
    owner_only = "".join(f"<li>{_esc(_t(data, m))}</li>" for m in OWNER_ONLY)
    if rel in ("own", "client"):
        exports = [_t(data, _EXPORT_NAME[p]) for p in dict.fromkeys(plats)
                   if p in _EXPORT_NAME]
        body = ("<p>" + _esc(_t(
            data, "This brief is built from publicly visible content, the same view "
                  "any visitor has. These numbers exist only inside your own "
                  "account:")) + "</p><ul>" + owner_only + "</ul>"
            + "<div class='note'><p>" + _esc(_t(
                data, "To add reach and saves to the next brief, send your {what}. "
                      "It takes a minute to download from the app and nothing else "
                      "can produce these numbers.",
                what=_t(data, " and ").join(exports) or _t(data, "analytics export")))
            + "</p></div>")
    else:
        body = ("<p>" + _esc(_t(
            data, "This brief is built from publicly visible content. The following "
                  "metrics are owner-only and not obtainable for an account you do "
                  "not own, by any tool at any price:")) + "</p><ul>" + owner_only
            + "</ul>")
    body += "<p class='muted'>" + _esc(_t(
        data, "Engagement counts shown are what the platform displays publicly, "
              "captured at the times listed. They move after capture.")) + "</p>"
    return Section(title, body)


CSS = """
/* Deliberately not a dashboard. This is a document someone reads top to bottom and
   may print, so it is built on a single measure, real vertical rhythm, and one
   accent. The previous version was default-styled tables on a white page, which read
   as a database dump rather than something anyone would pay for. */
:root{
  --bg:#fbfbfa; --panel:#fff; --fg:#16181d; --mut:#6b7280; --line:#e6e7eb;
  --accent:#1a56db; --accent-soft:#eaf0fe; --warm:#b45309; --good:#047857;
  --radius:10px; --measure:74ch;
}
@media(prefers-color-scheme:dark){:root{
  --bg:#0e1013; --panel:#15181d; --fg:#e9eaec; --mut:#9aa1ad; --line:#242832;
  --accent:#7aa2f7; --accent-soft:#182236; --warm:#d99a3a; --good:#4ade80;
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; padding:3.5rem 1.5rem 5rem; background:var(--bg); color:var(--fg);
  font:16px/1.65 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",
       Inter,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:960px;margin:0 auto}

/* masthead */
h1{font-size:2rem;line-height:1.2;letter-spacing:-.02em;margin:0 0 .3rem;font-weight:650}
.sub{color:var(--mut);margin:0 0 2.5rem;font-size:.95rem}
.sub .dot{opacity:.45;padding:0 .45rem}

/* section headings: a rule above, generous air, small caps label */
h2{
  font-size:.78rem; text-transform:uppercase; letter-spacing:.09em; font-weight:650;
  color:var(--mut); margin:3.2rem 0 1rem; padding-top:1rem;
  border-top:1px solid var(--line);
}
h1.sect{
  font-size:1.35rem; letter-spacing:-.01em; margin:4.5rem 0 .25rem;
  padding-top:2rem; border-top:2px solid var(--accent); font-weight:650;
}
h1.sect + .sub{margin-bottom:1.5rem}

/* stat cards: quiet, aligned, tabular */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:.7rem}
.card{
  background:var(--panel); border:1px solid var(--line); border-radius:var(--radius);
  padding:.85rem .95rem;
  /* A two-line label used to push its number below its neighbours' and break the
     row's baseline. Grid with the value in a bottom-anchored row fixes it without
     truncating any label. */
  display:grid; grid-template-rows:auto 1fr auto; min-height:6.4rem;
}
.card .k{
  color:var(--mut); font-size:.68rem; text-transform:uppercase; letter-spacing:.07em;
  font-weight:600; line-height:1.35;
}
.card .v{
  font-size:1.6rem; font-weight:650; letter-spacing:-.02em;
  font-variant-numeric:tabular-nums; align-self:end; margin-top:.35rem;
}
.card .sub{color:var(--mut);font-size:.7rem;font-weight:600;align-self:end}
.card .hint{color:var(--mut);font-size:.68rem;line-height:1.35;margin-top:.25rem;
  align-self:end;font-weight:450}
.sub3{font-size:.75rem;text-transform:uppercase;letter-spacing:.08em;
  color:var(--mut);margin:1.6rem 0 .6rem;font-weight:650}
table.exp td{vertical-align:top}
.exdesc{font-size:.85rem;color:var(--mut);max-width:46ch}
.exdesc ul{margin:.2rem 0 .2rem 1rem;padding:0}
.exdesc p{margin:.25rem 0}

/* tables: no vertical rules, hairline rows, numbers right and tabular */
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:.9rem;margin:0}
thead th{
  color:var(--mut); font-weight:600; font-size:.68rem; text-transform:uppercase;
  letter-spacing:.07em; text-align:left; padding:0 .7rem .5rem;
  border-bottom:1px solid var(--line); white-space:nowrap;
}
tbody td{padding:.7rem;border-bottom:1px solid var(--line);vertical-align:top}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover{background:var(--accent-soft)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.strong{font-weight:650}
td.nowrap{white-space:nowrap;color:var(--mut)}
a{color:var(--accent);text-decoration:none;border-bottom:1px solid transparent}
a:hover{border-bottom-color:currentColor}

/* weekday bars */
.bars{display:flex;flex-direction:column;gap:.4rem;margin:.2rem 0 .6rem}
.barrow{display:grid;grid-template-columns:2.6rem 1fr 8.5rem;align-items:center;gap:.7rem}
.barlab{font-size:.78rem;color:var(--mut);font-weight:600}
.bartrack{background:var(--panel);border:1px solid var(--line);border-radius:6px;
          height:1.4rem;overflow:hidden}
.bar{background:var(--accent);height:100%;border-radius:5px;min-width:3px}
.bar.thin{background:repeating-linear-gradient(45deg,var(--accent),var(--accent) 3px,
          transparent 3px,transparent 7px);opacity:.75}
.barval{font-size:.8rem;font-variant-numeric:tabular-nums;color:var(--mut)}

/* analysis blocks: the part a client actually reads */
.analysis{
  border:1px solid var(--line); border-left:3px solid var(--accent);
  border-radius:var(--radius); padding:1rem 1.1rem; margin:.8rem 0;
  background:var(--panel);
}
.ahead{margin-bottom:.6rem;font-size:.82rem;display:flex;gap:.5rem;align-items:baseline;
       flex-wrap:wrap}
.tag{background:var(--accent-soft);color:var(--accent);border-radius:5px;
     padding:.12rem .45rem;font-size:.68rem;font-weight:650;text-transform:uppercase;
     letter-spacing:.05em}
.abody{font-size:.92rem;max-width:var(--measure)}
.abody p{margin:.5rem 0}
.abody h4{margin:.9rem 0 .3rem;font-size:.8rem;text-transform:uppercase;
          letter-spacing:.06em;color:var(--mut);font-weight:650}
.abody strong{font-weight:650}
.abody ul{margin:.4rem 0 .4rem 1.15rem;padding:0}
.abody li{margin:.2rem 0}
.abody code{background:var(--bg);padding:.08rem .3rem;border-radius:4px;font-size:.86em}

.muted{color:var(--mut);font-size:.85rem;max-width:var(--measure)}
.note{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--warm);
      border-radius:var(--radius);padding:.85rem 1rem;margin:.8rem 0}
.note p{margin:.3rem 0}
ul.plain{margin:.4rem 0 .4rem 1.15rem;padding:0;color:var(--mut);font-size:.85rem}

.post{border-bottom:1px solid var(--line)}
.post:last-of-type{border-bottom:none}
.post>summary{
  display:grid; grid-template-columns:34px 5.6rem 1fr auto; gap:.75rem;
  align-items:center; padding:.6rem .2rem; cursor:pointer; list-style:none;
}
.post>summary::-webkit-details-marker{display:none}
.post>summary:hover{background:var(--accent-soft)}
.pthumb{width:34px;height:34px;border-radius:6px;object-fit:cover;display:block;
  border:1px solid var(--line)}
.pthumb.empty{display:flex;align-items:center;justify-content:center;
  background:var(--panel);color:var(--mut);font-size:.55rem;text-transform:uppercase;
  letter-spacing:.04em}
.pdate{color:var(--mut);font-size:.78rem;white-space:nowrap;
  font-variant-numeric:tabular-nums}
.phead{font-size:.9rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pnums{display:flex;gap:.5rem;color:var(--mut);font-size:.75rem;
  font-variant-numeric:tabular-nums;white-space:nowrap}
.pnums .n b{color:var(--fg);margin-right:.1rem}
.pdetail{display:grid;grid-template-columns:150px 1fr;gap:1.2rem;
  padding:.4rem .2rem 1.4rem}
.pbig{width:100%;border-radius:8px;border:1px solid var(--line);display:block}
.pbody{min-width:0}
.pmeta{color:var(--mut);font-size:.78rem;margin-bottom:.55rem}
.chips{display:flex;flex-wrap:wrap;gap:.35rem;margin-bottom:.7rem}
.chip{background:var(--panel);border:1px solid var(--line);border-radius:20px;
  padding:.15rem .6rem;font-size:.75rem;color:var(--mut);white-space:nowrap}
.chip b{color:var(--fg);font-variant-numeric:tabular-nums}
.chip.total{background:var(--accent-soft);border-color:transparent}
.chip.total b{color:var(--accent)}
.ptext{margin:.2rem 0 .7rem;font-size:.9rem;max-width:var(--measure);
  white-space:pre-wrap}
.ptx{margin:.2rem 0 .7rem;padding:.6rem .8rem;background:var(--panel);
  border:1px solid var(--line);border-radius:8px}
.ptxh{font-size:.68rem;text-transform:uppercase;letter-spacing:.07em;
  color:var(--mut);font-weight:650;margin-bottom:.25rem}
.ptx p{margin:0;font-size:.85rem;color:var(--mut);max-width:var(--measure)}
.pan{border-left:3px solid var(--accent);background:var(--panel);border-radius:8px;
  padding:.75rem .95rem;margin-top:.7rem}
.panhead{margin-bottom:.4rem}
@media(max-width:700px){
  .post>summary{grid-template-columns:34px 1fr auto}
  .pdate{display:none}
  .pdetail{grid-template-columns:1fr}
  .pbig{max-width:220px}
}
.vid{display:grid;grid-template-columns:110px 1fr;gap:1rem;padding:1rem 0;
  border-bottom:1px solid var(--line);break-inside:avoid}
.vid:last-child{border-bottom:none}
.vthumb{width:100%;border-radius:8px;border:1px solid var(--line);display:block}
.vbody{min-width:0}
.vlayer{margin:.5rem 0}
.vlabel{font-size:.65rem;text-transform:uppercase;letter-spacing:.08em;
  color:var(--mut);font-weight:650;margin-bottom:.15rem}
.vlayer p{margin:0;font-size:.87rem;max-width:var(--measure)}
details.more{display:inline}
details.more summary{cursor:pointer;color:var(--accent);font-size:.8rem;
  list-style:none}
details.more summary::-webkit-details-marker{display:none}
@media(max-width:640px){.vid{grid-template-columns:1fr}.vthumb{max-width:160px}}
@media print{details.more{display:block}details.more summary{display:none}}
.hookcols{display:grid;grid-template-columns:1fr 1fr;gap:1.5rem;align-items:start}
.hookcols td:first-child{font-size:.84rem;line-height:1.45}
h3.sub{margin-top:2rem;font-size:.72rem;text-transform:uppercase;letter-spacing:.08em;
  color:var(--mut);font-weight:650;margin:2rem 0 .5rem}
@media(max-width:820px){.hookcols{grid-template-columns:1fr}}
.thin{color:var(--mut);font-weight:700}
tr.gap-zero td{color:#f0a5a5}
tr.gap-low td{color:#e8c98a}
.thumbcell{width:64px;padding-right:0}
.tthumb{width:56px;height:56px;object-fit:cover;border-radius:6px;
  border:1px solid var(--line);display:block;background:#111}
.nothumb{display:block;width:56px;height:56px;border-radius:6px;
  border:1px dashed var(--line);opacity:.35}
@media(max-width:640px){.thumbcell{display:none}}
blockquote.cq{margin:.75rem 0;padding:.6rem 0 .6rem 1rem;
  border-left:2px solid var(--line);break-inside:avoid}
blockquote.cq p{margin:0 0 .25rem;font-size:.9rem;max-width:var(--measure)}
blockquote.cq cite{font-style:normal;font-size:.72rem;color:var(--mut)}
footer{margin-top:4rem;padding-top:1.2rem;border-top:1px solid var(--line);
       color:var(--mut);font-size:.8rem}

/* data age: always stated, loud when stale */
.age{color:var(--mut);font-size:.85rem;margin:-1.8rem 0 2rem}
.stale{border:1px solid #dc2626;border-left:4px solid #dc2626;border-radius:var(--radius);
  background:rgba(220,38,38,.07);padding:.8rem 1rem;margin:0 0 2rem;font-size:.92rem}
.stale b{color:#dc2626}
.execsum{font-size:1rem;max-width:var(--measure)}
.execsum p{margin:.4rem 0}
ol.prio{margin:.4rem 0 0 1.2rem;padding:0;max-width:var(--measure)}
ol.prio li{margin:.2rem 0 .9rem}
ol.prio p{margin:.2rem 0;font-size:.92rem}
.lbl{font-size:.68rem;text-transform:uppercase;letter-spacing:.07em;color:var(--mut);
  font-weight:650;margin-right:.3rem}
.pan + .pan{margin-top:.5rem}
.pan.personal{border-left-color:var(--line)}

@media print{
  :root{--bg:#fff;--panel:#fff}
  body{padding:0;font-size:11pt}
  .wrap{max-width:none}
  .card,.analysis,.note,tr,.post{break-inside:avoid}
  /* A PDF cannot be clicked, so every post opens for print. */
  .post>summary{display:none}
  .pdetail{display:grid !important;padding-top:1rem}
  .post{padding:.6rem 0}

  h2{break-after:avoid}
  h1.sect{break-before:page}
  tbody tr:hover{background:none}
  a{color:var(--fg);border-bottom:none}
}
"""



def _age_html(lang, anchors: list[int | None], *, now: int | None = None) -> str:
    """"Data captured <date> (<n> days ago)", plus a red box when it is stale.

    Every brief carries it. Without it a brief built in October from an August sync
    looked current, and the client reads "your typical post gets 21" as today's
    number.
    """
    now = now or store.now()
    got = [a for a in anchors if a]
    if not got:
        return (f"<p class='age'>{_esc(_t(lang, 'Not synced yet: no capture date.'))}"
                "</p>")
    oldest = min(got)
    days = max(0, int((now - oldest) // 86400))
    line = _t(lang, "Data captured {d} ({ago})", d=i18n.date(oldest, lang),
              ago=i18n.ago(days, lang))
    out = f"<p class='age'>{_esc(line)}</p>"
    if days > STALE_DAYS:
        out += ("<div class='stale'><b>" + _esc(_t(lang, "Stale data.")) + "</b> "
                + _esc(_t(lang, "These numbers are {n} days old. Posts published since "
                                "then are missing and every count has moved. Re-sync "
                                "before acting on this or sending it.", n=days))
                + "</div>")
    return out


def _window_note(data: dict) -> str:
    """Say what period the numbers cover and what fell outside it."""
    if not data.get("days") or not data.get("anchor"):
        return ""
    text = _t(data, "Covers {p}, the last sync, not up to today.", p=_period(data))
    if data.get("older"):
        text += " " + _t(data, "{n} older post(s) are outside this period and not "
                               "counted.", n=data["older"])
    return f"<p class='muted'>{_esc(text)}</p>"


def _sections(data: dict, template: str | None = None) -> list[Section]:
    return [
        s_account_summary(data), s_summary(data), s_experience(data), s_top(data),
        s_formats(data), s_hooks(data), s_audience(data), s_gaps(data),
        s_cadence(data), s_video(data), s_reposts(data), s_all_posts(data, template),
    ]


def _doc(lang, title: str, head: str, body: str) -> str:
    gen = i18n.stamp(int(time.time()), lang)
    return f"""<!doctype html><html lang="{i18n.lang_of(lang)}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title><style>{CSS}</style></head><body><div class="wrap">
<h1>{_esc(title)}</h1>
{head}
{body}
<footer>{_esc(_t(lang, "Generated {when} from the corpus store. Engagement figures are "
                       "point-in-time captures, not live values.", when=gen))}</footer>
</div></body></html>"""


def render(data: dict, *, title: str | None = None, template: str | None = None) -> str:
    acct = data["account"]
    lang = data.get("lang", "en")
    title = title or display_name(acct)
    items = data["items"]
    period = ""
    if items:
        period = (f"{i18n.date(items[-1]['published_at'], lang)} – "
                  f"{i18n.date(items[0]['published_at'], lang)}")

    sections = _sections(data, template) + [s_limits(data)]
    body = "".join(
        f"<h2>{_esc(s.title)}</h2><div class='tablewrap'>{s.html}</div>"
        for s in sections if s.html
    )
    plat = PLATFORM_NAME.get(acct.get("platform"), acct.get("platform"))
    sub = (f"<p class=\"sub\">{_esc(_t(lang, 'Content brief'))} &middot; {_esc(plat)} "
           f"&middot; {_esc(acct.get('handle'))}"
           f"{' &middot; ' + _esc(period) if period else ''}</p>")
    head = sub + _age_html(lang, [data.get("anchor")]) + _window_note(data)
    return _doc(lang, title, head, body)


def write(conn, account_id: int, out_path: str | Path, *, since=None, days=None,
          analysis_db: str | None = None, template: str | None = None,
          lang: str = "en", title: str | None = None, csv: bool = False) -> Path:
    data = gather(conn, account_id, since=since, days=days, analysis_db=analysis_db,
                  template=template, lang=lang)
    out = Path(out_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(data, template=template, title=title), encoding="utf-8")
    if csv:
        write_csv([data], out.with_name(out.stem + ".csv"))
    return out


# ── CSV: the posts as a spreadsheet ────────────────────────────────────────

CSV_COLUMNS = ("date", "platform", "account", "kind", "url", "opening", "reactions",
               "comments", "shares", "views", "engagement", "x_usual")


def _cell(v: str) -> str:
    """Defuse spreadsheet formulas. A caption is scraped third-party text, and one
    starting with "=" or "@" runs as a formula when the client opens the file."""
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def write_csv(datasets: list[dict], path: str | Path) -> Path:
    """Every original post in the brief, one row each.

    A client who wants to sort by comments or paste into their own report should
    not have to copy numbers out of HTML. x_usual is the post's engagement over
    that account's median in the same window, the same "typical post" the brief
    shows, so the two never disagree. UTF-8 with a BOM because Excel otherwise
    opens Croatian captions as mojibake.
    """
    import csv as _csv
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = _csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for d in datasets:
            items = sorted(_own(d["items"]), key=lambda i: i["published_at"],
                           reverse=True)
            if not items:
                continue
            usual = statistics.median([_engagement(i) for i in items]) or 0
            for it in items:
                e = _engagement(it)
                opening = ((it.get("body") or it.get("title") or "").strip()
                           .splitlines() or [""])[0]
                w.writerow([
                    time.strftime("%Y-%m-%d", time.gmtime(it["published_at"])),
                    it["platform"], d["account"]["handle"], it["kind"],
                    _cell(it.get("url") or ""), _cell(_clip(opening, 200)),
                    it.get("m_reactions") or 0, it.get("m_comments") or 0,
                    it.get("m_shares") or 0,
                    it.get("m_views") if it.get("m_views") is not None else "",
                    e, f"{e / usual:.2f}" if usual else ""])
    return path


# ── multi-account ───────────────────────────────────────────────────────────

def combined_title(datasets: list[dict]) -> str:
    """The client's name, for a document about one person across platforms. The
    LinkedIn name wins when there is one: it is the full legal-ish name, where an
    Instagram display name is often a nickname or carries a tagline."""
    accts = [d["account"] for d in datasets]
    pick = next((a for a in accts if a.get("platform") == "linkedin"), None) \
        or (accts[0] if accts else {})
    return display_name(pick) if pick else ""


def render_combined(datasets: list[dict], *, title: str | None = None,
                    subject: str | None = None,
                    template: str | None = None, lang: str | None = None) -> str:
    """One document covering several accounts.

    A person is not one profile. Reporting LinkedIn and Instagram as two separate
    PDFs makes the reader do the cross-platform comparison in their head, which is
    the part they actually wanted.
    """
    lang = i18n.lang_of(lang or (datasets[0].get("lang") if datasets else "en"))
    title = title or combined_title(datasets) or _t(lang, "Content brief")
    total_items = sum(len(_own(d["items"])) for d in datasets)
    total_eng = sum(_engagement(i) for d in datasets for i in _own(d["items"]))
    # Same rule as s_limits: provenance is where the data CAME FROM, not what the
    # account was labelled. Every one of these was fetched by the public scraper.
    consented = bool(datasets) and all(d.get("via_consented_export") for d in datasets)

    body = []
    # The account-level reads go first: they are the part the client acts on.
    for d in datasets:
        sm = s_account_summary(d)
        if sm.html:
            plat = PLATFORM_NAME.get(d["account"]["platform"], d["account"]["platform"])
            body.append(f"<h2>{_esc(sm.title)} &middot; {_esc(plat)}</h2>"
                        f"<div class='tablewrap'>{sm.html}</div>")

    head = [(_t(lang, "Profiles"), str(len(datasets)), _t(lang, "accounts in this report")),
            (_t(lang, "Original posts"), str(total_items), _t(lang, "excludes reposts")),
            (_t(lang, "Total interactions"), _fmt(total_eng),
             _t(lang, "reactions + comments + shares"))]
    for d in datasets:
        a, n = d["account"], len(_own(d["items"]))
        head.append((PLATFORM_NAME.get(a["platform"], a["platform"]),
                     _fmt(sum(_engagement(i) for i in _own(d["items"]))),
                     _t(lang, "interactions, {n} post(s)", n=n)))
    body.append(f"<h2>{_esc(_t(lang, 'Across all profiles'))}</h2>{_cards(head)}")

    # Cross-platform comparison is the whole reason these share a document.
    rows = []
    for d in datasets:
        a, items = d["account"], _own(d["items"])
        if not items:
            continue
        engs = sorted(_engagement(i) for i in items)
        # statistics.median, not engs[len//2] on a reversed list. The two disagree by
        # up to ~20% on an even count, and both were printed in the same document:
        # One brief said "typical post 78" in the summary and 64 in this table.
        med = int(statistics.median(engs)) if engs else 0
        rows.append(
            f"<tr><td>{_esc(PLATFORM_NAME.get(a['platform'], a['platform']))}</td>"
            f"<td>{_esc(a['handle'])}</td>"
            f"<td class='num'>{len(items)}</td>"
            f"<td class='num'>{_fmt(sum(engs))}</td>"
            f"<td class='num'>{_fmt(med)}</td>"
            # max(), not engs[0]: the sort was flipped to ascending when the median
            # was fixed, which silently turned "best post" into "worst post" and
            # shipped Instagram's best as 0 next to a summary card reading 47.8k.
            f"<td class='num strong'>{_fmt(max(engs))}</td>"
            f"<td class='num'>{_fmt(d['followers'])}</td></tr>")
    if rows:
        th = lambda s, num=True: (f"<th class='num'>{_esc(_t(lang, s))}</th>" if num
                                  else f"<th>{_esc(_t(lang, s))}</th>")
        body.append(
            f"<h2>{_esc(_t(lang, 'Platform comparison'))}</h2><div class='tablewrap'>"
            "<table><thead><tr>" + th("Platform", False) + th("Handle", False)
            + th("Posts") + th("Interactions") + th("Typical") + th("Best")
            + th("Followers") + "</tr></thead><tbody>" + "".join(rows)
            + "</tbody></table></div><p class='muted'>" + _esc(_t(
                lang, "Engagement is not comparable across platforms in absolute "
                      "terms: an Instagram like and a LinkedIn reaction cost the "
                      "reader different amounts of effort. Compare each platform "
                      "against itself over time, not against the other.")) + "</p>")

    for d in datasets:
        a = d["account"]
        plat = PLATFORM_NAME.get(a["platform"], a["platform"])
        body.append(f"<h1 class='sect'>{_esc(plat)} &middot; "
                    f"{_esc(display_name(a))}</h1>" + _window_note(d))
        for s in _sections(d, template)[1:]:     # the summary already went first
            if s.html:
                body.append(f"<h2>{_esc(s.title)}</h2>"
                            f"<div class='tablewrap'>{s.html}</div>")

    if datasets:
        rels = [d["account"].get("relationship") for d in datasets]
        lim = s_limits(dict(datasets[0], via_consented_export=consented),
                       platforms=[d["account"]["platform"] for d in datasets],
                       relationship=rels[0] if len(set(rels)) == 1 else None)
        body.append(f"<h2>{_esc(lim.title)}</h2>{lim.html}")

    sub = _esc(subject) + " &middot; " if subject else ""
    src = (_t(lang, "first-party analytics export") if consented
           else _t(lang, "publicly visible data"))
    head_html = (f"<p class=\"sub\">{_esc(_t(lang, 'Content brief'))} &middot; {sub}"
                 f"{_esc(_t(lang, '{n} profiles', n=len(datasets)))} &middot; "
                 f"{_esc(_t(lang, '{n} posts', n=total_items))} &middot; {_esc(src)}</p>"
                 + _age_html(lang, [d.get("anchor") for d in datasets]))
    return _doc(lang, title, head_html, "".join(body))


def write_combined(conn, account_ids: list[int], out_path: str | Path, *,
                   since=None, days=None, analysis_db: str | None = None,
                   title: str | None = None, subject: str | None = None,
                   pdf: bool = False, template: str | None = None,
                   lang: str = "en", csv: bool = False):
    datasets = [gather(conn, aid, since=since, days=days, analysis_db=analysis_db,
                       template=template, lang=lang)
                for aid in account_ids]
    out = Path(out_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    html_out = _html_path(out)
    html_out.write_text(
        render_combined(datasets, title=title, subject=subject, template=template,
                        lang=lang),
        encoding="utf-8")
    if csv:
        write_csv(datasets, html_out.with_suffix(".csv"))
    if not pdf:
        return html_out, None
    return html_out, to_pdf(html_out, html_out.with_suffix(".pdf"))


def _html_path(out: Path) -> Path:
    """out + .html without eating a dot in the name. with_suffix() turned
    "jordan.novak-content-audit" into "jordan.html", so the Instagram brief and the
    combined one overwrote each other."""
    if out.suffix.lower() in (".html", ".pdf"):
        return out.with_suffix(".html")
    return out.with_name(out.name + ".html")


# ── benchmark: one client against its competitors ──────────────────────────

def _bench_row(d: dict) -> dict:
    """The comparable numbers for one account. Same definitions as the brief's
    summary cards, so a figure here never disagrees with the account's own brief."""
    items = _own(d["items"])
    a = d["account"]
    out = {"id": a["id"], "handle": a["handle"], "name": display_name(a),
           "platform": a["platform"], "followers": d["followers"], "posts": len(items),
           "anchor": d.get("anchor")}
    if not items:
        return out
    engs = [_engagement(i) for i in items]
    out["per_week"] = _per_week(d, items)
    out["typical"] = int(statistics.median(engs))
    if d["followers"] and d["followers"] >= 1000:
        out["rate"] = out["typical"] / d["followers"] * 100
    out["video"] = sum(1 for i in items if "video" in (i["kind"] or "")) / len(items)
    by_kind: dict[str, list[int]] = {}
    by_day: dict[int, list[int]] = {}
    for i in items:
        by_kind.setdefault(i["kind"], []).append(_engagement(i))
        by_day.setdefault(_local(i["published_at"]).weekday(), []).append(_engagement(i))
    solid = {k: statistics.median(v) for k, v in by_kind.items() if len(v) >= 2}
    if solid:
        out["best_format"] = max(solid, key=solid.get)
    days = {k: statistics.median(v) for k, v in by_day.items() if len(v) >= 2}
    if days:
        out["best_day"] = max(days, key=days.get)
    best = max(items, key=_engagement)
    out["best"] = _engagement(best)
    out["best_url"] = best.get("url")
    out["top"] = sorted(items, key=_engagement, reverse=True)[:3]
    return out


TR_ME = " class='me'"
PLATFORM_NAME = {"linkedin": "LinkedIn", "instagram": "Instagram", "youtube": "YouTube"}
# Below this many posts an account's rates are anecdotes, so the benchmark shows
# them but does not turn them into "above/below the competitors" statements.
BENCH_MIN_POSTS = 5


def render_benchmark(datasets: list[dict], *, title: str | None = None,
                     period: str = "", subject_ids=None,
                     lang: str | None = None) -> str:
    """Subjects measured against everyone else, one table per platform.

    The subject is the first dataset unless `subject_ids` names several (a client
    with a LinkedIn and an Instagram account, each matched with same-platform
    rivals). A LinkedIn reaction and an Instagram like are not the same unit, so
    they never share a table or a median.

    Nothing is dropped silently. A rival on a platform the subject is not on, or
    with no posts in the period, is named at the top with the reason: the old page
    said "against 3 accounts" over a table with one row in it.
    """
    lang = i18n.lang_of(lang or (datasets[0].get("lang") if datasets else "en"))
    plat_name = lambda p: PLATFORM_NAME.get(p, p)
    rows = [_bench_row(d) for d in datasets]
    subjects = set(subject_ids or ([rows[0]["id"]] if rows else []))
    subj = [r for r in rows if r["id"] in subjects]
    subj_plats = list(dict.fromkeys(r["platform"] for r in subj))
    live_plats = {r["platform"] for r in subj if r["posts"]}
    skipped: list[tuple[str, str]] = []
    for r in rows:
        where = plat_name(r["platform"])
        if r["id"] in subjects:
            if not r["posts"]:
                skipped.append((f"{r['name']}, {where}",
                                _t(lang, "no posts in the period")))
        elif r["platform"] not in subj_plats:
            skipped.append((f"{r['name']}, {where}",
                            _t(lang, "the subject is not on {p}", p=where)))
        elif not r["posts"]:
            skipped.append((f"{r['name']}, {where}", _t(lang, "no posts in the period")))
        elif r["platform"] not in live_plats:
            skipped.append((f"{r['name']}, {where}",
                            _t(lang, "the subject has no {p} posts in the period",
                               p=where)))

    body, compared = [], set()
    for plat in subj_plats:
        me = next((r for r in subj if r["platform"] == plat and r["posts"]), None)
        group = [r for r in rows if r["platform"] == plat and r["posts"]
                 and r["id"] not in subjects]
        if me is None or not group:
            continue
        compared |= {r["id"] for r in group}
        group = [me] + group
        trs = []
        for r in group:
            is_me = r is me
            link = _href(r.get("best_url"))
            best = _fmt(r.get("best") or 0)
            if link and r.get("best") is not None:
                best = f"<a href='{link}' rel='noopener noreferrer'>{best}</a>"
            rate = i18n.num(f"{r['rate']:.2f}%", lang) if "rate" in r else "–"
            per_week = i18n.num(f"{r.get('per_week', 0):.1f}", lang)
            you = (f" <span class='muted'>({_esc(_t(lang, 'you'))})</span>"
                   if is_me else "")
            fmt_ = _t(lang, r["best_format"].replace("_", " ")) \
                if r.get("best_format") else "–"
            day = i18n.DAYS[lang][r["best_day"]] if "best_day" in r else "–"
            got = i18n.date_num(r["anchor"], lang) if r.get("anchor") else "–"
            trs.append(
                f"<tr{TR_ME if is_me else ''}><td>{_esc(r['name'])}{you}</td>"
                f"<td class='num'>{_fmt(r['followers']) if r['followers'] else '–'}</td>"
                f"<td class='num'>{r['posts']}</td>"
                f"<td class='num'>{per_week}</td>"
                f"<td class='num strong'>{_fmt(r.get('typical', 0))}</td>"
                f"<td class='num'>{rate}</td>"
                f"<td class='num'>{int(r.get('video', 0) * 100)}%</td>"
                f"<td>{_esc(fmt_)}</td><td>{_esc(day)}</td>"
                f"<td class='num'>{best}</td><td class='nowrap'>{got}</td></tr>")
        th = lambda s, num=True: (f"<th class='num'>{_esc(_t(lang, s))}</th>" if num
                                  else f"<th>{_esc(_t(lang, s))}</th>")
        body.append(
            f"<h2>{_esc(plat_name(plat))}</h2><div class='tablewrap'><table><thead><tr>"
            + th("Account", False) + th("Followers") + th("Posts") + th("Per week")
            + th("Typical post") + th("Eng. rate") + th("Video")
            + th("Best format", False) + th("Best day", False) + th("Best post")
            + th("Data from", False)
            + "</tr></thead><tbody>" + "".join(trs) + "</tbody></table></div>")

        others = group[1:]
        if me["posts"] < BENCH_MIN_POSTS:
            body.append("<p class='muted'>" + _esc(_t(
                lang, "{name} has only {n} post(s) here, too few to say whether it is "
                      "above or below anyone.", name=me["name"], n=me["posts"]))
                + "</p>")
        else:
            notes = []
            for key, label, fmt in (("per_week", "posts per week", "{:.1f}"),
                                    ("typical", "interactions on a typical post",
                                     "{:,.0f}"),
                                    ("rate", "engagement rate", "{:.2f}%"),
                                    ("video", "share of posts that are video",
                                     "{:.0%}")):
                vals = [r[key] for r in others
                        if key in r and r["posts"] >= BENCH_MIN_POSTS]
                if key not in me or not vals:
                    continue
                med = statistics.median(vals)
                word = ("above them" if me[key] > med * 1.1 else
                        "below them" if me[key] < med * 0.9 else "in line with them")
                notes.append("<li><b>" + _esc(_t(lang, label).capitalize()) + "</b>: "
                             + _esc(_t(lang, "{a} vs {b} for the competitors' median, "
                                             "{word}.", a=i18n.num(fmt.format(me[key]), lang),
                                       b=i18n.num(fmt.format(med), lang), word=_t(lang, word)))
                             + "</li>")
            if notes:
                body.append("<h3>" + _esc(_t(lang, "Where {name} stands",
                                             name=me["name"])) + "</h3><ul>"
                            + "".join(notes) + "</ul>")

        for r in others:
            if not r.get("top"):
                continue
            lis = []
            for it in r["top"]:
                link = _href(it.get("url"))
                first = _clip(((it.get("body") or it.get("title") or "").strip()
                               .splitlines() or [""])[0], 160)
                x = _engagement(it) / max(r.get("typical") or 1, 1)
                when = i18n.date(it["published_at"], lang)
                lis.append(
                    f"<li>{_fmt(_engagement(it))} <span class='muted'>("
                    + _esc(_t(lang, "{x}x their typical, {kind}, {when}",
                              x=i18n.num(f"{x:.1f}", lang), kind=_kind(lang, it["kind"]), when=when))
                    + ")</span> "
                    + (_esc(first) or f"<span class='muted'>{_esc(_t(lang, '(no text)'))}</span>")
                    + (f" <a href='{link}' rel='noopener noreferrer'>&#8599;</a>"
                       if link else "") + "</li>")
            body.append("<h3>" + _esc(_t(lang, "{name}: best posts", name=r["name"]))
                        + "</h3><ol>" + "".join(lis) + "</ol>")

    pre = ""
    if skipped:
        pre = ("<div class='note'><p><b>" + _esc(_t(lang, "Not compared:")) + "</b> "
               + "; ".join(f"{_esc(n)} ({_esc(why)})" for n, why in skipped)
               + "</p></div>")
    if not body:
        body.append("<p class='muted'>" + _esc(_t(
            lang, "Nothing to compare: each platform needs the subject and at least one "
                  "other account with posts.")) + "</p>")
    # One name per subject: Facebook gives no display name, so the same business
    # read "bistroharbor + Bistro Harbor". Names equal but for case and spaces are
    # one, and the one written for people ("Bistro Harbor") is kept.
    by_key: dict = {}
    for r in subj:
        k = re.sub(r"[\s._-]+", "", str(r["name"]).lower())
        cur = by_key.get(k)
        if cur is None or (cur == cur.lower() and r["name"] != r["name"].lower()):
            by_key[k] = r["name"]
    names = " + ".join(by_key.values()) or "–"
    title = title or _t(lang, "{name}: competitor benchmark", name=names)
    period_txt = (" &middot; " + _esc(period)) if period else ""
    head = (f"<p class=\"sub\">{_esc(_t(lang, '{name} against {n} account(s)', name=names, n=len(compared)))}"
            f"{period_txt} &middot; {_esc(_t(lang, 'publicly visible data, own posts only'))}</p>"
            + _age_html(lang, [r["anchor"] for r in rows
                               if r["id"] in subjects or r["id"] in compared]))
    foot = ("<p class='muted'>" + _esc(_t(
        lang, "Typical post is the median of reactions + comments + shares, which a "
              "single viral post cannot move. Engagement rate is shown only above "
              "1,000 followers. Best day uses the {tz} clock and needs two posts on "
              "that day. Each account's period ends at its own last sync (Data from), "
              "so accounts synced on different days cover slightly different weeks.",
        tz=_tz_label())) + "</p>")
    doc = _doc(lang, title, head, pre + "".join(body) + foot)
    return doc.replace("</style>", "tr.me td{font-weight:600}</style>", 1)


def write_benchmark(conn, account_ids: list[int], out_path: str | Path, *,
                    since=None, days=None, title: str | None = None,
                    period: str = "", pdf: bool = False, subject_ids=None,
                    lang: str = "en"):
    datasets = [gather(conn, aid, since=since, days=days, lang=lang)
                for aid in account_ids]
    html_out = _html_path(Path(out_path).expanduser())
    html_out.parent.mkdir(parents=True, exist_ok=True)
    html_out.write_text(render_benchmark(datasets, title=title, period=period,
                                         subject_ids=subject_ids, lang=lang),
                        encoding="utf-8")
    return html_out, (to_pdf(html_out, html_out.with_suffix(".pdf")) if pdf else None)


# ── PDF ─────────────────────────────────────────────────────────────────────

CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)


def _chrome() -> str | None:
    """A Chromium to print with: $CORPUS_CHROME, the macOS apps, then PATH, then a
    Playwright download.

    The Linux server that holds the live corpus has no system Chrome at all, only
    the Chromium builds Playwright keeps under ~/.cache/ms-playwright, so `--pdf`
    failed there with "no Chrome/Chromium found" on a machine that had three.
    Newest Playwright build first, by its numeric revision.
    """
    import os
    import shutil
    override = os.environ.get("CORPUS_CHROME")
    if override:
        hit = shutil.which(override) or (override if Path(override).exists() else None)
        if hit:
            return hit
    for p in CHROME_PATHS:
        if Path(p).exists():
            return p
    for name in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable", "chrome"):
        hit = shutil.which(name)
        if hit:
            return hit
    cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
                 or "~/.cache/ms-playwright").expanduser()

    def rev(p: Path) -> int:
        part = next((x for x in p.parts if x.startswith("chromium-")), "")
        digits = "".join(c for c in part.rsplit("-", 1)[-1] if c.isdigit())
        return int(digits or 0)
    found = [p for pat in ("chromium-*/chrome-linux*/chrome",
                           "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium")
             for p in cache.glob(pat) if p.is_file()]
    return str(max(found, key=rev)) if found else None


def to_pdf(html_path: str | Path, pdf_path: str | Path | None = None,
           *, timeout: int = 120) -> Path:
    """Render the HTML brief to PDF with headless Chrome.

    Chrome rather than a Python PDF library on purpose: the brief already has print
    styles, and a real browser is the only thing that renders the CSS the client will
    see. Adding weasyprint or reportlab would mean maintaining a second, subtly
    different layout that drifts from the HTML.
    """
    import subprocess

    html_path = Path(html_path).expanduser().resolve()
    pdf_path = Path(pdf_path).expanduser() if pdf_path else html_path.with_suffix(".pdf")
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    chrome = _chrome()
    if not chrome:
        raise RuntimeError(
            "no Chrome/Chromium found for PDF export. Open the HTML and print to PDF, "
            "or install a Chromium browser."
        )

    proc = subprocess.run(
        [chrome, "--headless", "--disable-gpu", "--no-sandbox",
         "--no-pdf-header-footer", f"--print-to-pdf={pdf_path}",
         html_path.as_uri()],
        capture_output=True, text=True, timeout=timeout,
    )
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        raise RuntimeError(
            f"PDF export produced nothing (exit {proc.returncode}): "
            f"{(proc.stderr or '')[:300]}"
        )
    return pdf_path


def write_pdf(conn, account_id: int, out_path: str | Path, **kw) -> tuple[Path, Path]:
    """Write the HTML brief and its PDF. Returns (html, pdf)."""
    html_out = _html_path(Path(out_path).expanduser())
    write(conn, account_id, html_out, **kw)
    return html_out, to_pdf(html_out, html_out.with_suffix(".pdf"))
