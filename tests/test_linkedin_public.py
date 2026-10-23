"""linkedin_public adapter, offline against seven recorded permalink pages.

The fixture is SYNTHETIC HTML, modelled on real pages captured cookie-free on
2026-08-06 and reduced to the markup the parser reads, gzipped inside one JSON (see
its `_meta`). People, text, ids and media URLs are invented. Each page was chosen because it is the only one in the set that
proves a particular thing, and most of them encode a bug that was live in this file
before the page was read carefully:

  * a permalink page also renders up to nine RELATED posts, each a full card with
    its own reaction count
  * a quote reshare renders the QUOTED post's images inside the same card
  * on a repost the JSON-LD reports the reshare's own engagement (0) while the DOM
    chip reports the original's (12)
  * a repost is provable from the DOM alone on a page with no JSON-LD at all
  * a count of zero is rendered as an ABSENT chip, not as "0"

Every expected value below was cross-checked against the same post as fetched by the
paid harvestapi actor and stored in the corpus, which is what makes this a fidelity
test and not a snapshot of our own output.
"""
import base64
import gzip
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import store, sync  # noqa: E402
from corpus.adapters import linkedin_public as lp  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "linkedin_public.json"
_RAW = json.loads(FIXTURE.read_text())
PAGES = {k: gzip.decompress(base64.b64decode(v)).decode()
         for k, v in _RAW["pages"].items()}

IMAGES = "7067491735594216252"     # 3-image post, not a repost
REPOST_LD = "7083765990265573858"  # bare repost, JSON-LD present
REPOST_DOM = "7453364366466469899"  # bare repost, NO JSON-LD
QUOTE = "7216773173150347277"      # reshare with own commentary
VIDEO = "7331300570158591729"      # PT30S + auto-caption
LINKCARD = "7442582381142713833"   # zero reactions + shared link
RELATED = "7298649399608741886"    # 9 cards in one document


def parse(aid, **kw):
    it = lp.parse(PAGES[aid], aid=aid, url=lp.permalink(aid), **kw)
    assert it is not None, f"{aid} did not parse"
    return it


def _offline(monkeypatch, statuses=None):
    """Serve the fixture instead of LinkedIn, and record what was asked for.

    statuses maps an activity id to an HTTP status to return instead of 200, which
    is how the throttle and failure paths are exercised without touching the net.
    """
    seen = {"urls": [], "sleeps": []}
    statuses = statuses or {}

    def fake_get(url, throttle):
        seen["urls"].append(url)
        aid = lp.activity_id(url)
        st = statuses.get(aid, 200)
        return (st, PAGES.get(aid, "") if st == 200 else "")

    monkeypatch.setattr(lp, "_get", fake_get)
    return seen


# ── refs: what this route will and will not accept ──────────────────────────

@pytest.mark.parametrize("ref,expected", [
    ("https://www.linkedin.com/feed/update/urn:li:activity:7067491735594216252/",
     "7067491735594216252"),
    ("https://www.linkedin.com/posts/jordan-novak-peric_launchpad2023-activity-"
     "7067491735594216252-aB3x", "7067491735594216252"),
    ("urn:li:activity:7067491735594216252", "7067491735594216252"),
    ("7067491735594216252", "7067491735594216252"),
    ("https://www.linkedin.com/in/jordan-novak-peric/", None),
    ("", None),
])
def test_activity_id_shapes(ref, expected):
    assert lp.activity_id(ref) == expected


def test_ugcpost_urn_is_not_an_activity_id():
    """Different id space. linkedin_posts keys rows on the bare ACTIVITY id, so
    accepting a ugcPost id here would file one post as two rows."""
    assert lp.activity_id("urn:li:ugcPost:7083747447309733751") is None


def test_profile_url_raises_and_says_why():
    with pytest.raises(RuntimeError) as e:
        lp.fetch("https://www.linkedin.com/in/jordan-novak-peric/")
    msg = str(e.value)
    assert "cannot enumerate" in msg and "999" in msg
    # It has to point at the route that CAN do the job, or the failure is a dead end.
    assert "linkedin_posts" in msg


def test_unusable_target_raises():
    with pytest.raises(RuntimeError):
        lp.fetch("not a post")


