"""Lens templates: a markdown file with frontmatter is the whole format.

Carried over from ig-saves, where a prompt being a file rather than a database row
turned out to matter more than expected: every edit is a git diff, so prompt history
is free and you can see exactly what changed when output quality moves.

The templates were written for Instagram reels and say so ("this reel", "first 3
seconds", "from the transcript"). Rendering is therefore platform-aware: a LinkedIn
text post gets told what it is, and the reel-specific framing is suppressed rather
than confusing the model into hallucinating a hook that never existed.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"

# gpt-oss-120b spends output tokens on reasoning before it emits anything. The
# ig-saves templates specify max_tokens as low as 460, which was fine for the model
# they were written against but can leave nothing for the answer here -- a request
# capped at 32 returned an empty completion and a json_validate_failed error. Floor
# every template well above its declared value.
MIN_MAX_TOKENS = 1024

_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)
_VAR = re.compile(r"\{\{(\w+)\}\}")


@dataclass
class Template:
    slug: str
    name: str
    body: str
    description: str = ""
    model: str | None = None
    temperature: float = 0.3
    max_tokens: int = MIN_MAX_TOKENS
    section: str | None = None
    path: Path | None = None

    @property
    def prompt_hash(self) -> str:
        """Identity of the prompt, so edits invalidate stale results automatically.

        Covers the model and sampling params too: the same text at a different
        temperature is a different experiment.
        """
        blob = f"{self.body}\x1f{self.model}\x1f{self.temperature}\x1f{self.max_tokens}"
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    @property
    def heading(self) -> str:
        return self.section or f"Analysis: {self.name}"


def _parse(path: Path) -> Template:
    raw = path.read_text()
    m = _FM.match(raw)
    meta, body = ({}, raw)
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            if ":" in line and not line.strip().startswith("#"):
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip().strip('"').strip("'")

    def _num(key, cast, default):
        try:
            return cast(meta[key])
        except (KeyError, ValueError, TypeError):
            return default

    return Template(
        slug=path.stem,
        name=meta.get("name", path.stem.replace("-", " ").title()),
        description=meta.get("description", ""),
        body=body.strip(),
        model=meta.get("model") or None,
        temperature=_num("temperature", float, 0.3),
        max_tokens=max(MIN_MAX_TOKENS, _num("max_tokens", int, MIN_MAX_TOKENS)),
        section=meta.get("section") or None,
        path=path,
    )


def load_all(directory: str | Path = TEMPLATE_DIR) -> dict[str, Template]:
    d = Path(directory).expanduser()
    if not d.exists():
        return {}
    return {p.stem: _parse(p) for p in sorted(d.glob("*.md"))}


def load(slug: str, directory: str | Path = TEMPLATE_DIR) -> Template:
    templates = load_all(directory)
    if slug not in templates:
        raise KeyError(
            f"no template {slug!r}. Available: {', '.join(sorted(templates)) or 'none'}"
        )
    return templates[slug]


# ── rendering ───────────────────────────────────────────────────────────────

# What a platform's content actually is, so a lens written for reels does not ask a
# LinkedIn essay about its first three seconds.
_SHAPE = {
    "short_video": "a short-form video (reel/short/tiktok) with a spoken transcript",
    "long_video":  "a long-form video with a spoken transcript",
    "carousel":    "a multi-image carousel post; the caption carries the argument",
    "image":       "a single-image post; the caption carries the argument",
    "text":        "a text-only post; there is no video, no transcript and no hook frame",
    "article":     "a long-form written article",
    "comment":     "a comment on someone else's post",
}


def context_block(*, platform: str, kind: str, author: str | None,
                  posted: str | None, metrics: dict | None) -> str:
    """Tell the model what it is looking at before the lens asks its question."""
    lines = [
        f"PLATFORM: {platform}",
        f"CONTENT TYPE: {_SHAPE.get(kind, kind)}",
    ]
    if author:
        lines.append(f"AUTHOR: {author}")
    if posted:
        lines.append(f"POSTED: {posted}")
    if metrics:
        shown = {k: v for k, v in metrics.items() if v not in (None, 0, "")}
        if shown:
            lines.append("ENGAGEMENT: " + ", ".join(f"{k}={v}" for k, v in shown.items()))
    if kind in ("text", "article", "image", "carousel"):
        lines.append(
            "NOTE: this is not video. Ignore any instruction about spoken lines, "
            "the first three seconds, or on-screen visuals; analyse the written "
            "text instead."
        )
    return "\n".join(lines)


def render(template: Template, fields: dict, *, standing_context: str = "") -> str:
    """Substitute {{vars}} and prepend context. Unknown vars become empty strings."""
    body = _VAR.sub(lambda m: str(fields.get(m.group(1), "") or ""), template.body)
    parts = [p for p in (standing_context.strip(), fields.get("_context", ""), body) if p]
    return "\n\n".join(parts).strip()


def ensure_json_word(prompt: str) -> str:
    """Groq rejects response_format=json_object unless the messages mention json.

    Verified live: "'messages' must contain the word 'json' in some form, to use
    'response_format' of type 'json_object'." Cheaper to append a line than to make
    every template author remember.
    """
    return prompt if "json" in prompt.lower() else prompt + "\n\nRespond in json."
