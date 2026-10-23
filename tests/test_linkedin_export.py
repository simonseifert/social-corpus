"""LinkedIn Analytics export adapter, offline against a recorded fixture.

The fixture is the sheet grids, and the test rebuilds a real .xlsx from them, so
openpyxl and the cell-type handling are exercised rather than stubbed. The point
is that a LinkedIn header rename fails here loudly instead of quietly writing
nulls into the corpus.
"""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import store, sync  # noqa: E402
from corpus.adapters import linkedin_export as le  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "linkedin_export.json"

# Epochs the fixture must normalise to, computed independently of the adapter.
EPOCH_MAR14 = 1773446400   # 2026-03-14T00:00:00Z, arrives as a datetime cell
EPOCH_MAR19 = 1773878400   # 2026-03-19T00:00:00Z, arrives as "3/19/2026"
EPOCH_FEB28 = 1772236800   # 2026-02-28T00:00:00Z, arrives as Excel serial 46081

ID_A = "7438214077065216731"
ID_B = "7431501234567890123"


def _cell(v):
    if isinstance(v, dict) and "__dt__" in v:
        return dt.datetime.fromisoformat(v["__dt__"])
    return v


def write_xlsx(sheets: dict, path: Path) -> Path:
    import openpyxl
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, grid in sheets.items():
        ws = wb.create_sheet(title=title)
        for row in grid:
            ws.append([_cell(c) for c in row])
    wb.save(path)
    return path


@pytest.fixture
def sheets():
    return json.loads(FIXTURE.read_text())["sheets"]


@pytest.fixture
def export(sheets, tmp_path):
    return write_xlsx(sheets, tmp_path / "linkedin_analytics.xlsx")


@pytest.fixture
def result(export):
    return le.fetch(str(export))


# ── normalisation contract ──────────────────────────────────────────────────

def test_two_ranking_tables_merge_into_one_row_per_post(result):
    """The sheet lists each post twice, once per ranking. Three posts, not six."""
    assert len(result.items) == 3
    ids = {i.platform_item_id for i in result.items}
    assert ID_A in ids and ID_B in ids


def test_merge_survives_differing_tracking_params(result):
    """The two tables link the same post with different utm params.

    Merging on the URL string would produce a duplicate row on every sync, so the
    activity id is the key.
    """
    a = [i for i in result.items if i.platform_item_id == ID_A]
    assert len(a) == 1
    assert a[0].metrics["reactions"] == 412 and a[0].metrics["impressions"] == 15302


def test_published_at_is_utc_epoch_seconds_from_all_three_encodings(result):
    by_id = {i.platform_item_id: i for i in result.items}
    assert by_id[ID_A].published_at == EPOCH_MAR14          # datetime cell
    assert by_id[ID_B].published_at == EPOCH_MAR19          # m/d/yyyy string
    article = next(i for i in result.items if "/pulse/" in i.url)
    assert article.published_at == EPOCH_FEB28              # Excel serial
    assert all(isinstance(i.published_at, int) for i in result.items)


def test_kind_comes_from_the_url_shape(result):
    by_id = {i.platform_item_id: i for i in result.items}
    assert by_id[ID_A].kind == "text"
    assert by_id[ID_B].kind == "text"
    assert next(i for i in result.items if "/pulse/" in i.url).kind == "article"


def test_kind_cannot_see_media_and_says_text(tmp_path):
    """A known blind spot, pinned so nobody reads `text` here as a fact.

    Feed permalinks are identical for a plain post, an image, a document carousel
    and a native video, and the export has no media column. Every non-pulse post
    therefore lands as `text`. item.kind is first-writer-wins in store.upsert_item,
    so a scraper route that DOES know cannot correct it afterwards.
    """
    grid = [["Post URL", "Post publish date", "Engagements"]]
    for n, tail in enumerate(("video-post", "document-carousel", "photo-post")):
        grid.append([f"https://www.linkedin.com/posts/alexrivera_{tail}"
                     f"-activity-74382140770652167{n:02d}-_kRw", "3/14/2026", 10])
    r, _ = le.parse_workbook(write_xlsx({"TOP POSTS": grid}, tmp_path / "k.xlsx"))
    assert len(r.items) == 3
    assert {i.kind for i in r.items} == {"text"}


