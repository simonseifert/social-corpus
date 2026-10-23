"""Backups for the thing this project calls its asset.

The whole premise is that the corpus outlives the vendors, which is empty if a disk
failure takes it. Code is in git; the database is not, and cannot be -- it holds
third-party personal data and grows into the tens of megabytes.

Two properties matter more than sophistication here:

  * A copy taken with `cp` while WAL is active can be torn. SQLite's online backup
    API takes a consistent snapshot of a live database, so that is what runs.
  * A backup you have never restored is a hope. `verify()` opens each snapshot and
    runs an integrity check plus a real query, and `restore()` exists so the path is
    exercised rather than theorised.

Media is deliberately NOT copied here. It is 55MB of derived artefacts (keyframes,
stills) that can be regenerated from the vendor, whereas the database holds the
metric time series, which cannot: a like count from three months ago is gone the
moment it is lost. `rclone sync ~/.corpus/media` covers media if it ever matters.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DIR = Path("~/.corpus/backups").expanduser()

# Enough history to survive noticing a problem late. Daily use makes this about a
# fortnight of restore points, at roughly 300KB each compressed.
KEEP = 14

# <db stem>-YYYYMMDDTHHMMSS.db[.gz]. The stem is what names the database a
# snapshot belongs to, so restore can refuse to put analysis.db over corpus.db.
_NAME = re.compile(r"^(?P<stem>.+)-\d{8}T\d{6}\.db(\.gz)?$")

# Written after every run, so `health` can tell "backed up and verified last
# night" from "nothing has run in a week" even when no new file was written.
STATE = ".last_run.json"


def stem_of(name: str | Path) -> str | None:
    """The database stem a snapshot file name belongs to, or None if it is not one."""
    m = _NAME.match(Path(name).name)
    return m.group("stem") if m else None


@dataclass
class Snapshot:
    path: Path
    taken_at: int
    bytes: int
    source: str

    @property
    def age_days(self) -> float:
        return (time.time() - self.taken_at) / 86400


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime())


def snapshot(db_path: str | Path, out_dir: str | Path = DEFAULT_DIR,
             *, compress: bool = True) -> Snapshot:
    """Take a consistent snapshot of a live database."""
    db_path = Path(db_path).expanduser()
    out_dir = Path(out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        raise FileNotFoundError(db_path)

    tmp = out_dir / f".{db_path.stem}-{_stamp()}.tmp"
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)
    try:
        # The online backup API, not a file copy: with WAL active a cp can capture a
        # torn page set that only fails when you try to restore it.
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    final = out_dir / f"{db_path.stem}-{_stamp()}.db"
    if compress:
        final = final.with_suffix(".db.gz")
        with open(tmp, "rb") as f_in, gzip.open(final, "wb", compresslevel=6) as f_out:
            shutil.copyfileobj(f_in, f_out)
        tmp.unlink()
    else:
        tmp.rename(final)

    return Snapshot(path=final, taken_at=int(time.time()),
                    bytes=final.stat().st_size, source=str(db_path))


def list_snapshots(out_dir: str | Path = DEFAULT_DIR,
                   stem: str | None = None) -> list[Snapshot]:
    out_dir = Path(out_dir).expanduser()
    if not out_dir.exists():
        return []
    snaps = []
    for p in sorted(out_dir.glob("*.db*")):
        s = stem_of(p.name)
        if s is None or p.name.startswith("."):
            continue
        # Exact stem, not a prefix: "corpus" must not claim "corpus-old-…" files.
        if stem and s != stem:
            continue
        snaps.append(Snapshot(path=p, taken_at=int(p.stat().st_mtime),
                              bytes=p.stat().st_size, source=""))
    return sorted(snaps, key=lambda s: -s.taken_at)


def prune(out_dir: str | Path = DEFAULT_DIR, *, keep: int = KEEP,
          stem: str | None = None) -> list[Path]:
    """Drop the oldest snapshots beyond `keep`. Returns what was removed."""
    snaps = list_snapshots(out_dir, stem)
    removed = []
    for s in snaps[keep:]:
        s.path.unlink(missing_ok=True)
        removed.append(s.path)
    return removed


def _open_maybe_gz(path: Path) -> Path:
    """Return a plain .db path, decompressing to a sibling temp file if needed."""
    if path.suffix != ".gz":
        return path
    tmp = path.with_suffix("")
    tmp = tmp.with_name(f".verify-{tmp.name}")
    with gzip.open(path, "rb") as f_in, open(tmp, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    return tmp


def _empty(db: Path) -> bool:
    """True when every table in the live database is empty."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%'")]
            return all(conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] == 0
                       for t in tables)
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _verify_copy(snap: Path, db: Path) -> dict:
    v = verify(snap)
    if not v["ok"] and v["integrity"] == "ok" and _empty(db):
        # A faithful copy of a database that holds nothing yet: analysis.db before
        # the first `analyze --execute`. Failing it failed every nightly backup on a
        # fresh install (field test, 2026-10-23), and a failed run prunes nothing.
        v = dict(v, ok=True, empty=True)
    return v


