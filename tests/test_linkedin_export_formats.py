"""LinkedIn's 2026 export shapes: renamed files, per-post exports, Page exports.

Synthetic files, laid out as documented by an open-source pipeline written
against real 2026 exports (KunojiLym/databricks-linkedin-analytics PR #6) and by
public parsers of the Page "All posts" sheet. See the adapter docstring for what
is verified and what is inferred.
"""
import datetime as dt
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from corpus import store, sync  # noqa: E402
from corpus.adapters import linkedin_export as le  # noqa: E402
from test_linkedin_export import write_xlsx  # noqa: E402

PID = "7481139462250881536"
URL = f"https://www.linkedin.com/posts/alexrivera_gyms-crm-activity-{PID}-AbCd"


# ── filenames ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,fmt,profile", [
    ("AggregateAnalytics_Alex Rivera_2026-06-18_2026-06-18.xlsx", "aggregate",
     "Alex Rivera"),
    ("AggregateAnalytics_Lena Marie Vogel_2025-10-01_2026-09-30 (1).xlsx",
     "aggregate", "Lena Marie Vogel"),
    ("Content_2025-08-01_2025-08-01_AlexRivera.xlsx", "aggregate", "AlexRivera"),
    (f"SinglePostAnalytics_Alex Rivera_{PID}.xlsx", "single_post", "Alex Rivera"),
    (f"PostAnalytics_AlexRivera_{PID} (2).xlsx", "single_post", "AlexRivera"),
    ("northwindprints_content_1761582620647.xls", "page", "northwindprints"),
])
def test_filenames_old_and_new_are_recognised(name, fmt, profile):
    got = le.classify_filename(name)
    assert got["format"] == fmt and got["profile"] == profile


def test_aggregate_filename_keeps_the_date_range():
    got = le.classify_filename("AggregateAnalytics_Alex Rivera_2025-10-01_2026-09-30.xlsx")
    assert (got["start"], got["end"]) == ("2025-10-01", "2026-09-30")


def test_other_files_are_not_exports():
    assert le.classify_filename("invoice_2026.xlsx") is None
    assert le.classify_filename("AggregateAnalytics_.xlsx") is None


# ── personal content export under its new name ──────────────────────────────

def _aggregate(url=URL):
    return {"TOP POSTS": [["Post URL", "Post publish date", "Engagements"],
                          [url, "6/18/2026", 120]],
            "FOLLOWERS": [["Total followers on 6/18/2026:", 4820]]}


def test_new_name_and_posts_permalinks_import(tmp_path):
    path = write_xlsx(_aggregate(), tmp_path /
                      "AggregateAnalytics_Alex Rivera_2026-06-18_2026-06-18.xlsx")
    r, notes = le.parse_workbook(path)
    assert [i.platform_item_id for i in r.items] == [PID]
    assert r.profile.handle == "alexrivera" and r.profile.follower_count == 4820
    assert r.profile.raw["filename"]["profile"] == "Alex Rivera"
    assert r.profile.raw["format"] == "aggregate"


def test_ugcpost_permalink_gets_the_namespaced_id(tmp_path):
    url = "https://www.linkedin.com/posts/alexrivera_x-ugcPost-7477502318864220160-ogG"
    r, _ = le.parse_workbook(write_xlsx(_aggregate(url), tmp_path / "a.xlsx"))
    assert r.items[0].platform_item_id == "ugcPost:7477502318864220160"


# ── per-post export ─────────────────────────────────────────────────────────

SINGLE = [
    ["Post URL", URL],
    ["Post Date", "Jun 18, 2026"],
    ["Post Publish Time", "9:41 AM"],
    ["Impressions", 12034],
    ["Members reached", 8000],
    ["Reactions", 140],
    ["Comments", 12],
    ["Reposts", 3],
    ["Saves", 9],
    ["Sends on LinkedIn", 4],
    ["Profile viewers from this post", 31],
    ["Followers gained from this post", 6],
    [None, None],
    ["Post viewer demographics", None, None],
    ["Category", "Value", "%"],
    ["Job titles", "Founder", "8.4%"],
    ["Locations", "Zagreb, Croatia", "< 1%"],
]