def test_impressions_and_reach_ride_in_metrics_not_columns(result):
    """Neither is a metric_snapshot column, so they must land in metrics_raw."""
    b = next(i for i in result.items if i.platform_item_id == ID_B)
    assert b.metrics["impressions"] == 20418
    assert set(b.metrics) - set(sync._METRIC_COLS) >= {"impressions", "engagements"}


def test_composite_engagements_is_kept_under_its_own_name(result):
    """Engagements has no breakdown. It fills reactions but stays labelled."""
    b = next(i for i in result.items if i.platform_item_id == ID_B)
    assert b.metrics["reactions"] == 388
    assert b.metrics["engagements"] == 388


def test_raw_preserves_the_untouched_vendor_row(result):
    """raw is the file's own row, not this adapter's reading of it.

    Vendor header text verbatim, values uncoerced, one entry per ranking table the
    post appeared in. If raw held the projection instead, a wrong projection could
    never be redone from the corpus.
    """
    a = next(i for i in result.items if i.platform_item_id == ID_A)
    rows = a.raw["rows"]
    assert len(rows) == 2, "the post is in both ranking tables"
    eng, imp = rows[0], rows[1]
    assert "utm_source=share" in eng["Post URL"], "raw must keep the full URL"
    assert eng["Engagements"] == 412 and imp["Impressions"] == 15302
    assert imp["Post Publish Date"] == "2026-03-14T00:00:00"
    assert "Post publish date" in eng, "the two blocks' header casing differs"
    assert a.url == a.url.split("?")[0], "url is the cleaned one, raw keeps the rest"
    assert json.dumps(a.raw), "raw has to survive json.dumps into raw_json"


def test_raw_keeps_columns_this_adapter_cannot_read(sheets, tmp_path):
    """The columns most worth keeping are the ones with no mapping yet.

    LinkedIn adds columns without notice. One that is neither a URL, a date nor a
    recognised metric must still reach raw_json, or the export has to be re-run to
    recover it and old files are gone.
    """
    grid = [list(r) for r in sheets["TOP POSTS"]]
    grid[2] = ["Post URL", "Post publish date", "Engagements", "Engagement rate",
               "Content type"]
    for row in grid[3:]:
        row[3:] = ["4.2%", "Image"]
    r, _ = le.parse_workbook(write_xlsx({"TOP POSTS": grid},
                                        tmp_path / "newcols.xlsx"))
    row = next(i for i in r.items if i.platform_item_id == ID_A).raw["rows"][0]
    assert row["Content type"] == "Image"
    assert row["Engagement rate"] == "4.2%"
    assert all("rate" not in m for i in r.items for m in i.metrics), \
        "a ratio must not be read as a counter"


def test_no_media_is_extracted_because_the_export_has_none(result):
    assert all(i.media == [] for i in result.items)


def test_daily_rows_become_profile_not_items(result):
    """ENGAGEMENT and FOLLOWERS are account-level. Three posts, no daily items."""
    assert len(result.items) == 3
    p = result.profile
    assert p.follower_count == 4820
    assert p.handle == "alexrivera", "handle is recovered from the permalink"
    assert p.raw["engagement"]["days"] == 2
    assert p.raw["followers"]["gained_in_period"] == 17
    assert p.posts_total is None, "top 50 is not the account's post count"


def test_account_level_signals_are_preserved(result):
    raw = result.profile.raw
    assert raw["discovery"] == {"impressions": 128456, "reach": 41230,
                                "period": "3/8/2025 - 3/7/2026"}
    demo = raw["demographics"]
    assert {"category": "Locations", "value": "Zagreb, Croatia",
            "percent": 12.0, "cell": "12.0%"} in demo
    assert not any(d["value"] == "Value" for d in demo), "header row must not leak"
    suppressed = next(d for d in demo if d["value"] == "11-50 employees")
    assert suppressed["percent"] == 0.5 and suppressed["cell"] == "< 1%", \
        "the midpoint is an estimate, so what LinkedIn actually said is kept"


def test_cost_is_zero(result):
    assert result.cost_usd == 0.0


# ── persistence + idempotency ───────────────────────────────────────────────

def _persist(tmp_path, result, name="t.db"):
    conn = store.connect(tmp_path / name)
    aid = store.upsert_account(conn, platform="linkedin", handle="alexrivera",
                               relationship="client")
    first = sync.persist(conn, le.ADAPTER, aid, result)
    return conn, aid, first


