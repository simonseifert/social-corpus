"""Credential loading and the Apify plan guard.

Keys live in ~/.reach/keys.env, the same file reach, ig-saves and places read. One
place to put a key beats four.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

REACH_KEYS = Path("~/.reach/keys.env").expanduser()
LOCAL_ENV = Path(__file__).resolve().parents[1] / ".env"

# Apify Store actors CAN be priced per subscription tier: in August 2026
# xquik/x-tweet-scraper billed $0.015 per tweet on FREE and $0.00015 on every paid tier,
# exactly 100x. By 2026-10-22 every actor corpus uses charged FREE the same as BRONZE
# (read live from /v2/acts/<id>), so refusing a free plan outright had become a false
# refusal. The guard now checks what actually costs money, live, before a large run:
#   1. the actor's price on THIS account's tier vs the paid (BRONZE) price, and
#   2. the run's estimate vs what is left of the monthly usage budget.
# plan.tier is the field price cards are keyed on; plan.id is a marketing name.
FREE_TIERS = {"FREE"}
FREE_PLANS = {"FREE", "DEVELOPER", "PERSONAL_FREE"}
GUARD_THRESHOLD = 50          # items; below this a run is cents and skips the check
TIER_MULTIPLIER_LIMIT = 1.5   # refuse when this tier pays more than 1.5x the paid price


class PlanGuardError(RuntimeError):
    pass


def load(path: Path | None = None) -> dict[str, str]:
    """Parse KEY=value files into a dict. Real environment variables win."""
    out: dict[str, str] = {}
    for p in ([path] if path else [REACH_KEYS, LOCAL_ENV]):
        if not p or not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v:
                out.setdefault(k, v)
    for k in list(out) + ["APIFY_TOKEN", "GROQ_API_KEY", "GEMINI_API_KEY"]:
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out


def redacted(keys: dict[str, str]) -> dict[str, str]:
    """For printing. Never log a value."""
    return {k: f"set ({len(v)} chars)" for k, v in sorted(keys.items())}


def apify_plan(token: str, *, timeout: int = 20) -> dict:
    req = urllib.request.Request(
        "https://api.apify.com/v2/users/me",
        headers={"Authorization": f"Bearer {token}",
                 "User-Agent": "corpus-cli/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read()).get("data", {})
    except urllib.error.HTTPError as e:
        raise PlanGuardError(f"could not read Apify plan (HTTP {e.code}); "
                             f"is APIFY_TOKEN valid?") from e
    except Exception as e:
        raise PlanGuardError(f"could not reach Apify to check the plan: {e}") from e

    plan = data.get("plan") or {}
    tier = str(plan.get("tier") or "").upper()
    plan_id = str(plan.get("id") or "").upper()

    # Fail closed. An unrecognised response shape (Apify renaming a field, a proxy
    # returning something else) must read as "free" and refuse the run, because the
    # cost of a wrong refusal is a retry and the cost of a wrong pass is 100x spend.
    if tier:
        is_free = tier in FREE_TIERS
    elif plan_id:
        is_free = plan_id in FREE_PLANS
    else:
        is_free = True
    if data.get("isPaying") is False:
        # A lapsed subscription keeps its plan name and bills at free-tier rates.
        is_free = True

    return {
        "username": data.get("username"),
        "plan_id": plan.get("id"),
        "tier": plan.get("tier"),
        "is_paying": data.get("isPaying"),
        "monthly_usage_usd": plan.get("monthlyUsageCreditsUsd"),
        "is_free": is_free,
    }


def _get(url: str, token: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                               "User-Agent": "corpus-cli/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read()).get("data") or {}
    except urllib.error.HTTPError as e:
        raise PlanGuardError(f"could not read {url.rsplit('/v2/', 1)[-1]} "
                             f"(HTTP {e.code}); is APIFY_TOKEN valid?") from e
    except Exception as e:
        raise PlanGuardError(f"could not reach Apify to check the plan: {e}") from e


def _events(token: str, actor: str) -> dict:
    """The actor's current pay-per-event price card: {event name: pricing}."""
    data = _get(f"https://api.apify.com/v2/acts/{actor}", token)
    infos = data.get("pricingInfos") or []
    if not infos:
        return {}
    return ((infos[-1].get("pricingPerEvent") or {}).get("actorChargeEvents") or {})


