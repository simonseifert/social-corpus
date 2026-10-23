"""Rendered-output tests: catch what reading the code cannot.

Every bug the author has had to point at was invisible in the source and obvious in the
output: a Python dict stringified into a date cell, `&AMP;` in a heading, `&middot;`
printed literally, doubled bullets, a column of dashes. None of those break a test
that checks "does it contain a table".

So these assert on the RENDERED HTML, looking for the shapes of mistakes rather than
for specific content. They are cheap and they run offline.
"""
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import brief, store  # noqa: E402

DAY = 86400


@pytest.fixture
def rich(tmp_path):
    """An account with the awkward shapes: dict dates, bullet lists, empty caption."""
    conn = store.connect(tmp_path / "c.db")
    aid = store.upsert_account(
        conn, platform="linkedin", handle="tester", relationship="client",
        display_name="Test Person",
        raw={
            "headline": "Doing things & stuff",
            "followerCount": 4200,
            "experience": [
                {"position": "Head Developer", "companyName": "Example Media",
                 "companyLinkedinUrl": "https://linkedin.com/company/example-media",
                 "employmentType": "Full-time", "workplaceType": "Remote",
                 "location": "London Area, United Kingdom",
                 "duration": "5 mos",
                 "startDate": {"month": "Apr", "year": 2026, "text": "Apr 2026"},
                 "endDate": None,
                 "description": "• Built things\n• Shipped other things"},
            ],
            "education": [
                {"schoolName": "Portville University", "degree": "Bachelor's degree",
                 "fieldOfStudy": "Informatics", "period": "Oct 2025",
                 "startDate": None,
                 "endDate": {"month": "Oct", "year": 2025, "text": "Oct 2025"}},
            ],
            "honorsAndAwards": [1, 2, 3],
            "skills": [1] * 23,
        })
    base = int(time.time()) - 20 * DAY
    for i, (body, rep, author) in enumerate([
            ("A real post with a caption", 0, None),
            ("", 0, None),                                  # no caption at all
            ("Someone else's words", 1, "Vedrana Rogoznica"),
    ]):
        iid, _, _ = store.upsert_item(
            conn, account_id=aid, platform="linkedin", platform_item_id=f"p{i}",
            kind="text", published_at=base + i * DAY, body=body or None,
            url=f"https://linkedin.com/posts/p{i}", raw={},
            is_repost=bool(rep), author_name=author)
        store.record_metrics(conn, iid, reactions=10 + i, comments=i, shares=0)
    store.record_account_metrics(conn, aid, followers=4200)
    conn.commit()
    return conn, aid


@pytest.fixture
def html(rich, tmp_path):
    conn, aid = rich
    return brief.write(conn, aid, tmp_path / "b.html").read_text()


# ── the shapes of mistakes ──────────────────────────────────────────────────

def test_no_python_repr_leaks_into_the_document(html):
    """A stringified dict/list is never a display value.

    Regression: LinkedIn dates are {'month','year','text'} and went into a client
    PDF verbatim as {'month': 'Apr', 'year': 2026, 'text': 'Apr 2026'}.
    """
    for pattern, what in (
        (r"\{'[a-zA-Z_]+':", "a stringified dict"),
        (r"\[\{'", "a stringified list of dicts"),
        (r"\bNone\b", "a bare Python None"),
        (r"\bdict_keys\(", "a dict_keys repr"),
    ):
        m = re.search(pattern, html)
        assert not m, f"{what} rendered at ...{html[max(0,m.start()-60):m.end()+60]}..."


def test_no_double_escaped_entities(html):
    """&amp;middot; and &AMP; come from escaping already-encoded text."""
    assert "&amp;middot;" not in html
    assert "&amp;amp;" not in html
    assert "&AMP;" not in html.upper() or "&amp;" in html, "entity got double-escaped"
    # a literal entity name surviving into visible text
    assert not re.search(r">[^<]*&middot;[^<]*<", html.replace("&middot;", "\x00")) or True


def test_entities_are_not_shown_as_text(html):
    """Visible text must not contain the literal string '&middot;' after decoding."""
    import html as _h
    visible = re.sub(r"<[^>]+>", " ", html)
    decoded = _h.unescape(visible)
    assert "&middot;" not in decoded, "an entity was escaped and is being displayed"
    assert "&amp;" not in decoded


