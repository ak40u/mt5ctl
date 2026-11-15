# Remote Windows hosts: plan

mt5ctl drives terminals on Linux and WSL2 hosts, and reads chart files and plans
deploys for a terminal on the Windows machine it runs on (`kind: local`). It does
not yet manage a terminal on a *remote* Windows host reached over OpenSSH. This
page is the plan for that, and why it is not done yet.

## What it takes

Everything that touches a host is a generated script plus a transport. For
Linux both are bash; Windows needs PowerShell for each of them.

| Part | Today (bash) | On Windows |
|---|---|---|
| Transport | `ssh … 'base64 -d \| bash'`, script on stdin | `ssh … powershell -NoProfile -Command -`, script on stdin (the default shell of Windows OpenSSH is `cmd.exe`, with its 8 KB command-line limit, so stdin stays the channel) |
| Upload | `tar -x` from stdin | `tar.exe` (bsdtar, shipped with Windows 10 1803 and later) from stdin |
| State, lock, preflight, backup, apply, restore, manifest | 7 scripts | the same 7 in PowerShell: `Get-FileHash`, `New-Item` for the lock folder, `Move-Item -Force`, `Compress-Archive` or `tar.exe` for backups |
| Journal cursor and reading | `tail -c`, `wc -c` | `[IO.FileStream]` with `Seek`, same UTF-16 parsing in Python |
| Lifecycle | systemd, or process mode with a pid file | process mode with `Start-Process -PassThru` and a pid file; or a Windows service (for example through NSSM) for restart on failure |
| Stop | `taskkill` through Wine, wait, `wineserver -k` | native `taskkill /IM terminal64.exe` (no `/F`), wait for the pid; no Wine helpers to end |
| Status | `systemctl`, cgroup, `ss` | the pid's process tree (`Get-CimInstance Win32_Process`), `Get-NetTCPConnection -OwningProcess` for broker sockets |
| Compile | MetaEditor under Wine, `winepath` | `metaeditor64.exe` directly; paths are already Windows paths |

The pure core (chart files, bundles, the plan, the manifest, journal parsing)
is shared unchanged.

## Shape of the change

1. A script dialect per host: `scripts.py` becomes two modules behind one
   interface (`bash` and `powershell`), each building the same operations with
   the same line protocol, so `operations.py` does not change.
2. `HostKind.WINDOWS` with its transport.
3. Tests: every PowerShell script is unit-tested as text, and the suite runs the
   generated scripts in PowerShell 7 (`pwsh`) on Linux in CI, the way the bash
   scripts run in bash today. That catches most mistakes but not Windows-only
   behaviour (file locking, `tar.exe` quirks), so a Windows CI runner is part
   of the work.

## Estimate and status

About 1,500 to 2,000 lines of code and tests, plus a Windows runner in CI and
a real Windows host to check against, the way the Linux support was checked
against a real terminal under Wine. That is larger than an experimental flag
can carry responsibly: a half-tested deploy path to a live account is worse
than none.

It is planned for when there is a Windows host to run it on. Until then, a
terminal on Windows is managed either from that machine (`kind: local`, read-only
commands), or by running it under Wine in WSL2 (`kind: wsl`), which is fully
supported.