def test_single_post_export(tmp_path):
    path = write_xlsx({"Post analytics": SINGLE},
                      tmp_path / f"SinglePostAnalytics_Alex Rivera_{PID}.xlsx")
    r, notes = le.parse_workbook(path)
    (it,) = r.items
    assert it.platform_item_id == PID
    assert it.published_at == int(dt.datetime(2026, 6, 18, 9, 41,
                                               tzinfo=dt.timezone.utc).timestamp())
    assert it.metrics == {"impressions": 12034, "reach": 8000, "reactions": 140,
                          "comments": 12, "shares": 3, "saves": 9, "follows": 6}
    # profile viewers are not post views
    assert "views" not in it.metrics
    assert it.raw["fields"]["Sends on LinkedIn"] == 4
    assert {d["value"] for d in it.raw["demographics"]} == {"Founder", "Zagreb, Croatia"}
    assert r.profile.raw["format"] == "single_post"


def test_legacy_per_post_export_with_two_sheets(tmp_path):
    perf = [["Highlights 6/18/2026 to 6/25/2026", None]] + SINGLE[:12]
    path = write_xlsx({"PERFORMANCE": perf,
                       "TOP DEMOGRAPHICS": [["Category", "Value", "%"],
                                            ["Seniority", "Owner", "9.7%"]]},
                      tmp_path / f"PostAnalytics_AlexRivera_{PID}.xlsx")
    (it,) = le.parse_workbook(path)[0].items
    assert it.metrics["impressions"] == 12034
    assert [d["value"] for d in it.raw["demographics"]] == ["Owner"]


def test_a_post_from_both_exports_is_one_post(tmp_path):
    a = le.parse_workbook(write_xlsx(_aggregate(), tmp_path / "a.xlsx"))[0]
    b = le.parse_workbook(write_xlsx({"Post analytics": SINGLE}, tmp_path / "b.xlsx"))[0]
    assert a.items[0].platform_item_id == b.items[0].platform_item_id


def test_per_post_csv_is_read_and_flagged_as_inferred(tmp_path):
    path = tmp_path / f"SinglePostAnalytics_Alex Rivera_{PID}.csv"
    path.write_text("\n".join(",".join(f'"{c}"' if c is not None else ""
                                       for c in row) for row in SINGLE[:12]))
    r, notes = le.parse_workbook(path)
    assert r.items[0].metrics["reactions"] == 140
    assert any("not verified" in n for n in notes)


# ── company Page export ─────────────────────────────────────────────────────

PAGE_HEADER = ["Post title", "Post link", "Post type", "Campaign name", "Posted by",
               "Created date", "Campaign start date", "Campaign end date", "Audience",
               "Impressions", "Views (excluding off-site video views)",
               "Off-site views", "Clicks", "Click through rate (CTR)", "Likes",
               "Comments", "Reposts", "Follows", "Engagement rate", "Content Type"]


def _page_rows():
    return [
        ["All posts data for the selected period", *[None] * 19],
        PAGE_HEADER,
        ["We just opened our second gym.\nCome say hi.",
         "https://www.linkedin.com/feed/update/urn:li:activity:7400000000000000001",
         "Organic", None, "Alex Rivera", "10/14/2026", None, None, "All followers",
         5400, 900, 7000, 120, 0.022, 85, 9, 4, 2, 0.04, "Video"],
        ["Carousel about pricing",
         "https://www.linkedin.com/feed/update/urn:li:activity:7400000000000000002",
         "Sponsored", "Q4", "Alex Rivera", "10/15/2026", None, None, None,
         2000, None, None, 30, 0.015, 20, 1, 0, 0, 0.02, "Document"],
    ]


