"""linkedin_posts adapter, offline against a recorded payload.

The fixture is three synthetic records modelled on harvestapi/linkedin-profile-posts
output recorded 2026-08-05: a two-image post, a repost of a company page carrying a link card, and a
video post. They come from three targets on purpose -- one profile's recent posts do
not cover that matrix, and the actor genuinely returns a mixed dataset when a run is
given several targetUrls. The point of pinning it is that a vendor reshaping its
output fails here loudly instead of quietly writing nulls into the corpus.
"""
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import store, sync  # noqa: E402
from corpus.adapters import linkedin_posts as li  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "linkedin_posts.json"
RECORDS = json.loads(FIXTURE.read_text())
IMAGES, REPOST, VIDEO = RECORDS

TARGET = "https://www.linkedin.com/in/taylorbrooks/"
HANDLE = "taylorbrooks"


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _intercept(monkeypatch, payload):
    """Swap the HTTP call for the fixture and hand back the captured Requests.

    A backfill makes TWO calls now -- the posts actor and the profile actor -- so
    every request is recorded and `calls` is filterable by actor. Keeping only the
    last one silently moved this assertion onto the profile call.
    """
    seen = {"calls": []}

    def fake_urlopen(req, timeout=None):
        rec = {"req": req, "timeout": timeout,
               "body": json.loads(req.data.decode()), "url": req.full_url}
        seen["calls"].append(rec)
        seen.update(rec)          # last call, for tests that only make one
        return _Resp(payload)

    monkeypatch.setattr(li.urllib.request, "urlopen", fake_urlopen)
    return seen


def _call_to(seen, actor_fragment):
    """The captured request whose URL names a given actor."""
    for c in seen["calls"]:
        if actor_fragment in c["url"]:
            return c
    raise AssertionError(f"no call to {actor_fragment}; "
                         f"saw {[c['url'] for c in seen['calls']]}")


def _fetch(monkeypatch, payload=RECORDS, target=TARGET, **kw):
    _intercept(monkeypatch, payload)
    return li.fetch(target, keys={"APIFY_TOKEN": "t0k"}, **kw)


def _item(rec):
    return li._item(rec)


# ── normalisation ───────────────────────────────────────────────────────────

def test_multi_image_post_normalisation():
    it = _item(IMAGES)
    assert it.platform_item_id == "7488406960445567399", \
        "the activity id is the dedup key and must not be derived"
    assert it.kind == "carousel"
    assert it.published_at == 1785375347, "postedAt.timestamp is MILLISECONDS"
    assert it.published_at == IMAGES["postedAt"]["timestamp"] // 1000, \
        "pins the fixture's own units: a fixture re-recorded in seconds must fail here"
    assert it.url.startswith("https://www.linkedin.com/posts/taylorbrooks_")
    assert it.body.startswith("Some notes on the pricing dashboard")
    assert it.is_repost is False
    assert it.title is None and it.lang is None and it.duration_s is None

    assert it.metrics["reactions"] == 7364
    assert it.metrics["comments"] == 386
    assert it.metrics["shares"] == 269
    assert it.metrics["reaction_types"]["LIKE"] == 6786
    assert it.metrics["reaction_types"]["EMPATHY"] == 197
    assert "views" not in it.metrics and "saves" not in it.metrics, \
        "LinkedIn exposes neither on this route; inventing a zero would poison averages"

    assert [(m.role, m.seq, m.mime) for m in it.media] == \
        [("slide", 0, "image/jpeg"), ("slide", 1, "image/jpeg")]
    assert (it.media[0].width, it.media[0].height) == (1329, 864)
    assert it.raw is IMAGES, "raw must be the untouched vendor payload"


def test_reactions_total_is_not_the_like_bucket():
    """engagement.likes is LinkedIn's name for the sum of every reaction type. Mapping
    it to `reactions` is only correct while that holds, so the fixture proves it."""
    for rec in RECORDS:
        eng = rec["engagement"]
        assert sum(r["count"] for r in eng["reactions"]) == eng["likes"]
        assert _item(rec).metrics["reactions"] == eng["likes"]


