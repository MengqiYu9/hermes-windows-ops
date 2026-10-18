#!/usr/bin/env python3
"""Diagnose a Hermes Agent source installation on Windows.

Read-only. Localizes an install/update/build failure in seconds by checking
the six areas that account for nearly every real-world failure:

  1. install layout        -- are the expected directories present?
  2. git reachability     -- is git able to reach GitHub directly?
  3. update channel        -- stable can silently skip main-following installs
  4. managed tool bookkeeping -- tree_digest crashes / phantom missing packages
  5. gateway process session -- Session 0 vs 1 breaks desktop authorization
  6. data integrity        -- state.db readable, baseline intact?

Usage:
    python diagnose.py
    python diagnose.py --json
    python diagnose.py --home "C:\\Users\\<user>\\AppData\\Local\\hermes"

Exit codes: 0 = no failures (warnings allowed), 1 = at least one FAIL.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

OK, WARN, FAIL, INFO = "OK", "WARN", "FAIL", "INFO"
results: list[dict] = []


def record(status: str, check: str, detail: str = "", hint: str = "") -> None:
    results.append({"status": status, "check": check, "detail": detail, "hint": hint})
    icon = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", INFO: "[INFO]"}[status]
    line = f"{icon} {check}"
    if detail:
        line += f" -- {detail}"
    print(line)
    if hint:
        print(f"         -> {hint}")


def read_text(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def run(cmd: list[str], timeout: int = 25, cwd: Path | None = None,
        strip_proxy: bool = True) -> tuple[int, str]:
    """Run a command with sandbox proxy variables removed."""
    env = dict(os.environ)
    if strip_proxy:
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                    "ALL_PROXY", "all_proxy"):
            env.pop(key, None)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              cwd=str(cwd) if cwd else None, env=env, errors="replace")
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"
    except OSError as exc:
        return 127, str(exc)


# ---------------------------------------------------------------- 1. layout

def check_layout(home: Path) -> None:
    print("\n=== 1. Install layout ===")
    checkout = home / "hermes-agent"
    tools = home / "tools"
    db = home / "state.db"

    for label, path, must in (
        ("data root", home, True),
        ("source checkout", checkout, True),
        ("hermes_cli/main.py", checkout / "hermes_cli" / "main.py", True),
        ("tools store", tools, True),
        ("config.yaml", home / "config.yaml", False),
        ("state.db", db, False),
    ):
        exists = path.exists()
        if exists or not must:
            record(OK if exists else WARN, label, str(path) if exists else "missing")
        else:
            record(FAIL, label, f"missing: {path}",
                   "check HERMES_HOME; a non-default location needs -HermesHome")

    # Leftovers that indicate an interrupted update
    stale = [p.name for p in home.glob(".hermes-update-in-progress*")]
    if stale:
        record(WARN, "stale update marker", ", ".join(stale),
               "an update was killed mid-run; a later update repairs it")

    exit_code = home / ".update_exit_code"
    if exit_code.exists():
        code = read_text(exit_code).strip()
        if code not in ("", "0"):
            record(WARN, "last update exit code", code,
                   f"see {home / 'logs' / 'update.log'}")

    # tools store hygiene
    if tools.is_dir():
        junk = []
        for entry in tools.iterdir():
            n = entry.name.lower()
            if n.startswith(".corrupt-") or n.startswith(".staging-") \
                    or n.endswith(".disabled") or "orphan" in n:
                junk.append(entry.name)
        if junk:
            record(WARN, "tools/ leftovers", ", ".join(junk)[:120],
                   "move them out with Git Bash `mv`; see SKILL.md failure mode 3")


# ------------------------------------------------------------------- 2. git

def check_git(home: Path) -> None:
    print("\n=== 2. Git reachability ===")
    checkout = home / "hermes-agent"
    if not (checkout / ".git").exists():
        record(WARN, "git checkout", "no .git; not a source install")
        return

    rc, out = run(["git", "log", "--oneline", "-1"], cwd=checkout)
    head = out.strip().splitlines()[0] if rc == 0 and out.strip() else "?"
    record(OK if rc == 0 else FAIL, "HEAD", head)

    rc, out = run(["git", "remote", "get-url", "origin"], cwd=checkout)
    record(INFO, "origin", out.strip())

    # A configured-but-dead proxy is the single most common cause.
    rc, out = run(["git", "config", "--global", "--get", "http.proxy"])
    if out.strip():
        proxy = out.strip()
        rc2, _ = run(["curl", "-s", "-m", "10", "-x", proxy,
                      "https://api.github.com", "-o", "/dev/null",
                      "-w", "%{http_code}"])
        dead = rc2 != 0
        if dead:
            record(FAIL, "global git proxy", f"{proxy} (unreachable)",
                   "git config --global --unset http.proxy https.proxy")
        else:
            record(INFO, "global git proxy", proxy)
    else:
        record(OK, "global git proxy", "none configured")

    rc, out = run(["curl", "-s", "-m", "15", "--noproxy", "*",
                   "https://api.github.com", "-o", "/dev/null",
                   "-w", "%{http_code}"])
    code = out.strip()
    if code == "200":
        record(OK, "GitHub direct reachability", "HTTP 200")
    else:
        record(WARN, "GitHub direct reachability", code or "no response",
               "github.com:443 is intermittently blocked; retry with backoff")


# ------------------------------------------------------- 3. update channel

def check_channel(home: Path) -> None:
    print("\n=== 3. Update channel ===")
    # Authoritative source: config.yaml -> update.installs.<install_id>.channel
    cfg = read_text(home / "config.yaml", 200_000)
    channels = _parse_channel_map(cfg)
    if not channels:
        record(WARN, "update.installs channels", "none recorded",
               "an unconfigured official checkout tracks stable; run "
               "`hermes update --set-channel main` to follow main")
    for install_id, ch in sorted(channels.items()):
        if ch == "stable":
            record(WARN, f"channel ({install_id})", "stable",
                   "a stable channel SKIPS updates on a main checkout and "
                   "reports 'Already up to date'. Fix: "
                   "hermes update --set-channel main")
        else:
            record(OK, f"channel ({install_id})", str(ch))

    # source-checks mirrors the resolved identity; useful for branch/behind.
    checks = home / "source-checks"
    if not checks.is_dir():
        record(WARN, "source-checks/", "missing",
               "no recorded identity; run `hermes update --install-id`")
        return
    for f in sorted(checks.glob("*.json")):
        try:
            obj = json.loads(read_text(f, 100_000) or "{}")
        except json.JSONDecodeError as exc:
            record(WARN, f.name, f"unparseable: {exc}")
            continue
        ident = obj.get("identity", {}) or {}
        status = obj.get("status", {}) or {}
        record(INFO, "install id", f.stem)
        record(OK if ident.get("branch") else WARN, f"{f.stem} branch",
               str(ident.get("branch")))
        head = str(ident.get("head") or "")[:12]
        if head:
            record(INFO, f"{f.stem} head", head)
        if status.get("updateAvailable"):
            record(WARN, f"{f.stem} update available",
                   f"behind {status.get('behind')}", "run `hermes update`")


def _parse_channel_map(cfg: str) -> dict[str, str]:
    """Read update.installs.<install_id>.channel out of config.yaml.

    Layout (note the two nesting levels):

        update:
          installs:
            a1b2c3d4e5f60718:
              path: ...
              channel: main

    Line-based on purpose: the structure is shallow and stable, and this avoids
    a PyYAML dependency, which the bare managed interpreter does not carry.
    """
    import re
    out: dict[str, str] = {}
    in_update = False
    in_installs = False
    current: str | None = None
    for line in cfg.splitlines():
        if re.match(r"^update\s*:", line):
            in_update, in_installs = True, False
            continue
        if not in_update:
            continue
        if re.match(r"^\S", line):                    # next top-level key
            break
        if re.match(r"^ {2}installs\s*:\s*$", line):  # level 2 -> level 3
            in_installs, current = True, None
            continue
        if not in_installs:
            continue
        if re.match(r"^ {4}\S", line) and ":" in line:  # level 4 -> install id
            head = line.strip().split(":", 1)[0].strip()
            current = head or None
            continue
        m = re.match(r"^ {6}channel\s*:\s*(\S+)", line)  # level 6
        if m and current:
            out[current] = m.group(1).strip("\"'")
    return out


def re_search(text: str, pattern: str) -> bool:
    import re
    return bool(re.search(pattern, text, re.M))


# Packages pm always materializes on a Windows install. Anything else in
# lock.json is an opt-in extra or a package `pm doctor` reports as `-`
# (not applicable on this platform), so its absence is not a defect.
CORE_TOOLS = {
    "git", "python", "node", "npm", "uv", "ripgrep", "ffmpeg",
    "agent-browser", "chromium", "cua-driver",
}


def _is_required_here(name: str) -> bool:
    return name in CORE_TOOLS


# -------------------------------------------------- 4. managed tool records

def check_tools(home: Path) -> None:
    print("\n=== 4. Managed tool bookkeeping ===")
    facts = home / "tools" / "facts.json"
    if not facts.exists():
        record(WARN, "tools/facts.json", "missing")
        return
    try:
        obj = json.loads(read_text(facts, 500_000) or "{}")
    except json.JSONDecodeError as exc:
        record(FAIL, "tools/facts.json", f"unparseable: {exc}")
        return

    packages = obj.get("packages", {})
    recorded = ", ".join(sorted(packages)) or "none"
    record(INFO, "recorded packages", recorded)

    checkout = home / "hermes-agent"
    lock = checkout / "pm" / "lock.json"
    if lock.exists():
        try:
            declared = json.loads(read_text(lock, 2_000_000)).get("packages", {})
        except json.JSONDecodeError:
            declared = {}
        # `pm doctor` marks packages "not installed" with `?` and platform-mismatched
        # ones with `-`. Only the former are genuine gaps on this machine.
        unrecorded = sorted(
            name for name in declared
            if name not in packages and _is_required_here(name)
        )
        if unrecorded:
            record(WARN, "required but unrecorded", ", ".join(unrecorded),
                   "run `hermes pm install <pkg>`; see SKILL.md failure mode 3 "
                   "if it fails with [Errno 22]")
        else:
            optional_missing = sorted(
                name for name in declared
                if name not in packages and not _is_required_here(name)
            )
            if optional_missing:
                record(INFO, "optional packages not installed",
                       ", ".join(optional_missing)[:110],
                       "normal: these are extras the current platform cannot use")

    # The dangling-symlink trap: mtab is present and breaks tree_digest.
    for git_dir in sorted((home / "tools").glob("git-*-win32-x64")):
        etc = git_dir / "etc"
        for name in ("mtab", "mtab.bak"):
            link = etc / name
            if link.exists() or link.is_symlink():
                try:
                    is_link = link.is_symlink()
                except OSError:
                    is_link = True
                record(FAIL, "dangling mtab symlink", str(link),
                       f"Git Bash: cd {etc} && mv {name} "
                       f"\"{home / 'tools' / 'mtab-orphan.lnk'}\"")
                break


# ----------------------------------------------------------- 5. gateway

def check_gateway(home: Path) -> None:
    print("\n=== 5. Gateway process session ===")
    state_file = home / "gateway_state.json"
    if not state_file.exists():
        record(WARN, "gateway_state.json", "missing")
        return
    try:
        st = json.loads(read_text(state_file, 100_000) or "{}")
    except json.JSONDecodeError as exc:
        record(FAIL, "gateway_state.json", f"unparseable: {exc}")
        return

    state, pid = st.get("gateway_state"), st.get("pid")
    sha = str(st.get("code_sha") or "")[:12]
    record(INFO, "gateway", f"state={state} pid={pid} code_sha={sha}")

    for name, plat in (st.get("platforms") or {}).items():
        s = plat.get("state")
        record(OK if s == "connected" else WARN, f"platform {name}", str(s))

    if not pid:
        return

    rc, out = run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV"])
    line = next((l for l in out.splitlines() if str(pid) in l), "")
    if not line:
        record(WARN, f"pid {pid}", "process not found",
               "a stale gateway_state.json; run `hermes gateway start`")
        return

    session0 = "Services" in line
    if session0:
        record(FAIL, f"pid {pid} session", "Services (0)",
               "desktop (Console/1) cannot read this process token, so "
               "`gateway ensure` returns reason_code=authorization. Restart the "
               "gateway from the user's own terminal: hermes gateway stop && "
               "hermes gateway start")
    else:
        record(OK, f"pid {pid} session", "Console (interactive)")


# ------------------------------------------------------------- 6. data

def check_data(home: Path) -> None:
    print("\n=== 6. Data integrity ===")
    db = home / "state.db"
    if not db.exists():
        record(WARN, "state.db", "missing")
        return

    try:
        import sqlite3
    except ImportError:
        record(WARN, "sqlite3", "unavailable")
        return

    uri = f"file:{db}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=15)
        cur = con.cursor()
    except sqlite3.Error as exc:
        record(FAIL, "state.db open", str(exc))
        return

    try:
        integrity = cur.execute("PRAGMA integrity_check").fetchone()[0]
        (record(OK, "integrity_check", integrity) if integrity == "ok"
         else record(FAIL, "integrity_check", integrity))

        for table in ("sessions", "messages"):
            try:
                n = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                record(OK, table, f"{n} rows")
            except sqlite3.Error as exc:
                record(WARN, table, str(exc)[:90])

        try:
            rows = cur.execute(
                "SELECT active, COUNT(*) FROM messages GROUP BY active").fetchall()
            record(INFO, "messages.active", str(dict(rows)))
        except sqlite3.Error:
            pass

        # Forked sessions are the signature of the auto-continue duplication bug.
        try:
            forks = cur.execute(
                "SELECT id, title FROM sessions "
                "WHERE parent_session_id IS NOT NULL AND parent_session_id != '' "
                "ORDER BY started_at DESC LIMIT 10").fetchall()
            if forks:
                listing = "; ".join(f"{r[0]}({r[1]})"[:70] for r in forks[:3])
                record(INFO, "forked sessions", f"{len(forks)}: {listing}",
                       "auto-continue forks a new session and re-persists the turn; "
                       "mitigate with desktop.auto_continue.enabled=false")
        except sqlite3.Error:
            pass
    finally:
        con.close()


# ------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Diagnose a Hermes Windows install")
    ap.add_argument("--home", help="HERMES_HOME path")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    home = Path(args.home) if args.home else Path(
        os.environ.get("HERMES_HOME")
        or (Path(os.environ["LOCALAPPDATA"]) / "hermes")
    )

    if args.json:
        results.clear()

    print("=" * 66)
    print(f"Hermes Windows diagnosis -- {home}")
    print("=" * 66)

    for fn in (check_layout, check_git, check_channel, check_tools,
               check_gateway, check_data):
        try:
            fn(home)
        except Exception as exc:  # a broken check must not abort the rest
            record(FAIL, fn.__name__, f"check crashed: {exc}")

    failures = [r for r in results if r["status"] == FAIL]
    warnings = [r for r in results if r["status"] == WARN]

    print("\n" + "=" * 66)
    summary = f"{len(failures)} failure(s), {len(warnings)} warning(s)"
    print(f"SUMMARY: {summary}")
    for r in failures:
        print(f"  FAIL  {r['check']}: {r['detail']}")
        if r["hint"]:
            print(f"        -> {r['hint']}")
    print("=" * 66)

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))

    return 1 if failures else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
