"""The scheduled path: the nightly script's failure policy and the unit wiring.

The script runs against a fake `corpus` and fake ssh/rsync on PATH, so the order
of steps and what happens when one fails are tested without touching a database,
a vendor, or the off-box host.
"""
import configparser
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "corpus-nightly.sh"
UNITS = ROOT / "deploy" / "systemd"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash")


def _fake(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


@pytest.fixture
def box(tmp_path):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    calls = tmp_path / "calls"
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "corpus-20261020T010000.db.gz").write_text("old")
    (backups / "corpus-20261022T013000.db.gz").write_text("new")
    (backups / "analysis-20261022T013000.db.gz").write_text("new")
    # fake corpus: logs its subcommand; FAIL_<sub>=rc makes it fail;
    # HAS_RETENTION=1 makes `--help` advertise retention
    corpus = _fake(tmp_path / "corpus", f"""
if [ "$1" = "--help" ]; then
  echo "usage: corpus {{add,sync,enrich,backup,health}}"
  [ -n "$HAS_RETENTION" ] && echo "    retention   delete old media"
  exit 0
fi
echo "corpus $*" >> {calls}
eval rc=\\${{FAIL_$1:-0}}
exit $rc
""")
    _fake(bin_ / "ssh", f'echo "ssh $*" >> {calls}\n'
                        '[ -n "$SSH_DOWN" ] && exit 255\nexit 0\n')
    _fake(bin_ / "rsync", f'echo "rsync $*" >> {calls}\nexit ${{RSYNC_RC:-0}}\n')
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}",
           "CORPUS_BIN": str(corpus), "CORPUS_BACKUP_DIR": str(backups),
           "CORPUS_OFFSITE_HOST": "backuphost"}
    for k in ("HAS_RETENTION", "SSH_DOWN", "RSYNC_RC"):
        env.pop(k, None)

    def run(**extra):
        if calls.exists():
            calls.unlink()
        p = subprocess.run(["bash", str(SCRIPT)], env={**env, **extra},
                           capture_output=True, text=True, timeout=60)
        log = calls.read_text().splitlines() if calls.exists() else []
        return p, log
    return run


def _steps(log):
    return [ln.split()[1] for ln in log if ln.startswith("corpus ")]


def test_a_clean_night_runs_every_step_in_order(box):
    p, log = box(HAS_RETENTION="1")
    assert p.returncode == 0, p.stdout + p.stderr
    assert _steps(log) == ["sync", "enrich", "enrich", "retention", "backup", "health"]
    assert "corpus retention --execute" in log
    assert "corpus enrich --ocr" in log
    rsync = [ln for ln in log if ln.startswith("rsync ")][0]
    # the newest snapshot of each database, not the old one
    assert "corpus-20261022T013000.db.gz" in rsync
    assert "analysis-20261022T013000.db.gz" in rsync
    assert "corpus-20261020T010000" not in rsync
    assert rsync.endswith("backuphost:corpus-backups-offsite/")


def test_retention_is_skipped_until_the_subcommand_exists(box):
    p, log = box()
    assert p.returncode == 0
    assert "retention" not in _steps(log)


def test_a_failed_sync_still_backs_up_but_never_runs_retention(box):
    p, log = box(HAS_RETENTION="1", FAIL_sync="1")
    assert p.returncode == 1
    assert _steps(log) == ["sync", "enrich", "enrich", "backup", "health"]
    assert "failures: sync(1)" in p.stdout


def test_a_step_that_finds_the_lock_taken_is_skipped_not_failed(box):
    """Exit 75 = another corpus run holds the lock (a dashboard job, a manual sync).
    Alerting on that would page the operator for using their own tool."""
    p, log = box(FAIL_sync="75")
    assert p.returncode == 0, p.stdout
    assert "skipped: another corpus run holds the lock" in p.stdout
    assert _steps(log) == ["sync", "enrich", "enrich", "backup", "health"]


def test_a_failed_backup_ships_nothing_off_box(box):
    p, log = box(FAIL_backup="1")
    assert p.returncode == 1
    assert not any(ln.startswith("rsync") for ln in log)
    assert "health" in _steps(log)


def test_offsite_host_unreachable_is_logged_not_a_failure(box):
    p, log = box(SSH_DOWN="1")
    assert p.returncode == 0, p.stdout
    assert "unreachable" in p.stdout
    assert not any(ln.startswith("rsync") for ln in log)


def test_rsync_failing_while_the_offsite_host_is_up_is_a_failure(box):
    p, _ = box(RSYNC_RC="23")
    assert p.returncode == 1
    assert "offsite" in p.stdout.splitlines()[-1]


def test_no_offsite_host_configured_skips_the_copy(box):
    p, log = box(CORPUS_OFFSITE_HOST="")
    assert p.returncode == 0, p.stdout
    assert "off-box copy skipped" in p.stdout
    assert not any(ln.startswith(("rsync", "ssh")) for ln in log)


def test_unhealthy_is_a_failure(box):
    p, _ = box(FAIL_health="2")
    assert p.returncode == 1 and "health(2)" in p.stdout


# ── unit files ──────────────────────────────────────────────────────────────

def _unit(name):
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read(UNITS / name)
    return cp


@pytest.mark.parametrize("name", ["corpus-nightly.service", "corpus-weekly.service",
                                  "ytdlp-upgrade.service"])
def test_every_scheduled_unit_alerts_on_failure(name):
    assert _unit(name)["Unit"]["OnFailure"] == "corpus-notify@%n.service"


def test_the_alert_unit_cannot_alert_about_itself():
    u = _unit("corpus-notify@.service")
    assert "OnFailure" not in u["Unit"]
    assert u["Service"]["ExecStart"].endswith("corpus notify --unit %i")


def test_nightly_unit_shape():
    s = _unit("corpus-nightly.service")["Service"]
    assert s["TimeoutStartSec"] == "2h" and s["IOSchedulingClass"] == "idle"
    text = (UNITS / "corpus-nightly.service").read_text()
    assert "CORPUS_TZ=" in text and "PATH=%h/.local/bin:" in text
    # the checkout path is a placeholder install-systemd.sh fills in
    assert "@CORPUS_REPO@/.venv/bin/corpus" in text
    assert "deploy/corpus-nightly.sh" in s["ExecStart"]
    assert _unit("corpus-nightly.timer")["Timer"]["OnCalendar"] == "*-*-* 01:30:00"


def test_weekly_timer_is_monday_morning():
    assert _unit("corpus-weekly.timer")["Timer"]["OnCalendar"] == "Mon *-*-* 08:00:00"
    assert _unit("corpus-weekly.service")["Service"]["ExecStart"].endswith(
        "corpus weekly --send")


def test_scripts_are_executable_and_parse():
    for s in ("corpus-nightly.sh", "install-systemd.sh", "ytdlp-upgrade.sh"):
        path = ROOT / "deploy" / s
        assert os.access(path, os.X_OK), s
        assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
