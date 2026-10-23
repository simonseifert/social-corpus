"""corpus - a social content back-catalog you own.

Design rule for this file: nothing here spends money or writes media without the user
having asked for that specific thing. `sync` respects freshness, `analyze` dry-runs by
default, and any Apify run past a threshold checks the plan tier first.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import backup as backup_mod
from . import brief as brief_mod
from . import comments as comments_mod, discover, freshness, keys as keys_mod, store, sync as sync_mod
from . import ops
from . import outliers as outliers_mod
from .adapters import REGISTRY, UNAVAILABLE, for_platform, get as get_adapter
from .analyze import run as analyze_mod
from .enrich import run as enrich_mod

DEFAULT_DB = "~/.corpus/corpus.db"
DEFAULT_ANALYSIS_DB = "~/.corpus/analysis.db"
DEFAULT_MEDIA = os.environ.get("CORPUS_MEDIA", "~/.corpus/media")
# Briefs are things you send to someone, so they belong where you look for
# files to attach, not buried in a dotfolder.
DEFAULT_BRIEFS = os.environ.get("CORPUS_BRIEFS", "~/Downloads")


def _c(s, code):
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else str(s)


def dim(s): return _c(s, "2")
def bold(s): return _c(s, "1")
def green(s): return _c(s, "32")
def yellow(s): return _c(s, "33")
def red(s): return _c(s, "31")


def open_db(args):
    return store.connect(args.db)


class AmbiguousAccount(ValueError):
    pass


def _resolve_account(conn, handle: str):
    """An account by id, handle, URL or `platform:handle`.

    A bare handle can name two accounts once a business is tracked on two platforms
    (a bistro on Instagram and on Facebook). Picking the first silently synced,
    briefed or forgot the wrong one; now it refuses and lists the exact names.
    """
    h = str(handle).strip()
    if ":" in h and not h.startswith("http"):
        plat, _, bare = h.partition(":")
        row = conn.execute("SELECT * FROM account WHERE platform=? AND handle=? "
                           "COLLATE NOCASE", (plat.lower(), bare.lstrip("@"))).fetchone()
        if row is not None:
            return row
    rows = conn.execute(
        "SELECT * FROM account WHERE handle=? COLLATE NOCASE OR url=? OR id=?",
        (h.lstrip("@"), h, h if h.isdigit() else -1)).fetchall()
    if len(rows) > 1:
        raise AmbiguousAccount(
            f"'{h}' matches {len(rows)} accounts: "
            + ", ".join(f"{r['platform']}:{r['handle']}" for r in rows)
            + ". Name one of them exactly.")
    row = rows[0] if rows else None
    if row is None:
        t = discover.parse_target(h)
        if t:
            row = conn.execute(
                "SELECT * FROM account WHERE platform=? AND handle=?",
                (t.platform, t.handle)).fetchone()
    return row


# ── commands ────────────────────────────────────────────────────────────────

def cmd_add(args):
    conn = open_db(args)
    t = discover.parse_target(args.target)
    if not t:
        print(red(f"'{args.target}' is not a profile URL or @handle."))
        print("Try:  corpus find \"Their Name\"   to search for it first.")
        return 2

    before = conn.execute(
        "SELECT relationship, client_ref FROM account WHERE platform=? AND handle=? "
        "COLLATE NOCASE", (t.platform, t.handle)).fetchone()
    aid = store.upsert_account(
        conn, platform=t.platform, handle=t.handle, url=t.url,
        relationship=args.relationship, client_ref=args.client_ref)
    if before is not None and before["relationship"] != args.relationship:
        # Re-adding is how a relationship is changed, but it happened silently, and
        # an `add @BistroHarbor --relationship competitor` meant as a new account
        # turned a client into a competitor whose posts may go to public providers.
        private = {"own", "client"}
        note = ""
        if before["relationship"] in private and args.relationship not in private:
            note = "; its posts are now PUBLIC for analysis routing"
        print(yellow(f"changed {t}: relationship {before['relationship']} -> "
                     f"{args.relationship}{note}. Re-add with --relationship "
                     f"{before['relationship']} to undo."))

    adapters = for_platform(t.platform, args.relationship)
    print(f"{green('added')} {bold(str(t))}  relationship={args.relationship}"
          f"{'  client=' + args.client_ref if args.client_ref else ''}"
          f"{'  client cleared' if args.client_ref == '' else ''}")
    if adapters:
        a = adapters[0]
        cost = "free" if not a.cost_per_1k else f"${a.cost_per_1k:.2f}/1k"
        print(f"  route: {bold(a.name)} ({cost})")
        if a.notes:
            print(dim(f"  {a.notes}"))
        if len(adapters) > 1:
            print(dim(f"  alternatives: {', '.join(x.name for x in adapters[1:])}"))
    else:
        print(yellow(f"  no adapter serves {t.platform}/{args.relationship} yet"))
    twins = conn.execute("SELECT COUNT(*) FROM account WHERE handle=? COLLATE NOCASE",
                         (t.handle,)).fetchone()[0]
    print(dim(f"  next: corpus sync {t if twins > 1 else t.handle}"))
    return 0


def cmd_find(args):
    hints = {}
    if args.location:
        hints["location"] = args.location
    if args.company:
        hints["company"] = args.company
    if args.keyword:
        hints["keyword"] = args.keyword

    targets, cands = discover.resolve(args.query, hints=hints or None)
    for t in targets:
        print(f"{green('resolved')} {t}  {dim(t.url)}")
    if targets and not cands:
        print(dim("\nadd with:  corpus add <url> --relationship competitor"))
        return 0

    if not cands:
        print(yellow("no candidates found."))
        print(dim("Names are searched, not identified. Try pasting the profile URL, "
                  "or narrow with --location / --company."))
        return 1

    print(bold("\ncandidates") + dim("  (a name is a search, not an identification "
                                     "- confirm before adding)"))
    for c in cands:
        print(c)
    print(dim("\nadd the right one with:  corpus add <url> --relationship <rel>"))
    return 0


def cmd_sync(args):
    conn = open_db(args)
    k = keys_mod.load()

    ids = None
    if args.handle and args.handle != "all":
        row = _resolve_account(conn, args.handle)
        if row is None:
            print(red(f"no account matching '{args.handle}'. Add it first."))
            return 2
        ids = [row["id"]]

    plan = freshness.plan(conn, account_ids=ids, force=args.force,
                          default_limit=args.limit)
    if not plan:
        print(yellow("no accounts. Add one with: corpus add <url> --relationship ..."))
        return 1

    # A handle tracked on two platforms (bistroharbor on Instagram and Facebook)
    # printed as two identical "bistroharbor: no new posts" lines; name the platform.
    shared = {r[0] for r in conn.execute(
        "SELECT lower(handle) FROM account GROUP BY lower(handle) HAVING COUNT(*) > 1")}

    def label(d):
        return f"{d.platform}:{d.handle}" if d.handle.lower() in shared else d.handle

    print(bold("plan") + dim(f"  ({freshness.summarize(plan)})"))
    for d in plan:
        print(f"  {d}")

    actionable = [d for d in plan if d.action != "skip"]
    if args.dry_run:
        # The same keys a real run passes, so the head route is the one that would
        # actually run (without them an Instagram dry run quoted the free route at $0
        # for a sync that would bill the paid one), priced by the adapter: per-page
        # vendors bill a whole page for a 10-post delta.
        heads = [get_adapter_for(conn, d, have_file=bool(args.file), keys=k)
                 for d in actionable]
        for d, a in zip(actionable, heads):
            if a:
                print(dim(f"  {label(d)}: via {a.name}"))
        est = sum(a.estimate(d.limit or 0) for d, a in zip(actionable, heads) if a)
        print(f"\n{bold('dry run')}: {len(actionable)} account(s) would be fetched, "
              f"est ${est:.4f}. Nothing spent.")
        return 0
    if not actionable:
        print(green("\neverything fresh, nothing to do."))
        return 0

    # Guard the whole run once, before the first billable call. The trigger is a
    # REACHABLE Apify route, not the head of the list: routes_for falls through when
    # a route fails, so an own-account sync that looks free lands on the paid public
    # actor the moment its cookies expire, and that is exactly the run worth guarding.
    # When no route in the plan can bill Apify (its adapters failed to import), the
    # check can only produce a false refusal, so it is skipped.
    # Worst case, not the plan's face value: a probe bills 1 item, but a probe that
    # finds something goes on to fetch the full --limit. Counting it as 1 let forty
    # due accounts read as 40 items and slip under the threshold as 2,000.
    def worst(d):
        return (d.limit or 0) + (args.limit if d.action == "probe" else 0)
    planned_items = sum(worst(d) for d in actionable)
    apify_routes = {
        a.apify_actor: a
        for d in actionable
        for a in routes_for(conn, d, have_file=bool(args.file), keys=k)
        if _is_apify(a)
    }
    est_usd = sum(max(((a.cost_per_1k or 0) for a in apify_routes.values()), default=0)
                  / 1000 * worst(d) for d in actionable)
    apify_refused = None
    if apify_routes and k.get("APIFY_TOKEN"):
        try:
            keys_mod.assert_can_spend(
                k["APIFY_TOKEN"], planned_items, est_usd=est_usd,
                actors=tuple(x for x in apify_routes if x),
                prices={x: a.cost_per_1k / 1000 for x, a in apify_routes.items()
                        if x and a.cost_per_1k})
        except keys_mod.PlanGuardError as e:
            # Refuse the run only when Apify is the route that would actually run.
            # When it is merely a fall-through behind ScrapeCreators or HarvestAPI,
            # refusing everything stopped a $0.06 sync dead because an unpaid Apify
            # account could not have been used anyway (field test, 2026-10-23); the
            # Apify routes are dropped from this run instead, so nothing falls into
            # the refused spend.
            heads = [get_adapter_for(conn, d, have_file=bool(args.file), keys=k)
                     for d in actionable]
            if any(h is not None and _is_apify(h) for h in heads):
                print(red(f"\nrefusing to run: {e}"))
                return 3
            apify_refused = str(e)
            print(yellow(f"\nApify fall-through disabled for this run: {e}"))
    # Routes outside Apify have no maxTotalChargeUsd; each brings its own check
    # (a balance read, a record budget). Only the route expected to run is asked:
    # a fall-through is already bounded per fetch by the adapter's own page/record cap.
    by_head: dict[str, list] = {}
    for d in actionable:
        h = get_adapter_for(conn, d, have_file=bool(args.file), keys=k)
        if h is not None and h.spend_check is not None:
            by_head.setdefault(h.name, [h, 0, 0.0])
            by_head[h.name][1] += worst(d)
            by_head[h.name][2] += h.estimate(worst(d))
    for h, items, usd in by_head.values():
        if items <= keys_mod.GUARD_THRESHOLD:
            continue
        try:
            h.spend_check(k, items, usd)
        except keys_mod.PlanGuardError as e:
            print(red(f"\nrefusing to run: {e}"))
            return 3

    # One job per account, all fetched concurrently, every write landing back here
    # on this thread. The vendor call is a single blocking POST that waits 14-83s
    # per account, so serialising it was spending wall clock on a concurrency the
    # Apify subscription already includes (32 slots, of which this used one).
    jobs = []
    for d in actionable:
        row = conn.execute("SELECT * FROM account WHERE id=?", (d.account_id,)).fetchone()
        # Every usable route, cheapest first. A route can fail for reasons the plan
        # could not know (missing cookies, an expired session, a vendor outage), and
        # the worker falls through to the next one without needing the database.
        candidates = routes_for(conn, d, have_file=bool(args.file), keys=k)
        if apify_refused:
            candidates = [a for a in candidates if not _is_apify(a)]
        if not candidates:
            print(yellow(f"  {label(d)}: no adapter for "
                         f"{row['platform']}/{row['relationship']}, skipped"))
            continue
        is_backfill = d.action == "backfill"
        jobs.append(sync_mod.Job(
            account_id=d.account_id,
            target=args.file or row["url"] or row["handle"],
            routes=candidates,
            handle=label(d),
            mode="backfill" if is_backfill else "delta",
            # Push the watermark to the vendor on a delta. Without it every "delta"
            # is a full window re-fetch: at 10 clients posting a few times a week
            # that is roughly $10-25/mo of re-buying posts already in the corpus.
            # See freshness.delta_since for why it is not last_scraped_at.
            since=None if is_backfill else freshness.delta_since(conn, row),
            fetch_kwargs={"limit": args.limit},
            probe=d.action == "probe",
            # Read here, not in the worker: the comparison is the only thing the
            # probe needs from the database, and reading it now keeps the worker
            # free of any connection at all.
            newest_id=freshness.newest_stored(conn, d.account_id)[0],
        ))

    total = {"new": 0, "changed": 0, "cost": 0.0}
    failures = 0

    def report(job, res):
        nonlocal failures
        for name, msg in res.get("fallbacks", []):
            print(dim(f"  {job.handle}: {name} unavailable ({msg[:70]}), "
                      f"fell through"))
        total["cost"] += res.get("cost_usd") or 0
        if res["status"] == "error":
            print(red(f"  {job.handle}: {res.get('error', 'no route')[:140]}"))
            failures += 1
            return
        if res.get("probed"):
            print(f"  {job.handle}: {dim('no new posts')}")
            return
        total["new"] += res["new"]
        total["changed"] += res["changed"]
        trunc = ""
        if res.get("truncated") and res.get("mode") == "backfill":
            trunc = yellow(f"  (stopped at --limit {args.limit}; older posts were not "
                           f"fetched, raise --limit to reach further back)")
        elif res.get("truncated"):
            trunc = yellow(f"  (cut off at --limit {args.limit}: posts since the last "
                           f"sync may be missing; the next sync goes back for them, "
                           f"or re-run with a higher --limit)")
        print(f"  {job.handle}: {green(str(res['new']) + ' new')}, "
              f"{res['changed']} changed, {res['seen']} seen{trunc}")

    t0 = store.now()
    sync_mod.run_jobs(conn, jobs, keys=k, workers=args.workers, on_result=report)
    _report_outliers(conn, [r["id"] for r in conn.execute(
        "SELECT id FROM item WHERE first_seen_at >= ?", (t0,))])

    # A run that printed errors and then "done" with exit 0 cannot be detected by
    # cron, and reads as success to a human skimming the tail.
    verb = bold("done") if not failures else red(f"done with {failures} failure(s)")
    print(f"\n{verb}  {total['new']} new, {total['changed']} changed, "
          f"${total['cost']:.4f}")
    return 1 if failures else 0


def _outlier_line(o: dict) -> str:
    fmt = ("%d %b" if time.localtime(o["published_at"]).tm_year == time.localtime().tm_year
           else "%d %b %Y")
    when = time.strftime(fmt, time.localtime(o["published_at"]))
    return (f"  {bold(o['handle'])} {dim(o['platform'])}  {green(str(o['x']) + 'x')} usual "
            f"({o['engagement']} vs {o['usual']})  {when}  {o['kind'].replace('_', ' ')}\n"
            f"    {o['opening'] or dim('(no text)')}\n    {dim(o['url'] or '')}")


def _report_outliers(conn, item_ids) -> None:
    """After a sync: did any of the posts it just brought in break out? Recent ones
    only: a first backfill brings in years of history, and a three-year-old viral
    post is not news."""
    if not item_ids:
        return
    found = outliers_mod.find(conn, days=30, item_ids=item_ids, relationships=None)
    if found:
        print(bold(f"\nbreakouts in this sync ({len(found)})"))
        for o in found:
            print(_outlier_line(o))


def cmd_outliers(args):
    conn = open_db(args)
    ids = None
    if args.handle:
        row = _resolve_account(conn, args.handle)
        if row is None:
            print(red(f"no account matching '{args.handle}'"))
            return 2
        ids = [row["id"]]
    rels = None if (args.all or ids) else ("competitor", "influencer")
    if rels and not args.json:
        n = conn.execute(
            "SELECT COUNT(*) FROM account WHERE relationship IN ('competitor','influencer')"
        ).fetchone()[0]
        if not n:
            # Printing "no post beat its usual" here read as "your competitors are
            # quiet" when there are no competitors to look at.
            print(yellow("no competitors are tracked, so there is nothing to scan."))
            print(dim("  add one:  corpus add <profile-url> --relationship competitor\n"
                      "  or look at your own and clients' accounts:  corpus outliers --all"))
            return 0
    found = outliers_mod.find(conn, days=args.days, min_x=args.x, account_ids=ids,
                              relationships=rels, min_eng=args.min)
    if args.json:
        print(json.dumps(found, ensure_ascii=False, indent=2))
        return 0
    if not found:
        print(dim(f"no post in the last {args.days} days beat its account's usual by "
                  f"{args.x:g}x or more with at least {args.min} interactions."))
        _stale_hint(conn, ids, rels)
        return 0
    print(bold(f"{len(found)} post(s) at {args.x:g}x+ their account's usual "
               f"(and {args.min}+ interactions), last {args.days} days"))
    for o in found:
        print(_outlier_line(o))
    print(dim("\n'usual' = median of that account's previous 30 posts "
              "(reactions + comments + shares)."))
    _stale_hint(conn, ids, rels)
    return 0


def _stale_hint(conn, ids, rels) -> None:
    """"Last 14 days" means nothing if the newest sync is two months old: say so,
    or an empty result reads as a quiet fortnight."""
    where, args = ["1=1"], []
    if ids:
        where.append(f"id IN ({','.join('?' * len(ids))})"); args += list(ids)
    if rels:
        where.append(f"relationship IN ({','.join('?' * len(rels))})"); args += list(rels)
    newest = conn.execute(f"SELECT MAX(last_scraped_at) FROM account "
                          f"WHERE {' AND '.join(where)}", args).fetchone()[0]
    if newest and store.now() - newest > brief_mod.STALE_DAYS * 86400:
        days = (store.now() - newest) // 86400
        print(yellow(f"note: the newest sync of these accounts is {days} days old; "
                     f"run `corpus sync` for a current answer."))


def _window(days):
    """--days for briefs and compare: None/0 = everything, else counted back from
    each account's last sync (brief.gather does the anchoring)."""
    return days if days else None


