"""Adapter registry.

An adapter that cannot import (a missing optional dependency, a syntax error mid-edit)
must not take the whole registry down with it. Losing one platform is a degraded run;
losing the CLI is a broken tool.
"""
from __future__ import annotations

import warnings

from .base import Adapter, FetchResult, Profile, RawItem, RawMedia  # noqa: F401

REGISTRY: dict[str, Adapter] = {}
UNAVAILABLE: dict[str, str] = {}

for _mod in ("linkedin_posts", "linkedin_export", "linkedin_public",
             "instagram_public", "instagram_client", "instagram_free",
             "youtube_shorts", "linkedin_harvest", "brightdata",
             "scrapecreators"):
    try:
        _m = __import__(f"{__name__}.{_mod}", fromlist=["ADAPTER"])
        # A vendor module may serve several platforms (Bright Data, ScrapeCreators):
        # it exports ADAPTERS, one Adapter per platform, instead of ADAPTER.
        for _a in getattr(_m, "ADAPTERS", None) or (_m.ADAPTER,):
            REGISTRY[_a.name] = _a
    except Exception as e:  # noqa: BLE001 - a bad adapter must not break the rest
        UNAVAILABLE[_mod] = f"{type(e).__name__}: {e}"
        warnings.warn(f"adapter {_mod} unavailable: {e}", stacklevel=2)


def for_platform(platform: str, relationship: str | None = None) -> list[Adapter]:
    """Adapters serving a platform, optionally filtered by relationship.

    Ordered complete-first then cheapest-first, so a caller taking [0] gets the
    least expensive route that can actually finish the job. For own/client that
    puts the consented export ahead of the paid public scrape, which is both
    cheaper and richer.

    cost_per_1k is the marginal price (what the next 1,000 cost once any free
    allowance is gone), so a free quota only breaks ties; see
    Adapter.free_monthly_records for why it is not priced in as $0.

    Shallow routes sort last regardless of price. A free route that tops out at a
    dozen posts is not "the cheapest way to do this", it is a different job, and
    ranking it on cost alone would silently downgrade every backfill on its
    platform to a top-up.
    """
    # Opt-in routes (instagram_client's session cookie) are out of the routing view
    # unless their env switch is set; the inventory view (no relationship) lists
    # them so `corpus adapters` still shows they exist.
    out = [a for a in REGISTRY.values()
           if a.platform == platform
           and (relationship is None or (relationship in a.relationships
                                         and a.opted_in()))]
    return sorted(out, key=lambda a: (
        a.shallow, a.cost_per_1k if a.cost_per_1k is not None else 1e9,
        # Equal marginal price: the route with a free allowance goes first, since
        # until it runs out it costs nothing at all.
        -a.free_monthly_records))


def get(name: str) -> Adapter:
    if name not in REGISTRY:
        known = ", ".join(sorted(REGISTRY)) or "none loaded"
        extra = ""
        if UNAVAILABLE:
            extra = "  (failed to load: " + "; ".join(
                f"{k}: {v}" for k, v in UNAVAILABLE.items()) + ")"
        raise KeyError(f"no adapter {name!r}. Available: {known}{extra}")
    return REGISTRY[name]


def platforms() -> list[str]:
    return sorted({a.platform for a in REGISTRY.values()})
