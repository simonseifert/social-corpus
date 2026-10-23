"""Privacy routing, sensitivity guard, transcript quality, account summary.

The routing tests stub the HTTP layer (urlopen), not llm.chat, so they assert on
where bytes would actually have gone rather than on what the router intended.
"""
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import llm, providers, store, textq  # noqa: E402
from corpus.analyze import account as AC  # noqa: E402
from corpus.analyze import privacy as P  # noqa: E402
from corpus.analyze import run as ar  # noqa: E402
from corpus.analyze import sensitivity as S  # noqa: E402

GOOD_SUMMARY = {
    "summary": "Studio posts 2.5 per week; typical post 118.",
    "priorities": [{"title": f"T{i}", "why": "median 118", "action": "do"}
                   for i in range(3)],
    "plan": [{"week": w, "idea": "i", "format": "text", "why": "w"}
             for w in (1, 2, 3, 4)],
}


@pytest.fixture
def tpl_dir(tmp_path):
    d = tmp_path / "templates"
    d.mkdir()
    (d / "demo.md").write_text("---\nname: Demo\n---\nAnalyse.\nCaption: {{caption}}\n")
    (d / "account-summary.md").write_text(
        (Path(__file__).resolve().parents[1] / "templates" /
         "account-summary.md").read_text())
    return d


@pytest.fixture
def wire(monkeypatch):
    """Record every outbound request; answer like the provider would."""
    sent = []
    reply = {"text": "an analysis"}

    def fake_urlopen(req, timeout=None):
        sent.append({"url": req.full_url, "body": json.loads(req.data)})
        if "/api/chat" in req.full_url:
            body = {"message": {"content": reply["text"]}}
        else:
            body = {"choices": [{"message": {"content": reply["text"]}}]}
        return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(providers, "key", lambda *a, **k: "k")
    llm._state.chain = None
    llm._state.idx = 0
    yield sent, reply
    llm._state.chain = None
    llm._state.idx = 0


def _ollama_up(monkeypatch, up=True):
    monkeypatch.setattr(providers, "_ollama_models", {
        providers.OLLAMA_URL: [providers.PROVIDERS["ollama"]["model"]] if up else None})


