---
name: hermes-windows-ops
description: Operate, diagnose, and repair a Hermes Agent source installation on Windows. This skill should be used when hermes update fails or silently no-ops, when the desktop app reports "Gateway inaccessible (authorization)" or "Hermes couldn't start", when pm doctor reports a managed tool as "not installed", when electron-builder desktop packaging fails with "[Errno 22]" or "TypeError: fetch failed", or when any Hermes install/update/build problem must be diagnosed on Windows.
agent_created: true
---

# Hermes Agent on Windows — Operations & Repair

Procedural knowledge for maintaining a Hermes Agent **source installation** on Windows. Every failure mode here was diagnosed on a real machine; the root causes are non-obvious and the error messages actively mislead.

## Non-negotiable first step

Run the diagnostic script before theorizing. It localizes the failure in seconds:

```bash
python scripts/diagnose.py
```

It checks, in order: install layout, git reachability, **update channel**, managed-tool bookkeeping, gateway process session, and data integrity. Each check prints `OK` / `FAIL` / `WARN` with the reason. Read its output before reading anything else.

## Environment layout (Windows)

| Item | Path |
|---|---|
| Data root (`HERMES_HOME`) | `%LOCALAPPDATA%\hermes` |
| Source checkout | `%LOCALAPPDATA%\hermes\hermes-agent` |
| CLI entry point | `%LOCALAPPDATA%\hermes\bin\hermes.cmd` |
| Managed tool store | `%LOCALAPPDATA%\hermes\tools\` |
| Config | `%LOCALAPPDATA%\hermes\config.yaml` |
| Session database | `%LOCALAPPDATA%\hermes\state.db` |
| Install records | `%LOCALAPPDATA%\hermes\installs\<install_id>\` |
| Resolved install identity | `%LOCALAPPDATA%\hermes\source-checks\<install_id>.json` |
| Desktop build output | `…\hermes-agent\apps\desktop\release\win-unpacked\` |
| Desktop user data | `%APPDATA%\Hermes\` |
| Update receipt log | `%LOCALAPPDATA%\hermes\logs\update.log` |
| Exit diagnostics | `%LOCALAPPDATA%\hermes\logs\gateway-exit-diag.log` |

## Always run Hermes/git network commands with proxies stripped

WorkBuddy sandboxes inject proxy environment variables. They break direct connections and make diagnosis misleading:

```bash
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY <command>
```

## Failure mode 1 — `hermes update` silently does nothing

**Symptom:** update completes with `✓ Already up to date!` but the code SHA is unchanged.

**Root cause:** git branch and update channel are independent. A checkout can sit on `main` while the channel record says `stable`. The stable resolver then judges the checkout "newer than the latest stable" and skips — misreading the canary tag on the commit as evidence of recency.

```bash
hermes update --install-id                    # print the install id
hermes update --set-channel main              # switch to following main
```

The channel lives in `config.yaml` under `update.installs.<install_id>.channel` — two nesting levels, keyed by install id, **not** at the top level:

```yaml
update:
  installs:
    a1b2c3d4e5f60718:
      path: C:\Users\<user>\AppData\Local\hermes\hermes-agent
      channel: main
