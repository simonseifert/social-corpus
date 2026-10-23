"""Suite-wide isolation.

No test may reach a real transcriber. test_parallel's enrich tests used to post
audio to the machine's own whisper server (WHISPER_SERVER_URL in ~/.reach/keys.env),
which made them slow, GPU-dependent, and left worker threads blocked in urlopen
after the test finished, so pytest hung at exit on some runs.

providers.usable() probes the local Ollama server. On a box where it is running,
so without the second fixture a test's routing would depend on whether a GPU server
happened to be up. Default: no local model. Tests that need one stub it explicitly.
It also marks the shared key file as already loaded: providers.load() copies the real
~/.reach/keys.env into os.environ, and a later key-parsing test then read the
developer's real APIFY_TOKEN back instead of its fixture value (and printed it).
"""
import pytest


@pytest.fixture(autouse=True)
def _no_ambient_transcribers(monkeypatch):
    from corpus.enrich import transcribe as tx
    monkeypatch.setattr(tx, "_whisper_servers", lambda: [])
    monkeypatch.setattr(tx, "_env_key", lambda name: None)


@pytest.fixture(autouse=True)
def _no_local_llm(monkeypatch):
    from corpus import providers
    monkeypatch.setattr(providers, "_ollama_models", {providers.OLLAMA_URL: None})
    monkeypatch.setattr(providers, "_loaded", True)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