# ── the normalisation contract ──────────────────────────────────────────────

def test_ids_are_the_same_key_space_as_the_paid_route():
    """The whole point of this adapter is that a refresh UPDATES the paid route's
    rows. A different id here would append duplicates forever instead."""
    for aid in PAGES:
        assert parse(aid).platform_item_id == aid


def test_published_at_is_unix_seconds_from_the_snowflake():
    """id >> 22 is epoch ms. Checked against all 53 stored posts with zero error,
    which is why it beats JSON-LD -- absent on two of these seven pages."""
    assert parse(IMAGES).published_at == 1685021337        # paid route agrees
    assert parse(REPOST_DOM).published_at == 1777020541    # and here LD does not exist
    for aid in PAGES:
        pub = parse(aid).published_at
        assert isinstance(pub, int)
        assert 1_400_000_000 < pub < 2_000_000_000, "not unix SECONDS"


def test_body_matches_the_paid_route_byte_for_byte():
    it = parse(IMAGES)
    assert it.body.startswith("Prošli tjedan, Luka Barić i ja imali smo priliku")
    assert it.body.endswith("#launchpad2023")
    # Mentions arrive as <a>Name</a> and the display name is part of the sentence.
    assert "Luka Barić" in parse(REPOST_LD).body


def test_counts_come_from_the_dom_chip_not_from_json_ld():
    """On this page JSON-LD counts the RESHARE's own reactions (0) and the DOM chip
    counts the ORIGINAL's (12). The paid route reports 12, so the chip wins."""
    ld = lp.json_ld(PAGES[REPOST_LD], REPOST_LD)
    like = [s for s in ld["interactionStatistic"] if "LikeAction" in s["interactionType"]]
    assert like[0]["userInteractionCount"] == 0
    assert parse(REPOST_LD).metrics["reactions"] == 12


def test_absent_chip_means_zero_not_unknown():
    """LinkedIn omits the chip rather than rendering a 0. The paid route stores 0
    reactions for this post, so absence must land as 0 and not as None."""
    assert 'data-num-reactions' not in PAGES[LINKCARD]
    m = parse(LINKCARD).metrics
    assert m["reactions"] == 0
    assert m["comments"] == 2


def test_no_shares_are_reported():
    """The one real regression against the paid route, and the reason `notes` warns
    that a refresh nulls m_shares. If a shares key ever appears here, that warning
    is wrong and has to be revisited."""
    for aid in PAGES:
        assert "shares" not in parse(aid).metrics


def test_reaction_breakdown_is_not_named_like_the_counted_one():
    """linkedin_posts stores {type: count} under `reaction_types`. This is an
    unordered top-3 with no counts, so it must not share the key."""
    m = parse(REPOST_DOM).metrics
    assert "reaction_types" not in m
    assert m["reaction_types_top"] == ["LIKE", "PRAISE", "EMPATHY"]


# ── the true author, which on a repost is not the account being refreshed ───

def test_bare_repost_credits_the_original_author():
    it = parse(REPOST_LD)
    assert it.is_repost is True
    # The page's own JSON-LD author is the resharer. The corpus wants the original poster.
    assert it.author_name == "Jordan Novak Perić"
    assert it.author_handle == "jordan-novak-peric"


def test_repost_is_detected_with_no_json_ld_at_all():
    """This page carries no JSON-LD and no reshare block; the only evidence is that
    the rendered author is not the handle in the canonical URL."""
    assert lp.json_ld(PAGES[REPOST_DOM], REPOST_DOM) is None
    it = parse(REPOST_DOM)
    assert it.is_repost is True
    assert it.author_name == "Casey Morgan"
    assert "jordan-novak-peric" in it.url          # the timeline entry is the resharer's


def test_quote_reshare_is_not_a_repost():
    """A reshare WITH commentary is an original post by the commenter -- which is
    what the paid route says too, so the two routes agree on one row."""
    it = parse(QUOTE)
    assert it.is_repost is False
    assert it.author_name == "Jordan Novak Perić"
    assert it.body == "Thanks Mia👌"


