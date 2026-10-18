#!/usr/bin/env python3
"""Back up a Hermes Agent Windows data root safely.

Uses SQLite's online backup API for `state.db` so the copy is consistent even
while the gateway is writing and a WAL is active. A plain file copy of a live
database can silently miss recent commits.

Usage:
    python backup_state.py                      # to <home>/backups/backup-<ts>/
    python backup_state.py "D:\\somewhere\\"     # to an explicit directory
    python backup_state.py --home <hermes_home> --out <dir>
    python backup_state.py --baseline-only       # print baseline, copy nothing

Also copies the small text config/credential files, and writes
`baseline.json` holding the counts that must match after any maintenance.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

SIDECAR_FILES = ["config.yaml", ".env", "auth.json", "kanban.db",
                 "channel_directory.json", "spawn-ledger.json"]


def collect_baseline(db: Path) -> dict:
    """Counts that must be identical before and after maintenance."""
    out: dict = {"collected_at": dt.datetime.now().isoformat(timespec="seconds")}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=20)
    except sqlite3.Error as exc:
        out["error"] = f"cannot open: {exc}"
        return out
    cur = con.cursor()
    try:
        for table in ("sessions", "messages"):
            try:
                out[f"{table}_count"] = cur.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error as exc:
                out[f"{table}_count"] = f"unavailable: {exc}"
        try:
            out["messages_by_active"] = {
                str(k): v for k, v in cur.execute(
                    "SELECT active, COUNT(*) FROM messages GROUP BY active")
            }
        except sqlite3.Error:
            pass
        # Forked sessions mark the auto-continue duplication bug; they are the
        # rows most likely to be damaged by a careless migration.
        try:
            out["forked_sessions"] = cur.execute(
                "SELECT id, title FROM sessions "
                "WHERE parent_session_id IS NOT NULL AND parent_session_id != '' "
                "ORDER BY started_at DESC LIMIT 20").fetchall()
            out["forked_sessions"] = [{"id": r[0], "title": r[1]}
                                      for r in out["forked_sessions"]]
        except sqlite3.Error:
            pass
        try:
            out["integrity_check"] = cur.execute(
                "PRAGMA integrity_check").fetchone()[0]
        except sqlite3.Error:
            pass
    finally:
        con.close()
    return out


def backup_db(src: Path, dst: Path) -> tuple[bool, str]:
    """Online backup; the source may be open and have an active WAL."""
    try:
        if dst.exists():
            dst.unlink()
        src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error as exc:
        return False, str(exc)
    try:
        dst_con = sqlite3.connect(str(dst))
        src_con.backup(dst_con)
        dst_con.close()
    except sqlite3.Error as exc:
        return False, f"backup failed: {exc}"
    finally:
        src_con.close()

    try:
        chk = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
        ok = chk.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        chk.close()
        return ok, ("verified" if ok else "integrity_check failed")
    except sqlite3.Error as exc:
        return False, f"verify failed: {exc}"


def main() -> int:
    # NOTE: escape every literal '%' in help strings as '%%'. argparse runs the
    # help text through %-formatting, so a raw Windows path like %LOCALAPPDATA%
    # raises ValueError: unsupported format character.
    ap = argparse.ArgumentParser(description="Back up a Hermes data root")
    ap.add_argument("--home", help="HERMES_HOME (default: %%LOCALAPPDATA%%\\hermes)")
    ap.add_argument("--out", help="destination directory")
    ap.add_argument("--baseline-only", action="store_true",
                    help="print the baseline and exit without copying")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    home = Path(args.home) if args.home else Path(
        os.environ.get("HERMES_HOME")
        or (Path(os.environ["LOCALAPPDATA"]) / "hermes"))
    db = home / "state.db"

    print(f"HERMES_HOME: {home}")
    if not db.exists():
        print(f"[FAIL] no state.db at {db}")
        return 1

    baseline = collect_baseline(db)
    print("\n=== Baseline (verify these after maintenance) ===")
    print(json.dumps(baseline, indent=2, ensure_ascii=False))

    if args.baseline_only:
        return 0

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = Path(args.out) if args.out else home / "backups" / f"backup-{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    print(f"\n=== Backing up to {dest} ===")

    ok, msg = backup_db(db, dest / "state.db")
    print(f"  state.db      {'OK  ' if ok else 'FAIL'} {msg}")
    if ok:
        size = (dest / "state.db").stat().st_size
        print(f"                 {size:,} bytes")

    for name in SIDECAR_FILES:
        src = home / name
        if src.exists():
            try:
                shutil.copy2(src, dest / name)
                print(f"  {name:<14} OK")
            except OSError as exc:
                print(f"  {name:<14} FAIL {exc}")

    facts = home / "tools" / "facts.json"
    if facts.exists():
        try:
            shutil.copy2(facts, dest / "facts.json")
            print(f"  {'facts.json':<14} OK (tool bookkeeping)")
        except OSError as exc:
            print(f"  {'facts.json':<14} FAIL {exc}")

    with open(dest / "baseline.json", "w", encoding="utf-8") as fh:
        json.dump(baseline, fh, indent=2, ensure_ascii=False)

    print(f"\n  baseline.json written. Compare it after the upgrade:")
    print(f"    python -c \"import json;a=json.load(open(r'{dest / 'baseline.json'}'));"
          f"print(a['sessions_count'],a['messages_count'])\"")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
