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

mt5ctl needs one unit per terminal. A system unit for Wine with its own virtual
display, as run for the formats in `mt5spec.py`:

```ini
[Unit]
Description=MetaTrader 5 (demo1)
After=network-online.target

[Service]
User=trader
Environment=WINEPREFIX=/home/trader/mt5/demo1-prefix
Environment=WINEDEBUG=-all
Environment=DISPLAY=:91
Environment=XAUTHORITY=/home/trader/mt5/demo1.Xauthority
WorkingDirectory=/home/trader/mt5/demo1
ExecStart=/usr/bin/xvfb-run -n 91 -f /home/trader/mt5/demo1.Xauthority -s "-screen 0 1280x1024x24 -nolisten tcp" /usr/bin/wine terminal64.exe /portable /skipupdate /config:cfg\\start.ini
ExecStop=/home/trader/mt5/stop-terminal.sh
TimeoutStopSec=60
KillMode=control-group
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

With a system unit, the SSH user needs a password-less sudo rule for
`systemctl start|stop|restart` of that unit (mt5ctl runs `sudo -n`, which fails
instead of prompting). With `service_scope: user`, mt5ctl uses
`systemctl --user` and needs no sudo.

### Stopping cleanly

**Required.** A terminal saves its profile only when it closes itself. Killed by
a signal, it saves nothing, and a deploy then works on charts the terminal never
wrote back. Three things make the difference, all observed on build 6320 under
Wine 11:

1. **Ask it to close.** Wine's `taskkill` without `/F` sends the close request;
   the terminal saves and exits within seconds, journal line `exit with code 0`.
2. **Wait for it, then end Wine.** systemd sends SIGTERM to every process left
   in the unit as soon as `ExecStop` returns. `taskkill` returns at once, so the
   terminal is killed halfway through closing; `ExecStop` has to wait until the
   main process is gone. The Wine helpers (`wineserver`, `services.exe`, ...)
   stay behind and ignore SIGTERM, so it then ends them with `wineserver -k`.
   With both, a stop takes about 10 s; without, the unit hangs until
   `TimeoutStopSec`.
3. **On its own display.** `ExecStop` needs the terminal's `DISPLAY` and
   `XAUTHORITY`. Without them `taskkill` reports that it sent the request, but
   the terminal does not close. Hence the fixed authority file passed to
   `xvfb-run -f` in the unit above.

`stop-terminal.sh`, executable, next to the terminal:

```sh
#!/bin/sh
# Ask the terminal to close and wait until it has. Needs WINEPREFIX, DISPLAY
# and XAUTHORITY from the unit, and $MAINPID, which systemd sets for ExecStop.
/usr/bin/wine taskkill /IM terminal64.exe >/dev/null 2>&1
i=0
while [ -n "$MAINPID" ] && kill -0 "$MAINPID" 2>/dev/null && [ "$i" -lt 50 ]; do
  sleep 1
  i=$((i + 1))
done
# The terminal is gone; end the Wine helpers left in the prefix. They ignore
# SIGTERM and would hold the unit until TimeoutStopSec.
/usr/bin/wineserver -k 2>/dev/null
/usr/bin/wineserver -w 2>/dev/null
exit 0
```

`taskkill /IM` addresses every `terminal64.exe` in the Wine prefix, so give each
terminal its own `WINEPREFIX`. A modal window also keeps the terminal from
closing — for example the "Select a company to open an account with" dialog of
a terminal with no account to log in to, which is what you get when the
`/config:` file cannot be read (journal: `cannot load config ... at start`).

`mt5ctl stop` and `deploy` tell the two outcomes apart by the terminal's own
journal: with its exit line, it closed itself; without, systemd had to end it,
and mt5ctl prints a warning. (systemd's `Result` for the unit is no guide: it
reads `exit-code` even after a clean exit.)

## Without systemd (process mode)

Where there is no systemd (WSL without it, a container, a host where you cannot
install units), mt5ctl can run the terminal itself. Instead of `service`, give
the terminal a `start_command` and a `stop_command`:

```yaml
terminals:
  demo1:
    host: box
    data_dir: /home/trader/mt5/demo1
    start_command: >-
      export WINEPREFIX=/home/trader/mt5/demo1-prefix;
      exec xvfb-run -n 91 -f /home/trader/mt5/demo1.Xauthority
      wine terminal64.exe /portable /skipupdate '/config:cfg\start.ini'
    stop_command: >-
      WINEPREFIX=/home/trader/mt5/demo1-prefix DISPLAY=:91
      XAUTHORITY=/home/trader/mt5/demo1.Xauthority
      /home/trader/mt5/stop-terminal.sh
