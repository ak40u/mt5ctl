<div align="center">

# mt5ctl

**Idempotent expert-advisor deploys for MetaTrader 5 terminals.**

Describe what a terminal should run as a folder of charts and compiled experts.
mt5ctl shows exactly what would change, applies it with a backup and automatic
restore, and touches nothing it did not put there. Built for headless terminals
under Wine and systemd on Linux and WSL2 — driven from a shell or by an AI agent
over MCP.

[![CI](https://github.com/ak40u/mt5ctl/actions/workflows/ci.yml/badge.svg)](https://github.com/ak40u/mt5ctl/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

</div>

---

> **Pre-release.** Every command works end to end in the test suite, which runs
> the real shell scripts against a simulated terminal, and the file formats and
> journal lines are checked against files saved by MetaTrader 5 build 6320,
> logged in to a broker. The few remaining assumptions are listed under
> [Limitations](#limitations). Try it on a demo terminal first.

## Why

A portfolio of systematic strategies on MetaTrader 5 means dozens of charts per
terminal, each running one expert advisor with its own inputs. Keeping that in
sync by hand — attach, set inputs, repeat on every terminal, remember which chart
was yours and which belongs to a watchdog — does not scale, and one stray click
on a live account is expensive.

mt5ctl treats a terminal like a deploy target:

- **A bundle is the desired state.** A folder laid out like the terminal's data
  folder: chart files, plus the `.ex5` experts they run.
- **A plan before every change.** `mt5ctl plan` lists what a deploy would add,
  update and remove. It never writes anything.
- **Managed subset only.** mt5ctl records what it placed and only ever changes
  those files. Charts you attached by hand, a watchdog, a trade copier are
  reported as *foreign* and left alone.
- **Compares what trades, not bytes.** The terminal keeps saving display state
  (scroll position, scale, window size) into chart files. mt5ctl compares symbol,
  timeframe, expert, expert mode and inputs, so scrolling a chart or restarting
  the terminal never counts as a change.
- **Careful apply.** Stop the terminal, wait until its process has exited, plan
  again on the quiet files, back up, apply, start, and wait until every expert
  has loaded. A failed apply is put back from the backup before the terminal
  starts again.
- **Lossless chart files.** Charts are read and written back byte for byte, in
  the terminal's own UTF-16 and CRLF, so changing one input disturbs nothing else.

## What a deploy touches

Inside the terminal's data folder, and nowhere else:

| Path | What happens |
|---|---|
| `MQL5/Profiles/Charts/<profile>/chartNN.chr` | the bundle's charts are placed; charts mt5ctl placed earlier and the bundle no longer has are removed |
| `MQL5/Experts/**/*.ex5` | the same, for experts; an expert is removed only when no remaining chart runs it |
| `MQL5/Profiles/Charts/<profile>/order.wnd` | the profile's window list is kept in step: removed charts dropped, new ones added, every other entry kept |
| `.mt5ctl/` | mt5ctl's own state: the manifest of what it placed, the five newest backups, upload staging, the lock |

On the host, it runs `systemctl stop` and `systemctl start` for the terminal's
unit — only when something actually changes.

It never touches other profiles, templates, presets, indicators or libraries,
`config/` (accounts, passwords, servers), Market Watch, history, the journal, or
anything outside the data folder. It never logs in to an account and never
handles credentials.

The other commands touch less: `adopt` writes only `.mt5ctl/deployed.json`;
`restart` runs `systemctl restart`; `compile` works in a temporary
`MQL5/Experts/mt5ctl-build-*` folder that it removes afterwards. `plan`,
`status`, `verify`, `chart show` and `bundle check` only read.

## Install

```bash
pipx install git+https://github.com/ak40u/mt5ctl     # or: pip install git+https://…
mt5ctl --version
```

Python 3.11 or newer on the machine you run mt5ctl from. What the terminal's host
needs is listed under [Supported hosts](#supported-hosts).

## Quick start: one terminal

You need a MetaTrader 5 terminal installed in portable mode under Wine, run by a
systemd unit, on a host you can reach with key-based SSH. An example unit file,
including a clean-shutdown `ExecStop`, is in [docs/deploy.md](docs/deploy.md#systemd-unit).

**1. Register the terminal** in `~/.config/mt5ctl/terminals.yaml`:

```yaml
hosts:
  vps:
    kind: ssh
    ssh: trader@vps.example.com       # an ~/.ssh/config alias works too

terminals:
  demo1:
    host: vps
    data_dir: /home/trader/mt5/demo1  # the folder that contains MQL5/
    service: mt5-demo1.service
    env: demo
```

**2. Check that mt5ctl can see it:**

```console
$ mt5ctl status demo1
demo1          demo active     connected    up 2h14m     journal 4s ago
```

**3. Make a bundle.** The quickest start is the terminal's own state: copy its
profile and the experts its charts run, then delete the charts you do not want
mt5ctl to manage (a watchdog, a manual chart) — they will stay foreign.

```bash
mkdir -p bundles/demo1/MQL5/Profiles/Charts bundles/demo1/MQL5/Experts
rsync -a trader@vps.example.com:/home/trader/mt5/demo1/MQL5/Profiles/Charts/Default bundles/demo1/MQL5/Profiles/Charts/
rsync -a trader@vps.example.com:/home/trader/mt5/demo1/MQL5/Experts/Portfolio bundles/demo1/MQL5/Experts/
rm bundles/demo1/MQL5/Profiles/Charts/Default/chart03.chr   # the watchdog stays foreign
```

```console
$ mt5ctl bundle check bundles/demo1
bundle OK: profile 'Default', 2 charts, 2 experts
  chart01.chr  ->  MQL5/Experts/Portfolio/Trend EURUSD H4.ex5
  chart02.chr  ->  MQL5/Experts/Portfolio/Reversal USDJPY H4.ex5
```

The copied `order.wnd` is ignored: mt5ctl keeps the terminal's own in step.

**4. Adopt** — once, because these files are already on the terminal. It only
records them as managed; nothing else changes and the terminal keeps running:

```console
$ mt5ctl adopt demo1 bundles/demo1
adopted 4 files on demo1
  foreign MQL5/Profiles/Charts/Default/chart03.chr (left unmanaged)
manifest: /home/trader/mt5/demo1/.mt5ctl/deployed.json
```

**5. Change the bundle, plan, deploy.** Here chart02 switches from the USDJPY
reversal strategy to a GBPUSD breakout: its chart file is replaced (written with
the [Python API](docs/chart-format.md#editing-charts-from-python) or copied from
another terminal) and the new `.ex5` dropped in, the old one deleted:

```console
$ mt5ctl plan demo1 bundles/demo1
plan for demo1 (profile 'Default'):
  + MQL5/Experts/Portfolio/Breakout GBPUSD H1.ex5
  ~ MQL5/Profiles/Charts/Default/chart02.chr
  - MQL5/Experts/Portfolio/Reversal USDJPY H4.ex5
  foreign MQL5/Profiles/Charts/Default/chart03.chr (not managed, left alone)
1 to add, 1 to update, 1 to remove, 2 unchanged

$ mt5ctl deploy demo1 bundles/demo1
deploy demo1:
  + MQL5/Experts/Portfolio/Breakout GBPUSD H1.ex5
  ~ MQL5/Profiles/Charts/Default/chart02.chr
  - MQL5/Experts/Portfolio/Reversal USDJPY H4.ex5
  foreign MQL5/Profiles/Charts/Default/chart03.chr (not managed, left alone)
1 to add, 1 to update, 1 to remove, 2 unchanged
backup: /home/trader/mt5/demo1/.mt5ctl/backups/20261115-120001-931e70.tar
restarted
verify: healthy — service active; connected; 2/2 experts loaded
```

Running the same deploy again changes nothing and does not restart the terminal:

```console
$ mt5ctl deploy demo1 bundles/demo1
deploy demo1:
  foreign MQL5/Profiles/Charts/Default/chart03.chr (not managed, left alone)
0 to add, 0 to update, 0 to remove, 4 unchanged; nothing to do
not restarted (nothing changed)
verify: healthy — service active; connected; 2/2 experts loaded
```

For a terminal tagged `env: live`, every changing command needs `--confirm-live`.
Every command takes `--json`.

## With an AI agent (MCP)

`mt5ctl serve` runs an MCP server on stdio with the same operations as tools.
Read-only tools are marked read-only and changing tools destructive, so clients
can ask before running them; a live terminal additionally requires
`confirm=true`.

Claude Code:

```bash
claude mcp add --scope user mt5ctl -- mt5ctl serve
```

Any other MCP client:

```json
{
  "mcpServers": {
    "mt5ctl": {
      "command": "mt5ctl",
      "args": ["serve", "--config", "/home/you/.config/mt5ctl/terminals.yaml"]
    }
  }
}
```

| Tool | Changes state |
|---|---|
| `mt5_list`, `mt5_status`, `mt5_plan`, `mt5_verify`, `mt5_chart` | no |
| `mt5_deploy`, `mt5_adopt`, `mt5_start`, `mt5_stop`, `mt5_restart`, `mt5_compile` | yes |

Then ask, for example: *"Plan deploying bundles/demo1 to demo1 and show me the
plan"*, *"Deploy it"*, *"Is demo1 healthy?"*. If the client gives up on a deploy
midway, the terminal is still started again and the lock released.

## Commands

| Command | What it does | Changes the terminal |
|---|---|---|
| `chart show <file>` | symbol, timeframe, expert and inputs of a `.chr` file | no |
| `bundle check <dir>` | validate a bundle | no |
| `list` | registered terminals | no |
| `plan <terminal> <bundle>` | what a deploy would change | no |
| `status [terminal…]` | service state, terminal process, broker connection, journal age | no |
| `verify <terminal>` | service, connection, every chart's expert loaded | no |
| `deploy <terminal> <bundle>` | apply a bundle | yes |
| `adopt <terminal> <bundle>` | record a bundle the terminal already runs as managed | manifest only |
| `start <terminal>` / `stop <terminal>` | start or stop through systemd; `stop` says whether the terminal closed itself | starts / stops it |
| `restart <terminal>` | restart through systemd, then verify | restarts it |
| `compile <terminal> <src> <out>` | build `.ex5` from `.mq5` with the terminal's MetaEditor | temporary folder |
| `serve` | run the MCP server (stdio) | — |

| Exit code | Meaning |
|---|---|
| `0` | success; for `plan`, the deploy can proceed |
| `1` | error: bad bundle, unreachable host, corrupt manifest, failed compile… (on stderr) |
| `2` | `plan`: a deploy would refuse, because a bundle file would overwrite a file mt5ctl does not manage |
| `3` | the terminal is not healthy (after `deploy` or `restart`, in `verify`, in `status`) |

## Bundles

```text
bundles/demo1/
├── MQL5/Profiles/Charts/Default/
│   ├── chart01.chr          one chart per strategy, with its expert and inputs
│   └── chart02.chr
└── MQL5/Experts/Portfolio/
    ├── Trend EURUSD H4.ex5  the compiled experts the charts run
    └── Breakout GBPUSD H1.ex5
```

- One profile per bundle, matching the terminal's `profile` (default `Default`).
- A chart's expert is found through its `path=` line (`Experts\Portfolio\…ex5`)
  and must be in the bundle.
- A chart without an expert is fine — for example, to put a symbol into Market
  Watch for currency conversion.
- Number charts so that, together with the foreign charts on the terminal, they
  run `chart01`, `chart02`, … without gaps. The terminal closes gaps itself when
  it saves the profile, which would move a managed chart to another file name,
  so a deploy that would leave a gap is refused.
- `bundle check` warns about charts whose expert has "Allow Algo Trading"
  unticked: they load, but do not trade.
- Anything else in the folder (sources, notes, logs) is ignored. Symlinks,
  absolute paths and `..` are refused.

The chart format and the Python API for generating charts from a template are in
[docs/chart-format.md](docs/chart-format.md).

## Configuration

The registry is looked up in this order: `--config PATH`, `$MT5CTL_CONFIG`,
`~/.config/mt5ctl/terminals.yaml`, `./terminals.yaml`. Keep it private: it names
your hosts. The full template is
[examples/terminals.example.yaml](examples/terminals.example.yaml).

| Terminal field | Meaning |
|---|---|
| `host` | a host from `hosts:` |
| `data_dir` | the folder containing `MQL5/`; for a `/portable` terminal, the install folder |
| `service` | its systemd unit; needed by `deploy`, `start`, `stop`, `restart` (or use `start_command`) |
| `service_scope` | `system` (default, via `sudo -n systemctl`) or `user` (`systemctl --user`) |
| `start_command`, `stop_command` | without systemd: mt5ctl runs the terminal itself ([process mode](docs/deploy.md#without-systemd-process-mode)) |
| `profile` | the chart profile mt5ctl manages, default `Default` |
| `env` | `demo` (default) or `live` |
| `wine`, `wineprefix`, `display` | for `compile`: the terminal's own Wine build, its prefix and X display. Set `wine` whenever `wineprefix` is set: another Wine build updates the prefix, and the terminal then stops at a Wine dialog on its next start |

### Supported hosts

| Host | Read-only commands | deploy, adopt, restart, status | compile |
|---|---|---|---|
| Linux with Wine and systemd, over SSH (`kind: ssh`) | yes | yes | yes |
| WSL2 on a Windows server, over SSH (`kind: wsl`) | yes | yes | yes |
| This machine, Linux or macOS (`kind: local`) | yes | yes | with Wine |
| This machine, Windows (`kind: local`) | `chart`, `bundle`, `plan` | no | no |
| A remote Windows host (OpenSSH + PowerShell) | not supported | | |

Terminals run under a systemd unit or, where there is none, in
[process mode](docs/deploy.md#without-systemd-process-mode). The host needs
`bash`, `tar`, `base64` and `sha256sum` (or `shasum`); with `ss`, `status`
attributes broker connections to each terminal. For a system unit, the
SSH user needs a password-less sudo rule for `systemctl start|stop|restart` of
that unit (mt5ctl uses `sudo -n`, which fails instead of prompting); a user unit
needs no sudo.

## Safety

- **One run at a time.** Deploy, adopt and restart hold a per-terminal lock; a
  second run refuses until the first finishes.
- **Nothing is written while the terminal runs.** After `systemctl stop`, mt5ctl
  waits until no terminal process is left in the unit's cgroup, then plans again
  on the files the terminal left behind.
- **Verified uploads.** Each file's sha256 is checked on the host before anything
  is moved into place; the manifest is written last.
- **Back to the previous state on failure.** A failed apply restores every touched
  file from the backup before the terminal starts again. If even the restore
  cannot finish, the error says so and names the backup.
- **Always started again.** Once asked to stop, the terminal is started again
  after an error, a dropped connection, Ctrl-C or a cancelled MCP call.
- **Refuses rather than guesses.** A bundle file landing on a file mt5ctl does not
  manage is a conflict and the deploy refuses. A corrupt manifest, or one listing
  a path outside the managed folders, stops everything. If any foreign chart's
  expert cannot be identified, no expert is removed.
- **Live terminals need confirmation**: `--confirm-live`, or `confirm=true` over MCP.
- **A terminal that cannot trade is not healthy.** Logged in with the investor
  password, or with its Algo Trading button switched off, a terminal loads every
  expert but no order goes out; `status` and `verify` report
  `TRADING DISABLED (investor mode)` or `ALGO TRADING OFF` and exit with code 3.
  `[Experts] Enabled=1` in the start config switches Algo Trading on at every
  start ([docs/deploy.md](docs/deploy.md#algo-trading)).
- **Not rolled back on a slow start.** If verify fails after a deploy, the files
  are as intended; mt5ctl reports what has not loaded (exit code 3) instead of
  undoing a correct deploy because the broker reconnected slowly.
- **Shell-safe.** Scripts travel base64-encoded over SSH's stdin and every value
  in them is single-quoted, so file names with spaces, quotes or `$` are safe.

## Limitations

- **Checked against one build.** File formats and journal lines are checked
  against MetaTrader 5 build 6320 under Wine 11. Every assumption about
  undocumented behaviour is in one module, [`mt5spec.py`](src/mt5ctl/mt5spec.py),
  each with its source; of `expertmode`, only the "Allow Algo Trading" bit is known.
- **Algo Trading is read from the journal.** The terminal keeps the button's
  state in a binary file, so mt5ctl sees it only when it was switched after the
  latest start. Switch it on with the start config
  ([docs/deploy.md](docs/deploy.md#algo-trading)).
- **A damaged expert file can go unnoticed in the journal.** A truncated `.ex5`
  and a failing `OnInit` are reported as failed; a file of random bytes logs
  nothing and is reported as "not loaded".
- **Compiling needs a path without spaces** for the terminal's data folder;
  folders with spaces inside the sources are handled.
- **No remote Windows yet.** Terminals on a Windows host are reachable through
  WSL2, or with `kind: local` for the read-only commands. The plan is in
  [docs/windows.md](docs/windows.md).
- **Files only.** mt5ctl does not log in to accounts, change terminal settings or
  manage Market Watch.

## Documentation

- [docs/deploy.md](docs/deploy.md): the deploy sequence step by step, adopting a
  running terminal, the systemd unit, compiling.
- [docs/chart-format.md](docs/chart-format.md): chart files, timeframe codes, the
  order file, editing charts from Python.
- [docs/windows.md](docs/windows.md): what managing a remote Windows host would take.
- [CHANGELOG.md](CHANGELOG.md), [CONTRIBUTING.md](CONTRIBUTING.md).

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest && .venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests && .venv/bin/mypy
```

## License

[MIT](LICENSE). MetaTrader and MQL5 are trademarks of MetaQuotes Ltd.; this
project is not affiliated with MetaQuotes.