_OWN_SIDE = ("own", "client")


def cmd_compare(args):
    conn = open_db(args)
    subjects, others = [], []
    if args.client:
        # The client's own accounts are the subject. Competitors tagged with the
        # same --client-ref (the natural way to say "the bistro's rivals") used to land
        # among the subjects and be benchmarked against themselves; they are the
        # default rivals instead when none are named.
        under = conn.execute(
            "SELECT * FROM account WHERE client_ref=? ORDER BY platform",
            (args.client,)).fetchall()
        subjects = [r for r in under if r["relationship"] in _OWN_SIDE]
        if not subjects:
            print(red(f"no own/client accounts under client '{args.client}'"))
            return 2
        names = args.accounts or [f"{r['platform']}:{r['handle']}" for r in under
                                  if r["relationship"] not in _OWN_SIDE]
    else:
        if len(args.accounts) < 2:
            print(red("give the subject and at least one account to compare it with, "
                      "or --client <ref> and the accounts"))
            return 2
        row = _resolve_account(conn, args.accounts[0])
        if row is None:
            print(red(f"no account matching '{args.accounts[0]}'. Add it first."))
            return 2
        subjects, names = [row], args.accounts[1:]
    if not names:
        print(red("name at least one account to compare against"))
        return 2
    for h in names:
        row = _resolve_account(conn, h)
        if row is None:
            print(red(f"no account matching '{h}'. Add it first."))
            return 2
        others.append(row)
    days = _window(args.days)
    period = (f"last {days} days before each account's last sync" if days
              else "all stored posts")
    if args.lang == "hr":
        period = (f"zadnjih {days} dana do zadnje sinkronizacije" if days
                  else "sve spremljene objave")
    stem = args.client or subjects[0]["handle"]
    out = Path(args.out or f"{DEFAULT_BRIEFS}/benchmark-{stem}{_lang_tag(args)}")
    ids = [r["id"] for r in subjects] + [r["id"] for r in others
                                         if r["id"] not in {s["id"] for s in subjects}]
    html, pdf = brief_mod.write_benchmark(
        conn, ids, out, days=days, period=period, title=args.title, pdf=args.pdf,
        subject_ids=[r["id"] for r in subjects], lang=args.lang)
    # Say in the terminal too what the page says: who was left out and why.
    subj_plats = {r["platform"] for r in subjects}
    for r in others:
        if r["platform"] not in subj_plats:
            print(yellow(f"skipped {r['handle']}: {r['platform']}, and the subject has "
                         f"no {r['platform']} account here"))
    print(f"{green('wrote')} {pdf or html}")
    return 0