@pytest.fixture
def db(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    ids = {}
    for handle, rel in (("acme", "client"), ("rival", "competitor")):
        aid = store.upsert_account(conn, platform="linkedin", handle=handle,
                                   relationship=rel, display_name=handle.title())
        ids[handle] = aid
        store.upsert_item(conn, account_id=aid, platform="linkedin",
                          platform_item_id=f"{handle}-1", kind="text",
                          published_at=1700000000, body=f"{handle} post body",
                          raw={})
    conn.commit()
    return conn, ids, str(tmp_path / "a.db")


def _run(conn, adb, tpl_dir, **kw):
    return ar.run(conn, "demo", db_path=adb, template_dir=tpl_dir, dry_run=False,
                  workers=1, **kw)


# ── routing ─────────────────────────────────────────────────────────────────

def test_default_public_order_has_no_deepseek():
    assert "deepseek" not in providers.DEFAULT_ORDER.split(",")
    assert providers.DEFAULT_ORDER.split(",")[0] == "ollama"
    assert providers.PRIVATE_PROVIDERS == ["ollama", "mistral"]


def test_client_item_never_reaches_deepseek_even_first_in_order(
        db, tpl_dir, wire, monkeypatch):
    conn, ids, adb = db
    sent, _ = wire
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    monkeypatch.setattr(providers, "PROVIDER_ORDER", ["deepseek", "ollama"])
    _ollama_up(monkeypatch)
    st = _run(conn, adb, tpl_dir, account_id=ids["acme"])
    assert st.done == 1
    assert sent and all("deepseek" not in s["url"] for s in sent)
    assert all("/api/chat" in s["url"] for s in sent)
    row = conn.execute("SELECT provider, privacy FROM a.analysis_result").fetchone()
    assert (row["provider"], row["privacy"]) == ("ollama", "private")


def test_competitor_item_follows_the_explicit_public_order(
        db, tpl_dir, wire, monkeypatch):
    """CORPUS_PROVIDERS still decides for public payloads."""
    conn, ids, adb = db
    sent, _ = wire
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    monkeypatch.setattr(providers, "PROVIDER_ORDER", ["deepseek", "ollama"])
    _ollama_up(monkeypatch)
    st = _run(conn, adb, tpl_dir, account_id=ids["rival"])
    assert st.done == 1 and "deepseek" in sent[0]["url"]


def test_no_private_provider_skips_rather_than_leaks(db, tpl_dir, wire, monkeypatch):
    conn, ids, adb = db
    sent, _ = wire
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    monkeypatch.setenv("GROQ_API_KEY", "x")
    _ollama_up(monkeypatch, up=False)
    st = _run(conn, adb, tpl_dir, account_id=ids["acme"])
    assert sent == [], "nothing may leave the machine"
    assert st.done == 0 and st.private_skipped == 1
    assert any("no private provider" in e for e in st.errors)
    assert "SKIP private" in st.plan[0]


def test_collab_shared_with_a_client_is_private(db, tpl_dir, wire, monkeypatch):
    conn, ids, adb = db
    sent, _ = wire
    item = conn.execute("SELECT id FROM item WHERE platform_item_id='rival-1'"
                        ).fetchone()["id"]
    conn.execute("INSERT INTO item_account VALUES (?,?)", (item, ids["acme"]))
    conn.commit()
    monkeypatch.setenv("GROQ_API_KEY", "x")
    _ollama_up(monkeypatch, up=False)
    st = _run(conn, adb, tpl_dir, account_id=ids["rival"])
    assert sent == [] and st.private_skipped >= 1


def test_explicit_chain_is_never_widened(wire, monkeypatch):
    """llm.chat with a private chain whose providers are spent must raise, not fall
    through to the public chain."""
    monkeypatch.setenv("GROQ_API_KEY", "x")
    llm._state.retired.add("ollama")
    with pytest.raises(llm.NoProviderError):
        llm.chat([{"role": "user", "content": "hi"}],
                 chain=[providers.provider("ollama")])
    assert wire[0] == []


def test_dry_run_shows_the_provider_per_item(db, tpl_dir, wire, monkeypatch):
    conn, ids, adb = db
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setattr(providers, "PROVIDER_ORDER", ["groq"])
    _ollama_up(monkeypatch, up=False)
    st = ar.run(conn, "demo", db_path=adb, template_dir=tpl_dir)
    plan = "\n".join(st.plan)
    assert "@rival [public] -> groq" in plan
    assert "@acme [private] -> SKIP private" in plan
    assert wire[0] == []


def test_ollama_request_shape(wire, monkeypatch):
    sent, _ = wire
    _ollama_up(monkeypatch)
    llm.chat([{"role": "user", "content": "hi json"}], json_mode=True,
             chain=[providers.provider("ollama")])
    body = sent[0]["body"]
    assert body["options"]["num_ctx"] == providers.PROVIDERS["ollama"]["ctx"]
    assert body["format"] == "json" and body["stream"] is False
    assert body["think"] is False
    assert llm.last_used()["name"] == "ollama"


# ── pseudonyms ──────────────────────────────────────────────────────────────

def test_pseudonyms_are_stable_and_scrub_bodies():
    out = P.pseudonymise([
        {"author_name": "Ana Kovač", "body": "How much is it?"},
        {"author_name": "Marko", "body": "@ana.k Ana Kovač is right, marko agrees"},
        {"author_name": "ana kovač", "body": "thanks"},
    ])
    assert [c["author_name"] for c in out] == ["Commenter A", "Commenter B",
                                               "Commenter A"]
    assert "Ana" not in out[1]["body"] and "@ana.k" not in out[1]["body"]
    assert "Commenter A is right" in out[1]["body"]


def _seed_account(conn, handle="acme", rel="client", n=12, comments=True):
    aid = store.upsert_account(conn, platform="instagram", handle=handle,
                               relationship=rel)
    for i in range(n):
        iid, *_ = store.upsert_item(
            conn, account_id=aid, platform="instagram",
            platform_item_id=f"{handle}{i}", kind="short_video" if i % 2 else "image",
            published_at=1760000000 + i * 86400 * 3,
            body=f"Post number {i} about training\nsecond line", raw={})
        conn.execute("UPDATE item SET m_reactions=?, m_comments=? WHERE id=?",
                     (10 * i + 5, i, iid))
    if comments:
        conn.execute(
            "INSERT INTO comment (item_id, platform_comment_id, published_at, body, "
            "author_name, fetched_at) VALUES (?,?,?,?,?,?)",
            (iid, "c1", 1761000000, "Ivana Horvat here, how much does a month cost?",
             "Ivana Horvat", 1761000000))
    conn.commit()
    return aid


def test_account_payload_is_pseudonymised_and_private(tmp_path, wire, monkeypatch):
    conn = store.connect(tmp_path / "c.db")
    aid = _seed_account(conn, rel="competitor")
    sent, reply = wire
    reply["text"] = json.dumps(GOOD_SUMMARY)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    monkeypatch.setattr(providers, "PROVIDER_ORDER", ["deepseek"])
    _ollama_up(monkeypatch)
    text, facts = AC.dossier(conn, aid)
    assert facts["has_comments"] and "Ivana" not in text and "Commenter A" in text
    tpl_root = Path(__file__).resolve().parents[1] / "templates"
    st = AC.run(conn, aid, db_path=str(tmp_path / "a.db"), dry_run=False,
                template_dir=tpl_root)
    assert st.done == 1
    # Competitor, but carrying comments: private chain only, and no name in flight.
    assert all("deepseek" not in s["url"] for s in sent)
    assert all("Ivana" not in json.dumps(s["body"]) for s in sent)


# ── sensitivity ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Prošli tjedan mi je preminula mama. Hvala svima.",
    "Sprovod je u petak.",
    "My dad passed away on Sunday.",
    "Moja mama se bori s bolešću već godinu dana.",
    "I was diagnosed with cancer in March.",
    "Zbogom rokeru! 🖤",            # bistroharbor's top post, 10 Oct 2026
    "Farewell, maestro 🕊️",
])
def test_personal_posts_are_caught(text):
    assert S.personal(text)


