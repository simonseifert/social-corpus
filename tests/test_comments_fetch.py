"""Comment fetching: projection, attribution and cost caps.

No network. The failure that costs money is a run without a cap; the failure that
corrupts the corpus is a comment filed under the wrong post.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from corpus import comments as C  # noqa: E402

LI_URL = "https://www.linkedin.com/posts/caseymorganai_x-activity-7472141669535096832-djCI"
LI_URL2 = "https://www.linkedin.com/posts/caseymorganai_y-activity-7488881230671839234-m-x1"


def test_cost_matches_the_published_price():
    assert C.estimate_cost("linkedin", 10, 25) == pytest.approx(0.50)
    assert C.estimate_cost("instagram", 10, 25) == pytest.approx(0.125)


def test_every_call_carries_a_charge_cap(monkeypatch):
    """Without maxTotalChargeUsd an ignored maxItems bills the whole thread."""
    seen = {}

    def fake(actor, body, token, *, charge_cap):
        seen["cap"] = charge_cap
        seen["body"] = body
        return []
    monkeypatch.setattr(C, "_call", fake)
    C.fetch("linkedin", [LI_URL], token="t", per_post=25)
    assert seen["cap"] > 0
    assert seen["cap"] <= C.estimate_cost("linkedin", 1, 25) * 2


def test_author_profile_enrichment_is_never_requested(monkeypatch):
    """'main' mode costs $0.002 more per comment for a dossier we do not hold."""
    seen = {}
    monkeypatch.setattr(C, "_call",
                        lambda a, b, t, *, charge_cap: seen.update(b) or [])
    C.fetch("linkedin", [LI_URL], token="t")
    assert seen["profileScraperMode"] == "short"


def test_comments_are_attributed_to_the_right_post(monkeypatch):
    """A misattributed comment is filed under whichever post was first in the batch."""
    payload = [
        {"id": "c1", "commentary": "on the first post",
         "postUrl": "https://www.linkedin.com/feed/update/urn:li:activity:7472141669535096832/"},
        {"id": "c2", "commentary": "on the second post",
         "postUrl": "https://www.linkedin.com/feed/update/urn:li:activity:7488881230671839234/"},
    ]
    monkeypatch.setattr(C, "_call", lambda *a, **k: payload)
    out = C.fetch("linkedin", [LI_URL, LI_URL2], token="t")
    assert [c.body for c in out[LI_URL]] == ["on the first post"]
    assert [c.body for c in out[LI_URL2]] == ["on the second post"]


def test_empty_comments_are_dropped(monkeypatch):
    monkeypatch.setattr(C, "_call", lambda *a, **k: [
        {"id": "c1", "commentary": "   "}, {"id": "c2", "commentary": "real"}])
    out = C.fetch("linkedin", [LI_URL], token="t")
    assert [c.body for c in out[LI_URL]] == ["real"]


def test_author_replies_are_flagged_by_handle_not_display_name(monkeypatch):
    """Two people share a display name; the handle in the profile URL is unique."""
    monkeypatch.setattr(C, "_call", lambda *a, **k: [
        {"id": "c1", "commentary": "thanks!",
         "actor": {"name": "Casey Morgan",
                   "linkedinUrl": "https://www.linkedin.com/in/caseymorganai"}},
        {"id": "c2", "commentary": "great post",
         "actor": {"name": "Casey Morgan",
                   "linkedinUrl": "https://www.linkedin.com/in/someone-else"}},
    ])
    out = C.fetch("linkedin", [LI_URL], token="t", author_handle="caseymorganai")
    flags = {c.platform_comment_id: c.is_author_reply for c in out[LI_URL]}
    assert flags == {"c1": True, "c2": False}


def test_instagram_projection_reads_its_own_field_names(monkeypatch):
    monkeypatch.setattr(C, "_call", lambda *a, **k: [
        {"id": "17900", "text": "where can I buy this", "ownerUsername": "someone",
         "timestamp": "2026-05-01T10:00:00.000Z", "likesCount": 4}])
    url = "https://www.instagram.com/p/ABC123/"
    out = C.fetch("instagram", [url], token="t")
    c = out[url][0]
    assert c.body == "where can I buy this" and c.author_name == "someone"
    assert c.reactions == 4 and c.published_at == 1777629600


def test_millisecond_and_second_timestamps_both_survive():
    assert C._epoch(1777629600000) == 1777629600
    assert C._epoch(1777629600) == 1777629600
    assert C._epoch(None) is None


def test_unknown_platform_is_refused():
    with pytest.raises(ValueError):
        C.fetch("tiktok", ["https://x"], token="t")


def test_share_suffixed_urls_still_yield_a_clean_post_id():
    """LinkedIn appends "-m-x1" style share suffixes after the activity id."""
    assert "7488881230671839234" in C._ids_in(LI_URL2)
    assert "74888812306718392341" not in C._ids_in(LI_URL2)


def test_a_comment_that_cannot_be_attributed_is_dropped_not_guessed(monkeypatch):
    """Filing it under whichever post came first would silently corrupt the audit."""
    monkeypatch.setattr(C, "_call", lambda *a, **k: [
        {"id": "c9", "commentary": "orphan",
         "postUrl": "https://www.linkedin.com/feed/update/urn:li:activity:99999/"}])
    out = C.fetch("linkedin", [LI_URL, LI_URL2], token="t")
    assert out[LI_URL] == [] and out[LI_URL2] == []


def test_vendor_shape_drift_never_reaches_sqlite(monkeypatch):
    """Binding a list raises mid-run, after earlier batches were already paid for.

    Payload shapes drift per RECORD, not just per actor, so the projection coerces
    rather than trusting the field's type.
    """
    monkeypatch.setattr(C, "_call", lambda *a, **k: [
        {"id": "c1", "commentary": {"text": "an object body"},
         "reactionsCount": [{"count": 2}, {"count": 3}],
         "actor": {"name": ["Casey", "Morgan"]}, "createdAt": {"timestamp": 1777629600}},
    ])
    c = C.fetch("linkedin", ["https://www.linkedin.com/feed/update/urn:li:activity:1/"],
                token="t")["https://www.linkedin.com/feed/update/urn:li:activity:1/"][0]
    assert c.body == "an object body"
    assert c.reactions == 5, "a per-type reaction breakdown must total, not crash"
    assert c.author_name == "Casey Morgan"
    assert c.published_at == 1777629600
    for v in (c.body, c.author_name, c.reactions, c.published_at):
        assert not isinstance(v, (list, dict)), "a non-scalar reached the store layer"


def test_coercion_helpers_reject_nonsense_without_raising():
    assert C._int("1,234 likes") == 1234
    assert C._int(None) is None and C._int(True) is None
    assert C._text([]) is None and C._text({"nope": 1}) is None


def test_instagram_real_payload_shape():
    """Recorded from apidojo~instagram-comments-scraper on 2026-08-08.

    The first draft guessed "text"/"postUrl"; the actor sends "message" and
    "inputSource". An unreadable body is DROPPED, so that drift looked exactly
    like a post with no comments -- which is why this is pinned to a real record.
    """
    rec = {
        "inputSource": "https://www.instagram.com/p/DILqGB_Czgk/",
        "postId": "DILqGB_Czgk", "type": "comment", "id": "17935093148895909",
        "message": "@jaffa_ri ce vas isto cipelarit sa robotikom",
        "createdAt": "2025-04-09T16:05:54.000Z", "likeCount": 1,
        "user": {"id": "6116877230", "username": "jordan.novak"},
    }
    c = C._instagram_comment(rec, None)
    assert c.body == "@jaffa_ri ce vas isto cipelarit sa robotikom"
    assert c.author_name == "jordan.novak" and c.reactions == 1
    assert c.published_at == 1744214754


def test_instagram_comments_are_attributed_by_input_source(monkeypatch):
    url = "https://www.instagram.com/p/DILqGB_Czgk/"
    monkeypatch.setattr(C, "_call", lambda *a, **k: [
        {"id": "1", "message": "hi", "inputSource": url, "postId": "DILqGB_Czgk"}])
    out = C.fetch("instagram", [url, "https://www.instagram.com/p/OTHER1/"],
                  token="t")
    assert [c.body for c in out[url]] == ["hi"]


def test_linkedin_is_fetched_one_post_at_a_time():
    """A LinkedIn post has two ids and the payload does not say which URL asked.

    In a multi-post batch that makes every comment unattributable, and they are
    dropped silently: ten of Jordan's posts returned zero comments that way while
    the same posts returned three each when asked for alone.
    """
    assert C.BATCH["linkedin"] == 1


def test_ugcpost_payload_still_attributes_in_a_single_post_call(monkeypatch):
    url = ("https://www.linkedin.com/posts/jordan-novak-peric_x-"
           "activity-7269647108465459204-EYw3")
    monkeypatch.setattr(C, "_call", lambda *a, **k: [
        {"id": "1", "commentary": "Bravo svaka cast!",
         "postId": "urn:li:ugcPost:7269647107551109120"}])
    out = C.fetch("linkedin", [url], token="t")
    assert [c.body for c in out[url]] == ["Bravo svaka cast!"], \
        "a ugcPost-id payload was dropped even in a single-post call"


def test_author_reply_is_a_whole_handle_match_not_a_substring():
    """"ana" is a substring of "anamarija"; her comments are not the author's."""
    rec = lambda url: {"id": "c", "commentary": "hi",  # noqa: E731
                       "actor": {"linkedinUrl": url}}
    assert not C._linkedin_comment(
        rec("https://www.linkedin.com/in/anamarija/"), "ana").is_author_reply
    assert C._linkedin_comment(
        rec("https://www.linkedin.com/in/Ana/?miniProfileUrn=x"), "ana").is_author_reply
    assert C._linkedin_comment(
        rec("https://www.linkedin.com/in/ana"), "https://www.linkedin.com/in/ana/"
    ).is_author_reply


