"""The analysis pass.

Everything here lives in a SECOND database file, analysis.db, attached on demand.
That is the toggle: the fetch and enrich paths never open it, so you can delete it,
re-run with better prompts, and the corpus is untouched. A config flag would not give
you that property.

Two caveats the separation costs, both real:

  * SQLite gives cross-database atomicity only when the main database is NOT in WAL
    mode. Ours is. So a crash mid-commit can leave analysis.db and corpus.db briefly
    inconsistent. Idempotency is what rescues this: re-running recomputes rather than
    corrupts, which is why the uniqueness constraint below matters more than it looks.
  * There are no cross-database foreign keys. analysis rows reference item ids with
    nothing enforcing they still exist, so `prune` exists to sweep orphans.
"""
from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .. import llm, providers, store, textq
from . import privacy as P
from . import sensitivity as S
from . import templates as T

ANALYSIS_SCHEMA = """
CREATE TABLE IF NOT EXISTS a.analysis_result (
  id            INTEGER PRIMARY KEY,
  target_kind   TEXT    NOT NULL,          -- item | account
  target_id     INTEGER NOT NULL,
  template      TEXT    NOT NULL,
  prompt_hash   TEXT    NOT NULL,          -- identity of the prompt text+model+params
  input_hash    TEXT    NOT NULL,          -- identity of the rendered input
  output_text   TEXT,
  output_json   TEXT,
  model         TEXT,
  provider      TEXT,
  tokens_in     INTEGER,
  tokens_out    INTEGER,
  cost_usd      REAL,
  latency_ms    INTEGER,
  created_at    INTEGER NOT NULL,
  -- The idempotency guarantee. Same target, same prompt, same input -> one row.
  -- Edit the prompt and prompt_hash changes, so the work reruns and the old result
  -- is kept alongside for comparison rather than overwritten.
  UNIQUE (target_kind, target_id, template, prompt_hash, input_hash)
);
CREATE INDEX IF NOT EXISTS a.ix_ar_target ON analysis_result(target_kind, target_id, created_at DESC);
CREATE INDEX IF NOT EXISTS a.ix_ar_tpl    ON analysis_result(template, created_at DESC);

CREATE TABLE IF NOT EXISTS a.analysis_run (
  id            INTEGER PRIMARY KEY,
  template      TEXT NOT NULL,
  selector      TEXT,                      -- how targets were chosen, for reproducibility
  status        TEXT NOT NULL DEFAULT 'running',
  n_targets     INTEGER,
  n_done        INTEGER DEFAULT 0,
  n_skipped     INTEGER DEFAULT 0,
  n_failed      INTEGER DEFAULT 0,
  est_cost_usd  REAL,
  cost_usd      REAL,
  started_at    INTEGER NOT NULL,
  finished_at   INTEGER
);
"""

DEFAULT_DB = "~/.corpus/analysis.db"


# Added after the first analysis.db files existed, so they arrive by ALTER rather than
# in the CREATE: CREATE TABLE IF NOT EXISTS never adds a column to an existing table.
#   lang     the output language asked for (--lang); NULL = the default, English
#   status   NULL for a normal result; 'personal' when the post was not assessed
#            because it is about grief/illness (sensitivity.py), so the brief can
#            say so instead of showing a hole
#   privacy  'private' | 'public': which provider chain the payload was allowed
_ADDED_COLUMNS = (("lang", "TEXT"), ("status", "TEXT"), ("privacy", "TEXT"))