def cmd_fix_paths(args):
    """Migrate media.local_path to media-root-relative. Dry run by default."""
    conn = open_db(args)
    res = store.fix_paths(conn, media_root=args.media, execute=args.execute)
    print(f"{bold('media paths')}  checked={res['checked']} already-relative={res['ok']} "
          f"rewrite={green(str(res['rewritten']))} "
          f"dangling={yellow(str(res['dangling'])) if res['dangling'] else 0} "
          f"outside-root={res['outside_root']}")
    if res["dangling"]:
        print(dim("  dangling = the file is not on this machine; the row is cleared so "
                  "enrich fetches the still again (keyframes need enrich --redo)"))
    if res["dry_run"]:
        print(yellow("dry run. Nothing changed. Add --execute to rewrite."))
    return 0


def cmd_weekly(args):
    from . import digest, notify
    conn = open_db(args)
    d = digest.build(conn, days=args.days, client_ref=args.client_ref)
    text = digest.render_text(d)
    print(text)
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(digest.render_html(d) if out.suffix.lower() in (".html", ".htm")
                       else text + "\n")
        print(dim(f"\nwrote {out}"))
    if args.send:
        if not notify.send(text, title="corpus weekly"):
            print(red("\nnot sent: no notify channel accepted it (see stderr)"))
            return 1
        print(dim("\nsent"))
    return 0


def cmd_notify(args):
    from . import notify
    if args.unit:
        text = notify.unit_failure_text(args.unit)
        title = f"corpus: {args.unit} failed"
    elif args.text:
        text, title = " ".join(args.text), "corpus"
    else:
        print(red("give a message, or --unit <systemd unit>"))
        return 2
    if args.dry_run:
        print(text)
        return 0
    ok = notify.send(text, title=title)
    print(green("sent") if ok else red("not sent (see stderr)"))
    return 0 if ok else 1


def cmd_retention(args):
    """Age out third-party comments on studied accounts. Dry run by default."""
    from . import retention
    conn = open_db(args)
    r = retention.apply(conn, months=args.months, raw_months=args.raw_months,
                        execute=args.execute)
    verb = "deleted" if r["execute"] else "would delete"
    print(f"{bold('retention')}  competitor/influencer comments only")
    print(f"  {verb} {r['delete']} comment(s) older than {r['months']:g} months")
    print(f"  {'blanked' if r['execute'] else 'would blank'} the raw vendor payload "
          f"of {r['blank_raw']} more, fetched over {r['raw_months']:g} months ago")
    print(dim(f"  {r['kept_private']} comment(s) on own/client posts: never touched "
              f"here (use `corpus forget`)"))
    if not r["execute"]:
        print(yellow("  dry run. Add --execute to apply."))
    return 0


def _is_apify(adapter) -> bool:
    return "APIFY_TOKEN" in tuple(adapter.needs)


def routes_for(conn, decision, *, have_file: bool = False, keys=None):
    """Every usable route for an account, cheapest first.

    for_platform() sorts cheapest-first, which puts the free consented export ahead
    of the paid scrape for own/client. That is the right default, but two routes can
    be unusable at call time for reasons cost does not capture:

      * the export reads a .xlsx the user has to download from LinkedIn first
      * the session route needs cookies that may not be configured

    Returning the whole list, rather than just the head, lets the caller fall through
    to the next route instead of failing on a free one that cannot run. A run that
    quietly costs three cents beats a run that errors.
    """
    row = conn.execute("SELECT * FROM account WHERE id=?",
                       (decision.account_id,)).fetchone()
    opts = for_platform(row["platform"], row["relationship"])
    if not have_file:
        opts = [a for a in opts if "export" not in a.name] or opts

    keys = keys or {}
    usable = [a for a in opts if all(keys.get(n) for n in a.needs)]
    return usable or opts


