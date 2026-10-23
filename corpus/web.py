"""A local dashboard for the corpus.

Stdlib only, on purpose. The project has exactly one runtime dependency and the
adapter layer deliberately has no framework in it; a web UI is not a good reason to
add Flask, a template engine and a migration story. http.server is enough for a
single operator on localhost, and keeping it here means the same code can later sit
behind a real server without a rewrite of anything but the transport.

Binding
-------
127.0.0.1 by default and it will not listen on a public interface without BOTH an
explicit --host and a token. This database holds comment text written by third
parties who never consented to anything; an unauthenticated public bind would be
the single worst thing in the project.
"""
from __future__ import annotations

import html
import json
import secrets
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import brief as brief_mod
from . import comments as comments_mod
from . import outliers as outliers_mod
from . import store

# ── job runner ──────────────────────────────────────────────────────────────

class Jobs:
    """Background work, so a 4-minute sync does not hold the request open.

    Deliberately in-process and unpersisted: a dashboard for one operator does not
    need a queue, and a restart losing job history is cheaper than a scheduler.
    One worker at a time, because SQLite takes exactly one writer.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._log: list[dict] = []
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def recent(self, n=12) -> list[dict]:
        with self._lock:
            return list(reversed(self._log[-n:]))

    def start(self, label: str, fn) -> dict:
        with self._lock:
            if self._busy:
                return {"ok": False, "error": "a job is already running"}
            self._busy = True
            rec = {"label": label, "started": time.time(), "state": "running",
                   "detail": ""}
            self._log.append(rec)

        def run():
            try:
                rec["detail"] = str(fn() or "")[:400]
                rec["state"] = "ok"
            except Exception as e:
                rec["state"] = "error"
                rec["detail"] = f"{type(e).__name__}: {e}"[:400]
                traceback.print_exc()
            finally:
                rec["finished"] = time.time()
                self._busy = False

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}


# ── rendering ───────────────────────────────────────────────────────────────

def _e(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def _fmt(n) -> str:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "-"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n/1000:.0f}k"
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)


CSS = """
*{box-sizing:border-box}
:root{--bg:#0d0e12;--fg:#e8e8ea;--mut:#8b8d98;--line:#25262e;--card:#14161c;
 --accent:#6ea8fe;--ok:#7ddc9a;--warn:#e8c98a;--bad:#f0a5a5}
@media(prefers-color-scheme:light){:root{--bg:#fbfbfd;--fg:#16171c;--mut:#63656f;
 --line:#e3e4ea;--card:#fff;--accent:#2563eb}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,
 BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:1100px;margin:0 auto;padding:2rem 1.5rem 5rem}
header{display:flex;align-items:baseline;gap:1rem;flex-wrap:wrap;
 border-bottom:1px solid var(--line);padding-bottom:1rem;margin-bottom:2rem}
h1{font-size:1.25rem;margin:0;letter-spacing:-.01em}
h2{font-size:.72rem;text-transform:uppercase;letter-spacing:.09em;color:var(--mut);
 margin:2.5rem 0 .85rem;font-weight:650}
.sub{color:var(--mut);font-size:.85rem}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.75rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
 padding:.9rem 1rem}
.card .k{font-size:.66rem;text-transform:uppercase;letter-spacing:.07em;
 color:var(--mut);font-weight:650}
.card .v{font-size:1.5rem;font-weight:640;letter-spacing:-.02em;margin-top:.15rem}
.card .h{font-size:.72rem;color:var(--mut);margin-top:.15rem}
table{width:100%;border-collapse:collapse;font-size:.88rem}
th{text-align:left;font-size:.66rem;text-transform:uppercase;letter-spacing:.07em;
 color:var(--mut);font-weight:650;padding:.5rem .6rem;border-bottom:1px solid var(--line)}
td{padding:.6rem;border-bottom:1px solid var(--line);vertical-align:top}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
.pill{display:inline-block;font-size:.68rem;padding:.12rem .45rem;border-radius:99px;
 border:1px solid var(--line);color:var(--mut)}
.pill.own{color:var(--ok);border-color:var(--ok)}
.pill.client{color:var(--accent);border-color:var(--accent)}
form.inline{display:inline}
button{font:inherit;font-size:.78rem;padding:.3rem .7rem;border-radius:7px;
 border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button:hover{border-color:var(--accent);color:var(--accent)}
button[disabled]{opacity:.4;cursor:not-allowed}
input[type=search],input[type=text]{font:inherit;padding:.5rem .7rem;border-radius:8px;
 border:1px solid var(--line);background:var(--card);color:var(--fg);width:100%}
.job{font-size:.8rem;padding:.4rem 0;border-bottom:1px solid var(--line);
 display:flex;gap:.6rem;align-items:baseline}
.state-ok{color:var(--ok)} .state-error{color:var(--bad)} .state-running{color:var(--warn)}
.hit{padding:.8rem 0;border-bottom:1px solid var(--line)}
.hit .meta{font-size:.72rem;color:var(--mut);margin-bottom:.2rem}
mark{background:rgba(110,168,254,.25);color:inherit;border-radius:2px}
.muted{color:var(--mut);font-size:.83rem}
.warnbar{background:rgba(240,165,165,.12);border:1px solid var(--bad);color:var(--bad);
 padding:.6rem .9rem;border-radius:8px;font-size:.82rem;margin-bottom:1.5rem}
.stale{color:var(--bad);font-weight:600}
.fresh{color:var(--ok)}
.overview{display:grid;grid-template-columns:1fr 1fr;gap:1.5rem}
@media(max-width:820px){.overview{grid-template-columns:1fr}}
"""


def page(title: str, body: str, *, jobs: Jobs | None = None) -> bytes:
    running = ""
    if jobs and jobs.busy:
        running = ("<div class='warnbar'>A job is running. SQLite takes one writer, "
                   "so other actions are blocked until it finishes. "
                   "<a href='javascript:location.reload()'>refresh</a></div>")
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{_e(title)}</title><style>{CSS}</style></head><body>"
            f"<div class='wrap'><header><h1><a href='/'>corpus</a></h1>"
            f"<span class='sub'>{_e(title)}</span></header>"
            f"{running}{body}</div></body></html>").encode()


# ── views ───────────────────────────────────────────────────────────────────

def _age_cell(ts, now: int) -> str:
    """Data age, red past the brief's stale line. The briefs were going out built
    on two-month-old syncs because nothing on this page said how old anything was."""
    if not ts:
        return "<span class='stale'>never synced</span>"
    days = int((now - ts) // 86400)
    cls = "stale" if days > brief_mod.STALE_DAYS else "fresh"
    when = time.strftime("%d %b %Y", time.gmtime(ts))
    return f"<span class='{cls}'>{days}d</span> <span class='muted'>{when}</span>"


def view_overview(conn) -> str:
    """What needs attention: stale accounts, recent breakouts, and a way into the
    benchmark for each client."""
    now = store.now()
    accts = [dict(r) for r in conn.execute(
        "SELECT * FROM account ORDER BY relationship, platform, handle")]
    out = ["<h2>Overview</h2><div class='overview'><div>"
           "<table><thead><tr><th>Account</th><th>Data age</th></tr></thead><tbody>"]
    for a in accts:
        out.append(
            f"<tr><td><a href='/a/{_acct_ref(a)}'>{_e(a['handle'])}"
            f"</a> <span class='pill {_e(a['relationship'])}'>{_e(a['relationship'])}"
            f"</span></td><td>{_age_cell(brief_mod._anchor(conn, a), now)}</td></tr>")
    out.append("</tbody></table></div><div>")

    found = outliers_mod.find(conn, days=90, relationships=None)
    found.sort(key=lambda o: -o["published_at"])
    if found:
        out.append("<table><thead><tr><th>Latest breakouts</th><th class='num'>x usual"
                   "</th></tr></thead><tbody>")
        for o in found[:6]:
            year = time.gmtime(o["published_at"]).tm_year
            fmt = "%d %b" if year == time.gmtime(now).tm_year else "%d %b %Y"
            link = (f" <a href='{_e(o['url'])}' rel='noopener noreferrer'>&#8599;</a>"
                    if str(o.get("url") or "").startswith(("http://", "https://")) else "")
            out.append(
                f"<tr><td><b>{_e(o['handle'])}</b> <span class='muted'>"
                f"{time.strftime(fmt, time.gmtime(o['published_at']))}</span>"
                f"<div class='muted'>{_e(o['opening'] or '(no text)')}{link}</div></td>"
                f"<td class='num'>{o['x']}x <span class='muted'>({_fmt(o['engagement'])})"
                f"</span></td></tr>")
        out.append("</tbody></table>")
    else:
        out.append("<p class='muted'>No post in the last 90 days beat its account's "
                   "usual by 3x with 50+ interactions.</p>")
    refs = sorted({a["client_ref"] for a in accts
                   if a["client_ref"] and a["relationship"] in ("client", "own")})
    rivals = [a for a in accts if a["relationship"] in ("competitor", "influencer")]
    if refs and rivals:
        out.append("<p>" + " &middot; ".join(
            f"<a href='/compare/{urllib.parse.quote(r)}'>compare {_e(r)} &rarr;</a>"
            for r in refs) + "</p>")
    elif refs:
        out.append("<p class='muted'>No competitors tracked to compare against: "
                   "<code>corpus add &lt;url&gt; --relationship competitor</code></p>")
    out.append("</div></div>")
    return "".join(out)


def view_index(conn, jobs: Jobs) -> str:
    rows = conn.execute("""
        SELECT a.id, a.handle, a.platform, a.display_name, a.relationship,
               a.client_ref, a.follower_count, a.last_scraped_at,
          (SELECT COUNT(*) FROM item i WHERE (i.account_id=a.id OR i.id IN (SELECT item_id FROM item_account WHERE account_id=a.id))
             AND COALESCE(i.is_repost,0)=0) posts,
          (SELECT COUNT(*) FROM comment c JOIN item i ON i.id=c.item_id
             WHERE (i.account_id=a.id OR i.id IN (SELECT item_id FROM item_account WHERE account_id=a.id))) cmts,
          (SELECT COUNT(*) FROM item i WHERE i.account_id=a.id
             AND COALESCE(i.transcript,'') <> '') transcripts
          FROM account a ORDER BY a.client_ref, a.platform""").fetchall()

    tot = {k: sum(r[k] or 0 for r in rows) for k in ("posts", "cmts", "transcripts")}
    cards = [("Accounts", str(len(rows)), "tracked profiles"),
             ("Posts", _fmt(tot["posts"]), "excluding reposts"),
             ("Comments", _fmt(tot["cmts"]), "audience text captured"),
             ("Transcribed", _fmt(tot["transcripts"]), "videos with speech read")]
    out = ["<div class='cards'>" + "".join(
        f"<div class='card'><div class='k'>{_e(k)}</div><div class='v'>{_e(v)}</div>"
        f"<div class='h'>{_e(h)}</div></div>" for k, v, h in cards) + "</div>",
        view_overview(conn)]

    out.append("<h2>Profiles</h2><table><thead><tr><th>Handle</th><th>Platform</th>"
               "<th></th><th class='num'>Posts</th><th class='num'>Comments</th>"
               "<th class='num'>Followers</th><th>Last sync</th><th></th>"
               "</tr></thead><tbody>")
    for r in rows:
        rel = _e(r["relationship"])
        last = (time.strftime("%d %b", time.gmtime(r["last_scraped_at"]))
                if r["last_scraped_at"] else "<span class='muted'>never</span>")
        out.append(
            f"<tr><td><a href='/a/{_acct_ref(r)}'>"
            f"{_e(r['handle'])}</a><div class='muted'>{_e(r['display_name'] or '')}"
            f"</div></td>"
            f"<td>{_e(r['platform'])}</td>"
            f"<td><span class='pill {rel}'>{rel}</span></td>"
            f"<td class='num'>{_fmt(r['posts'])}</td>"
            f"<td class='num'>{_fmt(r['cmts'])}</td>"
            f"<td class='num'>{_fmt(r['follower_count'])}</td>"
            f"<td>{last}</td>"
            f"<td>{_actions(r['handle'], jobs)}</td></tr>")
    out.append("</tbody></table>")

    refs = sorted({r["client_ref"] for r in rows if r["client_ref"]})
    if refs:
        out.append("<h2>Briefs</h2><p class='muted'>One document per person, across "
                   "every platform they are tracked on.</p><table><tbody>")
        for ref in refs:
            who = ", ".join(r["handle"] for r in rows if r["client_ref"] == ref)
            out.append(f"<tr><td><b>{_e(ref)}</b><div class='muted'>{_e(who)}</div>"
                       f"</td><td class='num'>"
                       f"<a href='/brief/{urllib.parse.quote(ref)}'>open brief &rarr;</a>"
                       f"</td></tr>")
        out.append("</tbody></table>")

    out.append("<h2>Search</h2><form method='get' action='/search'>"
               "<input type='search' name='q' placeholder='Search captions, "
               "transcripts, on-screen text and comments...' autofocus></form>")

    jl = jobs.recent()
    if jl:
        out.append("<h2>Recent jobs</h2>")
        for j in jl:
            dur = int((j.get("finished") or time.time()) - j["started"])
            out.append(f"<div class='job'><span class='state-{j['state']}'>"
                       f"{_e(j['state'])}</span><b>{_e(j['label'])}</b>"
                       f"<span class='muted'>{dur}s {_e(j['detail'])}</span></div>")
    return "".join(out)


# One per process. A page on another origin can make the browser POST here, but it
# cannot read this value out of our HTML (same-origin policy), so it cannot forge it.
CSRF = secrets.token_urlsafe(24)


def _actions(handle: str, jobs: Jobs) -> str:
    dis = " disabled" if jobs.busy else ""
    btns = []
    for action, label in (("sync", "sync"), ("enrich", "enrich"),
                          ("comments", "comments")):
        btns.append(
            f"<form class='inline' method='post' action='/run'>"
            f"<input type='hidden' name='csrf' value='{CSRF}'>"
            f"<input type='hidden' name='action' value='{action}'>"
            f"<input type='hidden' name='handle' value='{_e(handle)}'>"
            f"<button{dis}>{label}</button></form> ")
    return "".join(btns)


def _acct_ref(a) -> str:
    return urllib.parse.quote(f"{a['platform']}:{a['handle']}")


def view_account(conn, handle: str, jobs: Jobs) -> str | None:
    # platform:handle, because one handle can be on two platforms (a restaurant on
    # Instagram and Facebook) and /a/<handle> only ever opened the first.
    plat, sep, bare = handle.partition(":")
    acct = (conn.execute("SELECT * FROM account WHERE platform=? AND handle=?",
                         (plat, bare)).fetchone() if sep else
            conn.execute("SELECT * FROM account WHERE handle=?", (handle,)).fetchone())
    if acct is None:
        return None
    aid = acct["id"]
    stats = conn.execute(f"""
        SELECT COUNT(*) posts,
               SUM(COALESCE(m_reactions,0)) rx,
               SUM(COALESCE(m_comments,0)) cm,
               SUM(CASE WHEN COALESCE(transcript,'')<>'' THEN 1 ELSE 0 END) tx,
               SUM(CASE WHEN COALESCE(screen_text,'')<>'' THEN 1 ELSE 0 END) ocr
          FROM item WHERE {store.account_items("")} AND COALESCE(is_repost,0)=0""",
        (aid, aid)).fetchone()
    have = conn.execute(f"""SELECT COUNT(*) n FROM comment c JOIN item i
        ON i.id=c.item_id WHERE {store.account_items()}""", (aid, aid)).fetchone()["n"]

    cards = [("Posts", _fmt(stats["posts"]), "excluding reposts"),
             ("Reactions", _fmt(stats["rx"]), "across all posts"),
             ("Comments", f"{_fmt(have)} / {_fmt(stats['cm'])}", "text / counted"),
             ("Transcribed", _fmt(stats["tx"]), "videos with speech"),
             ("On-screen text", _fmt(stats["ocr"]), "read off the frames")]
    out = ["<div class='cards'>" + "".join(
        f"<div class='card'><div class='k'>{_e(k)}</div><div class='v'>{_e(v)}</div>"
        f"<div class='h'>{_e(h)}</div></div>" for k, v, h in cards) + "</div>",
        f"<p style='margin-top:1rem'>{_actions(handle, jobs)}</p>"]

    if acct["client_ref"]:
        out.append(f"<p><a href='/brief/{urllib.parse.quote(acct['client_ref'])}'>"
                   f"open the full brief &rarr;</a></p>")

    out.append("<h2>Posts</h2><table><thead><tr><th>Date</th><th>Type</th>"
               "<th class='num'>Rx</th><th class='num'>Cm</th><th>Opening</th>"
               "<th class='num'>Comments held</th></tr></thead><tbody>")
    for r in conn.execute(f"""
            SELECT i.id, i.published_at, i.kind, i.m_reactions, i.m_comments,
                   i.body, i.title, i.url,
                   (SELECT COUNT(*) FROM comment c WHERE c.item_id=i.id) held
              FROM item i WHERE {store.account_items()} AND COALESCE(i.is_repost,0)=0
             ORDER BY i.published_at DESC LIMIT 200""", (aid, aid)):
        when = time.strftime("%Y-%m-%d", time.gmtime(r["published_at"]))
        text = ((r["body"] or r["title"] or "").strip().split("\n") or [""])[0][:110]
        link = (f"<a href='{_e(r['url'])}' rel='noopener noreferrer'>&#8599;</a>"
                if (r["url"] or "").startswith(("http://", "https://")) else "")
        out.append(f"<tr><td>{when}</td><td>{_e(r['kind'].replace('_',' '))}</td>"
                   f"<td class='num'>{_fmt(r['m_reactions'])}</td>"
                   f"<td class='num'>{_fmt(r['m_comments'])}</td>"
                   f"<td>{_e(text)} {link}</td>"
                   f"<td class='num'>{_fmt(r['held'])}</td></tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _snippet(text: str, needle: str, width: int = 220) -> str:
    """Highlight the hit in context. Escapes first, so <mark> is the only markup."""
    t = " ".join((text or "").split())
    low, n = t.lower(), needle.lower()
    i = low.find(n)
    if i < 0:
        return _e(t[:width]) + ("&hellip;" if len(t) > width else "")
    start = max(0, i - width // 3)
    frag = t[start:start + width]
    esc = _e(frag)
    mark = _e(frag[i - start:i - start + len(needle)])
    if mark:
        esc = esc.replace(mark, f"<mark>{mark}</mark>", 1)
    return ("&hellip;" if start else "") + esc + ("&hellip;" if len(t) > start + width else "")


def view_search(conn, q: str) -> str:
    if not q.strip():
        return "<p class='muted'>Type something to search.</p>"
    out = [f"<form method='get' action='/search'><input type='search' name='q' "
           f"value='{_e(q)}'></form>"]

    try:
        # sqlite3.Row has no .get(), and the projection is not guaranteed to carry
        # every text layer, so normalise once here rather than at each use.
        # Control characters as highlight delimiters: they cannot occur in a
        # caption, so escaping the snippet first and swapping them for <mark>
        # afterwards can never turn a literal bracket into markup.
        hits = [dict(r) for r in store.search(conn, q, limit=40,
                                              open_mark="\x02", close_mark="\x03")]
    except Exception as e:
        return "".join(out) + f"<p class='muted'>{_e(str(e))}</p>"

    out.append(f"<h2>Posts &middot; {len(hits)}</h2>")
    for h in hits:
        when = time.strftime("%Y-%m-%d", time.gmtime(h["published_at"]))
        snip = (_e(h.get("snip") or "").replace("\x02", "<mark>")
                .replace("\x03", "</mark>"))
        link = ""
        if str(h.get("url") or "").startswith(("http://", "https://")):
            link = (f" &middot; <a href='{_e(h['url'])}' rel='noopener noreferrer'>"
                    f"open</a>")
        out.append(f"<div class='hit'><div class='meta'>{_e(h.get('handle') or '')}"
                   f" &middot; {when} &middot; {_e(h.get('kind') or '')}{link}</div>"
                   f"{snip or '<span class=muted>(no text)</span>'}</div>")

    # Comments are not in the FTS index (they arrived later and are third-party
    # content), so they get a plain LIKE. Slower, but the corpus is small and a
    # search that silently skipped what the audience said would be a poor search.
    like = f"%{q}%"
    crows = conn.execute("""
        SELECT c.body, c.author_name, a.handle, i.url
          FROM comment c JOIN item i ON i.id=c.item_id
          JOIN account a ON a.id=i.account_id
         WHERE c.body LIKE ? ORDER BY COALESCE(c.reactions,0) DESC LIMIT 30""",
        (like,)).fetchall()
    out.append(f"<h2>Comments &middot; {len(crows)}</h2>")
    for r in crows:
        out.append(f"<div class='hit'><div class='meta'>{_e(r['author_name'])} "
                   f"on {_e(r['handle'])}</div>{_snippet(r['body'], q)}</div>")
    if not hits and not crows:
        out.append("<p class='muted'>Nothing found.</p>")
    return "".join(out)


# ── server ──────────────────────────────────────────────────────────────────

def make_handler(*, db_path, analysis_db, media_root, jobs: Jobs, token: str | None,
                 brief_dir: Path, allowed_hosts: set[str] | None = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "corpus"

        def log_message(self, fmt, *args):
            pass                      # the CLI prints what matters

        # -- helpers
        def _db(self):
            return store.connect(db_path)

        def _host_ok(self) -> bool:
            """DNS rebinding: evil.example re-resolved to 127.0.0.1 reaches this port
            as same-origin and can read every page. Its requests still carry
            Host: evil.example, so only our own names are served."""
            if allowed_hosts is None:
                return True
            return (self.headers.get("Host") or "").lower() in allowed_hosts

        def _cookie_token(self) -> str:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "corpus_token":
                    return v
            return ""

        def _authed(self) -> bool:
            if not token:
                return True
            # Header for scripts, ?token= once from the printed URL, then a cookie:
            # links and forms never carry the query string, so without the cookie
            # every click after the first page was a 403.
            q = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query).get("token", [""])[0]
            got = (self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                   or q or self._cookie_token())
            ok = secrets.compare_digest(got, token)
            self._new_cookie = ok and bool(q)
            return ok

        def _send(self, body: bytes, code=200, ctype="text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # This page embeds only its own inline CSS and renders vendor-supplied
            # text; no third-party origin should ever be reachable from it.
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; "
                             "img-src data:; form-action 'self'")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            if getattr(self, "_new_cookie", False):
                self.send_header("Set-Cookie", f"corpus_token={token}; Path=/; "
                                 "HttpOnly; SameSite=Strict")
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, to="/"):
            self.send_response(303)
            self.send_header("Location", to)
            self.end_headers()

        # -- routes
        def do_GET(self):
            if not self._host_ok():
                return self._send(page("wrong host", "<p>Unknown Host.</p>"), 421)
            if not self._authed():
                return self._send(page("locked", "<p>Bad or missing token.</p>"), 403)
            u = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(u.path)
            qs = urllib.parse.parse_qs(u.query)
            conn = self._db()
            try:
                if path == "/":
                    return self._send(page("overview", view_index(conn, jobs),
                                           jobs=jobs))
                if path == "/search":
                    q = (qs.get("q") or [""])[0]
                    return self._send(page(f"search: {q}", view_search(conn, q),
                                           jobs=jobs))
                if path.startswith("/a/"):
                    body = view_account(conn, path[3:], jobs)
                    if body is None:
                        return self._send(page("not found", "<p>No such account.</p>"),
                                          404)
                    return self._send(page(path[3:], body, jobs=jobs))
                if path.startswith("/brief/"):
                    return self._brief(conn, path[len("/brief/"):])
                if path.startswith("/compare/"):
                    return self._compare(conn, path[len("/compare/"):])
                return self._send(page("not found", "<p>No such page.</p>"), 404)
            finally:
                conn.close()

        def _brief(self, conn, ref: str):
            """Render on demand and cache to disk, so a reload is instant."""
            safe = "".join(c for c in ref if c.isalnum() or c in "-_")
            if not safe:
                return self._send(page("bad ref", "<p>Bad reference.</p>"), 400)
            # The client's own accounts, as `corpus brief all --client-ref` does:
            # competitors tagged with the same ref are rivals, not the client.
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM account WHERE client_ref=? AND relationship IN "
                "('own','client') ORDER BY platform", (ref,))]
            if not ids:
                return self._send(page("not found",
                                       "<p>No accounts under that reference.</p>"),
                                  404)
            out = brief_dir / f"{safe}.html"
            # Regenerating a brief costs nothing but a second of CPU, and a stale
            # one after a sync is worse than the wait. 15 minutes is long enough
            # that clicking around is instant.
            fresh = out.exists() and (time.time() - out.stat().st_mtime) < 900
            if not fresh:
                try:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    # Same defaults as `corpus brief all --client-ref`: the last
                    # 12 months, every client-safe lens, the client's name as title.
                    brief_mod.write_combined(
                        conn, ids, out, analysis_db=analysis_db,
                        days=brief_mod.DEFAULT_WINDOW_DAYS, pdf=False)
                except Exception as e:
                    return self._send(page("brief failed",
                                           f"<p>{_e(type(e).__name__)}: {_e(e)}</p>"),
                                      500)
            self._send(out.read_bytes())

        def _compare(self, conn, ref: str):
            """A client's accounts against every tracked competitor, same platform
            only. Read-only, rendered on demand and cached like a brief."""
            safe = "".join(c for c in ref if c.isalnum() or c in "-_")
            if not safe or safe != ref:
                return self._send(page("bad ref", "<p>Bad reference.</p>"), 400)
            subj = [r["id"] for r in conn.execute(
                "SELECT id FROM account WHERE client_ref=? AND relationship IN "
                "('own','client') ORDER BY platform", (ref,))]
            # Every tracked competitor, including the ones tagged with this client's
            # ref (the natural way to say "the bistro's rivals"; they used to be dropped
            # from the rivals AND counted as the client).
            rivals = [r["id"] for r in conn.execute(
                "SELECT id FROM account WHERE relationship IN ('competitor','influencer')"
                " ORDER BY platform")]
            if not subj:
                return self._send(page("not found",
                                       "<p>No accounts under that reference.</p>"), 404)
            out = brief_dir / f"benchmark-{safe}.html"
            fresh = out.exists() and (time.time() - out.stat().st_mtime) < 900
            if not fresh:
                try:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    brief_mod.write_benchmark(
                        conn, subj + rivals, out, days=90, subject_ids=subj,
                        period="last 90 days before each account's last sync")
                except Exception as e:
                    return self._send(page("compare failed",
                                           f"<p>{_e(type(e).__name__)}: {_e(e)}</p>"),
                                      500)
            self._send(out.read_bytes())

        def do_POST(self):
            if not self._host_ok():
                return self._send(page("wrong host", "<p>Unknown Host.</p>"), 421)
            if not self._authed():
                return self._send(page("locked", "<p>Bad or missing token.</p>"), 403)
            if urllib.parse.urlparse(self.path).path != "/run":
                return self._send(page("not found", "<p>No such page.</p>"), 404)
            # POST /run spends money (comments). Any page the user visits can submit
            # a form here, so the request must prove it came from our own page.
            origin = self.headers.get("Origin")
            if origin and allowed_hosts is not None and \
                    urllib.parse.urlparse(origin).netloc.lower() not in allowed_hosts:
                return self._send(page("forbidden", "<p>Cross-origin request.</p>"), 403)
            n = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(n).decode())
            if not secrets.compare_digest((form.get("csrf") or [""])[0], CSRF):
                return self._send(page("forbidden", "<p>Stale or forged form; "
                                       "reload the page.</p>"), 403)
            action = (form.get("action") or [""])[0]
            handle = (form.get("handle") or [""])[0]
            self._start(action, handle)
            self._redirect(self.headers.get("Referer") or "/")

        def _start(self, action: str, handle: str):
            from . import keys as keys_mod

            def work():
                # Same cross-process lock as the CLI: a nightly sync and a
                # dashboard enrich must not both write. LockBusy fails the job.
                from . import ops
                with ops.run_lock(db_path, what=f"dashboard {action} {handle}"):
                    return _work()

            def _work():
                conn = store.connect(db_path)
                try:
                    acct = conn.execute("SELECT * FROM account WHERE handle=?",
                                        (handle,)).fetchone()
                    if acct is None:
                        # Raise, not return: a job that did nothing because the
                        # target does not exist is a failure, and reporting it
                        # green means nobody looks at it again.
                        raise LookupError(f"no account {handle!r}")
                    k = keys_mod.load()
                    if action == "comments":
                        # Same guard as the CLI; a refusal surfaces as a failed job.
                        comments_mod.assert_affordable(conn, [dict(acct)],
                                                       token=k.get("APIFY_TOKEN"))
                        r = comments_mod.sync(conn, dict(acct),
                                              token=k.get("APIFY_TOKEN"))
                        return (f"{r.get('comments', 0)} comments, "
                                f"${r.get('cost', 0):.2f}")
                    if action == "enrich":
                        from .enrich import run as enrich_run
                        st = enrich_run.run(conn, media_root=media_root,
                                            account_id=acct["id"], limit=200)
                        d = st.as_dict()
                        return (f"{d['transcribed']} transcribed, "
                                f"{d.get('images', 0)} images")
                    if action == "sync":
                        return ("sync is not wired to the dashboard yet; "
                                "run `corpus sync` in a terminal")
                    return f"unknown action {action!r}"
                finally:
                    conn.close()

            jobs.start(f"{action} {handle}", work)

    return Handler


def serve(*, db_path, analysis_db=None, media_root, host="127.0.0.1", port=8891,
          token: str | None = None, brief_dir: Path | None = None) -> None:
    """Run the dashboard. Refuses an unauthenticated public bind."""
    public = host not in ("127.0.0.1", "localhost", "::1")
    if public and not token:
        raise SystemExit(
            f"refusing to bind {host} without a token.\n"
            "This database holds comment text written by third parties who never "
            "consented to it being published. Pass --token, or bind 127.0.0.1 and "
            "reach it over an SSH tunnel:\n"
            f"    ssh -N -L {port}:127.0.0.1:{port} <this-machine>")

    brief_dir = Path(brief_dir or "~/.corpus/briefs").expanduser()
    jobs = Jobs()
    allowed: set[str] = set()     # filled once the real port is known (port 0)
    handler = make_handler(db_path=db_path, analysis_db=analysis_db,
                           media_root=media_root, jobs=jobs, token=token,
                           brief_dir=brief_dir, allowed_hosts=allowed)
    httpd = ThreadingHTTPServer((host, port), handler)
    real = getattr(httpd, "server_address", (host, port))[1]
    allowed.update(f"{n}:{real}" for n in {"127.0.0.1", "localhost", "[::1]", host.lower()})
    url = f"http://{host}:{port}/" + (f"?token={token}" if token else "")
    print(f"corpus dashboard  {url}")
    print("  ctrl-c to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