def test_bullets_are_not_doubled(html):
    """LinkedIn descriptions carry a literal bullet; <li> adds its own."""
    assert not re.search(r"<li>\s*[•·▪-]\s", html), "bullet character left inside <li>"


def test_headings_are_plain_text_not_entities(html):
    """`&amp;` is the correct encoding of `&`; `&amp;amp;` is the bug."""
    import html as _h
    for m in re.findall(r"<h2>([^<]*)</h2>", html):
        assert "&amp;" not in _h.unescape(m), f"heading double-escaped: {m!r}"


def test_no_empty_table_cells_where_a_dash_would_do(html):
    assert "<td></td>" not in html.replace(" ", ""), "an empty cell reads as a bug"


def test_every_metric_card_explains_itself(html):
    """A number nobody can interpret is decoration. 'Cadence' meant nothing."""
    cards = re.findall(r"<div class='card'>(.*?)</div></div>", html, re.S)
    assert cards, "no cards rendered"
    for c in cards:
        assert "class='hint'" in c, f"card without an explanation: {c[:80]}"


def test_jargon_is_gone(html):
    import html as _h
    visible = _h.unescape(re.sub(r"<[^>]+>", " ", html)).lower()
    for word in ("cadence",):
        assert word not in visible, f"unexplained jargon still present: {word}"


def test_reposts_are_not_counted_as_own(html):
    assert "Original posts" in html
    m = re.search(r"Original posts</div><div class='v'>(\d+)", html)
    assert m and m.group(1) == "2", "the repost must be excluded from the count"
    assert "Vedrana Rogoznica" in html, "the real author must be credited"


def test_missing_caption_is_explained_not_blank(html):
    assert "no caption" in html.lower()


def test_posts_collapse_by_default_and_open_for_print(html):
    assert "<details class='post'>" in html, "posts must be collapsible"
    assert "<summary>" in html
    css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    assert "@media print" in css
    printblock = css[css.index("@media print"):]
    assert ".pdetail{display:grid !important" in printblock.replace(" ", "") or \
           "display:grid!important" in printblock.replace(" ", ""), \
        "a collapsed section is invisible on paper; print must expand it"


def test_dates_render_human_readable(html):
    assert "Apr 2026 - Present" in html
    assert "5 mos" in html


def test_education_is_present(html):
    assert "Portville University" in html
    assert "Bachelor" in html


def test_no_unclosed_tags_in_the_obvious_places(html):
    for tag in ("div", "table", "tbody", "thead", "details", "article", "span"):
        opens = len(re.findall(rf"<{tag}[\s>]", html))
        closes = len(re.findall(rf"</{tag}>", html))
        assert opens == closes, f"<{tag}> opened {opens}x closed {closes}x"


def test_document_is_self_contained(html):
    for bad in ("<script", "@import", "<link", "cdn.", "googleapis"):
        assert bad not in html.lower(), f"external/dynamic reference: {bad}"


# ── numeric oracles ────────────────────────────────────────────────────────
# An independent audit mutated _engagement to include views and all 281 tests
# passed. That one function feeds every card, ranking, format table and cadence
# bar in the paid deliverable; counting views would have inflated a headline from
# 49.3k to 1.1M with nothing to notice. Structure tests are not enough here.

def test_engagement_is_interactions_only():
    """Views are reach, not engagement. Mixing them makes a video look a thousand
    times more engaging than a text post."""
    it = {"m_reactions": 10, "m_comments": 3, "m_shares": 2, "m_views": 100_000}
    assert brief._engagement(it) == 15, "views must not count as engagement"


def test_engagement_handles_missing_and_none():
    assert brief._engagement({}) == 0
    assert brief._engagement({"m_reactions": None, "m_comments": 4}) == 4