@pytest.mark.parametrize("text", [
    "Korak po korak do zraka i snage: 5 vježbi za leđa.",
    "Rip curl summer drop is live",
    "Umrijet ćete od smijeha uz ovaj reel",
    "Big announcement: new studio opening in Portville!",
    "Zbogom ljeto, dobrodošla jesen! ☀️🍂",
    "Dragi Marko, kao trener ostavio si trag u Portvilleu. Vidimo se uskoro, prijatelju. ❤️",
])
def test_ordinary_posts_are_not(text):
    assert S.personal(text) is None


def test_personal_post_is_skipped_and_stored(tmp_path, tpl_dir, wire, monkeypatch):
    conn = store.connect(tmp_path / "c.db")
    aid = store.upsert_account(conn, platform="linkedin", handle="rival",
                               relationship="competitor")
    store.upsert_item(conn, account_id=aid, platform="linkedin",
                      platform_item_id="g", kind="text", published_at=1700000000,
                      body="Prošli tjedan mi je preminula mama.", raw={})
    conn.commit()
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setattr(providers, "PROVIDER_ORDER", ["groq"])
    st = _run(conn, str(tmp_path / "a.db"), tpl_dir)
    assert wire[0] == [], "a grief post must not be sent for a marketing read"
    assert st.personal == 1
    r = conn.execute("SELECT status, output_text, cost_usd FROM a.analysis_result"
                     ).fetchone()
    assert (r["status"], r["output_text"]) == ("personal", S.SENTINEL)
    # Nothing was sent, so nothing was spent: it was priced at the default rate
    # and showed "actual=$0.0002" on a run that never left the machine.
    assert r["cost_usd"] == 0 and st.cost_usd == 0