def test_video_post_normalisation():
    it = _item(VIDEO)
    assert it.kind == "short_video"
    assert it.published_at == 1785888393
    assert it.duration_s is None, \
        "the actor reports no duration; enrichment probes the file for it"
    assert [(m.role, m.mime) for m in it.media] == \
        [("original", "video/mp4"), ("poster", "image/jpeg")]
    assert it.media[0].remote_url.startswith("https://dms.licdn.com/")


def test_repost_is_flagged_and_dated_by_the_reshare():
    """A repost carries the ORIGINAL's content decorated with repostedBy/repostedAt.
    repostedAt is read first so a vendor that starts reporting the original's publish
    time in postedAt cannot drag oldest_item_at, the backfill watermark."""
    it = _item(REPOST)
    assert it.is_repost is True
    assert REPOST["header"]["text"] == "Jamie Chen reposted this"
    assert it.published_at == REPOST["repostedAt"]["timestamp"] // 1000
    assert it.raw["author"]["universalName"] == "orbitlab", \
        "the author stays the original poster, which is why _profile matches on handle"


def test_a_reshare_carries_its_own_activity_id():
    """The reason platform_item_id stays the bare `id` even for a repost.

    LinkedIn activity ids are snowflakes: id >> 22 is epoch ms. The reshare's `id`
    decodes to repostedAt and is the activity in header.linkedinUrl (the reshare's
    permalink); the ORIGINAL's activity id is a different, older number carried in
    linkedinUrl and engagement.id. So the ids never collide on their own, and
    decorating them would file one post as two rows against linkedin_export, which
    emits this same bare activity id from a consented export.
    """
    it = _item(REPOST)
    assert it.platform_item_id == "7488424658041883389"

    original = re.search(r"activity-(\d+)", REPOST["linkedinUrl"]).group(1)
    assert original == "7485407087926325620" and original != REPOST["id"], \
        "the reshare does not borrow the original's activity id"
    assert f"urn:li:activity:{REPOST['id']}" in REPOST["header"]["linkedinUrl"], \
        "`id` is the reshare's own activity, which is what header.linkedinUrl points at"

    assert (int(REPOST["id"]) >> 22) == REPOST["repostedAt"]["timestamp"]
    assert (int(original) >> 22) < REPOST["postedAt"]["timestamp"], \
        "the original was published earlier; only its content rides along"

    # Stable across fetches, which is what makes it safe as a dedup key.
    assert _item(json.loads(json.dumps(REPOST))).platform_item_id == it.platform_item_id


def test_repost_url_points_at_the_reshare_not_the_original():
    """The row records the resharer's timeline entry -- its id and its date are the
    reshare's -- so its url must be too. linkedinUrl is the company's post."""
    it = _item(REPOST)
    assert it.url == \
        "https://www.linkedin.com/feed/update/urn:li:activity:7488424658041883389"
    assert "orbitlab" in REPOST["linkedinUrl"], "which is the original's permalink"
    assert "?" not in it.url, "the updateEntityUrn tail is not part of the permalink"

    assert _item(IMAGES).url == IMAGES["linkedinUrl"], \
        "a normal post keeps its own linkedinUrl"


def test_link_card_does_not_become_an_article_or_an_image():
    """Sharing a link is still a text post. The headline is kept because it is content
    the post displays, but only a native Pulse piece or newsletter may change `kind`."""
    it = _item(REPOST)
    assert it.kind == "text"
    assert it.title == "Orbit Lab is Shaping the Future of Personal Assistants"
    assert [(m.role, m.mime) for m in it.media] == [("thumbnail", "image/jpeg")]

    native = json.loads(json.dumps(REPOST))
    native["article"]["link"] = "https://www.linkedin.com/pulse/can-you-outsource-taste"
    assert _item(native).kind == "article"


