"""The account-level lens: one read on the whole account, run once per account.

Item lenses answer "what about this post"; the question a client actually asks is
"so what do I do next month". That needs the account's numbers in front of the
model, stated with their window and their basis, or the model invents its own
("aim for a 5% engagement rate"). So the input is a dossier built from the same
figures the brief prints, and the output is a fixed JSON shape the brief can lay
out without parsing prose:

  {"summary": str, "priorities": [{"title","why","action"}] x3,
   "plan": [{"week": 1-4, "idea", "format", "why"}] x4-8}

Stored as analysis_result(target_kind='account', target_id=account.id), JSON in
output_json and a markdown rendering in output_text.

Privacy: an own/client account is PRIVATE, and so is any dossier carrying audience
questions, whoever the account is. Questions are pseudonymised before rendering.
"""
from __future__ import annotations

import json
import re
import statistics
import time

from .. import llm
from . import privacy as P
from . import run as R
from . import sensitivity as S
from . import templates as T

SLUG = "account-summary"
TOP_N = 5
MAX_QUESTIONS = 10


def _date(ts) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(int(ts)))


def _opening(it: dict, limit: int = 140) -> str:
    for f in ("title", "body", "screen_text", "transcript"):
        for line in str(it.get(f) or "").splitlines():
            line = " ".join(line.split())
            if line:
                return line if len(line) <= limit else line[:limit - 1] + "…"
    return "(no text)"


def _numbers(it: dict, engagement: int) -> str:
    parts = [f"{int(it.get(f'm_{k}') or 0)} {k}"
             for k in ("reactions", "comments", "shares", "views")
             if it.get(f"m_{k}")]
    return ", ".join(parts + [f"engagement {engagement}"])