def get_adapter_for(conn, decision, *, have_file: bool = False, keys=None):
    r = routes_for(conn, decision, have_file=have_file, keys=keys)
    return r[0] if r else None


def cmd_enrich(args):
    conn = open_db(args)
    aid = None
    if args.handle:
        row = _resolve_account(conn, args.handle)
        if row is None:
            print(red(f"no account matching '{args.handle}'"))
            return 2
        aid = row["id"]

    if args.ocr or args.redo_ocr:
        res = enrich_mod.read_screen_text(conn, account_id=aid, limit=args.limit,
                                          redo=args.redo_ocr)
        if res.get("skipped"):
            print(yellow(f"  {res['skipped']}"))
        else:
            print(f"{bold('screen text')}  considered={res['considered']} "
                  f"read={green(str(res['read']))} chars={res['chars']}")
        if res.get("error"):
            print(red(f"  {res['error']}"))
            return 1
        return 0

    st = enrich_mod.run(conn, media_root=args.media, account_id=aid,
                        limit=args.limit, frames=args.frames,
                        workers=args.workers, image_workers=args.image_workers,
                        keep_audio=args.keep_audio, redo=args.redo,
                        dry_run=args.dry_run, allow_cloud=not args.no_cloud,
                        language=args.language)
    d = st.as_dict()
    print(f"{bold('enrich')}  considered={d['considered']} "
          f"transcribed={green(str(d['transcribed']))} frames={d['frames']} "
          f"images={green(str(d.get('images', 0)))} "
          f"expired={d['expired']} failed={red(str(d['failed'])) if d['failed'] else 0}"
          + (f" transcriber-down={red(str(d['unavailable']))}" if d.get("unavailable")
             else ""))
    for e in d["errors"]:
        print((dim if st.ok else red)(f"  {e}"))
    pruned = 0 if args.dry_run else ops.prune_thumbs(brief_mod.THUMB_CACHE)
    if pruned:
        print(dim(f"  pruned {pruned} thumbnail(s) older than "
                  f"{ops.THUMB_MAX_AGE_DAYS} days"))
    # Non-zero on any failure: a nightly unit that exits 0 over a run where every
    # item failed is a failure nobody is told about.
    return 0 if args.dry_run or st.ok else 1


def cmd_dashboard(args):
    from . import web
    web.serve(db_path=args.db, analysis_db=args.analysis_db,
              media_root=DEFAULT_MEDIA, host=args.host, port=args.port,
              token=args.token)
    return 0


def cmd_comments(args):
    """Fetch comment TEXT. Paid per comment, so it always shows the bill first."""
    conn = open_db(args)
    row = _resolve_account(conn, args.handle) if args.handle else None
    if args.handle and row is None:
        print(red(f"no account matching '{args.handle}'"))
        return 2
    accounts = ([dict(row)] if row else
                [dict(r) for r in conn.execute("SELECT * FROM account ORDER BY handle")])

    k = keys_mod.load()
    token = k.get("APIFY_TOKEN")
    if not token and not args.dry_run:
        print(red("APIFY_TOKEN missing; expected in ~/.reach/keys.env"))
        return 2

    if not args.dry_run:
        try:
            comments_mod.assert_affordable(conn, accounts, token=token,
                                           per_post=args.per_post, limit=args.limit,
                                           include_replies=args.replies)
        except keys_mod.PlanGuardError as e:
            print(red(f"refusing to run: {e}"))
            return 3

    total_cost = 0.0
    failures = 0
    for acct in accounts:
        plan = comments_mod.sync(conn, acct, token=token, per_post=args.per_post,
                                 limit=args.limit, include_replies=args.replies,
                                 dry_run=True)
        if plan.get("skipped"):
            print(dim(f"{acct['handle']}: {plan['skipped']}"))
            continue
        if not plan["posts"]:
            print(dim(f"{acct['handle']}: nothing to fetch"))
            continue

        est = plan["estimate"]
        print(f"{bold(acct['handle'])}  {plan['posts']} post(s) missing comments, "
              f"up to {args.per_post} each  ~{yellow('$%.2f' % est)}")
        if args.dry_run:
            total_cost += est
            continue

        # A run row, like sync's: comment spend used to leave no trace in
        # scrape_run, so `health` could neither total it nor see a failed batch.
        run_id = store.start_run(conn, adapter="comments", mode="comments",
                                 account_id=acct["id"])
        try:
            res = comments_mod.sync(
                conn, acct, token=token, per_post=args.per_post, limit=args.limit,
                include_replies=args.replies,
                on_batch=lambda done, tot, spent: print(
                    dim(f"  {done}/{tot} posts  ${spent:.2f}"), flush=True))
        except BaseException as e:
            store.finish_run(conn, run_id,
                             status="interrupted" if isinstance(e, KeyboardInterrupt)
                             else "error",
                             error=None if isinstance(e, KeyboardInterrupt)
                             else f"{type(e).__name__}: {e}")
            raise
        errs = res.get("errors") or []
        store.finish_run(conn, run_id, status="error" if errs else "ok",
                         seen=res.get("comments", 0), new=res.get("new", 0),
                         cost_usd=res["cost"],
                         error="; ".join(errs)[:2000] if errs else None)
        failures += bool(errs)
        total_cost += res["cost"]
        dup = res.get("duplicates") or 0
        print(f"  stored {green(str(res['comments']))} comment(s), "
              f"{res['new']} new, {'$%.2f' % res['cost']}"
              + (dim(f"  ({dup} duplicate row(s) returned and billed)") if dup else ""))
        for e in res.get("errors", []):
            print(red(f"  {e}"))
        if res.get("deferred"):
            print(yellow(f"  {res['deferred']} post(s) not reached before the batch "
                         f"limit; they stay queued for the next run"))

    label = "estimated" if args.dry_run else "spent"
    print(f"{bold('total')}  {label} ${total_cost:.2f}"
          + (red(f"  {failures} account(s) with failed batches") if failures else ""))
    return 1 if failures else 0


def cmd_analyze(args):
    conn = open_db(args)
    sel = {}
    if args.handle:
        row = _resolve_account(conn, args.handle)
        if row is None:
            print(red(f"no account matching '{args.handle}'"))
            return 2
        sel["account_id"] = row["id"]
    if args.template == "account-summary":
        # Account-level: one call per account, not one per post.
        from .analyze import account as acct_mod
        if not args.handle:
            print(red("account-summary needs --account <handle>"))
            return 2
        st = acct_mod.run(conn, sel["account_id"], db_path=args.analysis_db,
                          dry_run=not args.execute, lang=args.lang, force=args.force)
        return _print_analyze(st, args)
    if args.relationship:
        sel["relationship"] = args.relationship
    if args.platform:
        sel["platform"] = args.platform
    if args.limit:
        sel["limit"] = args.limit

    st = analyze_mod.run(conn, args.template, db_path=args.analysis_db,
                         standing_context=args.context or "",
                         dry_run=not args.execute, force=args.force,
                         workers=args.workers, lang=args.lang,
                         assess_personal=args.assess_personal,
                         context_public=args.context_public, **sel)
    return _print_analyze(st, args)


