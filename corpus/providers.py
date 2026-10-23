"""LLM provider chain configuration.

The fallback machinery in llm.py was lifted wholesale from the places CLI, where
each of its defences was written against a real incident: a token bucket that sleeps
instead of 429ing, a retry-after parser that understands "10m14.304s" (a seconds-only
regex is how a run died on 2026-08-02), and an exhausted-vs-throttled distinction so
a spent daily budget retires a provider instead of burning the attempt budget on it.

Only the config surface is local. Keys resolve from ~/.reach/keys.env, the same file
reach, ig-saves and places all read, so there is one place to put a key.

Reality check (2026-10-22, the author's Linux server): GROQ, GEMINI and DEEPSEEK keys exist, no
MISTRAL_API_KEY, and Ollama runs locally. So the private chain is ollama alone, and
the public chain is ollama -> groq -> gemini (deepseek only if CORPUS_PROVIDERS names
it).

Order puts groq first deliberately: gpt-oss-120b is the cheapest credible model for
per-item work at $0.15/M in and $0.60/M out, and per-item work is the bulk of the
volume. Gemini's million-token context is what you want for corpus-level passes,
which is why it sits second rather than being dropped.

PRIVACY (October 2026). What a provider does with the text decides whether it may see
it at all, before price or speed get a say. DeepSeek's hosted API stores requests in
China and may train on them, offers no DPA or SCCs, and several EU DPAs have already
acted against it; corpus had been sending client captions, transcripts and audience
comments there first. So two chains now exist:

  * PRIVATE payloads (an own/client account, or anything carrying audience comments)
    go only to CORPUS_PRIVATE_PROVIDERS, default "ollama,mistral": the local model
    on this box, or Mistral (EU-hosted, DPA). No match means the item is skipped,
    never quietly handed to the public chain. analyze/privacy.py decides the class.
  * PUBLIC payloads (competitor/influencer post text) use PROVIDER_ORDER, which now
    starts with the local model when it is reachable ($0) and does not contain
    deepseek unless CORPUS_PROVIDERS names it explicitly.
"""
from __future__ import annotations

import os
from pathlib import Path

REACH_KEYS = Path("~/.reach/keys.env").expanduser()
LOCAL_ENV = Path(__file__).resolve().parents[1] / ".env"

_loaded = False


def load() -> None:
    """Put the shared key files into the environment once. Existing env vars win.
    Parsing is keys.load's; two parsers had already drifted once."""
    global _loaded
    if _loaded:
        return
    from . import keys
    for k, v in keys.load().items():
        os.environ.setdefault(k, v)
    _loaded = True


def key(name: str, required: bool = True) -> str:
    load()
    v = os.environ.get(name, "")
    if required and not v:
        raise SystemExit(f"{name} is not set. Add it to {REACH_KEYS} or {LOCAL_ENV}.")
    return v


# Every provider here speaks the OpenAI chat format, so switching is configuration
# rather than adapters. tpm and ctx differ by two orders of magnitude across the
# list, which is why batch sizes are computed from the ACTIVE provider rather than
# fixed: a batch that is efficient on Gemini is a hard error on Cerebras.
# The local default, chosen by a measured run on 2026-10-22 (RX 6800 16GB, Vulkan,
# Ollama 0.32.14, think off, num_ctx 16384) over the same four prompts: a Croatian
# content-audit answered in Croatian, an English positioning read, a grief post that
# must come back as the PERSONAL sentinel, and the account-summary JSON.
#
#   model              fits 16GB?  tok/s      lens wall   JSON  sentinel  Croatian prose
#   gemma4:12b         yes (7.6G)  38-57      3-10s       ok    ok        good, slips
#   gemma4:26b         no (17.8G)  6-12       48-187s     ok    ok        best
#   qwen3.6:35b-a3b    no (21.5G)  8-11       71-119s     ok    ok        good; invented a
#                                                                         "50%" target
#   gemma4:12b +think  -           -          155s for the audit 12b did in 9s
#
# The box was shared during the run (another job kept swapping gpt-oss:20b in), so
# only gemma4:12b got an uncontended pass; the others' tok/s include CPU offload
# because neither fits in VRAM, which is the structural point. All three got every
# number in the account-summary right and all three returned the sentinel on the
# grief post. 12b is the default because it is the only one that stays resident
# next to anything else and answers in seconds; for a Croatian client deliverable
# CORPUS_OLLAMA_MODEL=gemma4:26b reads better and costs minutes, not money.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = "gemma4:12b"