def test_model_sentinel_is_stored_as_personal(db, tpl_dir, wire, monkeypatch):
    conn, ids, adb = db
    wire[1]["text"] = S.SENTINEL
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setattr(providers, "PROVIDER_ORDER", ["groq"])
    _run(conn, adb, tpl_dir, account_id=ids["rival"])
    r = conn.execute("SELECT status FROM a.analysis_result").fetchone()
    assert r["status"] == "personal"


def test_system_prompt_carries_the_rules_and_lang(db, tpl_dir, wire, monkeypatch):
    conn, ids, adb = db
    sent, _ = wire
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setattr(providers, "PROVIDER_ORDER", ["groq"])
    _run(conn, adb, tpl_dir, account_id=ids["rival"], lang="hr")
    system = sent[0]["body"]["messages"][0]["content"]
    assert S.SENTINEL in system and "Croatian" in system
    assert conn.execute("SELECT lang FROM a.analysis_result").fetchone()["lang"] == "hr"
    # Same post in English is a different result, not a cache hit.
    st = _run(conn, adb, tpl_dir, account_id=ids["rival"])
    assert st.done == 1


def test_old_analysis_db_gains_the_new_columns(tmp_path):
    import sqlite3
    old = tmp_path / "a.db"
    c = sqlite3.connect(old)
    c.executescript(ar.ANALYSIS_SCHEMA.replace("a.", ""))
    c.close()
    conn = store.connect(tmp_path / "c.db")
    ar.attach(conn, old)
    cols = {r[1] for r in conn.execute("PRAGMA a.table_info(analysis_result)")}
    assert {"lang", "status", "privacy"} <= cols


# ── transcripts ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Music Music Music Music Music",
    "Thank you.",
    "Субтитры создавал DimaTorzok",
    "[Music] la la la",
    "xq7 zzk 9f8 ##4 qq1 t9z k8k",
    ("oh baby baby never let me go, oh baby baby never let me go, "
     "oh baby baby never let me go, we dance all night"),
    # Field test 2026-10-23: whisper over Portville music-bed reels.
    "Hvala što pratite.",
    "Hvala što pratite kanal.\n Hvala što pratite kanal.",
    "Редактор субтитров А.Семкин Корректор А.Егорова",
    "ご視聴ありがとうございました",
])
def test_garbled_transcripts(text):
    assert textq.garbled(text)


@pytest.mark.parametrize("text", [
    "Danas vam pokazujem tri vježbe za leđa koje možete raditi za stolom.",
    "The biggest mistake new members make is wanting everything at once.",
    "Prst, vrt i krv su hrvatske riječi bez samoglasnika.",
    "Hvala što pratite kanal, a sada idemo dalje s receptom za brudet.",
    "",
])
def test_real_speech_is_not_garbled(text):
    assert not textq.garbled(text)


def test_lens_labels_and_withholds_transcripts():
    ok = ar.lens_transcript({"transcript": "We grew revenue by changing one thing."})
    assert ok.startswith("[machine transcript, may contain errors]")
    junk = ar.lens_transcript({"transcript": "Music Music Music Music"})
    assert "withheld" in junk and "Music Music" not in junk


# ── account summary contract ────────────────────────────────────────────────

def test_summary_contract():
    assert AC.problems(GOOD_SUMMARY) == []
    bad = dict(GOOD_SUMMARY, priorities=GOOD_SUMMARY["priorities"][:2])
    assert any("exactly 3" in p for p in AC.problems(bad))
    bad = dict(GOOD_SUMMARY, plan=[dict(GOOD_SUMMARY["plan"][0], week=5)] * 4)
    assert any("week" in p for p in AC.problems(bad))


