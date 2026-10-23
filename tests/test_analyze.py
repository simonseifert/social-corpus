"""Analysis-layer tests.

All offline: the LLM call is stubbed so these assert the layer's contract (toggle,
idempotency, cost estimation, prompt-version invalidation) rather than model output.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import store  # noqa: E402
from corpus.analyze import run as ar  # noqa: E402
from corpus.analyze import templates as T  # noqa: E402


@pytest.fixture
def tpl_dir(tmp_path):
    d = tmp_path / "templates"
    d.mkdir()
    (d / "demo.md").write_text(
        "---\nname: Demo\ndescription: d\nmodel: openai/gpt-oss-120b\n"
        "temperature: 0.3\nmax_tokens: 460\n---\n"
        "Analyse this.\nAuthor: {{author}}\nCaption: {{caption}}\n"
    )
    return d


@pytest.fixture
def seeded(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    aid = store.upsert_account(conn, platform="linkedin", handle="bob",
                               relationship="influencer", display_name="Bob")
    for i in range(3):
        store.upsert_item(conn, account_id=aid, platform="linkedin",
                          platform_item_id=f"p{i}", kind="text",
                          published_at=1700000000 + i, body=f"post {i}", raw={})
    conn.commit()
    return conn, aid


@pytest.fixture
def stub_llm(monkeypatch):
    calls = []

    def fake_chat(messages, **kw):
        calls.append(messages[0]["content"])
        return "stub analysis output"

    monkeypatch.setattr(ar.llm, "chat", fake_chat)
    monkeypatch.setattr(ar.llm, "active", lambda: {"name": "groq", "model": "stub"})
    monkeypatch.setattr(ar.providers, "available",
                        lambda: [{"name": "groq", "model": "stub"}])
    return calls


# ── templates ───────────────────────────────────────────────────────────────

def test_max_tokens_is_floored(tpl_dir):
    """460 is below what a reasoning model needs before it emits anything."""
    t = T.load("demo", tpl_dir)
    assert t.max_tokens == T.MIN_MAX_TOKENS


def test_prompt_hash_changes_with_body(tpl_dir):
    before = T.load("demo", tpl_dir).prompt_hash
    p = tpl_dir / "demo.md"
    p.write_text(p.read_text().replace("Analyse this.", "Analyse this differently."))
    assert T.load("demo", tpl_dir).prompt_hash != before


def test_prompt_hash_changes_with_temperature(tpl_dir):
    """Same text at a different temperature is a different experiment."""
    before = T.load("demo", tpl_dir).prompt_hash
    p = tpl_dir / "demo.md"
    p.write_text(p.read_text().replace("temperature: 0.3", "temperature: 0.9"))
    assert T.load("demo", tpl_dir).prompt_hash != before


def test_unknown_vars_render_empty_not_literal(tpl_dir):
    t = T.load("demo", tpl_dir)
    out = T.render(t, {"author": "Bob"})
    assert "{{caption}}" not in out and "Bob" in out


def test_text_post_context_suppresses_video_framing():
    ctx = T.context_block(platform="linkedin", kind="text", author="Bob",
                          posted="2026-01-01", metrics={})
    assert "not video" in ctx
    assert "first three seconds" in ctx


def test_video_context_does_not_suppress():
    ctx = T.context_block(platform="instagram", kind="short_video", author="Bob",
                          posted="2026-01-01", metrics={"views": 10})
    assert "not video" not in ctx
    assert "views=10" in ctx


def test_ensure_json_word():
    assert "json" in T.ensure_json_word("do a thing").lower()
    once = T.ensure_json_word("reply in json")
    assert once == "reply in json", "must not append when already present"


# ── the pass ────────────────────────────────────────────────────────────────

def test_dry_run_is_the_default_and_spends_nothing(seeded, tpl_dir, stub_llm):
    conn, _ = seeded
    st = ar.run(conn, "demo", db_path=":memory:" if False else
                str(Path(conn.execute("PRAGMA database_list").fetchall()[0][2]).parent
                    / "a.db"),
                template_dir=tpl_dir)
    assert st.considered == 3 and st.done == 0
    assert st.est_cost_usd > 0
    assert stub_llm == [], "dry run must not call the model"


def test_real_run_then_idempotent(seeded, tpl_dir, stub_llm, tmp_path):
    conn, _ = seeded
    db = str(tmp_path / "a.db")
    st = ar.run(conn, "demo", db_path=db, template_dir=tpl_dir, dry_run=False)
    assert (st.done, st.failed) == (3, 0)
    assert len(stub_llm) == 3

    st2 = ar.run(conn, "demo", db_path=db, template_dir=tpl_dir, dry_run=False)
    assert (st2.done, st2.skipped) == (0, 3)
    assert len(stub_llm) == 3, "no new model calls on a repeat run"


def test_editing_the_prompt_reruns_the_work(seeded, tpl_dir, stub_llm, tmp_path):
    conn, _ = seeded
    db = str(tmp_path / "a.db")
    ar.run(conn, "demo", db_path=db, template_dir=tpl_dir, dry_run=False)

    p = tpl_dir / "demo.md"
    p.write_text(p.read_text().replace("Analyse this.", "Analyse this MUCH better."))

    st = ar.run(conn, "demo", db_path=db, template_dir=tpl_dir, dry_run=False)
    assert st.done == 3, "a changed prompt must invalidate cached results"

    n = conn.execute("SELECT COUNT(*) c FROM a.analysis_result").fetchone()["c"]
    assert n == 6, "old results are kept alongside, not overwritten"


def test_changed_item_content_reruns(seeded, tpl_dir, stub_llm, tmp_path):
    conn, aid = seeded
    db = str(tmp_path / "a.db")
    ar.run(conn, "demo", db_path=db, template_dir=tpl_dir, dry_run=False)

    conn.execute("UPDATE item SET body='edited body' WHERE platform_item_id='p0'")
    conn.commit()
    st = ar.run(conn, "demo", db_path=db, template_dir=tpl_dir, dry_run=False)
    assert st.done == 1 and st.skipped == 2


def test_selector_filters_by_relationship(seeded, tpl_dir, stub_llm, tmp_path,
                                          monkeypatch):
    conn, _ = seeded
    # A client item routes to the private chain, so one has to exist.
    monkeypatch.setattr(ar.providers, "private_chain",
                        lambda: [{"name": "ollama", "model": "stub"}])
    other = store.upsert_account(conn, platform="linkedin", handle="client1",
                                 relationship="client")
    store.upsert_item(conn, account_id=other, platform="linkedin",
                      platform_item_id="c1", kind="text",
                      published_at=1700000009, body="client post", raw={})
    conn.commit()

    st = ar.run(conn, "demo", db_path=str(tmp_path / "a.db"), template_dir=tpl_dir,
                dry_run=False, relationship="client")
    assert st.considered == 1 and st.done == 1


def test_no_provider_configured_is_reported_not_raised(seeded, tpl_dir, monkeypatch,
                                                       tmp_path):
    conn, _ = seeded
    monkeypatch.setattr(ar.providers, "available", lambda: [])
    st = ar.run(conn, "demo", db_path=str(tmp_path / "a.db"), template_dir=tpl_dir)
    assert st.done == 0 and any("no LLM provider" in e for e in st.errors)


def test_llm_failure_is_counted_not_fatal(seeded, tpl_dir, monkeypatch, tmp_path):
    conn, _ = seeded

    def boom(messages, **kw):
        raise ar.llm.LLMError("provider exploded")

    monkeypatch.setattr(ar.llm, "chat", boom)
    monkeypatch.setattr(ar.providers, "available", lambda: [{"name": "groq"}])
    st = ar.run(conn, "demo", db_path=str(tmp_path / "a.db"), template_dir=tpl_dir,
                dry_run=False)
    assert st.failed == 3 and st.done == 0


def test_analysis_db_is_separate_and_droppable(seeded, tpl_dir, stub_llm, tmp_path):
    """The toggle is structural: deleting analysis.db must not touch the corpus."""
    conn, _ = seeded
    db = tmp_path / "a.db"
    ar.run(conn, "demo", db_path=str(db), template_dir=tpl_dir, dry_run=False)
    assert db.exists()

    items_before = conn.execute("SELECT COUNT(*) c FROM item").fetchone()["c"]
    ar.detach(conn)
    db.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)

    assert conn.execute("SELECT COUNT(*) c FROM item").fetchone()["c"] == items_before


def test_prune_removes_orphans(seeded, tpl_dir, stub_llm, tmp_path):
    """No cross-database FKs exist, so orphans need an explicit sweep."""
    conn, _ = seeded
    db = str(tmp_path / "a.db")
    ar.run(conn, "demo", db_path=db, template_dir=tpl_dir, dry_run=False)

    conn.execute("DELETE FROM item WHERE platform_item_id='p0'")
    conn.commit()
    assert ar.prune(conn, db_path=db) == 1
    assert ar.prune(conn, db_path=db) == 0


def test_cost_is_recorded_per_result(seeded, tpl_dir, stub_llm, tmp_path):
    conn, _ = seeded
    ar.run(conn, "demo", db_path=str(tmp_path / "a.db"), template_dir=tpl_dir,
           dry_run=False)
    row = conn.execute(
        "SELECT COUNT(*) n, SUM(cost_usd) c, SUM(tokens_in) ti FROM a.analysis_result"
    ).fetchone()
    assert row["n"] == 3 and row["c"] > 0 and row["ti"] > 0


# ── provider 429 classification ─────────────────────────────────────────────
# Real 429 bodies captured from Groq and Gemini on 2026-08-05. Misclassifying a
# per-minute limit as a spent budget retires a working provider for the whole run.

from corpus import llm as _llm  # noqa: E402

GROQ_TPM = ("Request too large for model `openai/gpt-oss-120b` in organization "
            "`org_x` service tier `on_demand` on tokens per minute (TPM): "
            "Limit 8000, Requested 9572, please reduce your message size and try "
            "again. Need more tokens? Upgrade to Dev Tier today")
GROQ_RATE = ("Rate limit reached for model `openai/gpt-oss-120b` on tokens per "
             "minute (TPM): Limit 8000, Used 7900, Requested 900. "
             "Please try again in 26.73s.")
GEMINI_DEPLETED = ('{"error":{"code":429,"message":"Your prepayment credits are '
                   'depleted. Please go to AI Studio to manage your project and '
                   'billing.","status":"RESOURCE_EXHAUSTED"}}')
GROQ_DAILY = ("Rate limit reached for model in organization on requests per day "
              "(RPD): Limit 1000, Used 1000. Please try again in 4h32m.")


def test_per_minute_limit_is_not_exhaustion():
    assert _llm._is_exhausted(GROQ_TPM) is False
    assert _llm._is_exhausted(GROQ_RATE) is False


def test_depleted_credits_is_exhaustion():
    assert _llm._is_exhausted(GEMINI_DEPLETED) is True


def test_per_day_limit_is_exhaustion():
    assert _llm._is_exhausted(GROQ_DAILY) is True


def test_groq_tpm_matches_the_advertised_limit():
    """Pacing to a wrong TPM causes the 429 storm the bucket exists to prevent."""
    from corpus import providers
    assert providers.PROVIDERS["groq"]["tpm"] == 8_000


def test_long_retry_after_fails_the_item_not_the_provider(monkeypatch):
    """A full per-minute window must not retire a provider with daily budget left.

    Regression: an analyse run lost 35 of 39 items to this while the Groq account
    still had 863 of 1000 daily requests available.
    """
    import urllib.error
    from corpus import llm

    llm._state.chain = None
    llm._state.idx = 0
    monkeypatch.setattr(llm.config, "available",
                        lambda: [{"name": "groq", "url": "http://x", "model": "m",
                                  "key_env": "GROQ_API_KEY", "tpm": 8000,
                                  "ctx": 128000}])
    monkeypatch.setattr(llm.config, "key", lambda *a, **k: "k")

    body = (b'Rate limit reached on tokens per minute (TPM): Limit 8000, '
            b'Used 8000. Please try again in 11m31s.')

    import io

    def boom(req, timeout=None):
        # fp is what HTTPError.read() drains, so the 429 body arrives intact
        raise urllib.error.HTTPError("http://x", 429, "Too Many Requests", {},
                                     io.BytesIO(body))

    monkeypatch.setattr(llm.urllib.request, "urlopen", boom)

    with pytest.raises(llm.LLMError) as ei:
        llm.chat([{"role": "user", "content": "hi"}], retries=1)
    assert "per-minute" in str(ei.value)
    assert llm._state.idx == 0, "the provider must NOT have been retired"
    llm._state.chain = None
    llm._state.idx = 0


def test_every_shipped_template_receives_the_content():
    """A lens with no {{body}} gets metadata only and says so at length.

    Regression: three hand-written lenses shipped with instructions but no variable
    placeholders, so the model replied "the brief gives metadata, not the caption"
    for 39 items across two profiles before anyone noticed.
    """
    from corpus.analyze import templates as TT
    for slug, tpl in TT.load_all().items():
        rendered = TT.render(tpl, {
            "author": "A", "platform": "linkedin", "posted": "2026-01-01",
            "body": "SENTINEL_BODY", "caption": "SENTINEL_BODY",
            "transcript": "SENTINEL_TRANSCRIPT", "format": "text",
            "collection": "own", "title": "", "url": "", "pillars": "",
        })
        assert "SENTINEL_BODY" in rendered, \
            f"template {slug!r} never receives the post text"


# Captured live on 2026-08-06 during a concurrent Groq run, NOT trimmed. The upsell
# URL at the end is the whole point: the recorded fixture above had it cut off, so
# the classifier looked correct while the real body retired a working provider on a
# four-second throttle. Wider concurrency makes per-minute 429s routine, which is
# what turned a latent bug into a run-killer.
GROQ_TPM_WITH_UPSELL_URL = (
    '{"error":{"message":"Rate limit reached for model `openai/gpt-oss-120b` in '
    'organization `org_01kwhq31eqe3dbsg5zcy552tw0` service tier `on_demand` on '
    'tokens per minute (TPM): Limit 8000, Used 7287, Requested 1241. Please try '
    'again in 3.96s. Need more tokens? Upgrade to Dev Tier today at '
    'https://console.groq.com/settings/billing","type":"tokens",'
    '"code":"rate_limit_exceeded"}}')


def test_an_upsell_url_does_not_read_as_a_billing_failure():
    assert _llm._is_exhausted(GROQ_TPM_WITH_UPSELL_URL) is False
    assert _llm._parse_wait(GROQ_TPM_WITH_UPSELL_URL) == pytest.approx(3.96)


def test_a_real_billing_failure_is_still_caught_without_a_url():
    assert _llm._is_exhausted(
        '{"error":{"message":"Your prepayment credits are depleted."}}') is True