def test_persisting_twice_yields_no_new_rows(tmp_path, export):
    conn, aid, first = _persist(tmp_path, le.fetch(str(export)))
    assert first == {"seen": 3, "new": 3, "changed": 0}

    second = sync.persist(conn, le.ADAPTER, aid, le.fetch(str(export)))
    assert second["new"] == 0 and second["changed"] == 0

    assert conn.execute("SELECT COUNT(*) c FROM item").fetchone()["c"] == 3
    assert conn.execute(
        "SELECT COUNT(*) c FROM metric_snapshot").fetchone()["c"] == 3, \
        "unchanged metrics must not append a second snapshot"


def test_impressions_land_in_metrics_raw(tmp_path, export):
    conn, _aid, _ = _persist(tmp_path, le.fetch(str(export)))
    row = conn.execute(
        """SELECT ms.metrics_raw, ms.reactions FROM metric_snapshot ms
           JOIN item i ON i.id = ms.item_id WHERE i.platform_item_id=?""",
        (ID_B,)).fetchone()
    assert row["reactions"] == 388
    assert json.loads(row["metrics_raw"])["impressions"] == 20418


def test_account_carries_the_demographics(tmp_path, export):
    conn, aid, _ = _persist(tmp_path, le.fetch(str(export)))
    acct = conn.execute("SELECT follower_count, raw_json FROM account WHERE id=?",
                        (aid,)).fetchone()
    assert acct["follower_count"] == 4820
    assert json.loads(acct["raw_json"])["demographics"]


# ── defensiveness ───────────────────────────────────────────────────────────

def test_sheets_are_found_case_insensitively(sheets, tmp_path):
    renamed = {"Discovery": sheets["DISCOVERY"], "Top Posts": sheets["TOP POSTS"],
               "  followers ": sheets["FOLLOWERS"]}
    r = le.fetch(str(write_xlsx(renamed, tmp_path / "renamed.xlsx")))
    assert len(r.items) == 3
    assert r.profile.follower_count == 4820


def test_missing_sheets_degrade_to_notes(sheets, tmp_path):
    only_posts = {"TOP POSTS": sheets["TOP POSTS"]}
    path = write_xlsx(only_posts, tmp_path / "partial.xlsx")
    r, notes = le.parse_workbook(path)
    assert len(r.items) == 3, "a partial import is still an import"
    assert r.profile.follower_count is None
    assert {"sheet missing: DISCOVERY", "sheet missing: FOLLOWERS",
            "sheet missing: DEMOGRAPHICS"} <= set(notes)


def test_missing_column_degrades_without_crashing(sheets, tmp_path):
    """Drop the whole impressions-ranked block. Engagements alone still import."""
    trimmed = [row[:3] for row in sheets["TOP POSTS"]]
    path = write_xlsx({"TOP POSTS": trimmed}, tmp_path / "noimp.xlsx")
    r, _notes = le.parse_workbook(path)
    assert len(r.items) == 3
    assert all("impressions" not in i.metrics for i in r.items)
    assert next(i for i in r.items if i.platform_item_id == "7438214077065216731"
                ).metrics["reactions"] == 412


def test_header_is_located_by_content_not_position(sheets, tmp_path):
    """Two extra banner rows above the header must not shift the parse."""
    padded = [["LinkedIn analytics"], [None], [None]] + sheets["TOP POSTS"]
    r, _ = le.parse_workbook(write_xlsx({"TOP POSTS": padded},
                                        tmp_path / "padded.xlsx"))
    assert len(r.items) == 3


def test_unparseable_dates_are_skipped_not_guessed(sheets, tmp_path):
    grid = [list(r) for r in sheets["TOP POSTS"]]
    grid[3][1] = "whenever"     # engagements block
    grid[4][5] = "whenever"     # impressions block, same post
    r, notes = le.parse_workbook(write_xlsx({"TOP POSTS": grid},
                                            tmp_path / "baddate.xlsx"))
    assert len(r.items) == 2
    assert any("no parseable publish date" in n for n in notes)


