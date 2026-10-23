"""Run adapters and persist what they yield.

The guard is the important part: one adapter blowing up must log to scrape_run and
let the others finish. When you fan out over six flaky vendors, an unhandled
exception in the third one silently costing you the fourth through sixth is the
failure mode that actually happens.

Fetching is concurrent, writing is not, and the split is structural rather than a
convention: `fetch_only` takes no `conn` at all, so a worker thread has nothing to
write with. store.connect() also omits check_same_thread=False, so if anyone ever
passes a connection across the boundary anyway they get a loud ProgrammingError
instead of a corrupted database.

Why concurrency here at all: both Apify adapters make one blocking POST to
run-sync-get-dataset-items and then wait, so four accounts cost the sum of four
vendor waits (measured 14-83s each, 172s serial for the four accounts in the
corpus). Measured 1.91x on two accounts with per-run latency unchanged, and the
Starter plan reports maxConcurrentActorRuns=32 against the 1 this pipeline used.
The parallelism is already paid for in the $29 subscription.
"""
from __future__ import annotations

import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import freshness, store
from .adapters.base import Adapter, FetchResult, TargetGone

# Metric keys that have real columns; anything else rides along in metrics_raw.
_METRIC_COLS = ("reactions", "comments", "shares", "saves", "views")

# Well under Apify's 32 concurrent actor slots on purpose: a run-sync call holds an
# HTTP connection open for up to 310s, and the plan guard in cli.py sizes the whole
# batch's items up front anyway, so the ceiling that matters is spend, not slots.
DEFAULT_WORKERS = 6


class PreflightError(RuntimeError):
    pass


def preflight(adapter: Adapter, keys: dict[str, str]) -> None:
    """Fail before spending money, not after."""
    missing = [k for k in adapter.needs if not keys.get(k)]
    if missing:
        raise PreflightError(
            f"{adapter.name}: missing {', '.join(missing)} "
            f"(expected in ~/.reach/keys.env)"
        )


def persist(conn, adapter: Adapter, account_id: int, result: FetchResult, *,
            mode: str = "delta", since: int | None = None) -> dict:
    """Write one FetchResult into the store. Idempotent."""
    seen = new = changed = 0

    if result.profile is not None:
        p = result.profile
        store.update_account_profile(
            conn, account_id, display_name=p.display_name, bio=p.bio, url=p.url,
            platform_uid=p.platform_uid, follower_count=p.follower_count, raw=p.raw)
        store.record_account_metrics(
            conn, account_id, followers=p.follower_count,
            following=p.following_count, posts_total=p.posts_total,
        )

    for it in result.items:
        seen += 1
        item_id, is_new, was_changed = store.upsert_item(
            conn,
            account_id=account_id,
            platform=adapter.platform,
            platform_item_id=it.platform_item_id,
            kind=it.kind,
            published_at=it.published_at,
            raw=it.raw,
            title=it.title,
            body=it.body,
            transcript=it.transcript,
            url=it.url,
            lang=it.lang,
            duration_s=it.duration_s,
            is_repost=it.is_repost,
            author_name=it.author_name,
            author_handle=it.author_handle,
            source=adapter.name,
        )
        new += is_new
        changed += was_changed
        # Already stored under a co-author (a collab post): this feed shows it too.
        if not is_new:
            store.link_item(conn, item_id, account_id)

        if it.metrics:
            extra = {k: v for k, v in it.metrics.items() if k not in _METRIC_COLS}
            store.record_metrics(
                conn, item_id,
                **{k: it.metrics.get(k) for k in _METRIC_COLS},
                raw=extra or None,
                carry=tuple(getattr(adapter, "unmeasured", ())),
            )

        # Media rows are replaced wholesale per item: vendors reorder carousel
        # slides between fetches, so matching on (role, seq) would drift.
        # Enrichment artefacts (keyframes, stored files) are kept -- only the
        # vendor-supplied references are refreshed.
        if it.media:
            conn.execute(
                "DELETE FROM media WHERE item_id=? AND state='referenced' "
                "AND role IN ('original','poster','slide','thumbnail')",
                (item_id,),
            )
            for m in it.media:
                store.add_media(
                    conn, item_id, role=m.role, seq=m.seq, remote_url=m.remote_url,
                    mime=m.mime, width=m.width, height=m.height,
                    t_offset_s=m.t_offset_s,
                )

    # Watermarks: newest drives delta, oldest drives backfill resume.
    if result.items:
        stamps = [i.published_at for i in result.items]
        conn.execute(
            """UPDATE account
                  SET last_scraped_at = ?,
                      oldest_item_at  = MIN(COALESCE(oldest_item_at, ?), ?)
                WHERE id = ?""",
            (store.now(), min(stamps), min(stamps), account_id),
        )
    elif result.empty_confirmed:
        # Checked and empty is still checked: without this the account stayed
        # "never synced" and came back as a backfill every night.
        conn.execute("UPDATE account SET last_scraped_at=? WHERE id=?",
                     (store.now(), account_id))
    _move_floor(conn, account_id, result, mode=mode, since=since)
    conn.commit()
    return {"seen": seen, "new": new, "changed": changed}


