# Hermes PM internals on Windows

Reference for the `pm` (package manager) subsystem — the layer that most often
misleads during Windows maintenance. Read this when a managed tool shows a
contradictory state: present on disk, yet reported as missing.

## Layout

| Item | Path |
|---|---|
| Managed tool store | `%LOCALAPPDATA%\hermes\tools\` |
| Tool bookkeeping | `tools\facts.json` |
| Package declarations | `<checkout>\pm\lock.json` |
| Store / digest code | `<checkout>\pm\store.py` |
| Artifacts and digests | `<checkout>\pm\lock.py`, `pm\packages.py` |
| Per-install environments | `installs\<install_id>\environments\<env_id>\venv` |

Each tool lives in a versioned directory: `<name>-<version>-<arch>`, e.g.
`uv-0.12.3-win32-x64`, `git-2.53.0+3-win32-x64`.

## `tools/facts.json` schema

```json
{
  "schema": 1,
  "packages": {
    "git": {
      "artifacts": ["<sha256 from pm/lock.json for this platform>"],
      "digest":   "<tree_digest of the tool directory>",
      "entry":    "git-2.53.0+3-win32-x64",
      "env":      {},
      "target":   "win32-x64",
      "version":  "2.53.0+3"
    }
  }
}
```

`entry`, `version`, and `target` identify the directory; `digest` proves the
bytes on disk are the bytes pm published. A directory with no matching entry is
invisible to `pm doctor` and gets reinstalled on the next update.

## `tree_digest()` — the algorithm and its Windows trap

From `pm/store.py`:

1. Walk the tree with `os.walk`.
2. Collect files and directory-symlinks/junctions; **skip `__pycache__`**
   (runtime state, not package bytes).
3. Sort by POSIX relative path.
4. For each entry feed the digest: `relpath` + `b"\0"`, then either
   - the **link target text** via `os.readlink()` for symlinks/junctions, or
   - the file's raw bytes.
5. SHA-256 over that stream.

**The trap:** `tree_digest` decides between the two branches with
`path.is_symlink() or is_junction(path)`. PortableGit ships
`etc/mtab → /proc/mounts`, an **MSYS symlink**. On Windows it materializes as a
*dangling reparse point*, and CPython's `Path.is_symlink()` returns **False** for
it — so control falls through to `open(path, "rb")`, which fails on the
Linux-style target with `[Errno 22] Invalid argument`.

Consequences, all from that one link:

| Symptom | Why |
|---|---|
| `pm install git` fails at `…\etc\mtab` | hashing runs during install verification |
| `pm doctor` says `? git: not installed` | doctor hashes to validate each entry |
| Desktop build fails on the git step | the build path installs/validates git first |

`load_pm_digest()` computes the same value; always call it rather than
reimplementing the hash — a hand-rolled variant will disagree on
`__pycache__` handling or symlink treatment and pm will reject the entry.

### Why no Windows API can delete it

The reparse point targets a Linux path, so the target is unresolvable on
Windows. `Remove-Item`, `File.Delete`, and `os.unlink` all fail with
`Access denied` (WinError 5) because the shell tries to resolve the link.

**Only the MSYS layer succeeds.** Git Bash sees it as a real symlink, so:

```bash
cd "…/tools/git-<ver>-win32-x64/etc"
mv mtab "../../../mtab-orphan.lnk"     # move it out of the hashed tree
```

Moving it out of the tool directory is enough — `tree_digest` no longer walks
it. Deleting it afterwards is optional and may still fail.

## Diagnosing "declared but not installed"

`pm doctor` marks packages three ways:

| Marker | Meaning |
|---|---|
| `✓ name version` | recorded and its digest matches |
| `? name: not installed` | declared in `lock.json`, no entry in `facts.json` |
| `- name: n/a on win32-x64 (reason)` | not applicable to this platform — **not a defect** |

Only the `?` rows warrant action. A large tail of `-` rows (llamacpp-*, termux,
dmgbuild, iron-proxy…) is normal on Windows and must not be treated as a
missing install.

## Reading `pm/lock.json`

```json
{
  "packages": {
    "git": {
      "version": "2.53.0+3",
      "artifacts": {
        "win32-x64": {
          "url":   "https://github.com/git-for-windows/git/releases/download/…/PortableGit-…-64-bit.7z.exe",
          "sha256": "b365da79…"
        },
        "win32-arm64": { "…": "…" }
      }
    }
  }
}
```

Derive `entry` as `f"{name}-{version}-{target}"` and take `artifacts[target].sha256`
verbatim. Do not guess the directory name or re-hash the download — the lock's
`sha256` is what the artifact was published as.

## `pm install` as the general repair

`hermes pm install` (optionally with a package name) reconciles the venv against
`uv.lock`. It resolves:

- `install out of sync (venv: out of sync with uv.lock)`
- `[WinError 206] 文件名或扩展名太长` — a Windows MAX_PATH overflow during
  bytecode compilation
- `no dependency environment is committed for this install`

Two benign messages during the run:

- `Bytecode compile failed; retrying the install without it` — falls back to
  on-import compilation; the install still succeeds.
- `Windows cua-driver refresh deferred (autostart registration requires UAC)` —
  deferred, not failed. Run `hermes computer-use install --upgrade` from an
  interactive terminal.

`hermes pm doctor` re-hashes every realized tool directory, so it is the
authoritative post-repair check.

## `pm install` re-downloads rather than reusing

Note that a failed `pm install` typically re-downloads the full artifact before
failing during hashing. On a metered or slow link this is expensive and
repeating the command without fixing the underlying cause will keep re-fetching.
Fix the cause first — for the git case that means moving `mtab` out first.
