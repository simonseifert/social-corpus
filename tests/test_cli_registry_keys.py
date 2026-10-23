"""Registry, key loading, the Apify plan guard, and the CLI surface.

Offline by design. The plan guard is the one piece here that talks to a vendor in
production, so it is tested against recorded /v2/users/me shapes for both a free and
a paid account. The free-tier case is the expensive one to get wrong: some Store
actors bill 100x more per item there, so a guard that fails open turns a $3 job into
a $300 one silently.
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import cli, keys as keyfile, store  # noqa: E402
from corpus.adapters import REGISTRY, UNAVAILABLE, for_platform, get  # noqa: E402
from corpus.adapters.base import Adapter, FetchResult, Profile, RawItem  # noqa: E402

# Recorded from the live endpoint on 2026-08-05 against this repo's own token,
# trimmed to the fields the guard reads. plan.tier is the field actor price cards key
# on; plan.id is a marketing name that changes more often.
PAID_ME = {"data": {"username": "alex", "isPaying": True,
                    "plan": {"id": "STARTER", "tier": "BRONZE",
                             "monthlyUsageCreditsUsd": 29}}}
FREE_ME = {"data": {"username": "someone", "isPaying": False,
                    "plan": {"id": "FREE", "tier": "FREE",
                             "monthlyUsageCreditsUsd": 5}}}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_urlopen(monkeypatch, payload, *, calls=None):
    def fake(req, timeout=None):
        if calls is not None:
            calls.append(req.full_url)
        assert req.get_header("Authorization", "").startswith("Bearer "), \
            "token must ride in the header, never the query string"
        assert "token=" not in req.full_url
        return _Resp(json.dumps(payload).encode())
    monkeypatch.setattr(keyfile.urllib.request, "urlopen", fake)


# ── registry ────────────────────────────────────────────────────────────────

def test_every_adapter_is_registered():
    assert set(REGISTRY) == {"linkedin_posts", "linkedin_export", "linkedin_public",
                             "instagram_public", "instagram_client",
                             "instagram_free", "youtube_shorts", "linkedin_harvest",
                             "brightdata_linkedin", "brightdata_instagram",
                             "scrapecreators_instagram", "scrapecreators_tiktok",
                             "scrapecreators_facebook"}
    assert not UNAVAILABLE, f"adapters failed to import: {UNAVAILABLE}"


def test_for_platform_filters_by_declared_relationship():
    # The consented-export route must never be offered for a competitor: that is the
    # own/client vs competitor/influencer split schema.sql calls a legal path, not a
    # reporting convenience.
    names = {a.name for a in for_platform("linkedin", "competitor")}
    assert names == {"linkedin_posts", "linkedin_harvest", "brightdata_linkedin"}

    own = {a.name for a in for_platform("linkedin", "own")}
    assert own == {"linkedin_posts", "linkedin_export", "linkedin_harvest",
                   "brightdata_linkedin"}

    assert {a.name for a in for_platform("instagram", "competitor")} == {
        "instagram_public", "instagram_free", "scrapecreators_instagram",
        "brightdata_instagram"}
    assert "instagram_client" not in {a.name for a in for_platform("instagram", "client")}


def test_the_ig_session_route_is_opt_in(monkeypatch):
    """Private-API session cookies on a timer got an account flagged twice. The
    route must never be picked unless the operator switched it on."""
    monkeypatch.delenv("CORPUS_ALLOW_IG_SESSION", raising=False)
    for rel in ("own", "client"):
        names = [a.name for a in for_platform("instagram", rel)]
        assert "instagram_client" not in names and names[0] == "scrapecreators_instagram"
    assert "instagram_client" in {a.name for a in for_platform("instagram")}, \
        "still listed in the inventory view"
    monkeypatch.setenv("CORPUS_ALLOW_IG_SESSION", "1")
    assert for_platform("instagram", "own")[0].name == "instagram_client"
    monkeypatch.setenv("CORPUS_ALLOW_IG_SESSION", "yes")
    assert "instagram_client" not in {a.name for a in for_platform("instagram", "own")}


def test_for_platform_is_cheapest_first():
    routes = for_platform("linkedin", "own")
    assert routes[0].name == "linkedin_export"
    costs = [a.cost_per_1k or 0 for a in routes if not a.shallow]
    assert costs == sorted(costs)


def test_a_shallow_route_sorts_last_however_cheap_it_is():
    """Cost ordering assumes every route would eventually return the same data. A
    free route with a hard ceiling breaks that assumption, and ranking it on price
    alone would make [0] silently downgrade every backfill on its platform."""
    routes = for_platform("instagram", "competitor")
    assert routes[0].name == "scrapecreators_instagram", "the route that can finish the job"
    assert routes[-1].name == "instagram_free"
    assert routes[-1].cost_per_1k == 0.0, "and it really is the cheapest one"
    assert [a.shallow for a in routes] == sorted(a.shallow for a in routes)


def test_for_platform_without_a_relationship_returns_the_whole_platform():
    # Three, including linkedin_public, which declares NO relationships so that no
    # plan can auto-select it. Passing no relationship skips that filter entirely,
    # which is what makes this the inventory view rather than the routing view.
    assert len(for_platform("linkedin")) == 5
    assert "linkedin_public" not in {a.name for a in for_platform(
        "linkedin", "competitor")}, "the routing view still excludes it"


def test_unknown_platform_is_empty_not_an_error():
    assert for_platform("myspace", "competitor") == []


def test_get_names_what_is_available_when_asked_for_something_missing():
    with pytest.raises(KeyError) as e:
        get("tiktok_posts")
    assert "linkedin_posts" in str(e.value)


def test_a_broken_adapter_degrades_instead_of_killing_the_registry(monkeypatch, tmp_path):
    """Losing one platform is a degraded run; losing the CLI is a broken tool."""
    import importlib
    import corpus.adapters as pkg

    real_import = __import__

    def boom(name, *a, **kw):
        if name.endswith("linkedin_export"):
            raise ModuleNotFoundError("No module named 'openpyxl'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", boom)
    with pytest.warns(UserWarning, match="linkedin_export unavailable"):
        reloaded = importlib.reload(pkg)
    try:
        assert "linkedin_export" not in reloaded.REGISTRY
        assert "linkedin_posts" in reloaded.REGISTRY
        assert "openpyxl" in reloaded.UNAVAILABLE["linkedin_export"]
    finally:
        monkeypatch.undo()
        importlib.reload(pkg)


# ── keys ────────────────────────────────────────────────────────────────────

def test_keyfile_parsing_tolerates_comments_blanks_and_quotes(tmp_path):
    p = tmp_path / "keys.env"
    p.write_text(
        "# a comment\n"
        "\n"
        'APIFY_TOKEN="apify_api_xyz"\n'
        "GROQ_API_KEY='gsk_abc'\n"
        "  SPACED_KEY = value with spaces  \n"
        "NOT_A_PAIR\n"
        "EMPTY=\n"
    )
    k = keyfile.load(p)
    assert k["APIFY_TOKEN"] == "apify_api_xyz"
    assert k["GROQ_API_KEY"] == "gsk_abc"
    assert k["SPACED_KEY"] == "value with spaces"
    assert "NOT_A_PAIR" not in k
    assert "EMPTY" not in k


def test_a_value_containing_an_equals_sign_survives(tmp_path):
    # Base64 and JWT-ish secrets are full of '=' padding; splitting on every '='
    # would truncate them into a key that authenticates as nobody.
    p = tmp_path / "keys.env"
    p.write_text("TOKEN=abc==def=\n")
    assert keyfile.load(p)["TOKEN"] == "abc==def="


def test_environment_wins_over_the_file(tmp_path, monkeypatch):
    p = tmp_path / "keys.env"
    p.write_text("APIFY_TOKEN=from_file\n")
    monkeypatch.setenv("APIFY_TOKEN", "from_env")
    assert keyfile.load(p)["APIFY_TOKEN"] == "from_env"


def test_redacted_never_exposes_a_value():
    out = keyfile.redacted({"APIFY_TOKEN": "apify_api_supersecret"})
    assert "supersecret" not in json.dumps(out)
    assert out["APIFY_TOKEN"] == "set (21 chars)"


# ── the spend guard ─────────────────────────────────────────────────────────
# Price cards below are trimmed from live /v2/acts/<id> responses (2026-10-22). FLAT is
# how every actor corpus uses is priced today; HUNDRED is the August 2026 xquik case.

def _card(free, bronze):
    return {"data": {"pricingInfos": [{"pricingModel": "PAY_PER_EVENT", "pricingPerEvent": {
        "actorChargeEvents": {
            "apify-actor-start": {"eventPriceUsd": 0.00005},
            "post": {"eventTieredPricingUsd": {
                "FREE": {"tieredEventPriceUsd": free},
                "BRONZE": {"tieredEventPriceUsd": bronze}}}}}}]}}


FLAT, HUNDRED = _card(0.002, 0.002), _card(0.015, 0.00015)
ROOM = {"data": {"current": {"monthlyUsageUsd": 1.0}, "limits": {"maxMonthlyUsageUsd": 5}}}
FULL = {"data": {"current": {"monthlyUsageUsd": 4.99}, "limits": {"maxMonthlyUsageUsd": 5}}}


def _patch_apify(monkeypatch, me=PAID_ME, limits=ROOM, card=FLAT, calls=None):
    def fake(req, timeout=None):
        url = req.full_url
        if calls is not None:
            calls.append(url)
        assert req.get_header("Authorization", "").startswith("Bearer ")
        assert "token=" not in url
        body = (limits if url.endswith("/users/me/limits") else
                me if url.endswith("/users/me") else card)
        return _Resp(json.dumps(body).encode())
    monkeypatch.setattr(keyfile.urllib.request, "urlopen", fake)


def test_paid_plan_with_budget_is_allowed(monkeypatch):
    _patch_apify(monkeypatch)
    info = keyfile.assert_can_spend("tok", 500, est_usd=1.0, actors=("a~b",))
    assert info["tier"] == "BRONZE" and info["budget_left_usd"] == 4.0


def test_free_plan_is_allowed_when_the_actor_is_flat_priced(monkeypatch):
    """The old guard refused every free plan. By October 2026 that refused runs that
    cost exactly the same, so it now reads the price card instead."""
    _patch_apify(monkeypatch, me=FREE_ME)
    assert keyfile.assert_can_spend("tok", 500, est_usd=1.0, actors=("a~b",))


def test_a_tier_multiplier_is_refused(monkeypatch):
    _patch_apify(monkeypatch, me=FREE_ME, card=HUNDRED)
    with pytest.raises(keyfile.PlanGuardError) as e:
        keyfile.assert_can_spend("tok", 500, actors=("xquik~x-tweet-scraper",))
    assert "100x" in str(e.value) and str(keyfile.GUARD_THRESHOLD) in str(e.value)


def test_a_run_that_does_not_fit_the_budget_is_refused(monkeypatch):
    _patch_apify(monkeypatch, limits=FULL)
    with pytest.raises(keyfile.PlanGuardError, match="monthly budget"):
        keyfile.assert_can_spend("tok", 500, est_usd=0.50, actors=("a~b",))


def test_small_runs_skip_the_check_entirely(monkeypatch):
    calls = []
    _patch_apify(monkeypatch, me=FREE_ME, card=HUNDRED, calls=calls)
    assert keyfile.assert_can_spend("tok", keyfile.GUARD_THRESHOLD, actors=("x~y",)) is None
    assert calls == []


def test_the_threshold_is_exclusive(monkeypatch):
    calls = []
    _patch_apify(monkeypatch, calls=calls)
    keyfile.assert_can_spend("tok", keyfile.GUARD_THRESHOLD + 1)
    assert calls, "one over the threshold must check"


def test_an_unrecognised_plan_shape_fails_closed(monkeypatch):
    # No tier and no plan id reads as FREE; a card with no FREE price is unknown.
    _patch_apify(monkeypatch, me={"data": {"username": "x", "plan": {}}},
                 card={"data": {"pricingInfos": [{"pricingPerEvent": {"actorChargeEvents": {
                     "post": {"eventTieredPricingUsd": {
                         "BRONZE": {"tieredEventPriceUsd": 0.002}}}}}}]}})
    with pytest.raises(keyfile.PlanGuardError, match="unknown"):
        keyfile.assert_can_spend("tok", 500, actors=("a~b",))


def test_a_bad_token_says_so_instead_of_passing(monkeypatch):
    def fake(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
    monkeypatch.setattr(keyfile.urllib.request, "urlopen", fake)
    with pytest.raises(keyfile.PlanGuardError, match="APIFY_TOKEN"):
        keyfile.assert_can_spend("tok", 500)


def test_an_unreachable_apify_blocks_the_run(monkeypatch):
    def fake(req, timeout=None):
        raise TimeoutError("no route to host")
    monkeypatch.setattr(keyfile.urllib.request, "urlopen", fake)
    with pytest.raises(keyfile.PlanGuardError):
        keyfile.assert_can_spend("tok", 500)


# ── CLI ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "corpus.db")


def _run(argv, db, monkeypatch=None):
    return cli.main(["--db", db] + argv)


def test_add_detects_platform_and_prints_the_route(db, capsys):
    assert _run(["add", "https://www.linkedin.com/in/taylorbrooks",
                 "--relationship", "influencer"], db) == 0
    out = capsys.readouterr().out
    assert "linkedin:taylorbrooks" in out
    # HarvestAPI direct: measured $0.004 per 100-post page, the cheapest deep route.
    assert "linkedin_harvest" in out
    assert "$0.04/1k" in out
    assert "linkedin_posts" in out, "the Apify actor stays listed as a fallback"

    conn = store.connect(db)
    row = conn.execute("SELECT * FROM account").fetchone()
    assert (row["platform"], row["handle"], row["relationship"]) == (
        "linkedin", "taylorbrooks", "influencer")


def test_add_rejects_a_string_that_is_not_a_profile(db, capsys):
    assert _run(["add", "some random words", "--relationship", "competitor"], db) == 2
    assert "not a profile URL" in capsys.readouterr().out


def test_add_records_the_client_ref(db):
    _run(["add", "https://instagram.com/orbitlab", "--relationship", "client",
          "--client-ref", "example-media"], db)
    conn = store.connect(db)
    assert conn.execute("SELECT client_ref FROM account").fetchone()[0] == "example-media"


def test_dry_run_spends_nothing(db, monkeypatch, capsys):
    """The point of --dry-run is that it cannot cost money. Assert that
    mechanically: any HTTP call at all during the command fails the test."""
    _run(["add", "https://www.linkedin.com/in/taylorbrooks",
          "--relationship", "influencer"], db)

    import urllib.request as ur

    def forbidden(*a, **kw):
        raise AssertionError("--dry-run made a network call")
    monkeypatch.setattr(ur, "urlopen", forbidden)
    monkeypatch.setattr(keyfile.urllib.request, "urlopen", forbidden)
    # Pinned keys: the estimate prices the route that would run, which depends on
    # which keys exist. Apify only here; HarvestAPI in the test below.
    monkeypatch.setattr(cli.keys_mod, "load", lambda *a, **kw: {"APIFY_TOKEN": "tok"})

    assert _run(["sync", "taylorbrooks", "--limit", "3", "--dry-run"], db) == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "$0.0060" in out          # 3 items x $2.00/1k
    assert "Nothing spent" in out

    conn = store.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM scrape_run").fetchone()[0] == 0


def test_dry_run_prices_a_per_page_route_by_the_page(db, monkeypatch, capsys):
    """HarvestAPI bills $0.004 per call whether the page holds 3 posts or 100. The
    per-item formula said $0.0001 for a 3-post delta, 40x under the real bill."""
    _run(["add", "https://www.linkedin.com/in/taylorbrooks",
          "--relationship", "influencer"], db)
    monkeypatch.setattr(cli.keys_mod, "load", lambda *a, **kw: {
        "APIFY_TOKEN": "tok", "HARVESTAPI_KEY": "h"})
    assert _run(["sync", "taylorbrooks", "--limit", "3", "--dry-run"], db) == 0
    assert "est $0.0040" in capsys.readouterr().out


def test_sync_asks_a_non_apify_head_route_for_its_own_spend_check(db, monkeypatch,
                                                                 capsys):
    """No maxTotalChargeUsd outside Apify: the route's own check (HarvestAPI: the
    remaining balance) must run before a large sync, and its refusal must stop it."""
    from corpus.adapters import linkedin_harvest as H
    _run(["add", "https://www.linkedin.com/in/taylorbrooks",
          "--relationship", "influencer"], db)
    monkeypatch.setattr(cli.keys_mod, "load", lambda *a, **kw: {"HARVESTAPI_KEY": "h"})
    monkeypatch.setattr(H, "account", lambda key: {"usage": {"balance": 0.001}})
    monkeypatch.setattr(cli.sync_mod, "fetch_only", lambda *a, **kw: pytest.fail(
        "a fetch was launched despite the refusal"))
    assert _run(["sync", "taylorbrooks", "--limit", "500"], db) == 3
    assert "HarvestAPI balance" in capsys.readouterr().out


def test_sync_refuses_a_large_overpriced_apify_run(db, monkeypatch, capsys):
    """The guard has to be wired into the command, not merely unit-tested.

    A unit-tested guard nobody calls is the exact failure this is here to prevent, so
    this drives it through cmd_sync and asserts no fetch happened.
    """
    _run(["add", "https://www.linkedin.com/in/taylorbrooks",
          "--relationship", "influencer"], db)

    monkeypatch.setattr(cli.keys_mod, "load", lambda *a, **kw: {"APIFY_TOKEN": "tok"})
    _patch_apify(monkeypatch, me=FREE_ME, card=HUNDRED)
    monkeypatch.setattr(cli.sync_mod, "fetch_only", lambda *a, **kw: pytest.fail(
        "a fetch was launched despite the refusal"))

    assert _run(["sync", "taylorbrooks", "--limit", "60"], db) == 3
    assert "refusing to run" in capsys.readouterr().out


def test_the_guard_covers_a_free_route_that_can_fall_through_to_a_paid_one(
        db, monkeypatch, capsys):
    """A cookie-route sync still gets tier-checked, and that is correct.

    cmd_sync falls through to the next route when one fails, so an own-account
    Instagram sync whose cookies have expired lands on the paid public actor. The
    unguarded version of that is the exact silent 100x: the user asked for the free
    route and got billed for 200 items on a free tier.
    """
    _run(["add", "https://instagram.com/myown", "--relationship", "own"], db)
    monkeypatch.setattr(cli.keys_mod, "load", lambda *a, **kw: {
        "APIFY_TOKEN": "tok", "IG_SESSIONID": "s"})
    _patch_apify(monkeypatch, me=FREE_ME, card=HUNDRED)
    monkeypatch.setattr(cli.sync_mod, "fetch_only", lambda *a, **kw: pytest.fail(
        "a fetch was launched despite the refusal"))

    assert _run(["sync", "myown", "--limit", "200"], db) == 3


def test_a_plan_with_no_reachable_apify_route_skips_the_tier_check(db, monkeypatch):
    # The trigger is a reachable paid route, not the mere presence of a token. When
    # the Apify adapters are unavailable (a failed import) every route in the plan is
    # free, and a tier check there can only produce a false refusal on a run that
    # would have cost nothing.
    _run(["add", "https://www.linkedin.com/in/alex", "--relationship", "own"], db)
    export_only = [a for a in REGISTRY.values() if a.name == "linkedin_export"]
    monkeypatch.setattr(cli, "routes_for", lambda *a, **kw: export_only)
    monkeypatch.setattr(cli.keys_mod, "load", lambda *a, **kw: {"APIFY_TOKEN": "tok"})
    monkeypatch.setattr(keyfile, "assert_can_spend", lambda *a, **kw: pytest.fail(
        "checked the Apify tier for a run with no Apify route in it"))
    # Stub the network half only. Everything downstream of it -- the job pool, the
    # persist, the run bookkeeping -- stays live.
    monkeypatch.setattr(cli.sync_mod, "fetch_only",
                        lambda adapter, *a, **kw: cli.sync_mod.Attempt(
                            adapter, result=FetchResult()))

    assert _run(["sync", "alex", "--limit", "200"], db) == 0


def test_sync_refuses_an_unknown_handle(db, capsys):
    assert _run(["sync", "nobody-here"], db) == 2
    assert "no account" in capsys.readouterr().out


def test_search_and_show_read_what_sync_wrote(db, capsys):
    conn = store.connect(db)
    aid = store.upsert_account(conn, platform="linkedin", handle="taylorbrooks",
                               relationship="influencer", url="https://x")
    item_id, _, _ = store.upsert_item(
        conn, account_id=aid, platform="linkedin", platform_item_id="7489894675928027136",
        kind="text", published_at=1785730046, raw={"vendor": "payload"},
        body="I'll raise a glass for lower emissions.",
        url="https://www.linkedin.com/feed/update/urn:li:activity:7489894675928027136",
        source="linkedin_posts")
    store.record_metrics(conn, item_id, reactions=1077, comments=144, shares=51)
    conn.commit()

    assert _run(["search", "emissions"], db) == 0
    out = capsys.readouterr().out
    assert "taylorbrooks" in out and "emissions" in out

    assert _run(["show", str(item_id)], db) == 0
    shown = capsys.readouterr().out
    assert "1077 reactions" in shown and "144 comments" in shown

    assert _run(["search", "nothingmatchesthis"], db) == 1
    assert _run(["show", "9999"], db) == 2


def test_health_judges_each_account_and_flags_stale(db, capsys, monkeypatch):
    """Per account, against its own TTL: one fresh account used to keep the whole
    adapter green while another behind it had not synced in weeks."""
    from corpus import backup, health
    monkeypatch.setattr(health, "transcription", lambda **kw: [])
    conn = store.connect(db)
    a = store.upsert_account(conn, platform="linkedin", handle="a", relationship="own")
    b = store.upsert_account(conn, platform="linkedin", handle="b",
                             relationship="competitor")
    for aid in (a, b):
        r = store.start_run(conn, adapter="linkedin_posts", mode="delta", account_id=aid)
        store.finish_run(conn, r, status="ok", seen=3, new=3)
    conn.execute("UPDATE scrape_run SET finished_at=? WHERE account_id=?",
                 (store.now() - 40 * 86400, b))
    conn.commit()
    backup.run([db], Path(db).parent / "backups")

    assert _run(["health"], db) == 1, "a stale account is a red line"
    out = capsys.readouterr().out
    assert "linkedin/a" in out and "linkedin/b" in out
    assert "STALE" in out and out.count("STALE") == 1


def test_adapters_command_shows_cost_and_relationships(db, capsys):
    assert _run(["adapters"], db) == 0
    out = capsys.readouterr().out
    for name in REGISTRY:
        assert name in out
    assert "free" in out and "$2.00" in out


def test_corpus_db_env_var_is_read_at_call_time(tmp_path, monkeypatch, capsys):
    # Reading it at import would mean a wrapper that sets CORPUS_DB later writes
    # into ~/.corpus/corpus.db instead: the real corpus, silently.
    target = tmp_path / "from-env.db"
    monkeypatch.setenv("CORPUS_DB", str(target))
    assert cli.main(["add", "https://instagram.com/orbitlab",
                     "--relationship", "competitor"]) == 0
    assert target.exists()


# ── profile: the read-only contract other tools check before paying ──────

def test_profile_reports_an_untracked_target_as_such(db, capsys):
    assert _run(["profile", "https://www.linkedin.com/in/nobody", "--json"], db) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "not_tracked"


def test_profile_returns_the_stored_payload_by_url_or_handle(db, capsys):
    conn = store.connect(db)
    store.upsert_account(conn, platform="linkedin", handle="taylorbrooks",
                         relationship="influencer",
                         url="https://www.linkedin.com/in/taylorbrooks/",
                         raw={"publicIdentifier": "taylorbrooks",
                              "experience": [{"position": "Co-chair"}]})
    conn.commit()
    for target in ("taylorbrooks", "https://www.linkedin.com/in/TaylorBrooks"):
        assert _run(["profile", target, "--json"], db) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "found" and out["complete"] is True
        assert out["profile"]["experience"][0]["position"] == "Co-chair"


def test_profile_flags_an_author_stub_as_incomplete(db, capsys):
    """A posts-only sync holds name/avatar/headline. Handing that to another tool as a
    full profile would make an empty career history look like a real finding."""
    conn = store.connect(db)
    store.upsert_account(conn, platform="linkedin", handle="stub",
                         relationship="competitor",
                         raw={"name": "Stub Person", "headline": "CEO"})
    conn.commit()
    assert _run(["profile", "stub", "--json"], db) == 0
    assert json.loads(capsys.readouterr().out)["complete"] is False


def test_profile_never_touches_the_network(db, monkeypatch, capsys):
    import urllib.request as ur

    def forbidden(*a, **kw):
        raise AssertionError("profile made a network call")
    monkeypatch.setattr(ur, "urlopen", forbidden)
    _run(["profile", "anyone", "--json"], db)


def test_probes_count_at_their_worst_case(db, monkeypatch):
    """A probe bills one item but fetches --limit when it finds something, so forty
    due probes are 40 x (1 + limit) items, not 40."""
    conn = store.connect(db)
    for i in range(40):
        store.upsert_account(conn, platform="linkedin", handle=f"p{i}",
                             relationship="competitor",
                             url=f"https://www.linkedin.com/in/p{i}/")
    conn.commit()
    from corpus import freshness
    monkeypatch.setattr(cli.freshness, "plan", lambda *a, **kw: [
        freshness.Decision(i + 1, f"p{i}", "linkedin", "probe", "ttl", 1) for i in range(40)])
    monkeypatch.setattr(cli.keys_mod, "load", lambda *a, **kw: {"APIFY_TOKEN": "tok"})
    seen = {}

    def guard(token, items, **kw):
        seen["items"] = items
        raise keyfile.PlanGuardError("stop here")
    monkeypatch.setattr(keyfile, "assert_can_spend", guard)
    assert _run(["sync", "--limit", "50"], db) == 3
    assert seen["items"] == 40 * 51


def test_comments_are_guarded_before_any_spend(db, monkeypatch, capsys):
    conn = store.connect(db)
    aid = store.upsert_account(conn, platform="linkedin", handle="busy",
                               relationship="influencer")
    iid, _, _ = store.upsert_item(conn, account_id=aid, platform="linkedin",
                                  platform_item_id="p1", kind="text", published_at=1,
                                  url="https://www.linkedin.com/posts/x", raw={})
    store.record_metrics(conn, iid, comments=40)
    conn.commit()
    monkeypatch.setattr(cli.keys_mod, "load", lambda *a, **kw: {"APIFY_TOKEN": "tok"})
    _patch_apify(monkeypatch, limits=FULL)
    from corpus import comments as cm
    monkeypatch.setattr(cm, "_call", lambda *a, **kw: pytest.fail(
        "comments were fetched past the guard"))
    assert _run(["comments", "busy", "--per-post", "60"], db) == 3
    assert "monthly budget" in capsys.readouterr().out


def test_a_price_rise_past_the_cap_headroom_is_refused(monkeypatch):
    """Spend caps are sized from corpus's own price. If the vendor doubles it, the
    cap stops the run early, Apify still says SUCCEEDED, and a half-fetched account
    reads as complete. Nothing in the response says so, so refuse up front."""
    _patch_apify(monkeypatch, card=_card(0.004, 0.004))
    with pytest.raises(keyfile.PlanGuardError) as e:
        keyfile.assert_can_spend("tok", 500, actors=("a~b",), prices={"a~b": 0.002})
    assert "0.004" in str(e.value) and "0.002" in str(e.value)
    # The start fee is per run, not per item, and is not mistaken for the price.
    _patch_apify(monkeypatch, card=_card(0.002, 0.002))
    assert keyfile.assert_can_spend("tok", 500, actors=("a~b",), prices={"a~b": 0.002})


def test_dry_run_prices_the_route_a_real_run_would_take(tmp_path, monkeypatch, capsys):
    """Without keys the dry run fell back to the keyless route and quoted $0 for a
    sync that would really bill the paid one."""
    from corpus import cli
    db = tmp_path / "c.db"
    monkeypatch.setattr(cli.keys_mod, "load", lambda *a, **k: {"APIFY_TOKEN": "t"})
    assert cli.main(["--db", str(db), "add", "https://www.instagram.com/somebiz/",
                     "--relationship", "competitor"]) == 0
    assert cli.main(["--db", str(db), "sync", "somebiz", "--dry-run"]) == 0
    out = capsys.readouterr().out
    import re
    est = float(re.search(r"est \$([0-9]+\.[0-9]+)", out).group(1))
    assert est > 0, out


def test_a_handle_on_two_platforms_must_be_named_exactly(tmp_path, capsys):
    from corpus import cli
    db = str(tmp_path / "c.db")
    for url in ("https://www.instagram.com/bistroharbor/",
                "https://www.facebook.com/bistroharbor"):
        assert cli.main(["--db", db, "add", url, "--relationship", "competitor"]) == 0
    capsys.readouterr()
    assert cli.main(["--db", db, "sync", "bistroharbor", "--dry-run"]) == 2
    err = capsys.readouterr().err
    assert "instagram:bistroharbor" in err and "facebook:bistroharbor" in err
    assert cli.main(["--db", db, "sync", "instagram:bistroharbor", "--dry-run"]) == 0


def test_the_item_price_is_the_primary_event_not_the_dearest(monkeypatch):
    """apidojo's card lists a $0.025 location-query that corpus never triggers. Read
    as the per-post price it refused every Instagram run as a 50x rise (field test,
    2026-10-23). The vendor flags the per-result event as primary; that is the price."""
    card = {"data": {"pricingInfos": [{"pricingPerEvent": {"actorChargeEvents": {
        "dataset-item": {"isPrimaryEvent": True, "eventTieredPricingUsd": {
            "FREE": {"tieredEventPriceUsd": 0.0005},
            "BRONZE": {"tieredEventPriceUsd": 0.0005}}},
        "location-query": {"eventTieredPricingUsd": {
            "FREE": {"tieredEventPriceUsd": 0.025},
            "BRONZE": {"tieredEventPriceUsd": 0.025}}}}}}]}}
    _patch_apify(monkeypatch, card=card)
    assert keyfile.assert_can_spend("tok", 500, actors=("a~b",), prices={"a~b": 0.0005})


def test_a_refused_apify_fallthrough_does_not_stop_a_scrapecreators_sync(
        db, monkeypatch, capsys):
    """Apify was blocked (unpaid invoice) while ScrapeCreators, the head Instagram
    route, was fine: the guard refused the whole run over a route that would never
    have been used. Now Apify is dropped from the run and the head route runs."""
    _run(["add", "https://www.instagram.com/somebiz/", "--relationship", "competitor"], db)
    monkeypatch.setattr(cli.keys_mod, "load", lambda *a, **kw: {
        "APIFY_TOKEN": "tok", "SCRAPECREATORS_API_KEY": "sc"})

    def guard(*a, **kw):
        raise keyfile.PlanGuardError("monthly budget exhausted")
    monkeypatch.setattr(keyfile, "assert_can_spend", guard)
    seen = []

    def fetch(adapter, *a, **kw):
        seen.append(adapter.name)
        return cli.sync_mod.Attempt(adapter, result=FetchResult())
    monkeypatch.setattr(cli.sync_mod, "fetch_only", fetch)
    real_run_jobs = cli.sync_mod.run_jobs
    routes = []

    def run_jobs(conn, jobs, **kw):
        routes.extend(a.name for j in jobs for a in j.routes)
        return real_run_jobs(conn, jobs, **kw)
    monkeypatch.setattr(cli.sync_mod, "run_jobs", run_jobs)
    assert _run(["sync", "somebiz", "--limit", "60"], db) == 0
    assert seen == ["scrapecreators_instagram"]
    assert routes and not any(r in ("instagram_public",) for r in routes), routes
    assert "Apify fall-through disabled" in capsys.readouterr().out


def test_a_shared_handle_is_printed_with_its_platform(tmp_path, monkeypatch, capsys):
    """Two identical "bistroharbor: no new posts" lines in a sync could not be told
    apart, and `add` suggested `corpus sync bistroharbor`, which then refused."""
    db = str(tmp_path / "c.db")
    monkeypatch.setattr(cli.keys_mod, "load", lambda *a, **kw: {
        "SCRAPECREATORS_API_KEY": "sc"})
    cli.main(["--db", db, "add", "https://www.instagram.com/bistroharbor/",
              "--relationship", "client"])
    cli.main(["--db", db, "add", "https://www.facebook.com/bistroharbor",
              "--relationship", "client"])
    out = capsys.readouterr().out
    assert "next: corpus sync facebook:bistroharbor" in out
    assert cli.main(["--db", db, "sync", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "facebook:bistroharbor: via" in out and "instagram:bistroharbor: via" in out


def test_re_adding_with_another_relationship_says_so(db, capsys):
    """`add @BistroHarbor --relationship competitor` silently turned a client into
    a competitor, whose posts the analysis may then send to public providers."""
    _run(["add", "https://www.instagram.com/bistroharbor/", "--relationship",
          "client"], db)
    capsys.readouterr()
    _run(["add", "https://www.instagram.com/BistroHarbor/", "--relationship",
          "competitor"], db)
    out = capsys.readouterr().out
    assert "client -> competitor" in out and "PUBLIC" in out
    conn = store.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM account").fetchone()[0] == 1


def test_forget_takes_platform_handle_and_names_what_it_erased(tmp_path, capsys):
    db = str(tmp_path / "c.db")
    A = ["--db", db, "--analysis-db", str(tmp_path / "a.db")]
    for url in ("https://www.instagram.com/twin/", "https://www.facebook.com/twin"):
        cli.main([*A, "add", url, "--relationship", "competitor"])
    capsys.readouterr()
    assert cli.main([*A, "forget", "twin"]) == 0
    assert "ALL of them" in capsys.readouterr().out
    assert cli.main([*A, "forget", "facebook:twin", "--execute"]) == 0
    out = capsys.readouterr().out
    assert "erased facebook:twin," in out and str(tmp_path / "backups") in out
    conn = store.connect(db)
    assert [r[0] for r in conn.execute("SELECT platform FROM account")] == ["instagram"]


def test_the_stale_warning_names_a_command_that_runs(tmp_path, capsys, monkeypatch):
    db = str(tmp_path / "c.db")
    A = ["--db", db, "--analysis-db", str(tmp_path / "a.db")]
    for url in ("https://www.instagram.com/twin/", "https://www.facebook.com/twin"):
        cli.main([*A, "add", url, "--relationship", "client"])
    conn = store.connect(db)
    conn.execute("UPDATE account SET last_scraped_at=?", (store.now() - 30 * 86400,))
    conn.commit()
    monkeypatch.setattr(cli, "DEFAULT_BRIEFS", str(tmp_path))
    capsys.readouterr()
    cli.main([*A, "brief", "instagram:twin"])
    assert "`corpus sync instagram:twin` first" in capsys.readouterr().out


def test_show_says_n_a_for_a_metric_the_platform_never_reports(db, capsys):
    conn = store.connect(db)
    aid = store.upsert_account(conn, platform="instagram", handle="x",
                               relationship="competitor")
    iid, _, _ = store.upsert_item(conn, account_id=aid, platform="instagram",
                                  platform_item_id="p", kind="image", raw={},
                                  published_at=1700000000, body="b")
    store.record_metrics(conn, iid, reactions=18, comments=2)
    conn.commit()
    capsys.readouterr()
    assert _run(["show", str(iid)], db) == 0
    out = capsys.readouterr().out
    assert "n/a shares" in out and "0 shares" not in out