def test_summary_repairs_once_then_stores(tmp_path, wire, monkeypatch):
    conn = store.connect(tmp_path / "c.db")
    aid = _seed_account(conn, comments=False)
    sent, reply = wire
    answers = iter([json.dumps(dict(GOOD_SUMMARY, plan=[])),
                    json.dumps(GOOD_SUMMARY)])
    monkeypatch.setattr(llm, "chat", lambda *a, **k: next(answers))
    monkeypatch.setattr(llm, "last_used", lambda: {"name": "ollama", "model": "m"})
    _ollama_up(monkeypatch)
    tpl_root = Path(__file__).resolve().parents[1] / "templates"
    st = AC.run(conn, aid, db_path=str(tmp_path / "a.db"), dry_run=False,
                lang="hr", template_dir=tpl_root)
    assert st.done == 1, st.errors
    got = AC.latest(conn, aid, db_path=str(tmp_path / "a.db"))
    assert got["data"] == GOOD_SUMMARY and got["lang"] == "hr"
    assert got["output_text"].startswith(GOOD_SUMMARY["summary"])


def test_invented_numbers_are_detected():
    d = dict(GOOD_SUMMARY, summary="Typical post 118, aim for 5000 followers.")
    assert AC.invented_numbers(d, "Typical post: 118") == ["5000"]


# ── fail-closed gaps found in review ────────────────────────────────────────

def test_competitor_dossier_with_a_client_collab_is_private(tmp_path, wire,
                                                            monkeypatch):
    conn = store.connect(tmp_path / "c.db")
    rival = _seed_account(conn, handle="rival", rel="competitor", comments=False)
    client = store.upsert_account(conn, platform="instagram", handle="acme",
                                  relationship="client")
    item = conn.execute("SELECT id FROM item WHERE platform_item_id='rival3'"
                        ).fetchone()["id"]
    conn.execute("INSERT INTO item_account VALUES (?,?)", (item, client))
    conn.commit()
    sent, _ = wire
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setattr(providers, "PROVIDER_ORDER", ["groq"])
    tpl_root = Path(__file__).resolve().parents[1] / "templates"
    st = AC.run(conn, rival, db_path=str(tmp_path / "a.db"), dry_run=False,
                template_dir=tpl_root)
    assert sent == [] and st.private_skipped == 1


def test_competitor_dossier_with_an_item_owned_by_a_client_is_private(tmp_path):
    """A collab stored under the client's account appears in the rival's dossier."""
    conn = store.connect(tmp_path / "c.db")
    rival = _seed_account(conn, handle="rival", rel="competitor", comments=False)
    client = _seed_account(conn, handle="acme", rel="client", n=1, comments=False)
    item = conn.execute("SELECT id FROM item WHERE account_id=?", (client,)
                        ).fetchone()["id"]
    conn.execute("INSERT INTO item_account VALUES (?,?)", (item, rival))
    conn.commit()
    _, facts = AC.dossier(conn, rival)
    assert facts["shared_private"]


def test_plain_competitor_dossier_stays_public(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    rival = _seed_account(conn, handle="rival", rel="competitor", comments=False)
    _, facts = AC.dossier(conn, rival)
    assert not facts["shared_private"] and not facts["has_comments"]


def test_standing_context_makes_the_payload_private(db, tpl_dir, wire, monkeypatch):
    conn, ids, adb = db
    sent, _ = wire
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setattr(providers, "PROVIDER_ORDER", ["groq"])
    st = _run(conn, adb, tpl_dir, account_id=ids["rival"],
              standing_context="Client acme pays 2k/month, renewal in May")
    assert sent == [] and st.private_skipped == 1
    st = _run(conn, adb, tpl_dir, account_id=ids["rival"],
              standing_context="Pillars: training, nutrition", context_public=True)
    assert st.done == 1 and "groq" in sent[0]["url"]