def dossier(conn, account_id: int, *, since: int | None = None) -> tuple[str, dict]:
    """(rendered input, facts). Facts carry what routing and tests need to know,
    notably whether audience comments made it in."""
    from .. import brief as B   # brief imports analyze lazily too; keep it one-way

    data = B.gather(conn, account_id, since=since)
    acct = data["account"]
    all_items = data["items"]
    own = B._own(all_items)
    lines = [
        f"Account: {acct.get('display_name') or acct['handle']} (@{acct['handle']}) "
        f"on {acct['platform']}, relationship: {acct['relationship']}",
    ]
    if data.get("followers"):
        lines.append(f"Followers: {data['followers']} at last check")
    if not own:
        lines.append("Original posts: 0 in the window. There is nothing to rank.")
        return "\n".join(lines), {"posts": 0, "has_comments": False,
                                  "relationship": acct["relationship"],
                                  "shared_private": P.touches_private(
                                      conn, [it["id"] for it in all_items])}

    first, last = own[-1]["published_at"], own[0]["published_at"]
    days = max(1, (last - first) // 86400)
    engs = {it["id"]: B._engagement(it) for it in own}
    lines += [
        f"Window: {_date(first)} to {_date(last)} ({days} days)",
        f"Original posts: {len(own)} ({len(own) / max(1, days / 7):.1f} per week)"
        + (f"; reposts excluded: {len(all_items) - len(own)}"
           if len(all_items) > len(own) else ""),
        f"Typical post (median engagement = reactions+comments+shares): "
        f"{int(statistics.median(engs.values()))}",
        f"Best post: {max(engs.values())}",
        "Format mix (posts, median engagement):",
    ]
    by_kind: dict[str, list[int]] = {}
    for it in own:
        by_kind.setdefault(it["kind"], []).append(engs[it["id"]])
    for kind, v in sorted(by_kind.items(), key=lambda kv: -statistics.median(kv[1])):
        lines.append(f"- {kind}: {len(v)} posts, median {int(statistics.median(v))}")

    by_day: dict[int, list[int]] = {}
    for it in own:
        by_day.setdefault(B._local(it["published_at"]).weekday(), []).append(
            engs[it["id"]])
    solid = {d: statistics.median(v) for d, v in by_day.items() if len(v) >= 2}
    if solid:
        best = max(solid, key=solid.get)
        worst = min(solid, key=solid.get)
        lines.append(
            f"Best day (median, days with 2+ posts): {B.DAYS[best]} {int(solid[best])} "
            f"({len(by_day[best])} posts); worst: {B.DAYS[worst]} "
            f"{int(solid[worst])} ({len(by_day[worst])} posts)")

    # Personal posts out of the rankings: a eulogy is often an account's best post,
    # and a ranking hands it to the model as the thing to repeat.
    rankable = [it for it in own if not S.personal(S.item_text(it))]
    if len(rankable) < len(own):
        lines.append(f"Personal posts excluded from rankings: "
                     f"{len(own) - len(rankable)}")
    ranked = sorted(rankable, key=lambda it: engs[it["id"]], reverse=True)
    n = min(TOP_N, len(ranked) // 2) or min(TOP_N, len(ranked))
    for title, chunk in (("Top", ranked[:n]),
                         ("Bottom", list(reversed(ranked[-n:])) if len(ranked) > n
                          else [])):
        if not chunk:
            continue
        lines.append(f"\n{title} {len(chunk)} posts:")
        for i, it in enumerate(chunk, 1):
            lines.append(f"{i}. {_date(it['published_at'])} {it['kind']} | "
                         f"{_numbers(it, engs[it['id']])} | \"{_opening(it)}\"")

    if len(own) >= 10:
        blobs = [" ".join(str(it.get(f) or "") for f in
                          ("title", "body", "transcript", "screen_text")).lower()
                 for it in own]
        lines.append(f"\nTopics (posts mentioning it, of {len(own)}):")
        # The brief's own matcher, so the summary and the brief's "what they never
        # mention" table can never disagree about the same posts.
        for label, words, extra in B._GAP_TOPICS:
            k = sum(1 for b in blobs if B._gap_hit(b, words, extra))
            pct = k / len(blobs) * 100
            how = ("never" if k == 0 else "rarely" if pct < 15 else
                   "sometimes" if pct < 40 else "often")
            lines.append(f"- {label}: {k} ({how})")

    audience = [c for c in data.get("comments") or [] if not c.get("is_author_reply")]
    audience.sort(key=lambda c: c.get("published_at") or 0, reverse=True)
    named = P.pseudonymise(audience)
    questions = [c for c in named if B._is_question(c.get("body") or "")]
    questions = questions[:MAX_QUESTIONS]
    if questions:
        lines.append("\nRecent audience questions (pseudonymised):")
        for c in questions:
            body = " ".join((c.get("body") or "").split())[:200]
            lines.append(f"- {c['author_name']}: \"{body}\"")

    return "\n".join(lines), {
        "posts": len(own), "has_comments": bool(questions),
        "relationship": acct["relationship"],
        # Every item the dossier draws on, reposts included, checked for an
        # own/client owner or collab link: fail closed on any of them.
        "shared_private": P.touches_private(conn, [it["id"] for it in all_items]),
    }


# ── output contract ─────────────────────────────────────────────────────────

def problems(d) -> list[str]:
    """Why `d` is not the promised shape. Empty when it is."""
    if not isinstance(d, dict):
        return ["the answer is not a JSON object"]
    out = []
    extra = set(d) - {"summary", "priorities", "plan"}
    if extra:
        out.append(f"unexpected keys {sorted(extra)}")
    if not isinstance(d.get("summary"), str) or not d.get("summary", "").strip():
        out.append("summary must be a non-empty string")
    pr = d.get("priorities")
    if not isinstance(pr, list) or len(pr) != 3:
        out.append("priorities must have exactly 3 entries")
    else:
        for i, p in enumerate(pr, 1):
            if not isinstance(p, dict) or not all(
                    isinstance(p.get(k), str) and p.get(k).strip()
                    for k in ("title", "why", "action")):
                out.append(f"priority {i} needs title, why and action strings")
    pl = d.get("plan")
    if not isinstance(pl, list) or not 4 <= len(pl) <= 8:
        out.append("plan must have 4 to 8 entries")
    else:
        for i, p in enumerate(pl, 1):
            wk = p.get("week") if isinstance(p, dict) else None
            if isinstance(wk, str) and wk.strip().isdigit():
                p["week"] = wk = int(wk)        # "2" is a formatting slip, not a lie
            if not isinstance(wk, int) or isinstance(wk, bool) or not 1 <= wk <= 4:
                out.append(f"plan item {i}: week must be an integer 1-4")
            if not isinstance(p, dict) or not all(
                    isinstance(p.get(k), str) and p.get(k).strip()
                    for k in ("idea", "format", "why")):
                out.append(f"plan item {i} needs idea, format and why strings")
    return out


_NUM = re.compile(r"\d[\d,.]*\d|\d")


def _norm(n: str) -> str:
    return n.replace(",", "").rstrip(".")


def invented_numbers(d: dict, source: str) -> list[str]:
    """Numbers of 2+ digits in the summary and the 'why's that the input never
    states. Small counts ("3 posts a week") are plan targets, not claims."""
    have = {_norm(m) for m in _NUM.findall(source)}
    text = " ".join([d.get("summary") or ""] +
                    [p.get("why") or "" for p in d.get("priorities") or []
                     if isinstance(p, dict)])
    return sorted({_norm(m) for m in _NUM.findall(text)
                   if len(_norm(m).replace(".", "")) >= 2 and _norm(m) not in have})


def to_markdown(d: dict) -> str:
    out = [d["summary"].strip(), "", "**Priorities**", ""]
    for i, p in enumerate(d["priorities"], 1):
        out.append(f"{i}. **{p['title'].strip()}**: {p['why'].strip()} "
                   f"*Next step:* {p['action'].strip()}")
    out += ["", "**Four-week plan**", ""]
    for p in sorted(d["plan"], key=lambda p: p["week"]):
        out.append(f"- Week {p['week']} ({p['format'].strip()}): {p['idea'].strip()} "
                   f"({p['why'].strip()})")
    return "\n".join(out)


# ── the pass ────────────────────────────────────────────────────────────────

def prepare(conn, account_id: int, tpl: T.Template, *, lang: str | None = None,
            since: int | None = None) -> R.Job:
    body, facts = dossier(conn, account_id, since=since)
    prompt = T.ensure_json_word(T.render(tpl, {"body": body}))
    privacy = P.classify(relationship=facts["relationship"],
                         has_comments=facts["has_comments"],
                         shared_with_private=facts["shared_private"])
    acct = conn.execute("SELECT handle FROM account WHERE id=?",
                        (account_id,)).fetchone()
    job = R.Job("account", account_id, f"account @{acct['handle']}", prompt,
                R.system_prompt(lang, json_mode=True), privacy,
                P.chain_for(privacy), json_mode=True, lang=lang,
                row={"facts": facts, "dossier": body})
    if not job.chain:
        job.skip = "private" if privacy == P.PRIVATE else "none"
        job.why = P.skip_reason(privacy) if privacy == P.PRIVATE else \
            "no LLM provider configured"
    return job


def _ask(tpl, job: R.Job) -> tuple[dict | None, str | None, dict | None, int]:
    """One call plus at most one repair. Local models get the shape right most of
    the time and wrong in small ways (two priorities, week "2"); telling them what
    was wrong fixes it more cheaply than failing the account."""
    started = time.time()
    messages = job.messages
    used, last_err = None, None
    for attempt in range(2):
        try:
            raw = llm.chat(messages, json_mode=True, temperature=tpl.temperature,
                           max_tokens=tpl.max_tokens, chain=job.chain)
        except llm.LLMError as e:
            return None, f"failed:{e}", None, 0
        used = llm.last_used() or job.chain[0]
        try:
            d = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        except (ValueError, TypeError):
            d = None
        if d is not None and S.is_sentinel(d):
            return d, None, used, int((time.time() - started) * 1000)
        issues = problems(d) if d is not None else ["the answer is not valid JSON"]
        if d is not None and not issues:
            bad = invented_numbers(d, job.row["dossier"])
            if bad and attempt == 0:
                issues = [f"these numbers are not in the input: {', '.join(bad)}; "
                          f"use only numbers stated in THE ACCOUNT"]
            else:
                return d, None, used, int((time.time() - started) * 1000)
        last_err = "; ".join(issues)
        messages = job.messages + [
            {"role": "assistant", "content": raw or ""},
            {"role": "user", "content": "That answer does not meet the contract: "
             f"{last_err}. Return the corrected JSON object only."}]
    return None, f"failed:invalid account-summary JSON ({last_err})", used, 0


def run(conn, account_id: int, *, db_path=R.DEFAULT_DB, dry_run: bool = True,
        lang: str | None = None, force: bool = False, since: int | None = None,
        template_dir=T.TEMPLATE_DIR) -> R.AnalyzeStats:
    R.attach(conn, db_path)
    tpl = T.load(SLUG, template_dir)
    job = prepare(conn, account_id, tpl, lang=lang, since=since)
    stats = R.AnalyzeStats(template=tpl.slug, considered=1)
    stats.plan.append(f"{job.label} [{job.privacy}] -> {job.destination}")

    if not force and R.existing(conn, target_kind="account", target_id=account_id,
                                template=tpl.slug, prompt_hash=tpl.prompt_hash,
                                input_hash=job.ihash):
        stats.skipped = 1
        return stats
    if job.skip:
        stats.private_skipped = 1
        stats.errors.append(job.why)
        return stats
    if not job.row["facts"]["posts"]:
        stats.errors.append("no original posts in the window; nothing to summarise")
        return stats

    stats.est_cost_usd = R.estimate_cost(job.system + job.prompt, tpl.max_tokens,
                                         job.chain[0]["name"])
    if dry_run:
        stats.errors.append(
            f"dry-run: would summarise {job.label} on {job.chain[0]['name']}, est "
            f"${stats.est_cost_usd:.4f}.")
        return stats

    d, err, used, latency = _ask(tpl, job)
    if err:
        stats.failed = 1
        stats.errors.append(f"{job.label}: {err}")
        return stats
    if S.is_sentinel(d):
        R._record(conn, tpl, job, S.SENTINEL, latency, used, status="personal")
    else:
        R._record(conn, tpl, job, to_markdown(d), latency, used,
                  output_json=json.dumps(d, ensure_ascii=False))
    stats.done = 1
    stats.cost_usd = conn.execute(
        "SELECT cost_usd FROM a.analysis_result WHERE target_kind='account' AND "
        "target_id=? AND template=? ORDER BY id DESC LIMIT 1",
        (account_id, tpl.slug)).fetchone()["cost_usd"] or 0.0
    return stats


def latest(conn, account_id: int, *, db_path=R.DEFAULT_DB) -> dict | None:
    """Newest stored summary for the account, JSON decoded. For the brief."""
    R.attach(conn, db_path)
    r = conn.execute(
        """SELECT output_json, output_text, lang, status, provider, created_at
             FROM a.analysis_result WHERE target_kind='account' AND target_id=?
              AND template=? ORDER BY created_at DESC, id DESC LIMIT 1""",
        (account_id, SLUG)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["data"] = json.loads(r["output_json"]) if r["output_json"] else None
    return d
