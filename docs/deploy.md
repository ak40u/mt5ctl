# Deploying

This page covers what `mt5ctl deploy`, `adopt`, `restart` and `compile` do on
the host, and what a host needs for them.

## The deploy sequence

```text
lock ─ plan ─┬─ nothing to change ─ verify (no restart) ─ unlock
             └─ preflight ─ stop ─ drain ─ plan again ─ backup ─ upload ─ apply ─ start ─ verify ─ unlock
                                                                    │
                                                         failure ── restore ─ start ─ error
```

1. **Lock.** `.mt5ctl/deploy.lock.d` in the data folder, created atomically. A
   second deploy, adopt or restart of the same terminal refuses while it exists.
   A lock older than 30 minutes is taken over (a crashed run never blocks forever).
2. **Plan** — the same diff `mt5ctl plan` shows. Any conflict (a bundle file that
   would land on a file mt5ctl does not manage) stops here, before the terminal
   is touched.
3. **Preflight.** `tar`, a sha256 tool, and the state folder on the same
   filesystem as the chart and expert folders (files are moved into place with
   `mv`, which is atomic only within one filesystem).
4. **Stop and drain.** `systemctl stop`, then wait until the unit is inactive and
   no `terminal64.exe` is left in its cgroup. The terminal writes chart files
   while it shuts down; nothing is written before it is gone.
5. **Plan again**, on the files as the terminal left them. If it saved a managed
   chart differently while stopping (for example without its expert), that chart
   is now an update and is put back.
6. **Backup** of every managed file and the manifest into
   `.mt5ctl/backups/<time>.tar`; the five newest are kept.
7. **Upload** of the files to add or update into `.mt5ctl/staging/`, as one tar
   over the SSH connection.
8. **Apply.** Every staged file's sha256 is checked before anything moves; then
   the files are moved into place, dropped files are removed, the profile's
   `order.wnd` is brought in step (see [chart-format.md](chart-format.md#the-order-file)),
   and the manifest is written last.
9. **Start and verify.** `systemctl start`, then poll until the service is
   active, the journal shows the broker authorization and every expert of the
   bundle logged `loaded successfully`, with no "trading has been disabled"
   (investor password) after the login — or until `--verify-timeout`
   (default 180 s). Verify reads only journal lines written after the start; the
   lines it looks for are defined in [`mt5spec.py`](../src/mt5ctl/mt5spec.py).

If the apply fails, every file it touched is removed and the backup unpacked
over them, then the terminal starts again and `deploy` exits with an error. If
even the restore cannot complete (the connection is gone), the error says so and
names the backup to unpack. A failed verify does not roll back; it exits with
code 3 and says what is missing.

Once the terminal has been asked to stop, it is started again whatever happens
next: an error, a dropped connection while stopping, or the caller giving up
(an MCP client cancelling the tool call, Ctrl-C). The apply and the start run
shielded from cancellation, and the lock is always released.

A deploy with nothing to change does not restart the terminal; its verify reads
the journal since the terminal's latest start (today's and yesterday's files).

## Adopting a running terminal

A deploy only manages paths listed in `.mt5ctl/deployed.json`. On a terminal set
up by hand there is no manifest, so the first deploy would refuse every existing
chart as a conflict. `mt5ctl adopt <terminal> <bundle>` records the bundle's
paths with the digests they have on the terminal now:

- nothing else changes, and the terminal keeps running;
- every bundle file must already be on the terminal;
- files that differ from the bundle are listed as *drifted* — the next deploy
  updates them;
- live charts outside the bundle are listed as *foreign* and stay unmanaged.

Then `mt5ctl plan` shows what a deploy would still change.

## Chart numbering

Charts are files `chart01.chr`, `chart02.chr`, … in the profile folder. A
foreign chart (a watchdog, say) occupies its number; number your bundle's
charts around it so that, together, they run without gaps.

The terminal closes gaps itself when it saves the profile (see
[chart-format.md](chart-format.md#numbering-and-ids)): a managed chart would
come back under another file name, and the next deploy would place its strategy
a second time. So `plan` reports any gap the deploy would leave, `deploy`
refuses, and the fix is to renumber the bundle. Removing a chart from the middle
counts too: to drop `chart02` while a foreign `chart03` stays, move another of
your charts into `chart02`, or give that slot a chart without an expert.

## systemd unit

mt5ctl needs one unit per terminal. A minimal system unit for Wine under Xvfb:

```ini
[Unit]
Description=MetaTrader 5 (demo1)
After=network-online.target

[Service]
User=trader
Environment=WINEPREFIX=/home/trader/.wine-demo1 DISPLAY=:91
WorkingDirectory=/home/trader/mt5/demo1
ExecStart=/usr/bin/wine terminal64.exe /portable
ExecStop=/usr/bin/wine taskkill /IM terminal64.exe
TimeoutStopSec=60
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

With a system unit, the SSH user needs a password-less sudo rule for
`systemctl start|stop|restart` of that unit (mt5ctl runs `sudo -n`, which fails
instead of prompting). With `service_scope: user`, mt5ctl uses
`systemctl --user` and needs no sudo.

### Stopping cleanly

`systemctl stop` alone ends the terminal with a signal, so it has no chance to
save its profile. Ask it to close instead; Wine's `taskkill` without `/F` does
that (observed on build 6320 with Wine 11: the terminal saves and exits within
seconds, journal line `exit with code 0`):

```ini
ExecStop=/usr/bin/wine taskkill /IM terminal64.exe
```

`taskkill /IM` addresses every `terminal64.exe` in the Wine prefix, so give each
terminal its own `WINEPREFIX`. mt5ctl calls `systemctl stop` and then waits for
the process to be gone, so it benefits from such a unit without any
configuration.

## Keeping the terminal build fixed

The formats mt5ctl relies on are checked against one terminal build (see
[`mt5spec.py`](../src/mt5ctl/mt5spec.py)). A terminal updates itself through
LiveUpdate when MetaQuotes releases a build. Observed under Wine (build 6320):
starting it with `/skipupdate` and making `terminal64.exe`, `metatester64.exe`
and `MetaEditor64.exe` read-only keeps the build, and the terminal and tester
keep working. Update deliberately, and check `mt5ctl plan` and `verify` on a
demo terminal after each update.

## Compiling

`mt5ctl compile <terminal> <sources> <out>` uploads a folder of `.mq5` files
(and their `.mqh` includes) to a temporary folder under the terminal's
`MQL5/Experts`, runs that terminal's `metaeditor64.exe` under Wine on each
folder, copies the `.ex5` files back into `<out>`, and removes the temporary
folder. `#include <…>` resolves against the terminal's `MQL5/Include`.

- Use a terminal set aside for building: a copy of an installation that is
  never started. Compiling does not need the terminal running.
- Set `wineprefix` and, if MetaEditor needs a display, `display` for that
  terminal in the registry.
- MetaEditor's exit code is not used. A compile succeeds when every `.mq5` has
  produced an `.ex5` and no log reports errors; otherwise `compile` prints the
  error lines and exits with code 1. Each folder gets its own `/log:<file>`,
  whose `Result: N errors, M warnings` line is read back.