PROVIDERS: dict[str, dict] = {
    "ollama": {
        # The native endpoint, not /v1/chat/completions: only /api/chat takes
        # options.num_ctx, and Ollama's default context (4096 at the time) silently
        # truncates a transcript-heavy prompt from the FRONT, which removes the
        # instructions rather than failing. It also takes think=false; with thinking
        # on, gemma4 spent most of each call reasoning about a 200-word answer.
        "url": f"{OLLAMA_URL}/api/chat",
        "api": "ollama",
        "key_env": None,             # no key: availability is "is it answering"
        "model": OLLAMA_MODEL,
        # Not a rate limit: a GPU has no tokens-per-minute, it has one queue. The
        # number is large so the token bucket never sleeps; max_workers is the real
        # throttle, because a 16-wide pool against one card only moves the wait
        # from the bucket into Ollama's queue and then into the HTTP timeout.
        "tpm": 10_000_000,
        "ctx": 16_384,
        "max_workers": 2,
        # A cold model load is ~30s on this card before the first token, and a
        # 2000-token answer at ~20 tok/s is another 100s. 180s (the cloud value)
        # timed out on the first call of a run.
        "timeout": 900,
        "vision": False,
        "local": True,
    },
    "mistral": {
        # EU-hosted (Paris), DPA available, and zero data retention on request.
        # The cloud option for PRIVATE payloads when the local box is not reachable.
        "url": "https://api.mistral.ai/v1/chat/completions",
        "key_env": "MISTRAL_API_KEY",
        "model": "mistral-small-latest",
        "tpm": 500_000,
        "ctx": 128_000,
        # Mistral's schema is max_tokens; it does not document max_completion_tokens
        # and an unknown field risks a 422 on every call.
        "max_tokens_field": "max_tokens",
        "vision": False,
    },
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY",
        "model": "openai/gpt-oss-120b",
        # 8000, read off x-ratelimit-limit-tokens on this account's free tier, not
        # guessed. An optimistic value here is worse than a pessimistic one: the
        # bucket paces to whatever it is told, so 12000 meant presenting Groq with
        # 50% more than it accepts, earning a 429 storm that the retry logic then
        # had to absorb. Paced correctly, the 429s mostly stop happening.
        # Free tier is also 1000 requests/day; a Dev Tier key raises both.
        "tpm": 8_000,
        "ctx": 128_000,
        "vision": False,
    },
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "key_env": "GEMINI_API_KEY",
        "model": "gemini-3.6-flash",
        "tpm": 250_000,
        "ctx": 1_000_000,
        "vision": True,
    },
    "deepseek": {
        "url": "https://api.deepseek.com/chat/completions",
        "key_env": "DEEPSEEK_API_KEY",
        # deepseek-v4-flash was retired in Oct 2026; the old name still works but is
        # served by V4.1-Flash-1017 anyway, so name what actually answers.
        "model": "deepseek-flash",
        "tpm": 1_000_000,
        "ctx": 64_000,
        "vision": False,
    },
    "cerebras": {
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "key_env": "CEREBRAS_API_KEY",
        "model": "gpt-oss-120b",
        "tpm": 30_000,
        "ctx": 8_192,  # free tier caps context here, well under a default batch
        "vision": False,
    },
    "nvidia": {
        "url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "key_env": "NVIDIA_API_KEY",
        "model": "deepseek-ai/deepseek-v3.1",
        "tpm": 60_000,
        "ctx": 128_000,
        "vision": False,
    },
}

# Rough USD per million tokens, for cost estimates only. Estimates exist so
# --dry-run can print a number before you spend; they are not billing.
PRICING: dict[str, tuple[float, float]] = {   # provider -> (input, output)
    "groq": (0.15, 0.60),
    "gemini": (0.25, 1.50),
    # PEAK rate (01-04 and 06-10 UTC weekdays); off-peak is half. Estimating at
    # peak errs high, which is the safe side for a number shown before spending.
    # Read from api-docs.deepseek.com/quick_start/pricing on 2026-10-22.
    "deepseek": (0.30, 1.20),
    "cerebras": (0.15, 0.60),
    "nvidia": (0.15, 0.60),
    "ollama": (0.0, 0.0),
    # The keyword pre-filter: a personal post is recorded under this name and no
    # provider sees it (priced at the default rate it reported spend that never was).
    "filter": (0.0, 0.0),
    # mistral-small-latest list price, Oct 2026.
    "mistral": (0.10, 0.30),
}