def verify(path: str | Path) -> dict:
    """Open a snapshot and prove it is usable.

    integrity_check alone passes on a database that is structurally fine and empty,
    so this also counts rows. A backup that restores to zero rows is not a backup.

    The tables checked are discovered rather than assumed: corpus.db and analysis.db
    have entirely different schemas, and hardcoding `item` marked every good
    analysis.db snapshot as corrupt.
    """
    path = Path(path).expanduser()
    try:
        tmp = _open_maybe_gz(path)
    except Exception as e:
        # A truncated or non-gzip file is a failed backup, not a crash. This runs
        # unattended, so it has to report rather than raise.
        return {"path": str(path), "integrity": f"unreadable: {e}",
                "counts": {}, "ok": False}

    try:
        conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%'")]
        counts = {}
        for t in tables:
            try:
                counts[t] = conn.execute(f'SELECT COUNT(*) c FROM "{t}"').fetchone()["c"]
            except sqlite3.Error:
                counts[t] = None
        conn.close()
    except sqlite3.DatabaseError as e:
        return {"path": str(path), "integrity": f"corrupt: {e}",
                "counts": {}, "ok": False}
    finally:
        if tmp != path:
            # Opening a database creates -wal/-shm beside it. Removing only the .db
            # leaves both behind on every verify, which is a slow litter of orphans
            # in the very directory that is supposed to be tidy.
            for suffix in ("", "-wal", "-shm"):
                Path(str(tmp) + suffix).unlink(missing_ok=True)

    # Usable means: structurally sound, and it actually carries data. Which table
    # holds that data differs per database, so the test is "any of them".
    ok = integrity == "ok" and any((v or 0) > 0 for v in counts.values())
    return {"path": str(path), "integrity": integrity, "counts": counts, "ok": ok}