def _print_analyze(st, args):
    d = st.as_dict()
    print(f"{bold('analyze')} {d['template']}  considered={d['considered']} "
          f"done={green(str(d['done']))} skipped={d['skipped']} "
          f"failed={red(str(d['failed'])) if d['failed'] else 0}  "
          f"est=${d['est_cost_usd']:.4f} actual=${d['cost_usd']:.4f}")
    # Anything considered but neither done, skipped (cached) nor failed was never
    # attempted: no provider configured, or the run aborted part way.
    unattempted = d["considered"] - d["done"] - d["skipped"] - d["failed"]
    no_provider = any(str(e).startswith("no LLM provider") for e in d["errors"])
    bad = no_provider or (args.execute and (d["failed"] or unattempted > 0))
    for e in d["errors"]:
        print((red if bad else dim)(f"  {e}"))
    # Where each item goes (or why it does not), so a dry run answers "does any
    # client text leave this machine" before anything is spent.
    if not args.execute:
        for line in d["plan"][:60]:
            print(dim(f"    {line}"))
        if len(d["plan"]) > 60:
            print(dim(f"    ... and {len(d['plan']) - 60} more"))
    if not args.execute and d["considered"]:
        print(dim("  add --execute to actually run it"))
    return 1 if bad else 0


def cmd_brief(args):
    conn = open_db(args)
    days = _window(args.days)
    common = dict(days=days, analysis_db=args.analysis_db, template=args.template,
                  lang=args.lang, csv=args.csv)

    if args.handle in ("all", "*"):
        # "all" used to mean every account in the database, which silently mixed two
        # different people into one document once a second person was tracked. Scope
        # it, and make an unscoped sweep something you have to ask for.
        where, qargs = [], []
        if args.client_ref:
            where.append("client_ref = ?"); qargs.append(args.client_ref)
        if args.relationship:
            where.append("relationship = ?"); qargs.append(args.relationship)
        elif args.client_ref:
            # A client's brief is the client's accounts. Competitors tagged with the
            # same --client-ref were folded into its headline numbers and its post
            # list (field test, 2026-10-23: a rival café inside a bistro's brief).
            left = conn.execute(
                "SELECT platform, handle FROM account WHERE client_ref=? AND "
                "relationship NOT IN ('own','client')", (args.client_ref,)).fetchall()
            if left:
                where.append("relationship IN ('own','client')")
                print(dim(f"  left out {len(left)} competitor/influencer account(s) "
                          f"under '{args.client_ref}' ("
                          + ", ".join(f"{r['platform']}:{r['handle']}" for r in left)
                          + "); `corpus compare --client "
                          f"{args.client_ref}` benchmarks against them"))
        sql = "SELECT id, platform, handle FROM account"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY platform"
        rows = conn.execute(sql, qargs).fetchall()

        distinct = conn.execute(
            "SELECT COUNT(DISTINCT COALESCE(client_ref, relationship)) n "
            "FROM account WHERE id IN (%s)" % ",".join("?" * len(rows)),
            [r["id"] for r in rows]).fetchone()["n"] if rows else 0
        if distinct > 1:
            print(yellow(
                f"warning: '{args.handle}' spans {distinct} different subjects. "
                f"A brief is about one person or brand; narrow it with "
                f"--client-ref or --relationship."))
        if not rows:
            print(red("no accounts to report on"))
            return 2
        stem = args.client_ref or "content"
        if args.relationship and args.relationship not in _OWN_SIDE:
            stem += f"-{args.relationship}"
        out = Path(args.out or f"{DEFAULT_BRIEFS}/{stem}-content-audit{_lang_tag(args)}")
        html, pdf = brief_mod.write_combined(
            conn, [r["id"] for r in rows], out, title=args.title,
            subject=args.subject, pdf=args.pdf, **common)
        print(f"{green('brief')} {html}" + (f"\n{green('pdf  ')} {pdf}" if pdf else ""))
        if args.csv:
            print(f"{green('csv  ')} {html.with_suffix('.csv')}")
        if not args.pdf:
            print(dim("  open it in a browser; posts expand on click. "
                      "--pdf if you need to send one."))
        _brief_age_warning(conn, [r["id"] for r in rows])
        return 0

    row = _resolve_account(conn, args.handle)
    if row is None:
        print(red(f"no account matching '{args.handle}'"))
        return 2

    # Named so nothing overwrites anything: the Croatian brief replaced the English
    # one, and facebook:bistroharbor's replaced instagram:bistroharbor's.
    twins = conn.execute("SELECT COUNT(*) FROM account WHERE handle=? COLLATE NOCASE",
                         (row["handle"],)).fetchone()[0]
    stem = f"{row['platform']}-{row['handle']}" if twins > 1 else row["handle"]
    out = Path(args.out or f"{DEFAULT_BRIEFS}/{stem}-content-audit{_lang_tag(args)}")
    if args.pdf:
        html, pdf = brief_mod.write_pdf(conn, row["id"], out, title=args.title, **common)
        print(f"{green('brief')} {html}\n{green('pdf  ')} {pdf}")
    else:
        html = brief_mod.write(conn, row["id"], brief_mod._html_path(out),
                               title=args.title, **common)
        print(f"{green('brief')} {html}")
        print(dim("  open it in a browser; posts expand on click. "
                  "--pdf if you need to send one."))
    if args.csv:
        print(f"{green('csv  ')} {html.with_suffix('.csv')}")
    _brief_age_warning(conn, [row["id"]])
    return 0


def _lang_tag(args) -> str:
    return "" if getattr(args, "lang", "en") in (None, "en") else f"-{args.lang}"


def _brief_age_warning(conn, ids) -> None:
    """The page says it in red; the terminal should too, before it gets sent."""
    for aid in ids:
        acct = dict(conn.execute("SELECT * FROM account WHERE id=?", (aid,)).fetchone())
        anchor = brief_mod._anchor(conn, acct)
        if anchor and store.now() - anchor > brief_mod.STALE_DAYS * 86400:
            twins = conn.execute("SELECT COUNT(*) FROM account WHERE handle=? COLLATE "
                                 "NOCASE", (acct["handle"],)).fetchone()[0]
            name = (f"{acct['platform']}:{acct['handle']}" if twins > 1
                    else acct["handle"])     # a bare shared handle is refused
            print(yellow(f"warning: {name} was last synced "
                         f"{(store.now() - anchor) // 86400} days ago; the brief says "
                         f"so at the top. `corpus sync {name}` first."))


def cmd_search(args):
    conn = open_db(args)
    hits = store.search(conn, args.query, limit=args.limit)
    if not hits:
        print(yellow("no matches."))
        return 1
    for h in hits:
        when = time.strftime("%Y-%m-%d", time.gmtime(h["published_at"]))
        print(f"{dim(when)}  {bold(h['handle'])} {dim('(' + h['relationship'] + ')')}  "
              f"{h['m_reactions'] or 0}r/{h['m_comments'] or 0}c")
        print(f"  {h['snip']}")
        if h["url"]:
            print(dim(f"  {h['url']}"))
    return 0


def backup_dir(args) -> Path:
    """Backups live beside the database they protect (~/.corpus/backups for the
    default one), so a --db pointed elsewhere never reads or prunes the real ones."""
    d = getattr(args, "dir", None)
    return Path(d).expanduser() if d else Path(args.db).expanduser().parent / "backups"


def cmd_backup(args):
    conn_paths = [args.db, args.analysis_db]
    args.dir = str(backup_dir(args))
    if args.restore:
        # The destination follows the snapshot's name, never a default: restoring
        # an analysis-*.db.gz used to overwrite corpus.db.
        stem = backup_mod.stem_of(args.restore)
        targets = {Path(p).expanduser().stem: Path(p).expanduser() for p in conn_paths}
        if stem not in targets:
            print(red(f"restore refused: {Path(args.restore).name} is not a snapshot "
                      f"of {' or '.join(sorted(targets))}"))
            return 2
        dest = targets[stem]
        try:
            out = backup_mod.restore(args.restore, dest, overwrite=args.force,
                                     expect_stem=stem)
        except Exception as e:
            print(red(f"restore refused: {e}"))
            return 2
        print(f"{green('restored')} {out}")
        print(dim("  the previous database was moved aside, not deleted"))
        return 0

    if args.list:
        snaps = backup_mod.list_snapshots(args.dir)
        if not snaps:
            print(yellow("no snapshots yet. Run: corpus backup"))
            return 1
        for s in snaps:
            v = backup_mod.verify(s.path)
            mark = green("ok") if v["ok"] else red("BAD")
            rows = sum(c or 0 for c in v["counts"].values())
            print(f"  {time.strftime('%Y-%m-%d %H:%M', time.localtime(s.taken_at))}  "
                  f"{mark:14} {s.bytes//1024:5}KB  {rows:6} rows  {s.path.name}")
        return 0

    res = backup_mod.run(conn_paths, args.dir, keep=args.keep)
    unchanged = set(res.get("unchanged") or [])
    for v in res["verified"]:
        mark = green("verified") if v["ok"] else red("FAILED")
        rows = sum(c or 0 for c in v["counts"].values())
        detail = ", ".join(f"{n} {t}" for t, n in sorted(
            v["counts"].items(), key=lambda kv: -(kv[1] or 0))[:3] if n)
        same = dim("  (unchanged since this snapshot, no new copy)") \
            if v["path"] in unchanged else ""
        print(f"  {mark}  {Path(v['path']).name}  {rows} rows ({detail}){same}")
    if res["pruned"]:
        print(dim(f"  pruned {len(res['pruned'])} old snapshot(s)"))
    if res.get("prune_skipped"):
        print(yellow("  nothing pruned: a snapshot failed verification, so every "
                     "older one is kept"))
    for e in res["errors"]:
        print(red(f"  {e}"))
    thumbs = ops.prune_thumbs(brief_mod.THUMB_CACHE)
    if thumbs:
        print(dim(f"  pruned {thumbs} thumbnail(s) older than "
                  f"{ops.THUMB_MAX_AGE_DAYS} days"))
    return 1 if res["errors"] else 0