# The public chain. DeepSeek used to lead it on throughput (1M TPM against Groq's
# free-tier 8k: a 39-item pass spent nine minutes waiting on Groq's bucket), but
# throughput does not outrank where the text ends up: it is no longer in the default
# order and runs only when CORPUS_PROVIDERS names it. The local model leads because it
# costs nothing; Mistral next because it is the EU option; Groq and Gemini after.
DEFAULT_ORDER = "ollama,mistral,groq,gemini,cerebras,nvidia"
PROVIDER_ORDER = [
    p.strip()
    for p in os.environ.get("CORPUS_PROVIDERS", DEFAULT_ORDER).split(",")
    if p.strip()
]

# Who may see PRIVATE payloads (own/client content, audience comments). Independent
# of PROVIDER_ORDER on purpose: CORPUS_PROVIDERS=deepseek for a competitor sweep must
# not widen what a client's text can reach.
PRIVATE_PROVIDERS = [
    p.strip()
    for p in os.environ.get("CORPUS_PRIVATE_PROVIDERS", "ollama,mistral").split(",")
    if p.strip()
]


def provider(name: str) -> dict:
    """One provider's config, with CORPUS_<NAME>_MODEL / _TPM / _CTX overrides."""
    p = dict(PROVIDERS[name])
    up = name.upper()
    p["name"] = name
    p["model"] = os.environ.get(f"CORPUS_{up}_MODEL", p["model"])
    p["tpm"] = int(os.environ.get(f"CORPUS_{up}_TPM", p["tpm"]))
    p["ctx"] = int(os.environ.get(f"CORPUS_{up}_CTX", p["ctx"]))
    return p


_ollama_models: dict[str, list[str] | None] = {}


def ollama_models(url: str | None = None, timeout: float = 2.0) -> list[str] | None:
    """Model names the local Ollama serves, or None when it is not answering.

    Probed once per process: a dry run over 400 items must not make 400 probes, and
    a box that was down at the start of a run is treated as down for the run rather
    than flapping items between local and skipped.
    """
    base = (url or OLLAMA_URL).rstrip("/")
    if base not in _ollama_models:
        import json
        import urllib.request
        try:
            with urllib.request.urlopen(f"{base}/api/tags", timeout=timeout) as r:
                _ollama_models[base] = [m.get("name", "") for m in
                                        json.loads(r.read()).get("models", [])]
        except Exception:
            _ollama_models[base] = None
    return _ollama_models[base]


def usable(name: str) -> bool:
    """Can this provider take a call right now: a key for cloud ones, a running
    server that has the configured model for the local one."""
    if name not in PROVIDERS:
        return False
    load()
    cfg = provider(name)
    if cfg.get("api") == "ollama":
        models = ollama_models()
        if not models:
            return False
        want = cfg["model"]
        return want in models or f"{want}:latest" in models
    return bool(os.environ.get(cfg["key_env"] or ""))


def available() -> list[dict]:
    """The PUBLIC chain: configured providers that can take a call, in order."""
    return [provider(n) for n in PROVIDER_ORDER if usable(n)]


def private_chain() -> list[dict]:
    """The only providers a PRIVATE payload may reach. Empty means skip, never
    "fall back to the public chain"."""
    return [provider(n) for n in PRIVATE_PROVIDERS if usable(n)]


def is_local(name: str) -> bool:
    return bool(PROVIDERS.get(name, {}).get("local"))


def price_per_m(name: str) -> tuple[float, float]:
    return PRICING.get(name, (0.20, 0.80))


# Several of these APIs sit behind Cloudflare, which rejects urllib's default
# "Python-urllib/3.x" agent with a 403 (error 1010).
USER_AGENT = "corpus-cli/0.1 (+https://github.com/simonseifert/social-corpus) python-urllib"