def restore(snapshot_path: str | Path, dest: str | Path, *,
            overwrite: bool = False, expect_stem: str | None = None) -> Path:
    """Restore a snapshot to `dest`. Refuses to clobber unless asked.

    Exists so the restore path is a tested code path rather than a plan. The
    existing database is moved aside rather than deleted, because the moment you
    most want a restore is the moment you are least sure it is the right one.
    """
    snapshot_path = Path(snapshot_path).expanduser()
    dest = Path(dest).expanduser()
    if expect_stem is not None and stem_of(snapshot_path) != expect_stem:
        # `backup --restore analysis-….db.gz` used to land over corpus.db, because
        # the destination was always the corpus database whatever the snapshot was.
        raise ValueError(f"{snapshot_path.name} is not a snapshot of {expect_stem}.db")
    if dest.exists() and not overwrite:
        raise FileExistsError(f"{dest} exists; pass overwrite=True")

    v = verify(snapshot_path)
    if not v["ok"]:
        raise RuntimeError(f"refusing to restore an unusable snapshot: {v}")

    if dest.exists():
        aside = dest.with_name(f"{dest.name}.replaced-{_stamp()}")
        dest.rename(aside)
        # WAL sidecars belong to the file being replaced, not to the restored one.
        for suffix in ("-wal", "-shm"):
            side = Path(str(dest) + suffix)
            if side.exists():
                side.rename(Path(str(aside) + suffix))

    tmp = _open_maybe_gz(snapshot_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(tmp, dest)
    if tmp != snapshot_path:
        tmp.unlink(missing_ok=True)
    return dest


def content_hash(path: str | Path) -> str:
    """sha256 of a snapshot's logical content (its SQL dump), gz or plain.

    Logical rather than bytes so a page-level difference with identical rows
    (a VACUUM, a freelist change) still counts as "unchanged".
    """
    path = Path(path)
    tmp = _open_maybe_gz(path)
    try:
        conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            h = hashlib.sha256()
            for line in conn.iterdump():
                h.update(line.encode())
                h.update(b"\n")
            return h.hexdigest()
        finally:
            conn.close()
    finally:
        if tmp != path:
            for suffix in ("", "-wal", "-shm"):
                Path(str(tmp) + suffix).unlink(missing_ok=True)


def run(db_paths, out_dir: str | Path = DEFAULT_DIR, *, keep: int = KEEP) -> dict:
    """Snapshot, verify and prune. What a scheduled job calls.

    Two rules learned the hard way:

      * Nothing is pruned when any snapshot in this run failed verification. A
        run that writes a bad snapshot and then deletes the oldest good one has
        made the backups strictly worse; repeat that for a fortnight and only bad
        ones are left.
      * A database identical to its newest snapshot is not written again. The Mac
        wrote eight identical daily copies of an idle corpus, and with 14 kept
        that pushed every real restore point out of the window. The newest
        snapshot is re-verified instead, so the run still proves a restore works.
    """
    out_dir = Path(out_dir).expanduser()
    out = {"taken": [], "verified": [], "pruned": [], "errors": [], "unchanged": []}
    stems = []
    for db in db_paths:
        db = Path(db).expanduser()
        if not db.exists():
            continue
        stems.append(db.stem)
        try:
            prev = list_snapshots(out_dir, db.stem)
            snap = snapshot(db, out_dir)
            if prev and prev[0].path != snap.path:
                try:
                    same = content_hash(snap.path) == content_hash(prev[0].path)
                except sqlite3.DatabaseError:
                    same = False        # an unreadable previous one is not "same"
                if same:
                    snap.path.unlink()
                    out["unchanged"].append(str(prev[0].path))
                    v = _verify_copy(prev[0].path, db)
                    out["verified"].append(v)
                    if not v["ok"]:
                        out["errors"].append(f"{prev[0].path.name}: {v['integrity']}")
                    continue
            out["taken"].append(str(snap.path))
            v = _verify_copy(snap.path, db)
            out["verified"].append(v)
            if not v["ok"]:
                out["errors"].append(f"{snap.path.name}: {v['integrity']}")
        except Exception as e:
            out["errors"].append(f"{db.name}: {type(e).__name__}: {e}")

    if out["errors"]:
        out["prune_skipped"] = True
    else:
        for stem in stems:
            out["pruned"] += [str(p) for p in prune(out_dir, keep=keep, stem=stem)]
    _write_state(out_dir, out)
    return out


def _write_state(out_dir: Path, res: dict) -> None:
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / STATE).write_text(json.dumps({
            "at": int(time.time()),
            "ok": not res["errors"] and bool(res["verified"]),
            "errors": res["errors"][:10],
            "verified": [{"path": Path(v["path"]).name, "ok": v["ok"]}
                         for v in res["verified"]],
            "unchanged": [Path(p).name for p in res["unchanged"]],
        }, indent=1))
    except OSError:
        pass


def last_run(out_dir: str | Path = DEFAULT_DIR) -> dict | None:
    """What the last `run` recorded, or None if none has."""
    try:
        return json.loads((Path(out_dir).expanduser() / STATE).read_text())
    except (OSError, ValueError):
        return None