def _move_floor(conn, account_id, result: FetchResult, *, mode, since) -> None:
    """Keep account.delta_floor honest.

    A delta returns the newest `limit` posts after `since`. If it came back full,
    there may be posts between `since` and the oldest one returned that nobody has
    fetched; the floor pins the next delta's lower bound there until a run gets all
    the way down to it. A full run (not truncated) that started at or below the
    floor, or a backfill that reached the end, closes it.
    """
    floor = conn.execute("SELECT delta_floor FROM account WHERE id=?",
                         (account_id,)).fetchone()["delta_floor"]
    if not result.truncated:
        if mode == "backfill" or since is None or floor is None or since <= floor:
            conn.execute("UPDATE account SET delta_floor=NULL WHERE id=?", (account_id,))
        return
    if mode == "backfill" or since is None or not result.items:
        return
    oldest = min(i.published_at for i in result.items)
    if oldest > since and (floor is None or since < floor):
        conn.execute("UPDATE account SET delta_floor=? WHERE id=?", (since, account_id))


# ── the network half: no conn, safe on a worker thread ──────────────────────


@dataclass
class Attempt:
    """One route tried once. Produced off-thread, written on the owner thread."""
    adapter: Adapter
    result: FetchResult | None = None
    error: str | None = None        # type + message + traceback, for scrape_run
    message: str = ""               # just the message, for the terminal
    started_at: int = 0
    finished_at: int = 0
    gone: bool = False              # the target does not exist / is private

    @property
    def ok(self) -> bool:
        return self.result is not None


def fetch_only(adapter: Adapter, target: str, *, mode: str = "delta",
               keys: dict[str, str] | None = None, since: int | None = None,
               **fetch_kwargs) -> Attempt:
    """Preflight + fetch, and nothing else. Never raises.

    No `conn` parameter is the point: this is the half that runs concurrently, and
    it cannot touch the database even by accident. Every failure comes back as a
    filled-in Attempt so the caller writes one row per attempt on its own thread.
    """
    keys = keys or {}
    started = store.now()
    try:
        preflight(adapter, keys)
        result = adapter.fetch(target, mode=mode, keys=keys, since=since,
                               **fetch_kwargs)
        return Attempt(adapter, result=result, started_at=started,
                       finished_at=store.now())
    except Exception as e:  # vendor down, bad input, network, auth
        return Attempt(
            adapter,
            error=f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
            # One line: an Apify 403 body printed as three lines of raw JSON.
            message=" ".join(str(e).split()), started_at=started,
            finished_at=store.now(), gone=isinstance(e, TargetGone))


@dataclass
class Job:
    """One account's worth of work: optionally probe, then fetch, with fallback."""
    account_id: int
    target: str
    routes: list[Adapter]           # cheapest first; tried in order until one works
    handle: str = ""
    mode: str = "delta"
    since: int | None = None
    fetch_kwargs: dict = field(default_factory=dict)
    # A limit-1 detector fetch before the real one. newest_id is read on the owner
    # thread before dispatch so the worker needs no database access to compare.
    probe: bool = False
    newest_id: str | None = None

    # filled in by the worker
    probe_attempt: Attempt | None = None
    has_work: bool = True
    attempts: list[Attempt] = field(default_factory=list)
    # bookkeeping owned by the writer thread
    run_id: int | None = None
    recorded: bool = False