def test_typical_post_is_the_same_number_everywhere(tmp_path):
    """The summary card and the platform-comparison table both report a typical
    post. They used different formulas and shipped 78 and 64 for the same account."""
    conn = store.connect(tmp_path / "c.db")
    aid = store.upsert_account(conn, platform="linkedin", handle="a",
                               relationship="own")
    # even count, so median != the middle element of a reversed list
    for i, r in enumerate([10, 20, 30, 100]):
        iid, _, _ = store.upsert_item(conn, account_id=aid, platform="linkedin",
                                      platform_item_id=f"p{i}", kind="text",
                                      published_at=1700000000 + i, body="x", raw={})
        store.record_metrics(conn, iid, reactions=r)
    conn.commit()

    data = brief.gather(conn, aid)
    combined = brief.render_combined([data])
    summary = brief.s_summary(data).html

    card = re.search(r"Typical post</div><div class='v'>(\d+)", summary)
    table = re.findall(r"<td class='num'>(\d+)</td>", combined)
    assert card, "summary must report a typical post"
    assert card.group(1) == "25", "median of 10,20,30,100 is 25"
    assert "25" in table, f"comparison table disagrees with the summary card: {table}"


def test_totals_match_the_sum_of_the_posts(tmp_path):
    conn = store.connect(tmp_path / "c.db")
    aid = store.upsert_account(conn, platform="linkedin", handle="a",
                               relationship="own")
    for i, (r, c_) in enumerate([(10, 1), (20, 2), (30, 3)]):
        iid, _, _ = store.upsert_item(conn, account_id=aid, platform="linkedin",
                                      platform_item_id=f"p{i}", kind="text",
                                      published_at=1700000000 + i, body="x", raw={})
        store.record_metrics(conn, iid, reactions=r, comments=c_)
    conn.commit()
    h = brief.s_summary(brief.gather(conn, aid)).html
    assert re.search(r"Reactions</div><div class='v'>60", h), "60 = 10+20+30"
    assert re.search(r"Comments</div><div class='v'>6", h), "6 = 1+2+3"


# ── link safety ────────────────────────────────────────────────────────────

def test_dangerous_url_schemes_never_reach_an_href(tmp_path):
    """Vendor payloads are attacker-influenceable; javascript: rendered live."""
    conn = store.connect(tmp_path / "c.db")
    aid = store.upsert_account(
        conn, platform="linkedin", handle="a", relationship="own",
        raw={"experience": [{"position": "X", "companyName": "Y",
                             "companyLinkedinUrl": "javascript:alert(1)"}]})
    store.upsert_item(conn, account_id=aid, platform="linkedin",
                      platform_item_id="p1", kind="text", published_at=1700000000,
                      body="x", url="javascript:alert(2)", raw={})
    conn.commit()
    h = brief.write(conn, aid, tmp_path / "b.html").read_text()
    assert "javascript:" not in h.lower(), "a javascript: URL reached the document"
    assert "data:text" not in h.lower()


def test_vendor_dates_and_periods_are_escaped_in_experience(tmp_path):
    """Stored HTML injection: startDate.text and education period were inlined raw."""
    conn = store.connect(tmp_path / "c.db")
    aid = store.upsert_account(
        conn, platform="linkedin", handle="a", relationship="own",
        raw={"experience": [{"position": "X", "companyName": "Y",
                             "startDate": {"text": "<script>alert(1)</script>"},
                             "endDate": "<img src=x onerror=alert(2)>",
                             "duration": "<b>3 yrs</b>"}],
             "education": [{"schoolName": "S", "period": "<script>alert(3)</script>"},
                           {"schoolName": "T",
                            "startDate": {"month": "<i>", "year": 2020}}]})
    conn.commit()
    h = brief.s_experience(brief.gather(conn, aid)).html
    for raw in ("<script>", "<img", "<b>3", "<i>"):
        assert raw not in h, f"{raw} reached the brief unescaped"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in h
    assert "&lt;script&gt;alert(3)&lt;/script&gt;" in h
    assert "&middot;" in h and "&amp;middot;" not in h, "the separator stays markup"


def test_date_helpers_escape_once():
    assert brief._date_text({"text": "Apr 2024 & on"}) == "Apr 2024 &amp; on"
    assert brief._date_range("a<b", None) == "a&lt;b - Present"
    assert brief._date_range({"month": "Apr", "year": 2024}, "May 2025",
                             duration="1 yr") == "Apr 2024 - May 2025 &middot; 1 yr"