def cmd_forget(args):
    conn = open_db(args)
    handle, platform = args.handle.lstrip("@"), args.platform
    # platform:handle, as every other command takes it; it was "no account
    # matching", and --platform did not list youtube, facebook or tiktok.
    if ":" in handle and not handle.startswith("http"):
        platform, _, handle = handle.partition(":")
        platform = platform.lower()
    try:
        res = store.forget(conn, handle=handle, platform=platform,
                           dry_run=not args.execute, media_root=DEFAULT_MEDIA,
                           thumb_dir=brief_mod.THUMB_CACHE)
    except ValueError as e:
        print(red(str(e)))
        return 2
    if not res["accounts"]:
        print(yellow(f"no account matching '{args.handle}'"))
        return 1

    for a in res["accounts"]:
        print(f"  {a['platform']}/{a['handle']} ({a['relationship']})")
    if len(res["accounts"]) > 1:
        print(yellow(f"  '{handle}' is tracked on {len(res['accounts'])} platforms and "
                     f"ALL of them are listed; name one as platform:handle to erase "
                     f"only that one"))
    print(f"  {res['items']} posts, {res['media']} media rows, "
          f"{res['metrics']} metric snapshots, {len(res['media_files'])} files on disk")

    if res["dry_run"]:
        print(yellow("\ndry run. Nothing deleted. Add --execute to erase."))
        return 0

    from .analyze import run as _ar
    pruned = _ar.prune(conn, db_path=args.analysis_db)
    names = ", ".join(f"{a['platform']}:{a['handle']}" for a in res["accounts"])
    print(f"{green('erased')} {names}, and pruned {pruned} orphaned analysis row(s)")
    print(dim("  raw_json went with the rows; there is no undo short of a backup"))
    # The real paths: this said ~/.corpus/backups and ~/Downloads whatever --db
    # and CORPUS_BRIEFS pointed at.
    print(yellow(f"  the daily backups still hold this account until they rotate out "
                 f"(14 kept); delete {backup_dir(args)}/* now if the erasure must be "
                 f"immediate, and any brief already sent or saved in {DEFAULT_BRIEFS}"))
    return 0


def cmd_health(args):
    """One line per check; exit 1 when any is red, so a timer can alert on it."""
    from . import health as health_mod
    conn = open_db(args)
    checks = health_mod.run_all(conn, backup_dir=backup_dir(args),
                                unavailable_adapters=UNAVAILABLE, keys=keys_mod.load())
    red_n = sum(c.level == health_mod.RED for c in checks)
    if args.json:
        print(json.dumps({"ok": not red_n, "red": red_n,
                          "checks": [c.as_dict() for c in checks]},
                         ensure_ascii=False, indent=1))
        return 1 if red_n else 0
    marks = {health_mod.OK: green("ok"), health_mod.WARN: yellow("warn"),
             health_mod.RED: red("RED")}
    section = None
    for c in checks:
        if c.check != section:
            section = c.check
            print(bold(f"\n{section}"))
        print(f"  {marks[c.level]:14} {c.subject:28} {c.message}")
    print(("\n" + red(f"{red_n} red line(s)")) if red_n else ("\n" + green("all clear")))
    return 1 if red_n else 0


def cmd_adapters(args):
    # Sized to the longest name: "scrapecreators_instagram" ran into its platform.
    w = max([len("adapter"), *(len(n) for n in REGISTRY), *(len(n) for n in UNAVAILABLE)]) + 2
    print(bold(f"{'adapter':{w}}{'platform':11}{'cost/1k':10}relationships"))
    for a in sorted(REGISTRY.values(), key=lambda x: (x.platform, x.name)):
        cost = "free" if not a.cost_per_1k else f"${a.cost_per_1k:.2f}"
        print(f"{a.name:{w}}{a.platform:11}{cost:10}{','.join(a.relationships)}")
        if a.notes:
            print(dim(f"  {a.notes}"))
    for k, v in UNAVAILABLE.items():
        print(red(f"{k:{w}}unavailable: {v}"))
    return 0


def cmd_show(args):
    conn = open_db(args)
    row = conn.execute(
        """SELECT i.*, a.handle, a.relationship FROM item i
             JOIN account a ON a.id=i.account_id WHERE i.id=?""", (args.item_id,)
    ).fetchone()
    if row is None:
        print(red(f"no item {args.item_id}"))
        return 2
    print(bold(f"#{row['id']}  {row['handle']}  {row['kind']}  "
               f"{time.strftime('%Y-%m-%d', time.gmtime(row['published_at']))}"))
    print(dim(row["url"] or ""))
    # A metric the platform never reports (Instagram shares) is unknown, not 0.
    def n(v):
        return "n/a" if v is None else v
    print(f"\n{bold('metrics')}  {n(row['m_reactions'])} reactions, "
          f"{n(row['m_comments'])} comments, {n(row['m_shares'])} shares, "
          f"{n(row['m_views'])} views")
    if row["body"]:
        print(f"\n{bold('body')}\n{row['body'][:1500]}")
    if row["transcript"]:
        print(f"\n{bold('transcript')} {dim('(' + (row['lang'] or '?') + ')')}"
              f"\n{row['transcript'][:1500]}")
    for r in analyze_mod.results_for(conn, row["id"], db_path=args.analysis_db):
        print(f"\n{bold(r['template'])} {dim(r['provider'] or '')}")
        print((r["output_text"] or r["output_json"] or "")[:1200])
    hist = conn.execute(
        """SELECT captured_at, reactions, comments FROM metric_snapshot
            WHERE item_id=? ORDER BY captured_at""", (row["id"],)).fetchall()
    if len(hist) > 1:
        print(f"\n{bold('engagement over time')}")
        for h in hist:
            print(dim(f"  {time.strftime('%Y-%m-%d %H:%M', time.localtime(h['captured_at']))}"
                      f"  {h['reactions']}r {h['comments']}c"))
    return 0


def cmd_profile(args):
    """The stored profile payload, read-only. This is the contract another tool can
    read before paying for the same actor run again, so it prints JSON and never
    touches the network. `complete` is False when all we hold is the posts actor's
    author stub (name, avatar, headline), which must not pass for a full profile."""
    conn = open_db(args)
    row = _resolve_account(conn, args.target)
    if row is None:
        if args.json:
            print(json.dumps({"status": "not_tracked", "target": args.target}))
        else:
            print(red(f"not tracked: {args.target}"))
        return 1
    try:
        raw = json.loads(row["raw_json"]) if row["raw_json"] else None
    except ValueError:
        raw = None
    complete = isinstance(raw, dict) and any(
        k in raw for k in ("experience", "education", "employeeCount", "followerCount"))
    out = {"status": "found", "platform": row["platform"], "handle": row["handle"],
           "relationship": row["relationship"], "url": row["url"],
           "display_name": row["display_name"], "follower_count": row["follower_count"],
           "last_scraped_at": row["last_scraped_at"], "complete": complete,
           "items": conn.execute("SELECT COUNT(*) FROM item WHERE account_id=?",
                                 (row["id"],)).fetchone()[0],
           "profile": raw}
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
        return 0
    when = (time.strftime("%Y-%m-%d", time.localtime(row["last_scraped_at"]))
            if row["last_scraped_at"] else "never")
    print(bold(f"{row['platform']}:{row['handle']}  {row['relationship']}"))
    print(f"{row['display_name'] or ''}  {dim(row['url'] or '')}")
    print(f"{out['items']} items, last synced {when}, "
          f"{'full profile' if complete else 'author stub only'}")
    return 0


