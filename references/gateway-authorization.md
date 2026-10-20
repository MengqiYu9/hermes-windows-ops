# Windows session isolation and the Hermes gateway handshake

Why the desktop app fails with `Gateway inaccessible (authorization)` even when
everything is installed, up to date, and the gateway is "running".

## The mechanism

The desktop app does not trust the gateway merely because it answers. Before
talking, it must confirm the gateway process is the one it expects. That
confirmation reads the **peer process's Windows security identifier (SID)**.

Call chain:

```
electron main: ensureLocalGateway()          apps/desktop/src/lib/…
  └─ query_identify(home, timeout)           hermes_cli/gateway_runtime_discovery.py
       └─ query_runtime_control(...)         gateway/runtime_bootstrap_windows.py
            └─ _peer_subject(handle)         line ~243
                 └─ _process_sid(pid)        line ~129
                      ├─ OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)
                      ├─ OpenProcessToken(TokenQuery)
                      └─ _token_sid(token)    ConvertSidToStringSidW
```

`_process_sid` raises `PermissionError` at each failure point:

- `'cannot verify bootstrap peer process'` — `OpenProcess` returned NULL
- `'cannot read bootstrap peer identity'` — `OpenProcessToken`/`TokenUser` failed
- `'cannot encode bootstrap peer identity'` — SID to string conversion failed

That exception propagates to `hermes_cli/gateway_runtime.py`, which catches it
and returns a deliberately opaque verdict:

```python
except PermissionError:
    return GatewayDiscovery("inaccessible", reason_code="authorization")
```

The `reason_code` is a bounded code, not a diagnosis — raw peer data and paths
are withheld from the message because they can contain secrets.

## Why sessions matter

`OpenProcess` and `OpenProcessToken` on another process are subject to Windows
session isolation. A process in **Session 0** (Services) is not reachable for
token queries from **Session 1** (Console, the interactive desktop).

| Component | Typical session |
|---|---|
| Interactive desktop app | Console / 1 |
| Gateway started from a terminal | Console / 1 |
| Gateway started by a **Windows service or scheduled task** | Services / 0 |
| Gateway started by an **agent sandbox / CI runner** | Services / 0 |

So the same installation works or fails purely based on **who started the
gateway**, not on any configuration difference.

### Prove it in one command

```bash
tasklist /FI "IMAGENAME eq python.exe" /FO CSV | findstr /i "Services Console"
```

The gateway row must read `Console`. Confirm programmatically:

```python
import sys
sys.path.insert(0, r"C:\Users\<user>\AppData\Local\hermes\hermes-agent")
from gateway.runtime_bootstrap_windows import _process_sid
_process_sid(<gateway_pid>)   # PermissionError => Session 0
```

## Distinguishing this from other startup failures

| Verdict / message | Meaning |
|---|---|
| `reason_code: authorization` | **cross-session SID read denied** — this document |
| `Gateway incompatible (…)` | protocol/version mismatch between app and runtime |
| `hermes gateway ensure produced no result` | the installed `hermes` lacks the ensure protocol, or the profile is missing |
| `Invalid local gateway endpoint` | a ready verdict that is not a loopback session-authority endpoint |
| `Unsafe gateway control path` | the control socket path is group-writable; needs chmod/chown (POSIX only) |
| `reason_code: control_timeout` | the gateway accepted the socket but did not answer in time |
| `reason_code: foreign_stale_lock` | `gateway.lock` belongs to another host/user |
| `reason_code: invalid_control_peer` | malformed peer data over the control channel |

A useful discriminator: compare the client's `client_code_sha` from
`hermes gateway ensure` against `code_sha` in `gateway_state.json`. **Equal**
means the versions agree, so an `authorization` verdict is about identity
(session), not version.

## Correct startup procedure

**First, find out whether a scheduled task is the cause.** This is the step that
matters most, and it is easy to skip:

```powershell
Get-ScheduledTask | Where-Object { $_.TaskName -match 'hermes' `
  -or ($_.Actions | Out-String) -match 'hermes' } |
  Select-Object TaskName, State, @{n='LogonType';e={$_.Principal.LogonType}}