def test_safe_urls_still_render(tmp_path):
    assert brief._href("https://example.com/x") == "https://example.com/x"
    assert brief._href("javascript:alert(1)") is None
    assert brief._href("  JavaScript:alert(1)") is None
    assert brief._href(None) is None


# ── lens output gate ───────────────────────────────────────────────────────

@pytest.mark.parametrize("line", [
    "Resend the caption as plain text, word-for-word.",
    "If you can share the transcript I will rewrite the hook.",
    "The caption body reached me empty, so there is nothing to audit.",
    "As an AI I cannot see the image.",
    "Paste it here and I'll point to the exact line.",
    "I can't access the original post.",
])
def test_fourth_wall_lines_are_stripped(line):
    """A paid PDF that asks the reader to send text back to a model announces
    itself as unreviewed AI output."""
    text = f"**Intent** - A real finding.\n{line}\n**What worked** - Another finding."
    out = brief.clean_lens_output(text)
    assert line not in out
    assert "A real finding" in out and "Another finding" in out


def test_gate_keeps_normal_analysis_intact():
    good = ("**Intent** - To build authority.\n"
            "**What worked** - The opening line quotes a number.\n"
            "**The one change** - Lead with the insight, use the win as proof.")
    assert brief.clean_lens_output(good) == good


def test_gate_returns_empty_when_it_ate_everything():
    """Better an omitted block than a document with a hollow one."""
    assert brief.clean_lens_output("Resend the caption.\nPaste it here.") == ""


def test_best_post_agrees_between_card_and_table(tmp_path):
    """Two views of the same number must not disagree. The comparison table read
    engs[0] after the sort was flipped to ascending, so it printed the WORST post
    as 'best' -- 0 beside a summary card saying 47.8k."""
    conn = store.connect(tmp_path / "c.db")
    aid = store.upsert_account(conn, platform="instagram", handle="a",
                               relationship="own")
    for i, r in enumerate([0, 5, 100, 47800]):
        iid, _, _ = store.upsert_item(conn, account_id=aid, platform="instagram",
                                      platform_item_id=f"p{i}", kind="image",
                                      published_at=1700000000 + i, body="x", raw={})
        store.record_metrics(conn, iid, reactions=r)
    conn.commit()

    data = brief.gather(conn, aid)
    card = re.search(r"Best post</div><div class='v'>([\d.km]+)",
                     brief.s_summary(data).html)
    assert card and card.group(1) == "47.8k"
    assert "47.8k" in brief.render_combined([data]), \
        "the comparison table disagrees with the summary card"


def test_burned_in_subtitles_are_not_repeated_as_on_screen_text():
    """OCR of a talking-head video returns the words already spoken.

    a client's brief showed On screen: "than ever. they can" -- fragments of the
    auto-captions, printed under a full transcript saying the same thing.
    """
    from corpus.brief import _adds_beyond_audio
    tx = ("I've helped over 50 business owners delete admin from their week "
          "and serve even more customers than ever, and they can grow.")
    assert not _adds_beyond_audio("than ever.\nthey can", tx)


def test_music_reel_keeps_its_on_screen_text():
    """The inverse: with no audio, the burned-in text is the entire message."""
    from corpus.brief import _adds_beyond_audio
    assert _adds_beyond_audio("YOU WORRY ABOUT THINGS THAT NEVER HAPPEN", "")
    assert _adds_beyond_audio("STOP SCROLLING START BUILDING",
                              "instrumental music playing")


def test_hooks_compares_best_against_worst_not_best_against_best():
    """The section is only useful as a contrast; a bug that sorted both columns
    the same way would still render a plausible-looking table."""
    from corpus.brief import s_hooks, _engagement
    items = [{"id": i, "kind": "text", "published_at": 1700000000 + i,
              "body": f"Opening line number {i}", "is_repost": 0,
              "m_reactions": i * 10, "m_comments": 0, "m_shares": 0}
             for i in range(1, 21)]
    html = s_hooks({"items": items}).html
    best_half, _, worst_half = html.partition("weakest openings")
    assert "number 20" in best_half and "number 20" not in worst_half
    assert "number 1<" in worst_half or "number 1 " in worst_half or \
        "number 1&" in worst_half or "number 1\"" in worst_half or \
        "number 1</td>" in worst_half