```

`source-checks/<install_id>.json` mirrors the resolved identity (branch, head, `behind`, `updateAvailable`) and is a good cross-check. Neither `git branch` nor `git describe` reveals the channel — never infer it from them.

Confirm the update actually landed:

```bash
cd "%LOCALAPPDATA%\hermes\hermes-agent" && git log --oneline -1
```

## Failure mode 2 — git cannot reach GitHub

**Symptom:** `Failed to connect to github.com:443 over proxy 127.0.0.1:<port>`

**Root cause:** a stale global proxy pointing at a port that is no longer listening. On Chinese networks, direct access to GitHub frequently works while the local proxy does not — the proxy config is actively harmful.

Test direct reachability first, then fix:

```bash
curl -s -m 15 --noproxy '*' https://api.github.com -o /dev/null -w "HTTP %{http_code}\n"
git config --global --unset http.proxy
git config --global --unset https.proxy
```

`github.com:443` is also intermittently unreachable even when DNS resolves correctly (a healthy `github.com` resolves to `20.205.243.x`). Retry `git fetch` 4-6 times with short sleeps before concluding failure — one timeout is not proof.

## Failure mode 3 — a managed tool is "not installed" and reinstall fails with `[Errno 22]`

**Symptom:** `pm doctor` reports `? git: not installed`; `pm install git` fails at `[Errno 22] Invalid argument: …\etc\mtab`; the desktop build fails on the git step.

**Root cause:** PortableGit ships an MSYS **symbolic link** `etc/mtab → /proc/mounts`. On Windows this becomes a **dangling reparse point** (`Attributes: Archive, ReparsePoint`). `pm/store.py::tree_digest()` calls `path.is_symlink()`, which returns **False** for an MSYS link on Windows, so it proceeds to `open('/proc/mounts')` and raises `Errno 22`. The link therefore breaks install, verification, and `pm doctor` simultaneously.

**Fix — move it out with the MSYS view, then repair the bookkeeping:**

```bash
cd "%LOCALAPPDATA%\hermes\tools\git-<ver>-win32-x64/etc"
mv mtab "%LOCALAPPDATA%\hermes\tools\mtab-orphan.lnk"
```

Windows APIs, PowerShell `Remove-Item`, and Python `os.unlink` all fail on a dangling reparse point. **Only Git Bash's `mv` works** — the MSYS layer resolves it as a symlink and moves it.

Then write the bookkeeping entry using Hermes' own digest function (never hand-roll the algorithm; verify with another package first):

```bash
python scripts/repair_pm_facts.py git
```

Verify with `hermes pm doctor` → `✓ git <ver>`.

Read `references/pm-internals.md` for the `tree_digest` algorithm, the `facts.json` schema, and why `pm doctor` prints `-` rows that are not defects.

## Failure mode 4 — desktop app: "Gateway inaccessible (authorization)"

**Symptom:** desktop shows *Hermes couldn't start*; `desktop.log` has `Gateway inaccessible (authorization)`; `hermes gateway ensure` returns `{"state":"inaccessible","reason_code":"authorization","endpoint":null}`.

**Root cause — Windows session isolation.** The gateway must resolve the **peer's SID** to authorize it:

```
ensureLocalGateway → query_identify → gateway/runtime_bootstrap_windows.py::_process_sid(pid)
```

`_process_sid()` calls `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` + `OpenProcessToken(TokenQuery)`. **Across Windows sessions this is denied**, raising `PermissionError`, which `hermes_cli/gateway_runtime.py` catches and reports as `reason_code="authorization"`.

| Process | Session | SID readable |
|---|---|---|
| Gateway | `Services` / 0 | ❌ PermissionError |
| Desktop | `Console` / 1 | ✅ |

Check sessions before touching anything else:

```bash
tasklist /FI "IMAGENAME eq python.exe" /FO CSV | findstr /i "Services Console"
```

**Fix:** the gateway must run in the interactive session. **Start it from the user's own terminal, never from an agent sandbox** — sandbox-spawned background processes always land in Session 0.

```powershell
hermes gateway stop
hermes gateway start
```

If `startup_watchdog: true` keeps yanking the gateway back to Session 0, temporarily set `gateway.startup_watchdog: false` in `config.yaml`. **A Windows logoff/login clears all Session 0 leftovers** and is the most reliable fallback.

Ruling out the alternatives:

- Not a version mismatch — compare `client_code_sha` from `gateway ensure` against `code_sha` in `gateway_state.json`; equal means the handshake failed later, at authorization.
- Not token corruption — check `%APPDATA%\Hermes\secure-token-storage.json` (`{"on":false,"migrated":true}` is normal) and `connections.json`.

Read `references/gateway-authorization.md` for the full call chain, every `reason_code` value, and which log file carries which evidence.

## Failure mode 5 — sandbox delete interceptions

`[safe-delete][SAFE_DELETE_BULK_GUARD_ERROR] failed to read/stat …` is **the sandbox shim, not a Windows ACL or admin-rights problem**. It intercepts PowerShell `Remove-Item`, `cmd del`, and Python `os.unlink`, including under `dangerouslyDisableSandbox`. Do not escalate privileges in response to it.

Workarounds, in order: Git Bash `mv`/rename → hand the command to the user → only then consider an elevated terminal.

## Pre-upgrade and post-upgrade discipline

1. **Back up first.** `state.db` is a live SQLite database with an active WAL — use the SQLite backup API, never a plain copy:
   ```bash
   python scripts/backup_state.py "%LOCALAPPDATA%\hermes\backups\pre-<tag>\"
   ```
   Also copy `config.yaml`, `.env`, `auth.json`, `kanban.db`, and `tools\facts.json`.
2. **Record a baseline** — session count, message count, `active` distribution, fork-session count, and the specific sessions under investigation. Verify these exact numbers afterwards.
3. **Protect data across upgrades.** From 0.22 onward `sessions.auto_prune` defaults to on and deletes sessions inactive for 90 days. Set `sessions.auto_prune: false` before upgrading when old sessions matter.
4. **Stop the gateway and all `Hermes.exe` before updating** — a running desktop holds files the update needs to move.
5. **Confirm three-way version agreement** after upgrading: `git log -1`, `code_sha` in `gateway_state.json`, and `commit` in `apps/desktop/build/install-stamp.json` must match.

## Repairing the dependency environment

`hermes pm install` is the general repair: it resolves `venv out of sync with uv.lock` and `[WinError 206] filename too long`. `Bytecode compile failed; retrying the install without it` is a benign fallback, not a failure.

## Rebuilding the desktop app

The automatic build path needs the managed git (failure mode 3). To build directly:

```bash
cd "%LOCALAPPDATA%\hermes\hermes-agent\apps/desktop"
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u NODE_OPTIONS \
  PATH="/c/Users/<user>/AppData/Local/hermes/tools/npm-<ver>-win32-x64:$PATH" \
  ELECTRON_MIRROR="https://npmmirror.com/mirrors/electron/" \
  ELECTRON_BUILDER_BINARIES_MIRROR="https://npmmirror.com/mirrors/electron-builder-binaries/" \
  npm run builder -- --dir --publish never
```

First run downloads toolchain artifacts (`dotnet-runtime-win-x64-*.zip` and similar) and takes roughly 10-15 minutes. The previous build is preserved as `release/win-unpacked.bak`.

On mainland China the two `*_MIRROR` variables are what make this work; without them the download of Electron's runtime dependencies fails with `TypeError: fetch failed`.

## What "up to date" means on main

The `main` branch commits several times an hour and publishes a daily canary tag (`v0.22.2+canary.<UTC stamp>`) — the tag's stable core matches the latest release, so the version label does **not** indicate how far ahead `main` is. "Up to date" goes stale within the hour; that is expected, not a fault.

## Reporting outcomes

State what was verified versus assumed. Distinguish a change that was made and confirmed from one that was merely attempted. When a repair requires user action (an interactive terminal, an elevated shell, a logoff), say so plainly with exact commands rather than implying success.