def test_document_post_becomes_slides():
    """coverPages is one entry per rendition WIDTH holding every page, not one entry
    per page. Reading it the other way stores a single slide for a ten-page deck."""
    rec = json.loads(json.dumps(IMAGES))
    rec["postImages"] = []
    rec["document"] = {"coverPages": [{"width": 800, "imageUrls": ["a", "b"]},
                                      {"width": 1933, "imageUrls": ["A", "B", "C"]}]}
    it = _item(rec)
    assert it.kind == "carousel"
    assert [(m.role, m.seq, m.remote_url) for m in it.media] == \
        [("slide", 0, "A"), ("slide", 1, "B"), ("slide", 2, "C")]


def test_epoch_prefers_ms_and_falls_back_to_iso():
    assert li._epoch({"timestamp": 1785375347243}) == 1785375347
    assert li._epoch({"timestamp": 1785375347}) == 1785375347
    assert li._epoch({"date": "2026-07-30T01:35:47.243Z"}) == 1785375347, \
        "the ISO branch covers the day `timestamp` disappears"
    assert li._epoch({}) is None and li._epoch(None) is None


def test_record_without_a_timestamp_is_dropped_not_dated():
    """published_at is NOT NULL. Inventing a date corrupts every recency query the
    row later lands in, so the record is dropped and the run's seen count shows it."""
    assert _item({"id": "x", "engagement": {}}) is None


# ── profile ─────────────────────────────────────────────────────────────────

def test_profile_comes_from_the_matching_author():
    p = li._profile(RECORDS, handle=HANDLE, seg="in")
    assert p.handle == "taylorbrooks" and p.display_name == "Taylor Brooks"
    assert p.platform_uid == IMAGES["author"]["id"]
    assert p.bio == "Founder and CEO at Example Corp"
    assert p.url == "https://www.linkedin.com/in/taylorbrooks/"
    assert p.follower_count is None, "a person's post payload carries no follower count"


def test_profile_ignores_a_reposted_authors_page():
    """A repost's author is the ORIGINAL poster and persist() upserts Profile.handle
    as an account row, so an unmatched author would branch the corpus in two."""
    assert li._profile([REPOST], handle=HANDLE, seg="in") is None


def test_company_author_yields_followers_not_a_bio():
    """Company pages put "93,868 followers" where people put a headline. Storing that
    string as a bio is worse than storing nothing."""
    p = li._profile([REPOST], handle="orbitlab", seg="company")
    assert p.follower_count == 93868 and p.bio is None
    assert p.url == "https://www.linkedin.com/company/orbitlab/"
    assert li._followers("Founder and CEO at Example Corp") is None


def test_target_must_be_a_url():
    assert li._target("https://www.linkedin.com/in/taylorbrooks?miniProfileUrn=urn%3A") \
        == ("https://www.linkedin.com/in/taylorbrooks/", "in", "taylorbrooks")
    assert li._target("linkedin.com/company/examplecorp/")[1] == "company"
    with pytest.raises(ValueError):
        li._target("taylorbrooks")  # /in/ or /company/ is not guessable from a handle


# ── request shape ───────────────────────────────────────────────────────────

def test_token_travels_in_the_header_and_costly_flags_stay_off(monkeypatch):
    seen = _intercept(monkeypatch, RECORDS)
    li.fetch(TARGET, keys={"APIFY_TOKEN": "t0k"}, mode="backfill", limit=200)

    posts = _call_to(seen, "linkedin-profile-posts")
    req = posts["req"]
    assert req.get_header("Authorization") == "Bearer t0k"
    assert "t0k" not in req.full_url, "a token in the query string leaks into logs"
    assert "maxTotalChargeUsd=" in req.full_url, "the run needs a hard charge cap"
    assert posts["timeout"] == 310, "below the 300s server cap abandons a billing run"

    assert posts["body"] == {"targetUrls": ["https://www.linkedin.com/in/taylorbrooks/"],
                             "maxPosts": 200,
                             "scrapeReactions": False,
                             "scrapeComments": False}, \
        "input keys are schema-verified names; the two scrape flags bill per reactor " \
        "and per comment body and the counts we store are free without them"

    # The profile call must be held to the same standard, not exempted for being new.
    prof = _call_to(seen, "linkedin-profile-scraper")
    assert prof["req"].get_header("Authorization") == "Bearer t0k"
    assert "t0k" not in prof["url"], "a token in the query string leaks into logs"
    assert "maxTotalChargeUsd=" in prof["url"], "the profile run needs a cap too"
    assert prof["body"] == {"urls": ["https://www.linkedin.com/in/taylorbrooks/"]}


