# hermes-windows-ops

A WorkBuddy skill for operating, diagnosing, and repairing
[Hermes Agent](https://github.com/NousResearch/hermes-agent) source installations on Windows.

Every failure mode documented here was diagnosed on a real machine. The root causes are
non-obvious, and — importantly — **the error messages actively point in the wrong direction**.

---

## Why this exists

Maintaining Hermes on Windows hits five walls that are not documented upstream and do not
explain themselves:

| Symptom | What it actually is |
|---|---|
| `hermes update` says `✓ Already up to date!` but the code never changes | The update **channel** is `stable` while the checkout sits on `main`; the stable resolver skips it. Git branch and update channel are independent settings. |
| `Failed to connect to github.com:443 over proxy 127.0.0.1:<port>` | A stale global proxy pointing at a dead port — while **direct access works fine**, making the proxy config actively harmful. |
| `pm doctor` says `? git: not installed`, reinstall fails with `[Errno 22]` | PortableGit ships `etc/mtab → /proc/mounts`. On Windows that is a *dangling reparse point*, and CPython's `Path.is_symlink()` returns `False` for it, so the digest walker tries to `open('/proc/mounts')`. One symlink breaks install, verification, and doctor at once. |
| Desktop shows `Gateway inaccessible (authorization)` | **Windows session isolation.** The gateway needs to read the peer's SID, which is denied across sessions. A gateway in Session 0 can never authorize a desktop in Session 1 — nothing to do with versions or tokens. |
| `[safe-delete][SAFE_DELETE_BULK_GUARD_ERROR]` on every delete | The agent sandbox shim, **not** a Windows ACL or admin-rights problem. Escalating privileges does nothing. |

Diagnosing these from the error text alone takes hours. This skill encodes the answers.

---

## Install

Copy the skill directory into your agent's skills folder:

```bash
# WorkBuddy (user scope)
cp -r hermes-windows-ops ~/.workbuddy/skills/

# Claude Code
cp -r hermes-windows-ops ~/.claude/skills/

# or any agent that reads SKILL.md from a skills directory
```

Then ask your agent about a Hermes install/update/build problem — the skill triggers
automatically. You can also run the diagnostic script directly at any time.

---

## Quick start

```bash
python scripts/diagnose.py
```

Read-only. Checks six areas and prints `OK` / `WARN` / `FAIL` with a concrete fix for each:

```
=== 1. Install layout ===
[ OK ] data root -- C:\Users\<user>\AppData\Local\hermes
[ OK ] source checkout -- ...\hermes\hermes-agent
[WARN] stale update marker -- .hermes-update-in-progress.lock

=== 2. Git reachability ===
[ OK ] HEAD -- c4d5e6f70a2 fix(gateway): stamp a host-side resolution failure...
[ OK ] global git proxy -- none configured
[ OK ] GitHub direct reachability -- HTTP 200

=== 3. Update channel ===
[WARN] channel (a1b2c3d4e5f60718) -- stable
         -> a stable channel SKIPS updates on a main checkout ...

=== 5. Gateway process session ===
[FAIL] pid 29780 session -- Services (0)
         -> desktop (Console/1) cannot read this process token ...

=== 6. Data integrity ===
[ OK ] integrity_check -- ok
[ OK ] sessions -- 69 rows
[ OK ] messages -- 10250 rows

SUMMARY: 1 failure(s), 2 warning(s)
```

Exit code is `1` when any check fails, so it drops straight into CI or a pre-upgrade gate.

```bash
python scripts/diagnose.py --json     # machine-readable
python scripts/diagnose.py --home D:\custom\hermes
```

---

## Scripts

### `diagnose.py`

Read-only health check across install layout, git reachability, update channel, PM
bookkeeping, gateway session, and data integrity. Safe to run at any time.

### `backup_state.py`

Backs up the data root before any maintenance. Uses SQLite's **online backup API** for
`state.db` — a plain file copy of a live database with an active WAL silently misses recent
commits. Also writes `baseline.json` with the counts that must match afterwards.

```bash
python scripts/backup_state.py
python scripts/backup_state.py --baseline-only     # just the counts, copies nothing
python scripts/backup_state.py --out D:\hermes-backup
```

```
=== Baseline (verify these after maintenance) ===
{
  "sessions_count": 69,
  "messages_count": 10250,
  "messages_by_active": { "0": 3237, "1": 7013 },
  "forked_sessions": [
    { "id": "2026...e6cf09", "title": "… #3" },
    { "id": "2026...1f1c6e", "title": "… #2" }
  ],
  "integrity_check": "ok"
}
```

### `repair_pm_facts.py`

Recomputes a managed tool's `tree_digest` with Hermes' own `pm.store` and writes the missing
`tools/facts.json` entry. Used after the `mtab` symlink is moved out.

```bash
python scripts/repair_pm_facts.py --list       # declared vs recorded
python scripts/repair_pm_facts.py git          # repair one package
python scripts/repair_pm_facts.py --all
python scripts/repair_pm_facts.py git --verify # dry run
```

The digest always comes from Hermes' own function — never hand-roll the hash, or `pm doctor`
will reject the entry. `entry`, `version`, and `artifacts[].sha256` are read from
`pm/lock.json` rather than hardcoded, so this keeps working across versions.

---

## Reference material

### `references/pm-internals.md`

The `tree_digest` algorithm and why the Windows symlink breaks it; the `facts.json` schema;
what `✓` / `?` / `-` markers in `pm doctor` actually mean (most `-` rows are *not* defects);
`pm install` as the general repair.

### `references/gateway-authorization.md`

The full SID-authorization call chain, a table of every `reason_code` with its real meaning,
which log file carries which evidence, and why an agent sandbox must never start the gateway.

---

## Discipline this skill enforces

**Before upgrading**

- Back up first, with the SQLite API — then record a baseline.
- Set `sessions.auto_prune: false`. From 0.22 onward it defaults to on and deletes sessions
  inactive for 90 days, silently.
- Stop the gateway and all `Hermes.exe`; a running desktop holds files the update must move.

**After upgrading**

- Confirm **three-way** version agreement: `git log -1`, `code_sha` in `gateway_state.json`,
  and `commit` in `apps/desktop/build/install-stamp.json`.
- Compare the backup baseline: session count, message count, `active` distribution.

**If any Hermes/git network command fails**, strip the sandbox proxy variables first — they
break direct connections and make diagnosis misleading:

```bash
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY <command>
```

---

## Notes

- `main` commits several times an hour and publishes a daily canary tag whose stable core
  matches the latest release. **"Up to date" goes stale within the hour** — that is expected,
  not a fault, and the version label says nothing about how far ahead `main` is.
- The MSIX/bundled desktop package is a separate distribution channel with its own update
  ownership. Everything here concerns the **source installation** route, which remains
  supported. You do not need MSIX if the source build works.
- On mainland China, `ELECTRON_MIRROR` and `ELECTRON_BUILDER_BINARIES_MIRROR` (npmmirror) are
  what make the desktop build succeed; without them the Electron toolchain download fails
  with `TypeError: fetch failed`.

---

## Contributing

Found a failure mode that isn't here, or a fix that no longer works against current Hermes?

Open an issue with the symptom and the diagnostic output. If a root cause turns out to be an
upstream bug in `pm/store.py` (the `is_symlink()` gap) or
`gateway/runtime_bootstrap_windows.py`, that is worth reporting to
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) too.

## License

MIT — see [LICENSE](LICENSE).

Upstream Hermes Agent is licensed separately; this repository documents and automates
operations against it and does not redistribute any Hermes code.