def attach(conn, path: str | Path = DEFAULT_DB):
    """Attach analysis.db as `a` and ensure its schema."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    already = any(r[1] == "a" for r in conn.execute("PRAGMA database_list"))
    if not already:
        conn.execute("ATTACH DATABASE ? AS a", (str(p),))
    conn.executescript(ANALYSIS_SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA a.table_info(analysis_result)")}
    for col, typ in _ADDED_COLUMNS:
        if col not in have:
            conn.execute(f"ALTER TABLE a.analysis_result ADD COLUMN {col} {typ}")
    conn.commit()
    return conn


def detach(conn):
    if any(r[1] == "a" for r in conn.execute("PRAGMA database_list")):
        conn.execute("DETACH DATABASE a")


@dataclass
class AnalyzeStats:
    template: str = ""
    considered: int = 0
    done: int = 0
    skipped: int = 0
    failed: int = 0
    personal: int = 0              # not assessed: grief/illness post
    private_skipped: int = 0       # private payload, no private provider reachable
    est_cost_usd: float = 0.0
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)
    # One line per item: where it would go (dry run) or went. Dry runs print it, so
    # "which of these leaves the machine" is answered before anything is spent.
    plan: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["errors"] = self.errors[:10]
        return d


# ── target selection + rendering ────────────────────────────────────────────

def targets(conn, *, account_id=None, relationship=None, platform=None,
            kind=None, limit=None, since=None, needs_transcript=False):
    where, args = ["i.deleted_at IS NULL"], []
    if account_id:
        where.append(store.account_items()); args += [account_id, account_id]
    if relationship:
        where.append("acc.relationship = ?"); args.append(relationship)
    if platform:
        where.append("i.platform = ?"); args.append(platform)
    if kind:
        where.append("i.kind = ?"); args.append(kind)
    if since:
        where.append("i.published_at >= ?"); args.append(int(since))
    if needs_transcript:
        where.append("i.transcript IS NOT NULL AND i.transcript <> ''")

    # A collab post is stored once under whichever co-author synced first. If any
    # co-author is own/client, the post is that client's content too.
    sql = f"""SELECT i.*, acc.handle, acc.display_name, acc.relationship,
                     EXISTS (SELECT 1 FROM item_account ia
                               JOIN account a2 ON a2.id = ia.account_id
                              WHERE ia.item_id = i.id
                                AND a2.relationship IN ('own','client'))
                       AS shared_private
                FROM item i JOIN account acc ON acc.id = i.account_id
               WHERE {' AND '.join(where)}
               ORDER BY i.published_at DESC"""
    if limit:
        sql += " LIMIT ?"; args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]


def build_input(row: dict, *, standing_context: str = "") -> dict:
    """Project an item row into the {{vars}} a template can use."""
    posted = time.strftime("%Y-%m-%d", time.gmtime(row["published_at"]))
    metrics = {k: row.get(f"m_{k}") for k in ("reactions", "comments", "shares", "views")}
    ctx = T.context_block(
        platform=row["platform"], kind=row["kind"],
        author=row.get("display_name") or row.get("handle"),
        posted=posted, metrics=metrics,
    )
    return {
        "_context": ctx,
        "author": row.get("display_name") or row.get("handle") or "",
        "handle": row.get("handle") or "",
        "platform": row["platform"],
        "format": row["kind"],
        "kind": row["kind"],
        "collection": row.get("relationship") or "",
        "caption": row.get("body") or "",
        "body": row.get("body") or "",
        "title": row.get("title") or "",
        "transcript": lens_transcript(row),
        # What is burned into the video. On a music-bed reel this is the argument,
        # and without it the lens correctly reports there is nothing to analyse.
        "screen_text": row.get("screen_text") or "",
        "url": row.get("url") or "",
        "posted": posted,
        "pillars": standing_context,
    }


def lens_transcript(row: dict) -> str:
    """The transcript as a lens should see it: labelled as machine output, withheld
    when it is junk, and marked when it is music rather than the speaker.

    Whisper's junk reads as confident text ("Music Music Music", a chorus eight
    times), and a lens analysed it as what the owner said. Saying "withheld" in the
    prompt matters: an empty slot made the model guess what the video was about.
    """
    t = (row.get("transcript") or "").strip()
    if not t:
        return ""
    # The pipeline already flagged it as probable background music.
    if row.get("lang") == "music":
        return f"[probable background music or lyrics, not the speaker: {t}]"
    verdict, why = textq.assess(t)
    if verdict == "music":
        return (f"[music or lyrics, not the speaker talking ({why}); do not treat "
                f"it as the author's words: {t}]")
    if verdict == "garbled":
        return (f"[machine transcript withheld as unreliable ({why}). Nothing "
                f"usable was said; do not guess at the spoken content.]")
    return f"[machine transcript, may contain errors] {t}"


LANGS = {"hr": "Croatian (hrvatski)", "en": "English"}


def system_prompt(lang: str | None = None, *, json_mode: bool = False) -> str:
    """Rules every lens carries, whatever its template says.

    Kept out of the template files so a new lens cannot ship without them.
    """
    parts = [
        "You analyse social media content for a content strategist.",
        S.SENSITIVITY_RULE,
        "Transcripts are machine transcriptions and may contain errors; do not "
        "quote an obviously garbled phrase as if it were said. A transcript marked "
        "withheld or as music is not the speaker's words.",
    ]
    if lang and lang != "en":
        name = LANGS.get(lang, lang)
        parts.append(
            f"Write your entire answer in {name}. Keep quoted post text in its "
            f"original language"
            + (", and keep JSON keys exactly as specified in English." if json_mode
               else "."))
    return "\n\n".join(parts)


def _input_hash(prompt: str, system: str = "") -> str:
    """Identity of everything sent. The system prompt is included because it carries
    the output language and the sensitivity rule: the same post in Croatian is a
    different result, not a cache hit."""
    blob = f"{system}\x1e{prompt}" if system else prompt
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def estimate_cost(prompt: str, max_tokens: int, provider_name: str) -> float:
    tin, tout = providers.price_per_m(provider_name)
    return (llm.estimate_tokens(prompt) / 1e6) * tin + (max_tokens / 1e6) * tout


# ── the pass ────────────────────────────────────────────────────────────────

def existing(conn, *, target_kind, target_id, template, prompt_hash, input_hash):
    return conn.execute(
        """SELECT id FROM a.analysis_result
            WHERE target_kind=? AND target_id=? AND template=?
              AND prompt_hash=? AND input_hash=?""",
        (target_kind, target_id, template, prompt_hash, input_hash),
    ).fetchone()


@dataclass
class Job:
    """One payload, fully decided before anything is sent: what the prompt is, how
    private it is, which providers may see it, and whether it goes at all."""
    target_kind: str
    target_id: int
    label: str
    prompt: str
    system: str
    privacy: str
    chain: list[dict]
    json_mode: bool = False
    lang: str | None = None
    skip: str | None = None        # 'personal' | 'private' | None
    why: str = ""
    row: dict = field(default_factory=dict)

    @property
    def ihash(self) -> str:
        return _input_hash(self.prompt, self.system)

    @property
    def messages(self) -> list[dict]:
        return [{"role": "system", "content": self.system},
                {"role": "user", "content": self.prompt}]

    @property
    def destination(self) -> str:
        if self.skip == "personal":
            return f"SKIP personal post, not assessed ({self.why})"
        if self.skip == "private":
            return "SKIP private, no private provider reachable"
        return self.chain[0]["name"] if self.chain else "SKIP no provider"


def item_privacy(row: dict, *, standing_context: str = "",
                 context_public: bool = False) -> str:
    """The item's class, and the prompt's: --context text is rendered into every
    prompt and typically holds client or personal notes, so a non-empty one makes
    the payload private unless the caller says it is not (--context-public)."""
    return P.classify(relationship=row.get("relationship"),
                      shared_with_private=bool(row.get("shared_private")),
                      private_context=bool(standing_context.strip())
                      and not context_public)


def prepare(tpl: T.Template, row: dict, *, standing_context: str = "",
            json_mode: bool = False, lang: str | None = None,
            assess_personal: bool = False, context_public: bool = False) -> Job:
    fields = build_input(row, standing_context=standing_context)
    prompt = T.render(tpl, fields, standing_context=standing_context)
    if json_mode:
        prompt = T.ensure_json_word(prompt)
    privacy = item_privacy(row, standing_context=standing_context,
                           context_public=context_public)
    job = Job("item", row["id"], f"item {row['id']} @{row.get('handle') or '?'}",
              prompt, system_prompt(lang, json_mode=json_mode), privacy,
              P.chain_for(privacy), json_mode=json_mode, lang=lang, row=row)
    hit = None if assess_personal else S.personal(S.item_text(row))
    if hit:
        job.skip, job.why = "personal", f"matched '{hit}'"
    elif not job.chain:
        job.skip = "private" if privacy == P.PRIVATE else "none"
        job.why = P.skip_reason(privacy) if privacy == P.PRIVATE else \
            "no LLM provider configured"
    return job


def _call(tpl: T.Template, job: Job):
    """The network half, safe to run concurrently. Touches no database.

    The second-to-last element says whether the failure ends the run rather than the
    item: a spent daily budget or an empty provider chain will not fix itself, so
    there is no point letting the remaining items each discover that for themselves.
    The chain is the job's own; llm.chat never widens it.
    """
    started = time.time()
    try:
        out = llm.chat(job.messages, json_mode=job.json_mode,
                       temperature=tpl.temperature, max_tokens=tpl.max_tokens,
                       chain=job.chain)
    except (llm.DailyLimitError, llm.NoProviderError) as e:
        return job, None, f"failed:{e}", 0, True, None
    except llm.LLMError as e:
        return job, None, f"failed:{e}", 0, False, None
    used = llm.last_used() or (job.chain[0] if job.chain else None)
    return job, out, None, int((time.time() - started) * 1000), False, used


def _record(conn, tpl, job: Job, out, latency, used=None, *, status=None,
            output_json=None) -> str:
    used = used or llm.last_used() or llm.active()
    text = json.dumps(out, ensure_ascii=False) if isinstance(out, (dict, list)) else str(out)
    if status is None and S.is_sentinel(out):
        status, text, out = "personal", S.SENTINEL, S.SENTINEL
    tin, tout = llm.estimate_tokens(job.system + job.prompt), llm.estimate_tokens(text)
    pin, pout = providers.price_per_m(used["name"])
    is_json = isinstance(out, (dict, list)) or output_json is not None

    conn.execute(
        """INSERT OR IGNORE INTO a.analysis_result
             (target_kind, target_id, template, prompt_hash, input_hash,
              output_text, output_json, model, provider, tokens_in, tokens_out,
              cost_usd, latency_ms, created_at, lang, status, privacy)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (job.target_kind, job.target_id, tpl.slug, tpl.prompt_hash, job.ihash,
         text if not isinstance(out, (dict, list)) else None,
         output_json if output_json is not None else (text if is_json else None),
         used.get("model"), used["name"], tin, tout,
         (tin / 1e6) * pin + (tout / 1e6) * pout,
         latency, store.now(), job.lang, status, job.privacy),
    )
    conn.commit()
    return "done"