def execute(job: Job, keys: dict[str, str] | None = None) -> Job:
    """The whole network side of one job. Never raises, never writes."""
    keys = keys or {}
    if job.probe and job.newest_id:
        a = fetch_only(job.routes[0], job.target, mode="delta", keys=keys,
                       limit=freshness.probe_limit(job.routes[0].platform))
        job.probe_attempt = a
        job.has_work = freshness.probe_verdict(a.result, job.newest_id)
        if not job.has_work:
            return job

    for adapter in job.routes:
        a = fetch_only(adapter, job.target, mode=job.mode, keys=keys,
                       since=job.since, **job.fetch_kwargs)
        job.attempts.append(a)
        if a.ok or a.gone:
            break
    return job


# ── the write half: owner thread only ───────────────────────────────────────


def record(conn, job: Job) -> dict:
    """Persist one executed job. Call this only on the thread that owns `conn`.

    Every attempt gets its own scrape_run row, including the routes that failed on
    the way to the one that worked -- a fallback that leaves no trace is how you
    end up unable to explain a bill.
    """
    job.recorded = True
    first = job.routes[0].name if job.routes else "?"

    if job.probe and job.probe_attempt is not None:
        # The probe already paid for one item, so bank the metric movement on the
        # post it returned even when the answer is "nothing new".
        freshness.record_probe_metrics(conn, job.probe_attempt.adapter,
                                       job.probe_attempt.result)
    probe_cost = (job.probe_attempt.result.cost_usd
                  if job.probe_attempt and job.probe_attempt.ok else None) or 0.0

    if not job.has_work:
        conn.execute("UPDATE account SET last_scraped_at=? WHERE id=?",
                     (store.now(), job.account_id))
        conn.commit()
        _finish(conn, job, status="ok", cost_usd=probe_cost or None)
        return {"adapter": first, "status": "ok", "probed": True, "seen": 0,
                "new": 0, "changed": 0, "cost_usd": probe_cost}

    if not job.attempts:
        _finish(conn, job, status="error", error="no route available")
        return {"adapter": first, "status": "error", "error": "no route",
                "seen": 0, "new": 0, "changed": 0}

    # Everything before the last attempt failed and fell through to the next route.
    fallbacks = []
    for a in job.attempts[:-1]:
        rid = store.start_run(conn, adapter=a.adapter.name, mode=job.mode,
                              account_id=job.account_id, started_at=a.started_at)
        store.finish_run(conn, rid, status="error", error=a.error)
        fallbacks.append((a.adapter.name, a.message))

    last = job.attempts[-1]
    if not last.ok:
        _finish(conn, job, status="error", error=last.error, adapter=last.adapter.name)
        return {"adapter": last.adapter.name, "status": "error",
                "error": last.message, "seen": 0, "new": 0, "changed": 0,
                "fallbacks": fallbacks, "cost_usd": probe_cost}

    before = conn.execute("SELECT last_scraped_at FROM account WHERE id=?",
                          (job.account_id,)).fetchone()
    counts = persist(conn, last.adapter, job.account_id, last.result,
                     mode=job.mode, since=job.since)
    cost = (last.result.cost_usd or 0.0) + probe_cost
    # What parsed is stored either way (it was paid for); the run is still a
    # failure when the vendor's payload stopped parsing.
    drift = drop_error(last.result)
    if drift and before is not None:
        # ...and not a sync: the one Short that parsed out of twelve stamped the
        # account fresh, so the next `sync` skipped it for 72h with 11 posts missing
        # (field test, 2026-10-23). The watermark goes back to where it was.
        conn.execute("UPDATE account SET last_scraped_at=? WHERE id=?",
                     (before["last_scraped_at"], job.account_id))
        conn.commit()
    _finish(conn, job, status="error" if drift else "ok", cost_usd=cost,
            adapter=last.adapter.name, error=drift, raw=last.result.raw_count,
            **counts)
    out = {"adapter": last.adapter.name, "status": "error" if drift else "ok",
           **counts,
           # The probe's item is part of what this sync cost. Leaving it out is how
           # a detector that costs more than the fetch it avoids stays invisible.
           "cost_usd": cost if probe_cost else last.result.cost_usd,
           "fallbacks": fallbacks}
    if drift:
        out["error"] = drift
    if last.result.truncated:
        # A truncated backfill that reads as complete is worse than a failed one.
        out["truncated"] = True
        out["mode"] = job.mode
    return out