def test_delta_does_not_pay_for_the_profile(monkeypatch):
    """A career does not change weekly; only a backfill should buy the profile."""
    seen = _intercept(monkeypatch, RECORDS)
    li.fetch(TARGET, keys={"APIFY_TOKEN": "t0k"}, mode="delta", limit=10)
    assert not any("profile-scraper" in c["url"] for c in seen["calls"]), \
        "a delta must not spend on the profile actor"


def test_scrape_flags_are_opt_in(monkeypatch):
    seen = _intercept(monkeypatch, RECORDS)
    li.fetch(TARGET, keys={"APIFY_TOKEN": "t0k"}, scrape_comments=True)
    assert seen["body"]["scrapeComments"] is True
    assert seen["body"]["scrapeReactions"] is False


def test_delta_pushes_since_to_the_vendor(monkeypatch):
    """postedLimitDate reads like an end bound but is a floor. Filtering client-side
    instead would still pay for every page older than the watermark."""
    seen = _intercept(monkeypatch, RECORDS)
    li.fetch(TARGET, keys={"APIFY_TOKEN": "t0k"}, since=1785375347)
    assert seen["body"]["postedLimitDate"] == "2026-07-30T01:35:47.000Z"

    li.fetch(TARGET, keys={"APIFY_TOKEN": "t0k"}, mode="backfill", since=1785375347)
    assert "postedLimitDate" not in seen["body"], \
        "a backfill must not carry a lower bound"


def test_truncated_is_set_on_a_full_page(monkeypatch):
    assert _fetch(monkeypatch, limit=3).truncated is True
    assert _fetch(monkeypatch, limit=50).truncated is False


def test_empty_unfiltered_pull_is_loud(monkeypatch):
    """A renamed input key makes an Apify run SUCCEED with an empty dataset. Recorded
    as ok with 0 items it would read as 'this page posts nothing'."""
    with pytest.raises(RuntimeError, match="0 rows"):
        _fetch(monkeypatch, payload=[], limit=10)

    quiet = _fetch(monkeypatch, payload=[], since=1785375347)
    assert quiet.items == [] and quiet.truncated is False, \
        "a delta window with nothing new is legitimate, verified against the live actor"


def test_missing_token_fails_before_spending(monkeypatch):
    _intercept(monkeypatch, RECORDS)
    with pytest.raises(RuntimeError, match="APIFY_TOKEN"):
        li.fetch(TARGET, keys={})


def test_cost_matches_observed_billing():
    # verified against /v2/actor-runs: 0 -> $0.00105, 2 -> $0.00405, 3 -> $0.00605,
    # 6 -> $0.01205 (one actor-start event plus $0.002 a post at BRONZE)
    assert li._estimate_cost(0) == 0.00105
    assert li._estimate_cost(2) == 0.00405
    assert li._estimate_cost(3) == 0.00605
    assert li._estimate_cost(6) == 0.01205
    assert li.ADAPTER.cost_per_1k == 2.0


# ── persistence ─────────────────────────────────────────────────────────────