# ── parser ──────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="corpus",
        description="A social content back-catalog you own. Rent the extraction, "
                    "keep the corpus.")
    p.add_argument("--db", help=f"default {DEFAULT_DB}, or $CORPUS_DB")
    p.add_argument("--analysis-db")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="track a profile")
    a.add_argument("target", help="profile URL or @handle")
    a.add_argument("--relationship", required=True,
                   choices=["own", "client", "competitor", "influencer"])
    a.add_argument("--client-ref",
                   help='which client this belongs to; on an existing account, '
                        '--client-ref "" clears it')
    a.set_defaults(func=cmd_add)

    f = sub.add_parser("find", help="search for someone's profiles by name")
    f.add_argument("query", nargs="+")
    f.add_argument("--location", action="append",
                   help="narrow a common name, e.g. --location Portville")
    f.add_argument("--company", action="append")
    f.add_argument("--keyword", action="append")
    f.set_defaults(func=cmd_find)

    s = sub.add_parser("sync", help="fetch new content (respects freshness)")
    s.add_argument("handle", nargs="?", default="all")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--force", action="store_true", help="ignore freshness")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--workers", type=int, default=sync_mod.DEFAULT_WORKERS,
                   help=f"accounts fetched concurrently "
                        f"(default {sync_mod.DEFAULT_WORKERS}; Apify Starter "
                        f"allows 32 concurrent actor runs)")
    s.add_argument("--file", help="for linkedin_export: path to the .xlsx")
    s.set_defaults(func=cmd_sync)

    cp = sub.add_parser("compare", help="one account (or a client's accounts) side by "
                                        "side with competitors")
    cp.add_argument("accounts", nargs="*",
                    help="the subject then the accounts to measure it against; with "
                         "--client, only the accounts to measure against")
    cp.add_argument("--client", help="client_ref: every platform of this client is "
                                     "compared with the same-platform accounts given")
    cp.add_argument("--days", type=int, default=90,
                    help="period to compare, counted back from each account's last "
                         "sync (default 90; 0 = everything stored)")
    cp.add_argument("--title")
    cp.add_argument("--out")
    cp.add_argument("--pdf", action="store_true")
    cp.add_argument("--lang", choices=brief_mod.i18n.LANGS, default="en",
                    help="language of the page (hr = Croatian)")
    cp.set_defaults(func=cmd_compare)

    ol = sub.add_parser("outliers", help="recent posts that beat their account's "
                                         "usual by a wide margin")
    ol.add_argument("handle", nargs="?")
    ol.add_argument("--days", type=int, default=outliers_mod.DEFAULT_DAYS)
    ol.add_argument("--x", type=float, default=outliers_mod.DEFAULT_X,
                    help=f"how many times the usual (default {outliers_mod.DEFAULT_X:g})")
    ol.add_argument("--min", type=int, default=outliers_mod.DEFAULT_MIN,
                    help=f"minimum interactions on the post "
                         f"(default {outliers_mod.DEFAULT_MIN})")
    ol.add_argument("--all", action="store_true",
                    help="include own and client accounts, not just competitors")
    ol.add_argument("--json", action="store_true")
    ol.set_defaults(func=cmd_outliers)

    e = sub.add_parser("enrich", help="transcripts and keyframes for video")
    e.add_argument("handle", nargs="?")
    e.add_argument("--limit", type=int)
    e.add_argument("--frames", type=int, default=6)
    e.add_argument("--media", default=DEFAULT_MEDIA)
    e.add_argument("--workers", type=int, default=enrich_mod.MAX_VIDEO_WORKERS,
                   help=f"videos in flight (default and maximum "
                        f"{enrich_mod.MAX_VIDEO_WORKERS}; measured 1.42x at 3 and "
                        f"slower than serial at 4)")
    e.add_argument("--image-workers", type=int, default=enrich_mod.IMAGE_WORKERS,
                   help=f"still downloads in flight (default "
                        f"{enrich_mod.IMAGE_WORKERS}; pure network, scales ~9x)")
    e.add_argument("--keep-audio", action="store_true")
    e.add_argument("--redo", action="store_true")
    e.add_argument("--dry-run", action="store_true")
    e.add_argument("--no-cloud", action="store_true",
                   help="local only; non-English video will be refused, not guessed")
    e.add_argument("--language", help="force a language code, e.g. hr")
    e.add_argument("--ocr", action="store_true",
                   help="read burned-in on-screen text off stored keyframes "
                        "(free, on-device: Vision on macOS, tesseract on Linux); "
                        "a separate pass, run it after a plain `enrich`")
    e.add_argument("--redo-ocr", action="store_true",
                   help="re-read on-screen text even where it exists")
    e.set_defaults(func=cmd_enrich)

    cm = sub.add_parser("comments",
                        help="fetch comment TEXT (paid per comment)")
    cm.add_argument("handle", nargs="?")
    cm.add_argument("--per-post", type=int, default=comments_mod.DEFAULT_PER_POST,
                    help="max comments per post (default 25; the tail is "
                         "'great post!' and you pay for it)")
    cm.add_argument("--limit", type=int, help="only the N most-commented posts")
    cm.add_argument("--replies", action="store_true",
                    help="also fetch replies to comments")
    cm.add_argument("--dry-run", action="store_true",
                    help="show the bill without spending")
    cm.set_defaults(func=cmd_comments)

    db = sub.add_parser("dashboard", help="browse the corpus in a browser")
    db.add_argument("--port", type=int, default=8891)
    db.add_argument("--host", default="127.0.0.1",
                    help="127.0.0.1 by default; a public bind requires --token")
    db.add_argument("--token", help="require this bearer token (needed to bind "
                                    "anything but localhost)")
    db.set_defaults(func=cmd_dashboard)

    an = sub.add_parser("analyze", help="run a lens over stored content")
    an.add_argument("template")
    an.add_argument("--handle", "--account", dest="handle")
    an.add_argument("--lang", choices=["en", "hr"],
                    help="language of the output (default English)")
    an.add_argument("--assess-personal", action="store_true",
                    help="run marketing lenses even on posts the keyword filter "
                         "reads as grief/illness (the prompt rule still applies)")
    an.add_argument("--relationship")
    an.add_argument("--platform")
    an.add_argument("--limit", type=int)
    an.add_argument("--context", help="standing context about you/your client "
                                      "(makes every payload private)")
    an.add_argument("--context-public", action="store_true",
                    help="the --context text is not private, so public items may "
                         "still use the public provider chain")
    an.add_argument("--force", action="store_true")
    an.add_argument("--workers", type=int, default=analyze_mod.DEFAULT_WORKERS,
                    help=f"ceiling on concurrent LLM calls (default "
                         f"{analyze_mod.DEFAULT_WORKERS}; cut down automatically to "
                         f"what the provider's tokens-per-minute allows)")
    an.add_argument("--execute", action="store_true",
                    help="actually spend; without it this is a dry run")
    an.set_defaults(func=cmd_analyze)

    b = sub.add_parser("brief", help="generate the client brief")
    b.add_argument("handle", help="a handle, or 'all' for one combined document")
    b.add_argument("--out")
    b.add_argument("--days", type=int, default=brief_mod.DEFAULT_WINDOW_DAYS,
                   help=f"period covered, counted back from the account's last sync "
                        f"(default {brief_mod.DEFAULT_WINDOW_DAYS}; 0 = everything)")
    b.add_argument("--pdf", action="store_true",
                   help="also render a PDF (slower, static, ~3x the size); "
                        "the HTML is the primary artefact")
    b.add_argument("--csv", action="store_true",
                   help="also write the posts as a CSV next to the HTML")
    b.add_argument("--lang", choices=brief_mod.i18n.LANGS, default="en",
                   help="language of the brief's own text (hr = Croatian); lens "
                        "output stays in whatever language it was written in")
    b.add_argument("--title", help="document title (default: the person's name)")
    b.add_argument("--subject", help="who this is about, for the subtitle")
    b.add_argument("--client-ref", help="scope `all` to one client")
    b.add_argument("--relationship",
                   choices=["own","client","competitor","influencer"],
                   help="scope `all` to one relationship")
    b.add_argument("--template",
                   help="show only this lens; without it every client-safe lens "
                        "is shown per post (never the personal ones)")
    b.set_defaults(func=cmd_brief)

    se = sub.add_parser("search", help="full-text search the corpus")
    se.add_argument("query")
    se.add_argument("--limit", type=int, default=15)
    se.set_defaults(func=cmd_search)

    bk = sub.add_parser("backup", help="snapshot, verify and prune the corpus")
    bk.add_argument("--dir", help="default: backups/ beside the database "
                                  "(~/.corpus/backups)")
    bk.add_argument("--keep", type=int, default=backup_mod.KEEP)
    bk.add_argument("--list", action="store_true", help="show snapshots and verify each")
    bk.add_argument("--restore", metavar="SNAPSHOT",
                    help="restore a snapshot over the current database")
    bk.add_argument("--force", action="store_true", help="allow restore to overwrite")
    bk.set_defaults(func=cmd_backup)

    fg = sub.add_parser("forget",
                        help="erase everything held about an account (GDPR)")
    fg.add_argument("handle")
    fg.add_argument("--platform", choices=["linkedin", "instagram", "youtube",
                                           "facebook", "tiktok"])
    fg.add_argument("--execute", action="store_true",
                    help="actually delete; without it this is a dry run")
    fg.set_defaults(func=cmd_forget)

    he = sub.add_parser("health", help="is the pipeline working? exits 1 on any red "
                                       "line")
    he.add_argument("--json", action="store_true")
    he.add_argument("--dir", help="backup directory (default: beside the database)")
    he.set_defaults(func=cmd_health)
    sub.add_parser("adapters", help="what routes exist and what they cost").set_defaults(
        func=cmd_adapters)

    fp = sub.add_parser("fix-paths", help="rewrite stored media paths relative to "
                                          "the media root (dry run by default)")
    fp.add_argument("--media", default=DEFAULT_MEDIA)
    fp.add_argument("--execute", action="store_true",
                    help="actually rewrite; without it this is a dry run")
    fp.set_defaults(func=cmd_fix_paths)
    wk = sub.add_parser("weekly", help="what changed this week, short enough for "
                                       "WhatsApp (--send delivers it)")
    wk.add_argument("--days", type=int, default=7)
    wk.add_argument("--client-ref", help="only this client's accounts")
    wk.add_argument("--send", action="store_true",
                    help="send it via the notify channel (WhatsApp / ntfy)")
    wk.add_argument("--out", help="also write it to a file (.html gets the HTML "
                                  "version, anything else the text)")
    wk.set_defaults(func=cmd_weekly)

    nt = sub.add_parser("notify", help="send a message on the alert channel; "
                                       "--unit reports a failed systemd unit")
    nt.add_argument("text", nargs="*")
    nt.add_argument("--unit", help="systemd user unit that failed: its last journal "
                                   "lines go in the message (OnFailure hook)")
    nt.add_argument("--dry-run", action="store_true",
                    help="print the message instead of sending it")
    nt.set_defaults(func=cmd_notify)
    rt = sub.add_parser("retention", help="age out third-party comments on "
                                          "competitor/influencer posts (GDPR)")
    rt.add_argument("--months", type=float, default=12,
                    help="delete comments older than this (default 12)")
    rt.add_argument("--raw-months", type=float, default=3,
                    help="blank the raw vendor payload sooner (default 3)")
    rt.add_argument("--execute", action="store_true",
                    help="actually delete; without it this is a dry run")
    rt.set_defaults(func=cmd_retention)

    sh = sub.add_parser("show", help="one item in full")
    sh.add_argument("item_id", type=int)
    sh.set_defaults(func=cmd_show)

    pr = sub.add_parser("profile", help="the stored profile payload (read-only; "
                                        "what other tools can check first)")
    pr.add_argument("target", help="handle, profile URL or account id")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=cmd_profile)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # Resolved here rather than at import, so a caller that sets CORPUS_DB after
    # importing the module cannot silently end up writing to the real corpus.
    args.db = args.db or os.environ.get("CORPUS_DB") or DEFAULT_DB
    args.analysis_db = (args.analysis_db
                        or os.environ.get("CORPUS_ANALYSIS_DB")
                        or DEFAULT_ANALYSIS_DB)
    if getattr(args, "query", None) and isinstance(args.query, list):
        args.query = [" ".join(args.query)] if args.cmd == "find" else args.query
    # systemd stops a unit with SIGTERM, which Python ignores for cleanup purposes:
    # no finally ran, and every open scrape_run row stayed 'running' forever.
    with ops.sigterm_as_interrupt():
        code = _dispatch(args)
    if argv is None and code in (130, ops.EX_TERMINATED):
        _exit_now_if_workers_linger(code)
    return code