# A vendor that changes its payload shape does not fail: the adapter drops what it
# cannot parse and the run completes "ok" with fewer posts, all of them billed.
# Below five records one odd row is noise; above it, losing a fifth is a format
# change and has to fail loudly.
DROP_MIN_RECORDS = 5
DROP_MAX_SHARE = 0.20


def drop_error(result: FetchResult) -> str | None:
    """An error message when too many vendor records failed to parse, else None."""
    raw = result.raw_count
    if raw is None or raw < DROP_MIN_RECORDS:
        return None
    dropped = raw - len(result.items)
    if dropped <= 0 or dropped / raw <= DROP_MAX_SHARE:
        return None
    billed = ", but were billed" if result.cost_usd else ""
    return (f"vendor payload drift: {dropped} of {raw} records did not parse "
            f"({dropped / raw:.0%}) and were dropped{billed}. The vendor's "
            f"output shape probably changed; check the adapter against a fresh "
            f"payload before the next run.")


def _finish(conn, job: Job, *, status: str, adapter: str | None = None, **kw) -> None:
    """Close the job's own run row, opening one first if it was never dispatched."""
    if job.run_id is None:
        a = job.attempts[-1] if job.attempts else job.probe_attempt
        job.run_id = store.start_run(
            conn, adapter=adapter or (job.routes[0].name if job.routes else "?"),
            mode=job.mode, account_id=job.account_id,
            started_at=a.started_at if a else None)
    elif adapter:
        # The row was opened against the cheapest route; say which one actually ran.
        conn.execute("UPDATE scrape_run SET adapter=? WHERE id=?",
                     (adapter, job.run_id))
    store.finish_run(conn, job.run_id, status=status, **kw)


def run_jobs(conn, jobs, *, keys: dict[str, str] | None = None,
             workers: int = DEFAULT_WORKERS, on_result=None) -> list[dict]:
    """Fetch every job concurrently; write each result as it lands, in order of
    completion, on this thread.

    Results stream: an interrupt loses only the fetches still in flight, never work
    already paid for and returned. Anything dispatched but not yet recorded is
    closed as 'interrupted' so no run row is left claiming to be running forever.
    """
    jobs = list(jobs)
    results: list[dict] = []
    if not jobs:
        return results

    # The row exists from the moment the fetch is dispatched, not from when it
    # returns: a hard kill mid-fetch must still leave evidence that money was spent.
    for j in jobs:
        j.run_id = store.start_run(
            conn, adapter=j.routes[0].name if j.routes else "?",
            mode="probe" if j.probe else j.mode, account_id=j.account_id)

    if workers <= 1 or len(jobs) == 1:
        try:
            for j in jobs:
                res = record(conn, execute(j, keys))
                results.append(res)
                if on_result:
                    on_result(j, res)
        finally:
            _sweep(conn, jobs)
        return results

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futs = [pool.submit(execute, j, keys) for j in jobs]
        for f in as_completed(futs):
            j = f.result()          # execute() never raises
            res = record(conn, j)
            results.append(res)
            if on_result:
                on_result(j, res)
    finally:
        _sweep(conn, jobs)
        # Drop queued work rather than waiting on it: a run-sync call can hold for
        # 310s, and Ctrl-C that takes five minutes to land is not an interrupt.
        pool.shutdown(wait=False, cancel_futures=True)
    return results


def _sweep(conn, jobs) -> None:
    for j in jobs:
        if not j.recorded and j.run_id is not None:
            store.finish_run(conn, j.run_id, status="interrupted")
            j.recorded = True


def run(conn, adapter: Adapter, *, account_id: int, target: str,
        mode: str = "delta", keys: dict[str, str] | None = None,
        since: int | None = None, **fetch_kwargs) -> dict:
    """Fetch + persist one account through one adapter, guarded.

    Never raises for adapter-level failures: the error lands in scrape_run and the
    caller keeps going. Programming errors in persist() are not caught -- those
    should be loud.
    """
    job = Job(account_id=account_id, target=target, routes=[adapter], mode=mode,
              since=since, fetch_kwargs=fetch_kwargs)
    out = run_jobs(conn, [job], keys=keys, workers=1)[0]
    out.pop("fallbacks", None)
    return out
