"""The one way corpus calls an Apify actor.

Every paid route goes through `call`: a blocking run-sync POST that returns the
run's dataset, with maxTotalChargeUsd on every request. That cap is the only real
defence against an unbounded run. A renamed input key does not error on Apify, it
SUCCEEDS -- with an empty dataset, or with a run that ignores maxItems/maxPosts and
bills everything on the profile.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

ENDPOINT = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
TIMEOUT_S = 310   # just above the server's 300s sync wait; below it the run keeps billing unseen


def call(actor: str, body: dict, token: str, *, charge_cap: float,
         timeout: int = TIMEOUT_S, too_slow: str = "ask for less per call") -> list[dict]:
    """Run `actor` on `body` and return its dataset rows. Raises RuntimeError."""
    req = urllib.request.Request(
        f"{ENDPOINT.format(actor=actor)}?maxTotalChargeUsd={charge_cap}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}",  # header, never the query string
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        if e.code == 408:
            raise RuntimeError(
                f"{actor} exceeded the 300s run-sync cap; {too_slow}. The run is "
                f"still going and still billing: {detail}") from e
        raise RuntimeError(f"apify {e.code} on {actor}: {detail}") from e
    except Exception as e:
        raise RuntimeError(f"apify call failed: {type(e).__name__}: {e}") from e
    if not isinstance(payload, list):
        raise RuntimeError(f"expected a dataset array, got {type(payload).__name__}")
    return [r for r in payload if isinstance(r, dict)]


def hit_cap(rows: int, price: float, cap: float) -> bool:
    """Did a run stop because it reached maxTotalChargeUsd?

    Apify ends such a run as SUCCEEDED with whatever it had, so the dataset alone
    looks complete. If one more row would not have fit under the cap, assume the cap
    is what stopped it, and do not file the result as everything there was.
    """
    return price > 0 and (rows + 1) * price > cap
