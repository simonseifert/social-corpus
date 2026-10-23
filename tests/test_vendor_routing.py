"""Route order across the vendor adapters, and the new platform targets.

The order is the product decision here: cheapest MARGINAL price first, a free
allowance only breaking ties, shallow routes last, and never the session-cookie
Instagram route for a public account.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import corpus.adapters as pkg  # noqa: E402
from corpus import discover  # noqa: E402
from corpus.adapters.base import Adapter, FetchResult  # noqa: E402


def _names(platform, rel="competitor"):
    # Through the package, not a from-import: another test reloads the registry.
    return [a.name for a in pkg.for_platform(platform, rel)]


def test_linkedin_order():
    """HarvestAPI $0.04/1k, then the Apify actor $2/1k (deep), then Bright Data,
    which is cheaper per record but documents ~10 posts per profile: shallow."""
    assert _names("linkedin") == ["linkedin_harvest", "linkedin_posts",
                                  "brightdata_linkedin"]


def test_instagram_order():
    """ScrapeCreators $0.16/1k < Apify $0.50/1k < Bright Data $1.50/1k marginal
    (5k free records/month only breaks ties), then the shallow free route."""
    got = _names("instagram")
    assert got == ["scrapecreators_instagram", "instagram_public",
                   "brightdata_instagram", "instagram_free"]
    assert "instagram_client" not in got


def test_tiktok_and_facebook_have_a_route():
    assert _names("tiktok") == ["scrapecreators_tiktok"]
    assert _names("facebook") == ["scrapecreators_facebook"]


def test_a_free_allowance_breaks_ties_but_never_beats_a_lower_price():
    noop = lambda *a, **k: FetchResult()  # noqa: E731
    cheap = Adapter("cheap", "zz", noop, ("competitor",), cost_per_1k=1.0)
    quota = Adapter("quota", "zz", noop, ("competitor",), cost_per_1k=1.5,
                    free_monthly_records=5000)
    tie = Adapter("tie", "zz", noop, ("competitor",), cost_per_1k=1.5)
    try:
        for a in (tie, quota, cheap):
            pkg.REGISTRY[a.name] = a
        assert _names("zz") == ["cheap", "quota", "tie"]
    finally:
        for n in ("cheap", "quota", "tie"):
            pkg.REGISTRY.pop(n, None)


def test_every_paid_vendor_route_declares_its_key_and_price():
    for a in pkg.REGISTRY.values():
        if a.name.startswith(("brightdata_", "scrapecreators_", "linkedin_harvest")):
            assert a.needs and a.cost_per_1k and a.estimate(10) > 0, a.name


def test_targets_for_new_platforms():
    t = discover.parse_target("https://www.tiktok.com/@student.center.portville?lang=hr")
    assert (t.platform, t.handle, t.url) == (
        "tiktok", "student.center.portville",
        "https://www.tiktok.com/@student.center.portville")
    f = discover.parse_target("https://www.facebook.com/bistroharbor/")
    assert (f.platform, f.handle, f.url) == (
        "facebook", "bistroharbor", "https://www.facebook.com/bistroharbor/")
    assert discover.parse_target("https://www.facebook.com/profile.php?id=1") is None
    assert discover.parse_target("https://www.facebook.com/groups/123") is None
    # existing shapes are untouched
    assert discover.parse_target("https://www.instagram.com/bistroharbor/").platform \
        == "instagram"