def _exit_now_if_workers_linger(code: int) -> None:
    """After an interrupt, do not wait for worker threads stuck in a network call.

    The run rows are already closed by then. But the interpreter joins every
    ThreadPoolExecutor worker at exit, so an enrich interrupted mid-transcription
    sat for up to the 300s whisper timeout before the process ended: under systemd
    that is a stop that turns into a SIGKILL, and at the terminal a Ctrl-C that does
    nothing. Only for the real CLI (argv is None); tests call main() with argv.
    """
    import threading
    if any(t.is_alive() and not t.daemon and t is not threading.main_thread()
           for t in threading.enumerate()):
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)


def _dispatch(args) -> int:
    try:
        if not needs_lock(args):
            return args.func(args) or 0
        try:
            with ops.run_lock(args.db, what=f"corpus {args.cmd}"):
                # Holding the lock means no other run is live, so any row still
                # 'running' from hours ago belongs to a process that died.
                n = _close_orphans(args.db)
                if n:
                    print(dim(f"marked {n} abandoned run(s) from a killed process "
                              f"as interrupted"))
                return args.func(args) or 0
        except ops.LockBusy as e:
            print(red(str(e)), file=sys.stderr)
            return ops.EX_TEMPFAIL
    except AmbiguousAccount as e:
        print(red(str(e)), file=sys.stderr)
        return 2
    except store.DamagedDatabase as e:
        print(red(str(e)), file=sys.stderr)
        return 1
    except ops.Terminated:
        print(dim("\nterminated"))
        return ops.EX_TERMINATED
    except KeyboardInterrupt:
        print(dim("\ninterrupted"))
        return 130


def _close_orphans(db) -> int:
    if not Path(db).expanduser().exists():
        return 0                  # never create a database just to sweep it
    import sqlite3
    try:
        conn = store.connect(db)
    except (sqlite3.OperationalError, store.DamagedDatabase):
        # housekeeping must never be why a run fails, nor stop `backup --restore`
        # from replacing a damaged database
        return 0
    try:
        return store.mark_orphaned_runs(conn)
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def needs_lock(args) -> bool:
    """Commands that write to the corpus (or spend money doing it) take the lock;
    dry runs and reads do not, so `health` or a brief works mid-sync."""
    cmd = args.cmd
    if cmd in ("sync", "enrich", "comments"):
        return not getattr(args, "dry_run", False)
    if cmd == "analyze":
        return bool(args.execute)
    if cmd == "backup":
        return not args.list
    if cmd in ("forget", "fix-paths"):
        return bool(args.execute)
    return False


if __name__ == "__main__":
    raise SystemExit(main())
