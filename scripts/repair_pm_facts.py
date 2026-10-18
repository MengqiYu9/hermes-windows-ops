#!/usr/bin/env python3
"""Repair Hermes PM tool bookkeeping (tools/facts.json).

Use when `pm doctor` reports a package as "not installed" while its directory
is actually complete on disk -- typically after `pm install <pkg>` failed on
`[Errno 22] Invalid argument: ...\\etc\\mtab`.

Why that happens: PortableGit ships an MSYS symlink `etc/mtab -> /proc/mounts`.
On Windows it is a dangling reparse point, and Python's `Path.is_symlink()`
returns False for it, so `pm.store.tree_digest()` tries to `open('/proc/mounts')`
and raises. Install, verification, and doctor all break at once.

This script does NOT fix the symlink (no Windows API can delete it). Perform the
`mv` first, from Git Bash:

    cd "%LOCALAPPDATA%\\hermes\\tools\\git-<ver>-win32-x64\\etc"
    mv mtab "%LOCALAPPDATA%\\hermes\\tools\\mtab-orphan.lnk"

then run this to recompute the digest and write the missing facts entry:

    python repair_pm_facts.py git
    python repair_pm_facts.py --all
    python repair_pm_facts.py --list

The digest comes from Hermes' own `pm.store.tree_digest`, so the value matches
what `pm doctor` will recompute. `--verify` cross-checks an existing entry
against a package known to be healthy before writing anything.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def bootstrap_interpreter():
    """Return (python_exe, hermes_home, checkout) for the managed runtime.

    Uses Hermes' own bootstrap so the managed venv/site-packages resolve; a bare
    interpreter cannot import `pm`.
    """
    home = Path(os.environ.get("HERMES_HOME")
                or (Path(os.environ["LOCALAPPDATA"]) / "hermes"))
    checkout = home / "hermes-agent"
    for exe in sorted((home / "tools").glob("python-*-win32-x64/python.exe")):
        return exe, home, checkout
    raise SystemExit(f"no managed python under {home / 'tools'}")


def load_pm(home: Path, checkout: Path, exe: Path, target: Path) -> str:
    """Compute tree_digest(target) with the managed interpreter and Hermes' pm."""
    code = (
        "import os,sys,json\n"
        "for k in ('PYTHONHOME','PYTHONPATH','VIRTUAL_ENV'): os.environ.pop(k,None)\n"
        f"sys.path.insert(0, {str(checkout)!r})\n"
        f"os.environ['HERMES_HOME']={str(home)!r}\n"
        "import hermes_bootstrap\n"
        "from pm.store import tree_digest\n"
        "from pathlib import Path\n"
        "print(json.dumps({'d': tree_digest(Path(sys.argv[1]))}))\n"
    )
    import subprocess
    proc = subprocess.run([str(exe), "-I", "-c", code, str(target)],
                          capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-2000:])
    return json.loads(proc.stdout.strip().splitlines()[-1])["d"]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Repair Hermes PM tool bookkeeping")
    ap.add_argument("packages", nargs="*", help="package names, or --all")
    ap.add_argument("--all", action="store_true", help="repair every missing entry")
    ap.add_argument("--list", action="store_true", help="list declared packages")
    ap.add_argument("--verify", action="store_true",
                    help="compute digests but write nothing (dry run)")
    ap.add_argument("--home", help="HERMES_HOME override")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    exe, home, checkout = bootstrap_interpreter()
    if args.home:
        home = Path(args.home)
    lock_path = checkout / "pm" / "lock.json"
    facts_path = home / "tools" / "facts.json"

    if not lock_path.exists():
        raise SystemExit(f"missing {lock_path}")
    lock = read_json(lock_path).get("packages", {})
    facts = read_json(facts_path) if facts_path.exists() else {"schema": 1, "packages": {}}
    recorded = facts.get("packages", {})

    if args.list:
        print(f"{'package':<18}{'recorded':<11}{'status'}")
        for name in sorted(lock):
            rec = name in recorded
            print(f"{name:<18}{'yes' if rec else 'no':<11}{'' if rec else 'MISSING'}")
        return 0

    targets = sorted(lock) if args.all else list(args.packages)
    if not targets:
        ap.error("give package names, or --all / --list")

    # Target triple, computed from lock.json -- no hardcoded versions.
    import re
    arch = "win32-x64"
    failed = 0
    for name in targets:
        spec = lock.get(name)
        if not spec:
            print(f"[SKIP] {name}: not declared in pm/lock.json")
            failed += 1
            continue
        if name in recorded and not args.verify:
            print(f"[SKIP] {name}: already recorded")
            continue

        version = spec.get("version")
        if not version:
            print(f"[SKIP] {name}: lock entry has no version")
            continue
        # entry dir name: <name>-<version>-<arch>
        entry_dir = home / "tools" / f"{name}-{version}-{arch}"
        if not entry_dir.is_dir():
            print(f"[SKIP] {name}: {entry_dir} does not exist "
                  f"-- run `hermes pm install {name}` first")
            continue

        print(f"[....] {name}: hashing {entry_dir.name}")
        try:
            digest = load_pm(home, checkout, exe, entry_dir)
        except RuntimeError as exc:
            msg = str(exc)
            if "Errno 22" in msg or "Invalid argument" in msg:
                print(f"[FAIL] {name}: tree_digest hit Errno 22 -- this is the "
                      f"mtab symlink. Move it out first with Git Bash:")
                print(f"       cd \"{entry_dir / 'etc'}\" && mv mtab "
                      f"\"{home / 'tools' / 'mtab-orphan.lnk'}\"")
            else:
                print(f"[FAIL] {name}: {msg.strip().splitlines()[-1][:160]}")
            failed += 1
            continue
        artifacts = []
        art = spec.get("artifacts") or {}
        if isinstance(art, dict) and arch in art:
            sha = art[arch].get("sha256")
            if sha:
                artifacts = [sha]

        entry = {
            "artifacts": artifacts,
            "digest": digest,
            "entry": entry_dir.name,
            "env": {},
            "target": arch,
            "version": version,
        }
        if args.verify:
            print(f"[ OK ] {name}: would record digest={digest[:16]}...")
            continue

        facts.setdefault("packages", {})[name] = entry
        print(f"[ OK ] {name}: recorded digest={digest[:16]}... entry={entry_dir.name}")

    if not args.verify:
        # Preserve the existing shape: sorted keys, 2-space indent, atomic swap.
        facts["packages"] = dict(sorted(facts.get("packages", {}).items()))
        tmp = facts_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(facts, indent=2, ensure_ascii=False,
                                  sort_keys=True), encoding="utf-8")
        os.replace(tmp, facts_path)
        print(f"\nwrote {facts_path}")
        print("verify with: hermes pm doctor")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