```

- `start` runs `start_command` in the data folder as a background process group
  and keeps its pid in `.mt5ctl/terminal.pid`; its output goes to
  `.mt5ctl/terminal.out`.
- `stop` runs `stop_command` with `$MAINPID` set, so the same
  `stop-terminal.sh` serves both modes. It waits up to 50 s for the group to
  exit, then ends it (TERM, then KILL). Whether the terminal closed itself is
  told by its journal, as with systemd.
- `deploy`, `restart`, `status` and `verify` work the same way.

What you give up: nothing restarts the terminal if it crashes or the host
reboots. Use a systemd unit where you can; process mode is for where you cannot.

## Logging in

A terminal under a unit has no one to type its password into the login dialog.
`mt5ctl login` logs it in once, headless, and the terminal keeps the password
(in `Config/accounts.dat`, which mt5ctl never touches):

```sh
mt5ctl login demo1 --server ExampleBroker-Demo --account 12345678 --password-stdin < password.txt
```

It stops the terminal, starts it once on a private virtual display with a
temporary start file holding the login, closes it as soon as the journal shows
the authorization (or the broker's refusal), shreds that file, starts the
terminal again the usual way and verifies it. Nothing stops it from starting
again: an error, a timeout or Ctrl-C included.

The password comes, in this order, from stdin (`--password-stdin`; `password`
over MCP), the `MT5CTL_PASSWORD_<account>` environment variable, or the
`<account>` key of `~/.config/mt5ctl/credentials.json` (or `$MT5CTL_CREDENTIALS`),
which must be `chmod 600`. It is never on a command line, in a log or in the
output: it reaches the host inside the script on SSH's stdin, and the temporary
file is created with mode 600 and shredded when the script ends.

For the terminal to log in by itself after that, its own start file (the one
the unit passes with `/config:`) names the account and server, **without a
password**:

```ini
[Common]
Login=12345678
Server=ExampleBroker-Demo
[Experts]
Enabled=1
```

Observed (build 6320): with this file the terminal logs in with the saved
password at every start. Started with `/login:<account>` instead, or with no
start file, it opens the login dialog and waits there; `login` then reports
`NOT connected` after the restart. Run `login` again when the password changes.

## Algo Trading

The terminal's **Algo Trading** button must be on for any expert to trade. Its
state is kept in `Config/settings.ini`, which is binary, so mt5ctl cannot read
it from disk. What is observed (build 6320):

- Switching the button logs `automated trading is disabled` / `... enabled`
  in the journal; nothing is logged at start.
- `[Experts]` with `Enabled=1` in the `/config:` start file switches it on at
  every start, overriding a manual "off".

So the way to have it on without a GUI is that start file — the one the unit
passes with `/config:` (the same file that names the account, see
[Logging in](#logging-in)):

```ini
[Experts]
Enabled=1
```

Every `deploy` and `restart` then switches it back on. `mt5ctl autotrading`
reports the button: from the journal when it was switched during the session,
otherwise from the start file the journal says the terminal was launched with
(only its `[Experts] Enabled` value is read; the rest of the file, which may
hold a password, never leaves the host). `status` and `verify`
report `ALGO TRADING OFF` (and "not healthy") when the journal shows it was
switched off after the latest start. Each expert also has its own "Allow Algo
Trading" (bit 0 of `expertmode` in its chart); `bundle check` warns about
charts where that is off.

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