def test_company_author_name_has_no_aria_label_prefix():
    """The lockup opens with an image anchor whose body is empty; reading the name
    off the wrong anchor stores 'View organization page for X'."""
    name, handle, info = lp._lockup(
        lp.main_card(PAGES[REPOST_DOM]), "main-feed-activity-card__entity-lockup")
    assert name == "Casey Morgan"
    assert handle == "caseymorganai"
    assert not name.startswith("View ")
    assert "Helping Small Agencies" in info


# ── media, and the two ways it leaks in from a neighbouring post ────────────

def test_related_posts_do_not_contribute_their_numbers():
    """This one document holds nine cards and nine reaction counts. Unscoped
    extraction does not error here, it reports a stranger's engagement."""
    page = PAGES[RELATED]
    assert page.count('data-num-reactions=') == 9
    card = lp.main_card(page)
    assert card.count('data-num-reactions=') == 1
    it = parse(RELATED)
    assert it.metrics["reactions"] == 21          # the paid route's number
    assert it.metrics["comments"] == 4


def test_quoted_posts_images_are_not_this_posts_images():
    """The quoted post's image block sits inside the same card. The paid route
    files this post as `text`; counting the nested image makes it `image`."""
    card = lp.main_card(PAGES[QUOTE])
    assert 'feed-images-content__list-item' in card    # present, but not ours
    it = parse(QUOTE)
    assert it.media == []
    assert it.kind == "text"


def test_link_card_image_is_a_thumbnail_and_does_not_change_kind():
    it = parse(LINKCARD)
    assert [m.role for m in it.media] == ["thumbnail"]
    assert it.kind == "text"                      # a link share is still a text post
    assert it.title                               # the headline is kept for FTS


def test_multi_image_post_yields_ordered_slides():
    it = parse(IMAGES)
    assert it.kind == "carousel"
    assert [m.role for m in it.media] == ["slide"] * 3
    assert [m.seq for m in it.media] == [0, 1, 2]
    assert all(m.remote_url.startswith("https://media.licdn.com/") for m in it.media)


def test_video_yields_an_original_and_a_poster():
    it = parse(VIDEO)
    assert [m.role for m in it.media] == ["original", "poster"]
    # enrich finds video by role='original'; a poster claiming that role breaks it.
    assert it.media[0].mime == "video/mp4"
    assert ".mp4" in it.media[0].remote_url or "/mp4-" in it.media[0].remote_url


def test_duration_is_parsed_and_drives_the_kind_split():
    """The paid actor reports no duration at all, so it files every video as
    short_video. Here the split is real."""
    assert lp._duration_s("PT30S") == 30.0
    assert lp._duration_s("PT3M48S") == 228.0
    assert lp._duration_s(None) is None
    assert parse(VIDEO).duration_s == 30.0
    assert parse(VIDEO).kind == "short_video"
    assert parse(RELATED).duration_s == 228.0
    assert parse(RELATED).kind == "long_video"


# ── the auto-caption, which is not a transcript ─────────────────────────────

def test_auto_caption_is_kept_but_never_written_as_a_transcript():
    """It is a machine caption that rendered Croatian speech as German nonsense,
    and enrich only considers items whose transcript IS NULL -- so writing it would
    permanently stop this video from ever being really transcribed."""
    it = parse(VIDEO)
    assert it.transcript is None
    assert it.raw["dom"]["auto_caption"].startswith("Ja, ja, Herr Fax")


def test_captions_opt_in_writes_it():
    assert parse(VIDEO, captions=True).transcript.startswith("Ja, ja, Herr Fax")


# ── comments ────────────────────────────────────────────────────────────────

def test_comment_bodies_ride_along_in_metrics_raw():
    sample = parse(RELATED).metrics["comment_sample"]
    assert len(sample) == 4
    assert sample[0]["author"] and sample[0]["text"]
    # The count is the count; the bodies are an arbitrary top-N slice of it.
    assert parse(RELATED).metrics["comments"] == 4


def test_comment_sample_is_capped_far_below_the_real_count():
    """191 comments, 10 bodies. This page has no JSON-LD either, so the bodies came
    off the DOM -- both halves of _comments have to work."""
    it = parse(REPOST_DOM)
    assert it.metrics["comments"] == 191
    assert len(it.metrics["comment_sample"]) == 10
    assert it.metrics["comment_sample"][0]["author"] == "Casey Morgan"