def test_post_id_is_stable_across_row_order_and_tracking_params(sheets, tmp_path):
    """The dedup key must survive everything the vendor is free to change.

    Row order and utm/tracking parameters both move between exports. If either
    reached the id, every monthly sync would insert a fresh copy of the whole file
    instead of updating the rows already there.
    """
    grid = [list(r) for r in sheets["TOP POSTS"]]
    head, body = grid[:3], grid[3:]
    body.reverse()
    for row in body:
        for c in (0, 4):
            if isinstance(row[c], str):
                row[c] = row[c].split("?")[0] + "?utm_source=email&trackingId=9zQ%3D"
    a = le.fetch(str(write_xlsx({"TOP POSTS": sheets["TOP POSTS"]},
                                tmp_path / "one.xlsx")))
    b = le.fetch(str(write_xlsx({"TOP POSTS": head + body}, tmp_path / "two.xlsx")))
    assert sorted(i.platform_item_id for i in a.items) == \
           sorted(i.platform_item_id for i in b.items)
    assert ID_A in {i.platform_item_id for i in b.items}


def test_a_ranking_table_that_matches_nothing_is_reported(sheets, tmp_path):
    """Half an import must not read as a whole one.

    If LinkedIn moves the permalink into a cell hyperlink, values_only hands back
    the display text and the block silently contributes zero rows.
    """
    grid = [list(r) for r in sheets["TOP POSTS"]]
    for row in grid[3:]:
        row[4] = "View post"
    r, notes = le.parse_workbook(write_xlsx({"TOP POSTS": grid},
                                            tmp_path / "hyperlinked.xlsx"))
    assert len(r.items) == 3
    assert any("matched no post URLs" in n for n in notes)


def test_fraction_encoded_percentages_are_scaled(sheets, tmp_path):
    """A percent-formatted cell reads back as 0.084, a text cell as '8.4%'."""
    demo = [list(r) for r in sheets["DEMOGRAPHICS"]]
    for row in demo[1:]:
        row[2] = _cell(None) if row[2] is None else float(
            str(row[2]).replace("%", "").replace("< ", "")) / 100
    r, notes = le.parse_workbook(write_xlsx(
        {"DEMOGRAPHICS": demo, "TOP POSTS": sheets["TOP POSTS"]},
        tmp_path / "frac.xlsx"))
    got = r.profile.raw["demographics"]
    assert next(d for d in got if d["value"] == "Zagreb, Croatia")["percent"] == 12.0
    assert next(d for d in got if d["value"] == "Founder")["percent"] == 8.4
    assert any("scaled to percent" in n for n in notes)
    assert next(d for d in got if d["value"] == "Founder")["cell"] == 0.084, \
        "the untouched cell rides along so the scaling stays auditable"


def test_junk_header_row_yields_no_posts_and_says_so(tmp_path):
    path = write_xlsx({"TOP POSTS": [["nothing", "useful"], ["at", "all"]]},
                      tmp_path / "junk.xlsx")
    r, notes = le.parse_workbook(path)
    assert r.items == []
    assert any("no 'Post URL' header" in n for n in notes)


def test_since_filters_without_touching_the_file(export):
    r = le.fetch(str(export), since=EPOCH_MAR19)
    assert [i.platform_item_id for i in r.items] == [ID_B]


def test_limit_marks_the_result_truncated(export):
    r = le.fetch(str(export), limit=1)
    assert len(r.items) == 1 and r.truncated is True


def test_caller_handle_overrides_the_derived_one(export):
    r = le.fetch(str(export), handle="acme-media")
    assert r.profile.handle == "acme-media"


def test_competitor_relationship_is_refused(export):
    with pytest.raises(ValueError, match="owns the account"):
        le.fetch(str(export), relationship="competitor")
    assert le.ADAPTER.relationships == ("own", "client")


def test_missing_file_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        le.fetch(str(tmp_path / "nope.xlsx"))


def test_non_xlsx_is_a_clear_error(tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("this is not a workbook")
    with pytest.raises(ValueError, match="not a readable"):
        le.fetch(str(bad))


def test_adapter_needs_no_keys(tmp_path):
    """Preflight has to pass with an empty key dict: this route costs nothing."""
    sync.preflight(le.ADAPTER, {})
    assert le.ADAPTER.cost_per_1k == 0.0


def test_run_guards_a_bad_target(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    aid = store.upsert_account(conn, platform="linkedin", handle="alexrivera",
                               relationship="client")
    out = sync.run(conn, le.ADAPTER, account_id=aid, target=str(tmp_path / "gone.xlsx"))
    assert out["status"] == "error" and "FileNotFoundError" in \
        conn.execute("SELECT error FROM scrape_run WHERE id=1").fetchone()["error"]
