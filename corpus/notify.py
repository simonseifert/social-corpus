"""Tell the operator something happened, on the phone they actually read.

corpus ran unattended on a Mac for months and then silently stopped: the last
scheduled sync was 78 days before anyone noticed, because a launchd job that fails
writes to a log file nobody opens. So every scheduled unit now ends in one of two
places: a weekly digest, or a failure alert, both sent here.

The channel is a local WhatsApp REST bridge (any HTTP endpoint that accepts
(POST {"recipient","message"} with an X-API-Key header). An ntfy URL is optional
on top, for a second path that does not depend on the bridge being up. Stdlib only:
this runs from a systemd OnFailure hook, the worst possible moment to discover a
missing dependency.

Config: ~/.config/corpus/notify.json
  {"rest_url": "...", "rest_env": "<env file with API_KEY=...>",
   "recipient": "<number>@s.whatsapp.net", "ntfy_url": "https://ntfy.sh/<topic>",
   "unit_detail": "tail" | "status"}
falling back to ~/.config/notify-bridge.json (a shared file another tool on the
same box may already use for the same bridge), so the bridge is configured once. $CORPUS_NOTIFY_CONFIG overrides both (tests).

The API key is read at send time and never printed, logged or put in an error.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

CONFIG = Path("~/.config/corpus/notify.json")
FALLBACK = Path("~/.config/notify-bridge.json")

# WhatsApp takes far more, but an alert nobody scrolls is the point of a short one.
MAX_CHARS = 3500
JOURNAL_LINES = 15
JOURNAL_LINE_CHARS = 220


def _config_path() -> Path | None:
    env = os.environ.get("CORPUS_NOTIFY_CONFIG")
    if env:
        p = Path(env).expanduser()
        return p if p.is_file() else None
    for p in (CONFIG, FALLBACK):
        p = p.expanduser()
        if p.is_file():
            return p
    return None


def load_config() -> dict:
    p = _config_path()
    if p is None:
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def _api_key(env_file: str) -> str | None:
    """API_KEY from a dotenv-style file. Quotes stripped, `export ` tolerated."""
    try:
        lines = Path(env_file).expanduser().read_text().splitlines()
    except OSError:
        return None
    for ln in lines:
        ln = ln.strip()
        if ln.startswith("export "):
            ln = ln[7:].lstrip()
        if ln.startswith("API_KEY="):
            return ln.split("=", 1)[1].strip().strip("'\"") or None
    return None


def _post(req: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _send_whatsapp(cfg: dict, text: str, timeout: float) -> bool:
    url, recipient = cfg.get("rest_url"), cfg.get("recipient")
    if not (url and recipient and cfg.get("rest_env")):
        return False
    key = _api_key(cfg["rest_env"])
    if not key:
        print("notify: whatsapp: no API_KEY in the configured env file",
              file=sys.stderr)
        return False
    req = urllib.request.Request(
        url, method="POST",
        data=json.dumps({"recipient": recipient, "message": text}).encode(),
        headers={"X-API-Key": key, "Content-Type": "application/json"})
    try:
        body = _post(req, timeout)
        # The bridge answers 200 with {"success": false} when WhatsApp itself
        # refused (logged out, bad JID), so the status code alone is not delivery.
        return bool(json.loads(body or b"{}").get("success"))
    except Exception as e:  # noqa: BLE001 - an alert path must not raise
        # type + reason only: never the request, which carries the key header.
        print(f"notify: whatsapp failed: {type(e).__name__}: "
              f"{getattr(e, 'reason', '') or getattr(e, 'code', '')}", file=sys.stderr)
        return False


def _send_ntfy(cfg: dict, text: str, title: str | None, timeout: float) -> bool:
    url = cfg.get("ntfy_url")
    if not url:
        return False
    headers = {"Title": (title or "corpus")[:200]}
    if cfg.get("ntfy_token"):
        headers["Authorization"] = f"Bearer {cfg['ntfy_token']}"
    req = urllib.request.Request(url, method="POST", data=text.encode(),
                                 headers=headers)
    try:
        _post(req, timeout)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"notify: ntfy failed: {type(e).__name__}", file=sys.stderr)
        return False


def send(text: str, *, title: str | None = None, timeout: float = 20) -> bool:
    """Deliver `text` on every configured channel. True if any of them took it.

    Never raises: the callers are a failure hook and a cron job, and an exception
    here would turn "the sync failed" into "the sync failed and so did the alert".
    """
    cfg = load_config()
    if not cfg:
        print("notify: no config (~/.config/corpus/notify.json or "
              "~/.config/notify-bridge.json)", file=sys.stderr)
        return False
    text = text if len(text) <= MAX_CHARS else text[:MAX_CHARS - 1] + "…"
    ok_wa = _send_whatsapp(cfg, text, timeout)
    ok_ntfy = _send_ntfy(cfg, text, title, timeout)
    return ok_wa or ok_ntfy


# ── failure reports ─────────────────────────────────────────────────────────

# A journal tail is the most useful thing in a failure alert and the most likely
# to carry a secret: a traceback quoting a request URL with ?token=, a curl -H
# echoed by `set -x`, an env dump. Scrubbed before it leaves the box.
_S = r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTH)"
_SECRET_PATTERNS = (
    # Authorization headers whatever the scheme ("Bearer x", "Basic x", "token x"),
    # first: the NAME=value rule below would otherwise eat only the scheme word.
    (re.compile(r"(?i)\b(authorization[\"']?\s*[:=]\s*[\"']?)"
                r"(?:[A-Za-z-]+\s+)?[^\s\"',;]+"), r"\1***"),
    # Quoted JSON / dict keys: "api_key": "abc", 'token': 'abc'
    (re.compile(r"(?i)([\"'][A-Za-z0-9_.-]*" + _S + r"[A-Za-z0-9_.-]*[\"']\s*[:=]\s*)"
                r"([\"'])[^\"']*\2"), r"\1\2***\2"),
    (re.compile(r"(?i)([\"'][A-Za-z0-9_.-]*" + _S + r"[A-Za-z0-9_.-]*[\"']\s*[:=]\s*)"
                r"[^\s,}\]\"']+"), r"\1***"),
    # NAME=value / NAME: value where the name says it is a secret
    # ("keyframes: 6" and "tokens: 1200" are not secrets: the name has to END in
    # the word, optionally with a _SUFFIX.)
    (re.compile(r"(?i)\b([A-Z0-9_]*?" + _S + r"(?:_[A-Z0-9]+)*)\b(\s*[=:]\s*)(['\"]?)"
                r"(?!\*\*\*)[^\s'\"&,;]+"), r"\1\2\3***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1***"),
    # URL userinfo: https://user:pass@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1***@"),
    (re.compile(r"(?i)([?&](?:token|key|apikey|api_key|access_token|sig)=)[^&\s]+"),
     r"\1***"),
    (re.compile(r"\bapify_api_[A-Za-z0-9]+"), "***"),
    (re.compile(r"\b(?:sk|gsk|pk)-[A-Za-z0-9_-]{16,}"), "***"),
)
# Anything this long, unbroken and with digits in it is a credential or a hash;
# either way it is noise in an alert. No "/" in the class, so a long path survives.
_OPAQUE = re.compile(r"\b(?=[A-Za-z0-9+_-]*\d)[A-Za-z0-9+_-]{32,}={0,2}")


def scrub(line: str) -> str:
    for pat, repl in _SECRET_PATTERNS:
        line = pat.sub(repl, line)
    return _OPAQUE.sub("***", line)


def unit_status(unit: str) -> str:
    """"result=exit-code, exit status 1" from systemd, or "" off systemd."""
    exe = shutil.which("systemctl")
    if not exe:
        return ""
    try:
        out = subprocess.run(
            [exe, "--user", "show", unit, "-p", "Result", "-p", "ExecMainStatus"],
            capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    kv = dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)
    parts = []
    if kv.get("Result"):
        parts.append(f"result={kv['Result']}")
    if kv.get("ExecMainStatus") not in (None, ""):
        parts.append(f"exit status {kv['ExecMainStatus']}")
    return ", ".join(parts)


def journal_tail(unit: str, lines: int = JOURNAL_LINES) -> list[str]:
    """The unit's last lines from the user journal, scrubbed and trimmed.

    Empty on a box without journalctl (macOS): the alert still goes out, it just
    says less.
    """
    exe = shutil.which("journalctl")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, "--user", "-u", unit, "-n", str(lines), "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    tail = []
    for ln in out.splitlines()[-lines:]:
        ln = scrub(ln.rstrip())
        if not ln.strip():
            continue
        tail.append(ln if len(ln) <= JOURNAL_LINE_CHARS
                    else ln[:JOURNAL_LINE_CHARS - 1] + "…")
    return tail


def unit_failure_text(unit: str, tail: list[str] | None = None, *,
                      detail: str | None = None) -> str:
    """Unit name, exit status and, by default, a short scrubbed journal tail.

    The default goes to the operator's own WhatsApp, where the tail is what makes the
    alert actionable. `"unit_detail": "status"` in notify.json sends only the name
    and status, for a channel where even a scrubbed log line is too much.
    """
    detail = detail or load_config().get("unit_detail") or "tail"
    status = unit_status(unit)
    head = f"corpus: {unit} failed on {socket.gethostname()}"
    if status:
        head += f" ({status})"
    if detail == "status":
        return head + f"\njournalctl --user -u {unit} -n 50"
    if tail is None:
        tail = journal_tail(unit)
    if not tail:
        return head + "\n(no journal lines available)"
    return head + "\n\n" + "\n".join(tail) + \
        f"\n\njournalctl --user -u {unit} -n 50"