# ── raw ─────────────────────────────────────────────────────────────────────

def test_raw_carries_the_vendor_record_untouched():
    it = parse(IMAGES)
    ld = it.raw["ld"]
    # Verbatim: the vendor's own keys and values, not a reprojection of them.
    assert ld["@type"] == "SocialMediaPosting"
    assert ld["articleBody"] == it.body
    assert it.raw["_source"]["adapter"] == "linkedin_public"
    assert it.raw["_source"]["html_bytes"] == len(PAGES[IMAGES])
    assert json.dumps(it.raw)                     # must survive persist()


def test_raw_survives_a_page_with_no_json_ld():
    it = parse(REPOST_DOM)
    assert it.raw["ld"] is None
    assert it.raw["dom"]["reactions"] == 60


# ── fetch: cost, ordering, truncation, and the failure paths ────────────────

def test_fetch_is_free_and_needs_no_key(monkeypatch):
    _offline(monkeypatch)
    res = lp.fetch(urls=list(PAGES))
    assert res.cost_usd == 0.0
    assert lp.ADAPTER.needs == ()
    assert lp.ADAPTER.cost_per_1k == 0.0
    assert len(res.items) == len(PAGES)


def test_items_come_back_newest_first(monkeypatch):
    _offline(monkeypatch)
    stamps = [i.published_at for i in lp.fetch(urls=list(PAGES)).items]
    assert stamps == sorted(stamps, reverse=True)


def test_limit_caps_requests_and_flags_truncated(monkeypatch):
    seen = _offline(monkeypatch)
    res = lp.fetch(urls=list(PAGES), limit=2)
    assert len(seen["urls"]) == 2, "limit must stop us before the request, not after"
    assert len(res.items) == 2
    assert res.truncated is True


def test_a_dropped_page_also_counts_as_truncated(monkeypatch):
    """Asking for six and getting five is not a clean pull, and sync surfaces
    truncated -- so a gap has to set it too, not just an over-long id list."""
    _offline(monkeypatch, statuses={RELATED: 429})
    res = lp.fetch(urls=list(PAGES))
    assert len(res.items) == len(PAGES) - 1
    assert res.truncated is True


def test_a_429_does_not_fail_the_run(monkeypatch):
    _offline(monkeypatch, statuses={IMAGES: 429, VIDEO: 429})
    res = lp.fetch(urls=[IMAGES, VIDEO, QUOTE])
    assert [i.platform_item_id for i in res.items] == [QUOTE]


def test_everything_failing_is_loud(monkeypatch):
    """A run logged ok with 0 items reads downstream as 'these posts are gone'."""
    _offline(monkeypatch, statuses={a: 429 for a in PAGES})
    with pytest.raises(RuntimeError, match="nothing usable"):
        lp.fetch(urls=list(PAGES))


def test_since_filters_client_side(monkeypatch):
    _offline(monkeypatch)
    res = lp.fetch(urls=[IMAGES, REPOST_DOM], since=1_700_000_000)
    assert [i.platform_item_id for i in res.items] == [REPOST_DOM]


def test_target_and_urls_are_pooled(monkeypatch):
    """A profile URL in `target` supplies the account, never the posts."""
    _offline(monkeypatch)
    res = lp.fetch("https://www.linkedin.com/in/jordan-novak-peric/",
                   urls=[IMAGES, VIDEO])
    assert len(res.items) == 2
    assert res.profile is not None
    assert res.profile.handle == "jordan-novak-peric"
    assert res.profile.follower_count == 706       # matches the stored value


def test_profile_is_not_taken_from_a_strangers_page(monkeypatch):
    """persist() upserts Profile.handle as an account row and appends a follower
    snapshot unconditionally. Both wrong answers are worse than None: a stranger's
    handle branches the corpus, and a NULL follower count reads as a collapse to
    zero rather than as 'not measured'."""
    _offline(monkeypatch)
    # A third party's post, asked for as a different account.
    assert lp.fetch("https://www.linkedin.com/in/alexrivera/",
                    urls=[REPOST_DOM]).profile is None
    # The client's post reshared by someone else: the JSON-LD author is the resharer,
    # so there is no follower count for the client on it and None is the honest answer.
    assert lp.fetch("https://www.linkedin.com/in/jordan-novak-peric/",
                    urls=[REPOST_LD]).profile is None