def test_persist_twice_is_idempotent(tmp_path, monkeypatch):
    conn = store.connect(tmp_path / "t.db")
    aid = store.upsert_account(conn, platform="linkedin", handle=HANDLE,
                               relationship="competitor")

    first = sync.persist(conn, li.ADAPTER, aid, _fetch(monkeypatch))
    assert (first["seen"], first["new"], first["changed"]) == (3, 3, 0)

    counts = lambda t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]  # noqa: E731
    media_after_first, snaps_after_first = counts("media"), counts("metric_snapshot")
    assert media_after_first == 5, \
        "2 image slides, 1 link-card thumbnail, and the video's mp4 plus its poster"

    second = sync.persist(conn, li.ADAPTER, aid, _fetch(monkeypatch))
    assert second["new"] == 0, "a re-sync of unchanged data must insert nothing"
    assert second["changed"] == 0
    assert counts("item") == 3
    assert counts("media") == media_after_first, "media is replaced, not appended"
    assert counts("metric_snapshot") == snaps_after_first, "metrics are change-only"
    assert counts("account") == 1, "the author block must not spawn a second account"

    row = conn.execute("SELECT kind, published_at, is_repost, m_reactions, m_shares "
                       "FROM item WHERE platform_item_id=?", (VIDEO["id"],)).fetchone()
    assert row["kind"] == "short_video" and row["published_at"] == 1785888393
    assert row["is_repost"] == 0 and row["m_shares"] == 22

    extra = conn.execute("SELECT metrics_raw FROM metric_snapshot ms JOIN item i "
                         "ON i.id=ms.item_id WHERE i.platform_item_id=?",
                         (IMAGES["id"],)).fetchone()["metrics_raw"]
    assert json.loads(extra)["reaction_types"]["PRAISE"] == 128, \
        "the per-type breakdown has no column and rides along in metrics_raw"

    reposted = conn.execute("SELECT is_repost FROM item WHERE platform_item_id=?",
                            (REPOST["id"],)).fetchone()
    assert reposted["is_repost"] == 1


def test_a_reposted_original_and_the_repost_are_separate_rows(tmp_path, monkeypatch):
    """Track a competitor and someone who reshares them and you get two rows, because
    the reshare has its own activity id. The original's id is the one in its
    linkedinUrl; nothing has to be synthesised to keep them apart."""
    conn = store.connect(tmp_path / "t.db")
    original = json.loads(json.dumps(REPOST))
    for k in ("repostedBy", "repostedAt", "header"):
        original.pop(k, None)
    # what the company's own timeline returns for that post: its own activity id
    original["id"] = original["entityId"] = "7485407087926325620"

    for handle, payload in (("orbitlab", [original]), ("jamiechen", [REPOST])):
        aid = store.upsert_account(conn, platform="linkedin", handle=handle,
                                   relationship="competitor")
        sync.persist(conn, li.ADAPTER, aid,
                     _fetch(monkeypatch, payload=payload,
                            target=f"https://www.linkedin.com/company/{handle}/"))

    rows = conn.execute("SELECT platform_item_id, is_repost FROM item "
                        "ORDER BY is_repost").fetchall()
    assert [(r["platform_item_id"], r["is_repost"]) for r in rows] == [
        ("7485407087926325620", 0),
        ("7488424658041883389", 1),
    ]


def test_the_dedup_key_matches_what_linkedin_export_emits(tmp_path, monkeypatch):
    """Cross-adapter contract: linkedin_export._post_id pulls the bare activity id out
    of a permalink so a post ingested from a consented export and later re-scraped
    publicly collapses onto ONE row. Decorating the id here would break that."""
    from corpus.adapters import linkedin_export as le

    for rec in RECORDS:
        it = _item(rec)
        assert le._post_id(it.url) == it.platform_item_id, \
            f"{it.url} would land as a second row"


def test_share_urn_is_the_fallback_when_the_id_vanishes():
    """A hash of linkedinUrl would not do: its slug is generated from the post's first
    words and LinkedIn regenerates it on an edit, so the key would drift."""
    rec = json.loads(json.dumps(IMAGES))
    rec.pop("id"), rec.pop("entityId")
    assert _item(rec).platform_item_id == "ugcPost:7488406958745715120", \
        "namespaced like linkedin_export, so a ugcPost id cannot pose as an activity id"

    rec.pop("shareUrn")
    assert len(_item(rec).platform_item_id) == 32, "last resort is the stable_id hash"