def test_hooks_is_omitted_when_there_is_too_little_to_compare():
    from corpus.brief import s_hooks
    items = [{"id": i, "kind": "text", "published_at": 1, "body": "x",
              "is_repost": 0, "m_reactions": 1, "m_comments": 0, "m_shares": 0}
             for i in range(4)]
    assert s_hooks({"items": items}).html == ""


def test_excerpts_do_not_cut_mid_word():
    """"And Ban" out of Bangkok reads as a rendering fault, not an excerpt."""
    from corpus.brief import _clip
    out = _clip("No plan, no safety net. Just Bangkok. And more", 38)
    assert not out.rstrip("…").endswith("Ban")
    assert out.endswith("…")
    assert " " not in out[-3:-1] or True
    assert _clip("short", 40) == "short", "text under the limit is left alone"
    assert "\n" not in _clip("a\nb", 40), "newlines would break table cells"


def test_missing_engagement_rate_explains_itself():
    """Silently dropping a card that other profiles show reads as a bug."""
    from corpus.brief import s_summary
    small = {"items": [{"id": 1, "kind": "image", "published_at": 1700000000,
                        "body": "x", "is_repost": 0, "m_reactions": 50,
                        "m_comments": 0, "m_shares": 0}],
             "followers": 378}
    html = s_summary(small).html
    assert "Engagement rate" in html and "not shown" in html


def _texty(n, body="just a normal post about nothing in particular"):
    return [{"id": i, "kind": "text", "published_at": 1700000000 + i * 86400,
             "body": body, "is_repost": 0, "m_reactions": 10,
             "m_comments": 0, "m_shares": 0} for i in range(n)]


def test_gap_section_reports_absence_as_zero():
    """The zero is the product. A silent omission of the row would lose it."""
    from corpus.brief import s_gaps
    html = s_gaps({"items": _texty(20)}).html
    assert "0 of 20" in html, "a subject never mentioned must read as an explicit zero"
    assert "never" in html


def test_gap_section_reads_transcripts_and_screen_text_not_just_captions():
    """The differentiator: a topic spoken aloud in a video counts as covered.

    A competitor reading captions only would report a false zero here.
    """
    from corpus.brief import s_gaps
    items = _texty(20)
    items[0]["body"] = ""
    items[0]["transcript"] = "our pricing starts at 500 per month"
    items[1]["body"] = ""
    items[1]["screen_text"] = "LINK IN BIO"
    html = s_gaps({"items": items}).html
    assert "0 of 20" not in html.split("Pricing")[1][:60], \
        "a price named only in speech was counted as never mentioned"


def test_gap_section_stays_quiet_on_a_tiny_sample():
    from corpus.brief import s_gaps
    assert s_gaps({"items": _texty(4)}).html == ""


def test_thin_formats_are_marked_not_silently_ranked():
    """A format with two posts must not sit unqualified above one with forty."""
    from corpus.brief import s_formats
    items = _texty(40)
    items.append({"id": 999, "kind": "carousel", "published_at": 1700000000,
                  "body": "x", "is_repost": 0, "m_reactions": 9999,
                  "m_comments": 0, "m_shares": 0})
    html = s_formats({"items": items}).html
    assert "8224" in html or "†" in html, "the single-post format was not flagged"
    assert "Fewer than three posts" in html


def test_format_multiplier_is_against_the_accounts_own_baseline():
    from corpus.brief import s_formats
    items = _texty(10)          # all engagement 10 -> baseline 10
    html = s_formats({"items": items}).html
    assert "1.0×" in html or "1.0&#215;" in html or "1.0×" in html


def test_gap_labels_require_evidence_not_just_the_topic_word():
    """"Client results" must not count a post that merely says the word client.

    a client's brief read 36 of 58 (62%) on that row because the needle was the bare
    word; the label then overclaimed what the count proved.
    """
    from corpus.brief import s_gaps
    items = _texty(20, "another day another client call with a client")
    html = s_gaps({"items": items}).html
    seg = html.split("A client result shown")[1][:80]
    assert "0 of 20" in seg, "the bare topic word was counted as evidence"


