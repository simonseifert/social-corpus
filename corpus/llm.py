"""LLM client: OpenAI-compatible, multi-provider, with a per-minute token budget.

The shape of this file is dictated by one measurement. A full `places find` run
needs roughly 130k tokens. Groq's free tier allows 100k *per day*, and the
daily budget is invisible — the x-ratelimit headers expose only the per-minute
window, so you discover the wall by hitting it. On 2026-08-02 that killed a run
after it had already paid to scrape 26 places.

So: every provider is tried in order, a provider that reports its daily budget
spent is retired for the process, and the next one picks up. All of them speak
the OpenAI chat format, so this is bookkeeping rather than adapters.

Batching is driven by the *active* provider's tpm and ctx, because they differ
by two orders of magnitude — Gemini allows 250k TPM and a 1M context, Cerebras
30k TPM but only an 8k context on the free tier. A batch size that is efficient
on one is a hard error on the other.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque

from . import providers as config


class LLMError(RuntimeError):
    pass


class DailyLimitError(LLMError):
    """This provider's per-day budget is spent. Waiting will not help; the
    caller should move to the next provider."""


class NoProviderError(LLMError):
    pass


def estimate_tokens(text: str) -> int:
    """Deliberately pessimistic. Croatian and quoted review text tokenise worse
    than English prose, and overshooting costs a short sleep while
    undershooting costs a 429 and a wasted round-trip."""
    return int(len(text) / 3.0) + 8


def _parse_wait(detail: str) -> float | None:
    """Providers phrase waits as '10m14.304s', '1h2m', or '13.5s'.

    A regex that understands only the seconds form silently falls back to a
    short retry and burns the attempt budget against a ten-minute wall — which
    is exactly how a run died on 2026-08-02.
    """
    m = re.search(r"try again in ((?:\d+h)?(?:\d+m)?(?:[\d.]+s)?)", detail)
    if not m or not m.group(1):
        return None
    total = 0.0
    for value, unit in re.findall(r"([\d.]+)([hms])", m.group(1)):
        total += float(value) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total or None


def _is_exhausted(detail: str) -> bool:
    """True when retrying this provider is pointless for the rest of the run.

    Covers two different things that both arrive as a 429:
      - a spent per-day token budget (Groq's TPD)
      - a billing/credit problem (Gemini returns "Your prepayment credits are
        depleted" as RESOURCE_EXHAUSTED, with no retry-after)

    Getting this wrong is expensive in both directions. Treating a billing
    failure as transient means sleeping and retrying against a wall that will
    never come down; treating a per-minute 429 as fatal retires a provider that
    would have been fine ten seconds later.
    """
    # Strip URLs before matching. Groq's per-minute 429 ends with an upsell link to
    # console.groq.com/settings/BILLING, and matching the bare word inside that URL
    # classified a 3.96-second throttle as a spent budget: the provider was retired,
    # the chain emptied, and a healthy run died. Captured live on 2026-08-06 while
    # running eight items concurrently, which is exactly when TPM 429s are common.
    d = re.sub(r"https?://\S+", "", detail.lower())

    # Check the transient shapes FIRST. Groq's per-minute 429 reads "on tokens per
    # minute (TPM): Limit 8000, Requested 9572" and its request-too-large 429 reads
    # "please reduce your message size" -- neither is a spent budget, and retiring
    # the provider for the whole run on either one throws away a chain that was
    # working. Verified against live 429 bodies on 2026-08-05.
    transient = ("per minute", "tpm", "rpm", "tokens per minute",
                 "requests per minute", "reduce your message size",
                 "request too large", "try again in")
    if any(s in d for s in transient) and not any(
            s in d for s in ("per day", "tpd", "credits", "depleted", "billing")):
        return False

    return any(
        s in d
        for s in (
            "per day", "tpd", "daily", "quota",
            "credits", "depleted", "billing", "payment", "insufficient",
        )
    )


class _TokenBucket:
    """Rolling 60s window so we never present a provider with more than its TPM
    in any minute.

    A reservation has to cover max_tokens because the completion length is unknown
    until it arrives, but leaving it at max_tokens is what made Groq runs serial:
    est is roughly 660 in + 1024 reserved out, so 8000 TPM admits 4.75 calls a
    minute and a 39-item pass took 493s of pure queueing (measured 490-553s) at any
    worker count. Templates actually returned 63-995 output tokens against that
    1024, so `settle` rewrites the entry once the real length is known and hands the
    difference back to the window.
    """

    def __init__(self, tpm: int):
        self.tpm = tpm
        # Entries are mutable so a reservation can be corrected in place; take()
        # hands the entry back as the receipt.
        self.events: deque[list] = deque()
        self.lock = threading.Lock()

    def take(self, tokens: int, log=None) -> list:
        while True:
            with self.lock:
                now = time.monotonic()
                while self.events and now - self.events[0][0] > 60:
                    self.events.popleft()
                used = sum(t for _, t in self.events)
                if used + tokens <= self.tpm or not self.events:
                    entry = [now, tokens]
                    self.events.append(entry)
                    return entry
                wait = 61 - (now - self.events[0][0])
            if log:
                log(f"    · tpm budget reached ({used}/{self.tpm}), waiting {wait:.0f}s")
            time.sleep(max(1.0, wait))

    def settle(self, reservation: list | None, actual: int) -> None:
        """Correct a reservation to what the call really used.

        Clamped to the window's own ceiling: a single call charged more than tpm
        would stall the bucket for a full minute over an estimate. Under-refunding
        only costs speed, over-refunding costs a 429 that the retry path handles.
        """
        if reservation is None:
            return
        with self.lock:
            reservation[1] = max(0, min(int(actual), self.tpm))


class _State:
    """Which providers are configured, which are spent, and their budgets.

    Spent providers are remembered by NAME rather than by a position in one chain,
    because there are now two chains (public and private, see providers.py) and a
    provider retired for its daily budget is retired for both.
    """

    def __init__(self):
        self.chain: list[dict] | None = None
        self.retired: set[str] = set()
        self.buckets: dict[str, _TokenBucket] = {}

    # The old positional counter, kept as a view so callers and tests that reset it
    # (`_state.idx = 0`) keep working.
    @property
    def idx(self) -> int:
        return len(self.retired)

    @idx.setter
    def idx(self, value: int) -> None:
        if value == 0:
            self.retired.clear()

    def load(self) -> None:
        if self.chain is None:
            self.chain = config.available()
            if not self.chain:
                keys = ", ".join(p["key_env"] for p in config.PROVIDERS.values()
                                 if p.get("key_env"))
                raise NoProviderError(
                    f"No LLM provider configured. Start Ollama, or set one of: {keys} "
                    f"in ~/.reach/keys.env or the repo .env."
                )

    def current(self, chain: list[dict] | None = None) -> dict:
        """First unretired provider of `chain` (default: the public chain).

        An explicit chain is never widened: a private call whose providers are all
        spent raises rather than drifting onto the public chain.
        """
        if chain is None:
            self.load()
            chain = self.chain
        for p in chain:
            if p["name"] not in self.retired:
                return p
        if not chain:
            raise NoProviderError("No provider is allowed for this call.")
        raise NoProviderError(
            "Every allowed provider has exhausted its daily budget. "
            "Add another key or wait for the quota to reset."
        )

    def bucket(self, p: dict) -> _TokenBucket:
        if p["name"] not in self.buckets:
            self.buckets[p["name"]] = _TokenBucket(p["tpm"])
        return self.buckets[p["name"]]

    def retire(self, log=None, reason: str = "", name: str | None = None,
               chain: list[dict] | None = None) -> bool:
        """Retire a provider for the run. True if `chain` still has one left.

        The reason matters: "daily budget spent" and "billing credits depleted"
        both arrive as a 429 but need completely different action from you.
        """
        spent = name or self.current(chain)["name"]
        self.retired.add(spent)
        why = f" ({reason})" if reason else ""
        try:
            nxt = self.current(chain)["name"]
        except NoProviderError:
            if log:
                log(f"    → {spent} unusable{why}, and no providers remain")
            return False
        if log:
            log(f"    → {spent} unusable{why}, switching to {nxt}")
        return True


_state = _State()

# Which provider answered the most recent chat() on THIS thread. Analysis runs calls
# in a pool and records each result on the main thread, and with two chains the
# globally "active" provider is no longer necessarily the one that answered.
_used = threading.local()


def last_used() -> dict | None:
    return getattr(_used, "provider", None)


def active() -> dict:
    return _state.current()


def budget() -> tuple[int, int]:
    """(tpm, ctx) of the active provider — what batching must respect."""
    p = _state.current()
    return p["tpm"], p["ctx"]


def max_prompt_tokens(reserve_output: int) -> int:
    """Largest prompt that fits both one minute's budget and the context window."""
    tpm, ctx = budget()
    return max(500, min(tpm, ctx) - reserve_output - 200)


def _ollama_payload(p: dict, messages, *, json_mode, temperature, max_tokens) -> dict:
    """Ollama's native /api/chat shape. See providers.py for why not /v1."""
    payload = {
        "model": p["model"],
        "messages": messages,
        "stream": False,
        # Reasoning on a 200-word lens answer roughly tripled latency and did not
        # change the verdicts in the benchmark recorded in providers.py.
        "think": False,
        "keep_alive": "10m",
        "options": {"num_ctx": p["ctx"], "temperature": temperature,
                    "num_predict": max_tokens},
    }
    if json_mode:
        payload["format"] = "json"
    return payload


def _content(p: dict, body: dict) -> str:
    if p.get("api") == "ollama":
        return (body.get("message") or {}).get("content") or ""
    return body["choices"][0]["message"]["content"]


def chat(messages: list[dict], *, json_mode: bool = True, temperature: float = 0.2,
         max_tokens: int = 2000, retries: int = 3, log=None,
         chain: list[dict] | None = None) -> str:
    """One completion. `chain` restricts which providers may answer; analysis always
    passes one (see analyze/privacy.py), and it is never widened on failure."""
    est = sum(estimate_tokens(m["content"]) for m in messages) + max_tokens
    last = None
    _used.provider = None

    while True:
        p = _state.current(chain)
        if p.get("api") == "ollama":
            payload = _ollama_payload(p, messages, json_mode=json_mode,
                                      temperature=temperature, max_tokens=max_tokens)
        else:
            payload = {
                "model": p["model"],
                "messages": messages,
                "temperature": temperature,
                p.get("max_tokens_field", "max_completion_tokens"): max_tokens,
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
        headers = {
            "Content-Type": "application/json",
            # Several of these sit behind Cloudflare, which 403s
            # (error 1010) on urllib's default agent.
            "User-Agent": config.USER_AGENT,
        }
        if p.get("key_env"):
            headers["Authorization"] = f"Bearer {config.key(p['key_env'])}"

        exhausted, reason = False, ""
        prompt_tokens = est - max_tokens
        for attempt in range(retries):
            reservation = _state.bucket(p).take(min(est, p["tpm"]), log=log)
            req = urllib.request.Request(
                p["url"], data=json.dumps(payload).encode(), headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=p.get("timeout", 180)) as r:
                    content = _content(p, json.loads(r.read()))
                _used.provider = p
                # Hand back whatever the reservation over-charged, so the next call
                # in this minute is not queued behind output tokens nobody used.
                _state.bucket(p).settle(
                    reservation, prompt_tokens + estimate_tokens(content or ""))
                return content
            except urllib.error.HTTPError as e:
                detail = e.read().decode()[:500]
                last = f"{p['name']} HTTP {e.code}: {detail}"
                if e.code == 429:
                    if _is_exhausted(detail):
                        exhausted = True
                        d = detail.lower()
                        reason = ("billing/credits" if any(
                            x in d for x in ("credit", "billing", "payment", "insufficient"))
                            else "daily budget spent")
                        break
                    wait = (_parse_wait(detail) or 20.0) + 1
                    if wait > 300:
                        # A long retry-after on a per-minute limit is NOT exhaustion.
                        # The token bucket is per-process, so consecutive CLI runs
                        # arrive with a server-side window already full and get told
                        # to wait several minutes. Retiring the provider here killed
                        # a whole analyse run (35 of 39 items) while the account still
                        # had 863 of 1000 daily requests left. Fail this item, keep
                        # the provider, and let the next item try once the window has
                        # rolled. Only per-day and billing limits retire a provider.
                        raise LLMError(
                            f"{p['name']} rate-limited for {wait / 60:.0f} min "
                            f"(per-minute window full, not a spent budget). "
                            f"Re-run to continue; completed items are cached."
                        ) from e
                    if log:
                        log(f"    · {p['name']} 429, sleeping {wait:.0f}s")
                    time.sleep(wait)
                    continue
                if e.code in (400, 413) and "context" in detail.lower():
                    raise LLMError(
                        f"Prompt exceeds {p['name']}'s {p['ctx']} token context. "
                        f"Lower the batch size. {detail}"
                    ) from e
                if e.code < 500:
                    raise LLMError(last) from e
            except Exception as e:  # noqa: BLE001 - transient network, retry
                last = f"{p['name']}: {e}"
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))

        if exhausted:
            if _state.retire(log=log, reason=reason, name=p["name"], chain=chain):
                continue
            raise DailyLimitError(
                f"All providers exhausted. Last: {last}"
            )
        raise LLMError(f"{p['name']} failed after {retries} attempts: {last}")


def chat_json(messages: list[dict], **kw) -> dict:
    raw = chat(messages, json_mode=True, **kw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        # Models occasionally wrap JSON in prose despite json_mode.
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start : end + 1])
        raise LLMError(f"Non-JSON response: {raw[:300]}") from e