def test_page_export_carries_text_kind_and_clean_metrics(tmp_path):
    # Named .xls the way LinkedIn names it; this one is a zip (re-saved), which
    # is read by content, not extension.
    path = write_xlsx({"Metrics": [["Aggregated metrics", None, None],
                                   ["Date", "Impressions (total)", "Clicks (total)"],
                                   ["10/14/2026", 5400, 120], ["10/15/2026", 2000, 30]],
                       "All posts": _page_rows()},
                      tmp_path / "acme-gyms_content_1761582620647.xls")
    r, notes = le.parse_workbook(path)
    by = {i.platform_item_id: i for i in r.items}
    video = by["7400000000000000001"]
    assert video.body.startswith("We just opened our second gym.")
    assert video.kind == "short_video" and by["7400000000000000002"].kind == "carousel"
    assert video.metrics == {"impressions": 5400, "views": 900, "clicks": 120,
                             "reactions": 85, "comments": 9, "shares": 4,
                             "follows": 2}
    assert video.author_name == "Alex Rivera"
    assert video.raw["row"]["Off-site views"] == 7000
    assert notes == []
    # the page's daily totals ride on the profile; no URL is guessed for it
    assert r.profile.raw["page_metrics"] == {"days": 2, "impressions": 7400,
                                             "clicks": 150}
    assert r.profile.url is None


def test_page_export_as_csv(tmp_path):
    import csv
    path = tmp_path / "acme-gyms_content_1761582620647.csv"
    with path.open("w", newline="") as f:
        csv.writer(f).writerows(_page_rows())
    r, _ = le.parse_workbook(path)
    assert len(r.items) == 2 and r.items[0].metrics["reactions"] in (85, 20)


def test_binary_xls_without_a_reader_says_what_to_do(tmp_path, monkeypatch):
    path = tmp_path / "acme_content_1.xls"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 512)
    monkeypatch.setitem(sys.modules, "xlrd", None)
    monkeypatch.setattr("shutil.which", lambda n: None)
    monkeypatch.setattr(le, "SOFFICE_PATHS", ())   # the Mac has LibreOffice.app
    with pytest.raises(ValueError, match="save as .xlsx"):
        le.parse_workbook(path)


def test_page_posts_reach_the_database_with_their_text(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    aid = store.upsert_account(conn, platform="linkedin", handle="acme-gyms",
                               relationship="client")
    path = write_xlsx({"All posts": _page_rows()}, tmp_path / "p.xlsx")
    out = sync.run(conn, le.ADAPTER, account_id=aid, target=str(path))
    assert out["status"] == "ok", out
    bodies = [r["body"] for r in conn.execute("SELECT body FROM item ORDER BY id")]
    assert any(b and "second gym" in b for b in bodies)


# ── folders ─────────────────────────────────────────────────────────────────

def test_a_folder_picks_the_newest_export(tmp_path):
    old = write_xlsx(_aggregate(), tmp_path /
                     "Content_2025-08-01_2025-08-01_AlexRivera.xlsx")
    new = write_xlsx(_aggregate(), tmp_path /
                     "AggregateAnalytics_Alex Rivera_2026-06-18_2026-06-18.xlsx")
    (tmp_path / "holiday.jpg").write_bytes(b"x")
    t = time.time()
    os.utime(old, (t - 86400, t - 86400))
    os.utime(new, (t, t))
    assert le.find_export(tmp_path) == new
    r, _ = le.parse_workbook(tmp_path)
    assert r.profile.raw["file"] == new.name


def test_a_folder_with_two_peoples_exports_is_refused(tmp_path):
    write_xlsx(_aggregate(), tmp_path /
               "AggregateAnalytics_Alex Rivera_2026-06-18_2026-06-18.xlsx")
    write_xlsx(_aggregate(), tmp_path /
               "AggregateAnalytics_Some Client_2026-06-18_2026-06-18.xlsx")
    with pytest.raises(ValueError, match="several profiles"):
        le.parse_workbook(tmp_path)


def test_a_folder_without_exports_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="no LinkedIn export"):
        le.parse_workbook(tmp_path)