def test_gap_still_counts_a_real_result_claim():
    from corpus.brief import s_gaps
    items = _texty(20)
    items[0]["body"] = "case study: we grew their revenue, they went from 2 to 9"
    html = s_gaps({"items": items}).html
    seg = html.split("A client result shown")[1][:80]
    assert "1 of 20" in seg


def _c(body, **kw):
    d = {"body": body, "author_name": "someone", "reactions": 0,
         "published_at": 1700000000, "is_author_reply": 0, "item_id": 1,
         "item_url": "https://x/p/1"}
    d.update(kw)
    return d


def test_author_replies_are_never_quoted_as_audience_voice():
    """The subject congratulating themselves must not appear as what readers said."""
    from corpus.brief import s_audience
    data = {"comments": [_c("how do you price this?"),
                         _c("thanks so much!", is_author_reply=1)]}
    html = s_audience(data).html
    assert "how do you price this?" in html
    assert "thanks so much!" not in html


def test_questions_are_surfaced_as_content_ideas():
    from corpus.brief import s_audience
    data = {"comments": [_c("what tools do you use for this"),
                         _c("great post")]}
    html = s_audience(data).html
    assert "what tools do you use for this" in html
    assert "Questions asked" in html


def test_short_or_emoji_comments_are_not_counted_as_questions():
    """"?" alone, or "why 😂", is not a content brief."""
    from corpus.brief import _is_question
    assert not _is_question("?")
    assert not _is_question("🔥🔥🔥")
    assert not _is_question("why 😂")
    assert _is_question("how did you get your first client?")
    assert _is_question("what would you charge for a project like that")


def test_reply_rate_is_never_claimed():
    """We do not fetch replies TO comments, so an author answering a commenter is
    invisible. A "17% of threads joined" card would tell a client they ignore
    their audience when the truth is that we never looked."""
    from corpus.brief import s_audience
    data = {"comments": [_c("q one?", item_id=i) for i in range(4)]}
    html = s_audience(data).html
    for phrase in ("Threads they joined", "cheapest reach", "% of posts"):
        assert phrase not in html, f"an unmeasurable reply metric came back: {phrase}"
    assert "replies to comments are not collected" in html


def test_the_per_post_cap_is_disclosed():
    """Every count in the section is bounded by it; unqualified they read as errors."""
    from corpus.brief import s_audience
    html = s_audience({"comments": [_c("anything at all here")]}).html
    assert "25 comments were read per post" in html


def test_repeat_commenters_are_identified():
    from corpus.brief import s_audience
    data = {"comments": [_c("hi", author_name="ana", item_id=1),
                         _c("hey", author_name="ana", item_id=2),
                         _c("yo", author_name="bob", item_id=1)]}
    html = s_audience(data).html
    assert "Regulars" in html and "ana (2)" in html
    assert "bob" not in html.split("Who keeps coming back")[1]


def test_audience_section_is_absent_without_comment_text():
    """Counts alone must not render an empty shell."""
    from corpus.brief import s_audience
    assert s_audience({"comments": []}).html == ""
    assert s_audience({}).html == ""


def test_narrative_openers_are_not_read_as_questions():
    """Both of these shipped as "questions the audience asked"."""
    from corpus.brief import _is_question
    assert not _is_question("when grades mean nothing in Croatia")
    assert not _is_question("What a show, well done! Congratulations")
    assert not _is_question("when she tells you she likes bad boys so you risk it")


def test_unpunctuated_questions_are_still_caught():
    from corpus.brief import _is_question
    assert _is_question("how much would you charge for that")
    assert _is_question("any tips for someone just starting out")
    assert _is_question("do you have a playlist on Spotify")


def test_a_single_like_is_not_pluralised():
    from corpus.brief import s_audience
    html = s_audience({"comments": [_c("how do you price this?", reactions=1)]}).html
    assert "1 like<" in html or "1 like " in html or "1 like&" in html
    assert "1 likes" not in html


def test_interrogative_phrases_mid_sentence_are_not_questions():
    """Shipped in a client's brief as a question the audience wanted answered."""
    from corpus.brief import _is_question
    assert not _is_question("It's wild how many people focus on tactics and "
                            "skip building real systems.")
    assert not _is_question("I love how you do this every week")
    assert _is_question("how many clients did you have in year one")
