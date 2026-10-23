"""Resolve a person or a pasted link to concrete platform accounts.

Two paths, with very different confidence, and the module refuses to blur them:

  * A URL or @handle is unambiguous. Parse it, done.
  * A NAME is a search, and search is not identification. The repo author's own
    full name returns four different people on LinkedIn (a holding-company founder,
    a product manager, an HR lead, another entrepreneur) and none of them is the
    author. Instagram name search is
    worse: it returns post captions that merely contain the words.

So name lookup returns RANKED CANDIDATES for a human to confirm, never an answer.
Auto-adding the top hit would quietly build a corpus of the wrong person, and every
number downstream would be confidently wrong.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field

PLATFORM_PATTERNS = [
    ("linkedin", "person",  re.compile(r"linkedin\.com/in/([A-Za-z0-9_%-]+)", re.I)),
    ("linkedin", "company", re.compile(r"linkedin\.com/company/([A-Za-z0-9_%-]+)", re.I)),
    ("instagram", "person", re.compile(r"instagram\.com/(?!p/|reel/|reels/|stories/|explore/)"
                                       r"([A-Za-z0-9_.]+)", re.I)),
    ("youtube", "person", re.compile(r"youtube\.com/@([A-Za-z0-9_.-]+)", re.I)),
    ("tiktok", "person", re.compile(r"tiktok\.com/@([A-Za-z0-9_.]+)", re.I)),
    # Pages only: a vanity slug. profile.php?id=, groups, reels and share links are
    # not a page feed the facebook route can read.
    ("facebook", "company", re.compile(
        r"facebook\.com/(?!profile\.php|groups/|share|watch|reel|events/|pages/|people/)"
        r"([A-Za-z0-9.\-]{2,})", re.I)),
]

# Handles that look like profiles but are platform furniture.
RESERVED = {"about", "legal", "privacy", "accounts", "explore", "developer",
            "directory", "pub", "feed", "company", "school", "help", "p", "reel",
            "login", "marketplace"}


@dataclass
class Target:
    platform: str
    handle: str
    kind: str = "person"          # person | company
    url: str = ""

    def __str__(self) -> str:
        return f"{self.platform}:{self.handle}"


@dataclass
class Candidate:
    target: Target
    title: str = ""
    snippet: str = ""
    score: float = 0.0
    signals: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        sig = f"  [{', '.join(self.signals)}]" if self.signals else ""
        return f"{self.score:>4.0f}  {self.target.url or self.target}{sig}\n      {self.title[:90]}"


def parse_target(s: str) -> Target | None:
    """Parse a URL or @handle. Returns None when the string is not a profile ref."""
    s = s.strip()
    for platform, kind, pat in PLATFORM_PATTERNS:
        m = pat.search(s)
        if m:
            handle = m.group(1).rstrip("/").lower()
            if handle in RESERVED:
                continue
            if platform == "youtube":
                return Target(platform, handle, kind, f"https://www.youtube.com/@{handle}")
            if platform == "tiktok":
                return Target(platform, handle, kind, f"https://www.tiktok.com/@{handle}")
            if platform == "facebook":
                return Target(platform, handle, kind, f"https://www.facebook.com/{handle}/")
            base = ("https://www.linkedin.com/in/" if (platform, kind) == ("linkedin", "person")
                    else "https://www.linkedin.com/company/" if platform == "linkedin"
                    else "https://www.instagram.com/")
            return Target(platform, handle, kind, f"{base}{handle}/")

    m = re.fullmatch(r"@([A-Za-z0-9_.]+)", s)
    if m:
        # A bare @handle has no platform. Instagram is the convention for that shape,
        # but say so rather than pretending it was specified.
        return Target("instagram", m.group(1).lower(), "person",
                      f"https://www.instagram.com/{m.group(1).lower()}/")
    return None


def _reach_search(query: str, timeout: int = 60) -> list[dict]:
    """Search via the reach CLI. Returns [] rather than raising when unavailable."""
    try:
        proc = subprocess.run(["reach", "search", query], capture_output=True,
                              text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []

    out, cur = [], {}
    for line in proc.stdout.splitlines():
        if line.startswith("- "):
            if cur:
                out.append(cur)
            cur = {"title": line[2:].strip(), "url": "", "snippet": ""}
        elif cur and line.strip().startswith("http"):
            cur["url"] = line.strip()
        elif cur:
            cur["snippet"] += " " + line.strip()
    if cur:
        out.append(cur)
    return out


def _score(cand: Target, name: str, title: str, snippet: str,
           hints: dict | None) -> tuple[float, list[str]]:
    """Rank a candidate. Deliberately conservative: nothing here proves identity."""
    score, signals = 0.0, []
    text = f"{title} {snippet}".lower()
    parts = [p for p in re.split(r"\s+", name.lower()) if len(p) > 1]

    if all(p in text for p in parts):
        score += 30
        signals.append("full name")
    elif any(p in text for p in parts):
        score += 10

    slug = cand.handle.replace("-", "").replace("_", "").replace(".", "")
    if "".join(parts) in slug:
        score += 25
        signals.append("name in handle")

    # A LinkedIn slug ending in a long random suffix is an auto-generated one, which
    # correlates with a low-activity account, not with the person you are looking for.
    if re.search(r"-[0-9a-f]{6,}$", cand.handle):
        score -= 10
        signals.append("auto slug")

    for key, pts, label in (("location", 25, "location"),
                            ("company", 20, "company"),
                            ("keyword", 15, "keyword")):
        for want in (hints or {}).get(key, []) or []:
            if want.lower() in text:
                score += pts
                signals.append(f"{label}:{want}")
                break

    return score, signals


def find_by_name(name: str, *, platforms=("linkedin", "instagram"),
                 hints: dict | None = None, limit: int = 8) -> list[Candidate]:
    """Search for a person's profiles. Returns ranked candidates, never a verdict.

    `hints` sharpens an otherwise hopeless query on a common name:
        {"location": ["Portville", "Croatia"], "company": ["Example Media"],
         "keyword": ["automation"]}
    """
    queries = {
        "linkedin": f'site:linkedin.com/in "{name}"',
        "instagram": f'site:instagram.com "{name}"',
    }
    seen: dict[str, Candidate] = {}
    for platform in platforms:
        for hit in _reach_search(queries.get(platform, f'"{name}" {platform}')):
            t = parse_target(hit.get("url", ""))
            if not t or t.platform != platform:
                continue
            sc, sig = _score(t, name, hit.get("title", ""), hit.get("snippet", ""), hints)
            key = str(t)
            if key not in seen or seen[key].score < sc:
                seen[key] = Candidate(target=t, title=hit.get("title", "").strip(),
                                      snippet=hit.get("snippet", "").strip()[:300],
                                      score=sc, signals=sig)
    return sorted(seen.values(), key=lambda c: -c.score)[:limit]


def verify_linkedin(handle: str, *, expect: dict | None = None) -> dict:
    """Fetch a LinkedIn profile and report what it actually says.

    Confirmation is the caller's job; this only surfaces the facts to compare.
    """
    try:
        proc = subprocess.run(
            ["reach", "linkedin", f"https://www.linkedin.com/in/{handle}"],
            capture_output=True, text=True, timeout=180)
        line = next((l for l in proc.stdout.splitlines() if l.strip().startswith("{")), None)
        if not line:
            return {"ok": False, "error": "no data returned"}
        d = json.loads(line)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    loc = d.get("location") or {}
    got = {
        "ok": True,
        "name": " ".join(x for x in (d.get("firstName"), d.get("lastName")) if x),
        "headline": d.get("headline"),
        "location": loc.get("linkedinText") if isinstance(loc, dict) else loc,
        "followers": d.get("followerCount"),
        "connections": d.get("connectionsCount"),
        "url": f"https://www.linkedin.com/in/{handle}",
    }
    if expect:
        blob = json.dumps(got).lower()
        got["matches"] = {k: (str(v).lower() in blob) for k, v in expect.items()}
    return got


def resolve(inputs, *, hints: dict | None = None) -> tuple[list[Target], list[Candidate]]:
    """Split a mixed list of URLs and names into resolved targets and candidates."""
    targets, candidates = [], []
    for raw in inputs:
        t = parse_target(raw)
        if t:
            targets.append(t)
        else:
            candidates.extend(find_by_name(raw, hints=hints))
    return targets, candidates