IG = [f"https://www.instagram.com/p/POST{i}/" for i in range(3)]


def test_a_batch_used_up_by_one_viral_post_leaves_the_rest_queued(tmp_path, monkeypatch):
    """Instagram's maxItems is shared by the batch. One viral post took all of it,
    the others came back empty, and were filed as fetched for good."""
    from corpus import store
    conn = store.connect(tmp_path / "c.db")
    aid = store.upsert_account(conn, platform="instagram", handle="v",
                               relationship="competitor")
    for i, u in enumerate(IG):
        iid, _, _ = store.upsert_item(conn, account_id=aid, platform="instagram",
                                      platform_item_id=f"p{i}", kind="image",
                                      published_at=1700000000 + i, raw={}, url=u)
        store.record_metrics(conn, iid, comments=900 if i == 0 else 5)
    conn.commit()
    monkeypatch.setattr(C, "_call", lambda actor, body, token, *, charge_cap: [
        {"id": f"c{n}", "message": "x", "inputSource": IG[0]}
        for n in range(body["maxItems"])])
    acct = dict(conn.execute("SELECT * FROM account").fetchone())
    res = C.sync(conn, acct, token="t", per_post=2)
    assert res["deferred"] == 2
    left = {r["url"] for r in store.items_missing_comments(conn, aid, per_post=2)}
    assert left == set(IG[1:])