def _multiplier(events: dict, tier: str) -> float:
    worst = 1.0
    for ev in events.values():
        tiers = ev.get("eventTieredPricingUsd")
        if not tiers:
            continue
        paid = (tiers.get("BRONZE") or {}).get("tieredEventPriceUsd")
        mine = (tiers.get(tier) or {}).get("tieredEventPriceUsd")
        if mine is None or not paid:
            return float("inf")
        worst = max(worst, mine / paid)
    return worst


def _item_price(events: dict, tier: str) -> float | None:
    """What one result costs on this tier: the event the vendor flags as primary
    (the per-result charge), else the dearest event that is not the once-per-run
    start fee.

    Taking the dearest event outright read apidojo's $0.025 `location-query`, an
    input mode corpus never uses, as the per-post price, and refused every
    Instagram run as a 50x price rise (field test, 2026-10-23)."""
    primary = {n: ev for n, ev in events.items() if ev.get("isPrimaryEvent")}
    prices = []
    for name, ev in (primary or events).items():
        if "start" in name.lower():
            continue
        tiers = ev.get("eventTieredPricingUsd")
        p = ((tiers.get(tier) or {}).get("tieredEventPriceUsd") if tiers
             else ev.get("eventPriceUsd"))
        if isinstance(p, (int, float)):
            prices.append(float(p))
    return max(prices) if prices else None


def tier_multiplier(token: str, actor: str, tier: str) -> float:
    """Highest ratio, across the actor's events, of this tier's price to BRONZE's.
    1.0 for flat pricing. Fails closed: a tier the price card does not list counts as
    unknown and refuses, because a wrong pass is the expensive mistake."""
    return _multiplier(_events(token, actor), tier)


# Every run carries maxTotalChargeUsd sized from corpus's own per-item price with
# 1.5-2x headroom. If the vendor raises the price past that, the cap stops the run
# early and Apify still reports SUCCEEDED: the dataset looks complete, a comment
# thread is marked fetched, a backfill reads as the whole account. Nothing in the
# response says the cap was hit, so the guard refuses before the run instead.
PRICE_DRIFT_LIMIT = 1.25


def assert_can_spend(token: str, planned_items: int, *, est_usd: float = 0.0,
                     actors: tuple[str, ...] = (),
                     prices: dict[str, float] | None = None,
                     threshold: int = GUARD_THRESHOLD) -> dict | None:
    """Refuse a large Apify run that would be overpriced on this tier, priced above
    what corpus sizes its spend caps for (`prices`: actor -> expected $/item), or
    would not fit the remaining monthly budget. Small runs pass unchecked: three
    items are cents whatever the tier, and a round-trip on every command is the
    worse trade."""
    if planned_items <= threshold:
        return None
    info = apify_plan(token)
    tier = str(info.get("tier") or "").upper() or ("FREE" if info["is_free"] else "")
    for actor in actors:
        events = _events(token, actor) if tier else {}
        mult = _multiplier(events, tier) if tier else float("inf")
        if mult > TIER_MULTIPLIER_LIMIT:
            raise PlanGuardError(
                f"{actor} charges {'an unknown multiple' if mult == float('inf') else f'{mult:.0f}x'} "
                f"the paid price on tier {tier or 'unknown'!r}. {planned_items} items "
                f"could cost dollars instead of cents. Upgrade, or re-run with a limit "
                f"of {threshold} or fewer.")
        want = (prices or {}).get(actor)
        live = _item_price(events, tier) if want else None
        if want and live and live > want * PRICE_DRIFT_LIMIT:
            raise PlanGuardError(
                f"{actor} now charges ${live:g} per item; corpus expects ${want:g} and "
                f"sizes its spend cap from that, so the run would stop early and look "
                f"complete. Update the price in corpus before running.")
    lim = _get("https://api.apify.com/v2/users/me/limits", token)
    used = ((lim.get("current") or {}).get("monthlyUsageUsd"))
    cap = ((lim.get("limits") or {}).get("maxMonthlyUsageUsd"))
    if used is not None and cap is not None and used + est_usd > cap:
        raise PlanGuardError(
            f"this run (~${est_usd:.2f}) does not fit the Apify monthly budget: "
            f"${used:.2f} of ${cap:.2f} used. Apify refuses runs past the cap; raise "
            f"it or pay at console.apify.com/billing.")
    info["budget_left_usd"] = None if used is None or cap is None else round(cap - used, 2)
    return info


# Kept so external callers (and old notes) do not break; same semantics as before
# when called without actors: budget only.
assert_paid_plan = assert_can_spend