def record_personal(conn, tpl, job: Job) -> str:
    """Store the pre-filter's verdict, so the brief shows "personal post, not
    assessed" and a re-run does not re-decide it. No provider saw the text."""
    _record(conn, tpl, job, S.SENTINEL, 0, {"name": "filter", "model": None},
            status="personal")
    return "personal"


def analyze_one(conn, tpl: T.Template, row: dict, *, standing_context: str = "",
                json_mode: bool = False, force: bool = False,
                lang: str | None = None) -> str:
    job = prepare(tpl, row, standing_context=standing_context,
                  json_mode=json_mode, lang=lang)
    if not force and existing(conn, target_kind="item", target_id=row["id"],
                              template=tpl.slug, prompt_hash=tpl.prompt_hash,
                              input_hash=job.ihash):
        return "skipped"
    if job.skip == "personal":
        return record_personal(conn, tpl, job)
    if job.skip:
        return f"skipped:{job.why}"
    job, out, err, latency, _fatal, used = _call(tpl, job)
    if err:
        return err
    return _record(conn, tpl, job, out, latency, used)


DEFAULT_WORKERS = 16


def _width(jobs: list[Job], workers: int, per_call: int) -> tuple[int, str]:
    """How many calls to keep in flight: the tightest of the providers in use.

    A provider's own max_workers wins (a single GPU), else its tokens-per-minute
    divided by a call's size (Groq's 8000 TPM admits about four).
    """
    width, who = workers, ""
    for p in {j.chain[0]["name"]: j.chain[0] for j in jobs if j.chain}.values():
        cap = p.get("max_workers")
        if not cap and p.get("tpm"):
            cap = max(1, p["tpm"] // max(1, per_call))
        if cap and cap < width:
            width, who = cap, p["name"]
            reason = (f"{who} runs at most {cap} at once" if p.get("max_workers") else
                      f"{who} allows {p['tpm']} tokens per minute and each call "
                      f"needs ~{per_call}, so the rest would only queue")
    return max(1, width), (reason if who else "")


def run(conn, template: str, *, db_path=DEFAULT_DB, standing_context: str = "",
        json_mode: bool = False, force: bool = False, dry_run: bool = True,
        template_dir=T.TEMPLATE_DIR, workers: int = DEFAULT_WORKERS,
        lang: str | None = None, assess_personal: bool = False,
        context_public: bool = False, **selector) -> AnalyzeStats:
    """Run one lens over a selection of items.

    dry_run defaults to TRUE. Spending money should be the thing you opt into, not
    the thing you forget to opt out of.

    Every item is routed on its own (privacy.py): an own/client post only ever
    reaches CORPUS_PRIVATE_PROVIDERS and is skipped when none is reachable; a post
    about grief or illness is not sent at all (sensitivity.py).

    `workers` is a ceiling, not a promise: it is cut down to whatever the providers
    in use can keep in flight. 16 is right for a provider that is latency-bound
    rather than rate-bound (measured 54k TPM used of DeepSeek's 1M at workers=6,
    with p50 latency of 8-14s), about 4 on Groq's 8000 TPM free tier, and 2 on the
    local GPU.
    """
    attach(conn, db_path)
    # An interrupted run leaves status="running" forever and nothing else sweeps it.
    conn.execute("UPDATE a.analysis_run SET status='interrupted' "
                 "WHERE status='running' AND started_at < ?", (store.now() - 3600,))
    conn.commit()
    tpl = T.load(template, template_dir)
    if tpl.slug == "account-summary":
        # One call per ACCOUNT (analyze/account.py). Run per item it would ask the
        # account question of every post and store 400 answers to it.
        st = AnalyzeStats(template=tpl.slug)
        st.errors.append("account-summary runs per account: "
                         "corpus analyze account-summary --account <handle>")
        return st
    rows = targets(conn, **selector)
    stats = AnalyzeStats(template=tpl.slug, considered=len(rows))

    if not providers.available() and not providers.private_chain():
        stats.errors.append(
            "no LLM provider configured; start Ollama (OLLAMA_URL) or add "
            "MISTRAL_API_KEY / GROQ_API_KEY to ~/.reach/keys.env"
        )
        return stats

    # Estimate on the real rendered prompts, not a guess: template length varies
    # ten-fold across the lenses and transcripts dominate the input. The rendered
    # prompt is kept rather than re-rendered later, because two render sites that
    # must agree exactly is a bug waiting for someone to edit one of them.
    prepared: list[Job] = []
    personal: list[Job] = []
    est_tokens = 0
    for row in rows:
        job = prepare(tpl, row, standing_context=standing_context,
                      json_mode=json_mode, lang=lang,
                      assess_personal=assess_personal,
                      context_public=context_public)
        if not force and existing(conn, target_kind="item", target_id=row["id"],
                                  template=tpl.slug, prompt_hash=tpl.prompt_hash,
                                  input_hash=job.ihash):
            stats.skipped += 1
            continue
        stats.plan.append(f"{job.label} [{job.privacy}] -> {job.destination}")
        if job.skip == "personal":
            stats.personal += 1
            personal.append(job)
            continue
        if job.skip:
            stats.private_skipped += 1
            continue
        stats.est_cost_usd += estimate_cost(job.system + job.prompt, tpl.max_tokens,
                                            job.chain[0]["name"])
        est_tokens += llm.estimate_tokens(job.system + job.prompt) + tpl.max_tokens
        prepared.append(job)

    if stats.private_skipped:
        stats.errors.append(
            f"{stats.private_skipped} private item(s) skipped: "
            + P.skip_reason(P.PRIVATE))
    if stats.personal:
        stats.errors.append(
            f"{stats.personal} personal post(s) (grief/illness) not assessed; "
            f"--assess-personal overrides the keyword filter")

    if dry_run:
        by: dict[str, int] = {}
        for j in prepared:
            by[j.chain[0]["name"]] = by.get(j.chain[0]["name"], 0) + 1
        where = ", ".join(f"{n} on {k}" for k, n in by.items()) or "nothing to send"
        stats.errors.append(
            f"dry-run: would analyse {len(prepared)} item(s) with {tpl.slug} "
            f"({where}), est ${stats.est_cost_usd:.4f}. "
            f"{stats.skipped} already done."
        )
        return stats

    for job in personal:
        record_personal(conn, tpl, job)

    # Size the pool to the provider's minute, not to what was asked for. Groq's
    # 8000 TPM against ~1700 tokens a call admits four; the other two threads would
    # queue on the token bucket's lock and, because that wait is inside the timed
    # region, show up as "slow model calls" rather than as the queue they are.
    # Measured: three Groq runs at workers=6 ran at exactly 1.00x effective
    # concurrency. DeepSeek's 1M TPM leaves the requested value untouched.
    per_call = est_tokens // max(1, len(prepared))
    effective, why = _width(prepared, workers, per_call)
    if effective < workers:
        stats.errors.append(f"workers {workers} -> {effective}: {why}")

    run_started = store.now()
    run_id = conn.execute(
        """INSERT INTO a.analysis_run (template, selector, n_targets, est_cost_usd,
                                       started_at, status)
           VALUES (?,?,?,?,?, 'running')""",
        (tpl.slug, json.dumps(selector, default=str), len(prepared),
         stats.est_cost_usd, run_started),
    ).lastrowid
    conn.commit()

    # A rate-limit collapse used to churn the whole worklist recording failures and
    # then report status "done": three real runs went 39/39 failed in under a second
    # and looked successful. Bail after a run of provider-level failures so the run
    # is visibly incomplete and re-runnable.
    #
    # The threshold scales with concurrency deliberately. With N calls in flight a
    # single 429 burst produces up to N failures back to back before any success can
    # reset the counter, so a fixed 3 would abort a perfectly healthy wide run.
    # LLM calls run concurrently; every database write stays on this thread, because
    # SQLite allows exactly one writer. DeepSeek's 1M TPM makes latency rather than
    # rate the constraint, so a serial loop wasted most of the wall clock waiting.
    abort_after = max(3, effective + 1)
    consecutive = 0

    def _completed():
        """Yield results as they land, not after the whole batch.

        Collecting everything first means an interrupt throws away every call
        already paid for, which defeats the resumability the cache exists to give.
        """
        if effective > 1 and len(prepared) > 1:
            pool = ThreadPoolExecutor(max_workers=effective)
            try:
                futs = [pool.submit(_call, tpl, j) for j in prepared]
                for f in as_completed(futs):
                    yield f.result()
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        else:
            for j in prepared:
                yield _call(tpl, j)

    # Closed explicitly rather than left to the garbage collector: breaking out of
    # the loop below has to cancel the queued calls, not wait for a refcount to
    # happen to drop.
    stream = _completed()
    try:
        for job, out, err, latency, fatal, used in stream:
            outcome = err or _record(conn, tpl, job, out, latency, used)
            if outcome == "done":
                stats.done += 1
                consecutive = 0
            elif outcome == "skipped":
                stats.skipped += 1
                consecutive = 0
            else:
                stats.failed += 1
                stats.errors.append(f"{job.label}: {outcome}")
                consecutive += 1
                if fatal or consecutive >= abort_after:
                    remaining = len(prepared) - (stats.done + stats.skipped
                                                 + stats.failed)
                    why = ("the provider chain is exhausted" if fatal else
                           f"{consecutive} consecutive provider failures")
                    stats.errors.append(
                        f"aborted: {why}; {remaining} item(s) not attempted. "
                        f"Completed work is cached, so re-running resumes rather "
                        f"than repeating."
                    )
                    break
    except BaseException:
        # Ctrl-C mid-run: every completed call is already committed by _record, so
        # only the bookkeeping needs closing. Leaving it 'running' makes an
        # abandoned run indistinguishable from one still in progress for an hour.
        conn.execute(
            """UPDATE a.analysis_run SET status='interrupted', n_done=?, n_skipped=?,
                                         n_failed=?, finished_at=? WHERE id=?""",
            (stats.done, stats.skipped, stats.failed, store.now(), run_id))
        conn.commit()
        raise
    finally:
        stream.close()

    # This run's spend, not the template's lifetime spend. Summing by template made
    # every subsequent run report a total that included every previous one, so
    # "actual=$0.0476" overstated a run that actually cost a fraction of it.
    stats.cost_usd = conn.execute(
        """SELECT COALESCE(SUM(cost_usd),0) c FROM a.analysis_result
            WHERE template=? AND created_at >= ?""",
        (tpl.slug, run_started),
    ).fetchone()["c"]

    conn.execute(
        """UPDATE a.analysis_run SET status=?, n_done=?, n_skipped=?, n_failed=?,
                                     cost_usd=?, finished_at=? WHERE id=?""",
        ("done" if not stats.failed else "incomplete",
         stats.done, stats.skipped, stats.failed, stats.cost_usd, store.now(), run_id),
    )
    conn.commit()
    return stats


def results_for(conn, item_id: int, *, db_path=DEFAULT_DB) -> list[dict]:
    attach(conn, db_path)
    return [dict(r) for r in conn.execute(
        """SELECT template, output_text, output_json, model, provider, cost_usd,
                  created_at, lang, status, privacy
             FROM a.analysis_result
            WHERE target_kind='item' AND target_id=?
            ORDER BY created_at DESC""",
        (item_id,),
    )]


def prune(conn, *, db_path=DEFAULT_DB) -> int:
    """Delete results whose item no longer exists.

    Needed because SQLite has no cross-database foreign keys, so ON DELETE CASCADE
    cannot reach across the attach boundary.
    """
    attach(conn, db_path)
    cur = conn.execute(
        """DELETE FROM a.analysis_result
            WHERE target_kind='item'
              AND target_id NOT IN (SELECT id FROM main.item)"""
    )
    conn.commit()
    return cur.rowcount