def test_a_batch_that_ended_on_its_own_marks_empty_threads_fetched(tmp_path, monkeypatch):
    from corpus import store
    conn = store.connect(tmp_path / "c.db")
    aid = store.upsert_account(conn, platform="instagram", handle="v",
                               relationship="competitor")
    for i, u in enumerate(IG):
        iid, _, _ = store.upsert_item(conn, account_id=aid, platform="instagram",
                                      platform_item_id=f"p{i}", kind="image",
                                      published_at=1700000000 + i, raw={}, url=u)
        store.record_metrics(conn, iid, comments=3)
    conn.commit()
    monkeypatch.setattr(C, "_call", lambda *a, **k: [
        {"id": "c1", "message": "x", "inputSource": IG[0]}])
    res = C.sync(conn, dict(conn.execute("SELECT * FROM account").fetchone()),
                 token="t", per_post=2)
    assert res["deferred"] == 0
    assert store.items_missing_comments(conn, aid, per_post=2) == []


def test_a_run_that_reached_its_charge_cap_is_detected():
    from corpus import apify
    assert apify.hit_cap(30, 0.002, 0.061)        # a 31st row would cost 0.062
    assert not apify.hit_cap(10, 0.002, 0.061)


def test_a_saturated_batch_nobody_can_attribute_is_not_rebought_forever(tmp_path, monkeypatch):
    """If no row in a full batch maps to a post, deferring would re-buy it each run."""
    from corpus import store
    conn = store.connect(tmp_path / "c.db")
    aid = store.upsert_account(conn, platform="instagram", handle="v",
                               relationship="competitor")
    for i, u in enumerate(IG):
        iid, _, _ = store.upsert_item(conn, account_id=aid, platform="instagram",
                                      platform_item_id=f"p{i}", kind="image",
                                      published_at=1700000000 + i, raw={}, url=u)
        store.record_metrics(conn, iid, comments=5)
    conn.commit()
    monkeypatch.setattr(C, "_call", lambda actor, body, token, *, charge_cap: [
        {"id": f"c{n}", "message": "x", "inputSource": "https://elsewhere/"}
        for n in range(body["maxItems"])])
    res = C.sync(conn, dict(conn.execute("SELECT * FROM account").fetchone()),
                 token="t", per_post=2)
    assert res["deferred"] == 0
    assert store.items_missing_comments(conn, aid, per_post=2) == []