```

A stock `hermes gateway install` registers `Hermes_Gateway` with
`LogonType=S4U`, triggered 30 s after logon. **S4U means "run whether or not
the user is logged on", and such tasks always execute in Session 0.** That is
the entire cause: sign in → 30 s → gateway in Session 0 → the desktop in
Session 1 can never authorize it.

If `LogonType` is `S4U`, manual restarts only fix one login at a time.
Re-register the task as `Interactive` instead (Administrator; see SKILL.md
failure mode 4 for the full command). `Set-ScheduledTask` cannot change
`LogonType`, so the task has to be unregistered and re-registered.

When there is no such task (or it is already `Interactive`), a plain restart in
the user's own terminal is enough:

```powershell
# In the user's own terminal (Console session)
hermes gateway stop
hermes gateway start
```

**`gateway.startup_watchdog: false` does NOT help.** The process doing the
relaunching is the watchdog supervisor itself, which is already running; a
config change does not stop it, and 20 s after `gateway stop` the log shows
`gateway exited (code 0) - restarting in 10s` followed by
`gateway not running - launching`.

**Never start or stop the gateway from an agent sandbox.** Background processes
spawned there land in Session 0 and are not killable from the sandbox:
`taskkill /F` reports access denied and the command line is unreadable.
`hermes gateway stop` stops the outermost process, but the watchdog immediately
rebuilds the chain (typically ~4 MB supervisor + ~40 MB helper + ~220-440 MB
gateway) **still in Session 0**, reproducing the failure.

Kill the whole chain from an Administrator PowerShell:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ForEach-Object {
  $p = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
  if ($p -and $p.SessionId -eq 0) { Stop-Process -Id $_.ProcessId -Force }
}
```

**A Windows logoff/login clears every Session 0 leftover** and remains the
most reliable fallback when the process chain is already established.

## The desktop will not recover on its own

`apps/desktop/electron/local-gateway.ts` can kill and replace a gateway it
cannot reach — `createStaleGatewayRestarter` (`main.ts`) is wired in and the
desktop holds more privilege from Session 1. It does not fire in this case:

```ts
const stale = restartStale && result.code === 0 && payload.state === 'ready'
              ? staleGatewayCode(payload) : null
```

`staleGatewayCode()` compares `endpoint.code_sha` with `client_code_sha` only;
identical builds are not stale. So `state: inaccessible` + matching versions
means the desktop retries forever while the Session 0 owner never exits.
**Waiting does not help** — this is worth knowing before suggesting a retry.

## Upstream added precise diagnostics in 0.22.2+canary

Commit `eecaa7af9a` (refs #139845, #140252) — *fix(windows): name an elevated
or other-session gateway instead of "authorization"* — replaces the misleading
catch-all with real verdicts:

| reason_code | Meaning |
|---|---|
| `gateway_other_session` | owner runs in a different session (names the PID) |
| `gateway_elevated` | owner is elevated / its token cannot be opened |
| `authorization` | anything else — the old catch-all |

Code: `gateway/runtime_bootstrap_windows.py` (`GATEWAY_ELEVATED`,
`GATEWAY_OTHER_SESSION`), `hermes_cli/gateway_runtime.py` (names the cause,
falls back to `authorization`).

**Practical consequence: check `reason_code` before chasing credentials.** An
earlier diagnosis of this symptom went down the `${VAR}` env-expansion path for
hours before the upgrade revealed `gateway_other_session`, which named the
cause in one line. Upgrading often brings better diagnostics than the code
around them.

## Logs that carry the evidence

| File | What to look for |
|---|---|
| `%LOCALAPPDATA%\hermes\logs\desktop.log` | `Desktop boot failed: Gateway inaccessible (authorization)`, and the `ensureLocalGateway` frame |
| `%LOCALAPPDATA%\hermes\logs\gui.log` | the gateway's own view; **absent new lines mean the handshake never completed** |
| `%LOCALAPPDATA%\hermes\logs\gateway-exit-diag.log` | one `gateway.start` record per launch with `pid`, `detached`, `console_window_attached`; a repeating stop/start cycle points at the watchdog |
| `%LOCALAPPDATA%\hermes\logs\gateway-watchdog.log` | `gateway exited (code 0) - restarting in 10s` — confirms an auto-restart loop |
| `%LOCALAPPDATA%\hermes\gateway_state.json` | `gateway_state`, `pid`, `code_sha`, per-platform state |

## Note on `startup_watchdog`

This watchdog is **not** the same thing as an autostart service. It runs inside
the gateway process tree and restarts the gateway after a clean exit. Setting it
to `false` does not stop an installed Windows scheduled task from launching the
gateway at logon — remove that separately with `hermes gateway uninstall` (or
`schtasks /delete /tn <name>`).

Check whether autostart is installed:

```powershell
hermes gateway status     # reports "not as a system service" or the task name
```

## Impact when the desktop app cannot connect

The CLI and messaging gateways are unaffected — they hold their own long-lived
connections (e.g. the messaging WebSocket) and never perform this per-launch SID
handshake. Sessions, kanban, cron, and skills all keep working. Only the desktop
UI is blocked.