def test_duplicate_refs_are_fetched_once(monkeypatch):
    seen = _offline(monkeypatch)
    res = lp.fetch(urls=[IMAGES, lp.permalink(IMAGES), f"urn:li:activity:{IMAGES}"])
    assert len(seen["urls"]) == 1
    assert len(res.items) == 1


# ── transport ───────────────────────────────────────────────────────────────

def test_throttle_holds_the_measured_ceiling():
    """4 workers on a fast page would otherwise run at 20 req/s, past the 22.6 req/s
    where the first 429 appeared. The floor is what bounds it, not the pool size."""
    t = lp._Throttle(50.0)
    import time
    start = time.monotonic()
    for _ in range(5):
        t.wait()
    assert time.monotonic() - start >= 4 / 50.0
    assert lp.MAX_RPS <= 18.5, "measured clean ceiling"


def test_a_429_is_retried_before_it_is_given_up_on(monkeypatch):
    """The observed 429 cleared in 0.6s, so it is a burst throttle, not a ban."""
    calls = {"n": 0}
    slept = []

    class _Err(lp.urllib.error.HTTPError):
        def __init__(self):
            super().__init__("u", 429, "Too Many Requests", {}, None)

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _Err()

        class _R:
            status = 200

            def read(self):
                return PAGES[IMAGES].encode()

            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False
        return _R()

    monkeypatch.setattr(lp.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(lp.time, "sleep", lambda s: slept.append(s))
    status, body = lp._get(lp.permalink(IMAGES), lp._Throttle(0))
    assert status == 200 and body
    assert calls["n"] == 3
    assert slept == [lp.BACKOFF_S, lp.BACKOFF_S * 2], "backoff must actually back off"


def test_no_secret_is_ever_put_in_a_url(monkeypatch):
    seen = _offline(monkeypatch)
    lp.fetch(urls=[IMAGES], keys={"APIFY_TOKEN": "t0k"})
    assert seen["urls"] == [lp.permalink(IMAGES)]
    assert "t0k" not in seen["urls"][0]


# ── registry and persistence ────────────────────────────────────────────────

def test_it_is_registered_but_never_auto_selected():
    from corpus import adapters
    assert adapters.get("linkedin_public") is lp.ADAPTER
    for rel in ("competitor", "influencer", "own", "client"):
        assert lp.ADAPTER not in adapters.for_platform("linkedin", rel), (
            "this route cannot take the profile URL cmd_sync passes it")
    assert lp.ADAPTER.shallow is True


def test_notes_admit_what_the_route_cannot_do():
    """`corpus adapters` prints this. Overstating coverage here is worse than
    having no adapter at all."""
    n = lp.ADAPTER.notes.lower()
    assert "cannot discover" in n
    assert "shares" in n


def test_persist_round_trip(tmp_path, monkeypatch):
    """The real contract: what this yields has to land in the schema unchanged."""
    conn = store.connect(tmp_path / "c.db")
    aid = store.upsert_account(conn, platform="linkedin",
                               handle="jordan-novak-peric", relationship="own")
    _offline(monkeypatch)
    res = lp.fetch("https://www.linkedin.com/in/jordan-novak-peric/", urls=list(PAGES))
    counts = sync.persist(conn, lp.ADAPTER, aid, res)
    assert counts["new"] == len(PAGES)

    row = conn.execute("SELECT * FROM item WHERE platform_item_id=?",
                       (REPOST_LD,)).fetchone()
    assert row["is_repost"] == 1
    assert row["author_name"] == "Jordan Novak Perić"
    assert row["source"] == "linkedin_public"
    assert row["m_reactions"] == 12
    assert json.loads(row["raw_json"])["ld"]["@type"] == "SocialMediaPosting"

    # Re-running must update the same rows, not append new ones. This is the whole
    # reason the id is the paid route's id.
    again = sync.persist(conn, lp.ADAPTER, aid, res)
    assert again["new"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM item").fetchone()["c"] == len(PAGES)
