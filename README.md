<div align="center">

# mt5ctl

**Idempotent expert-advisor deploys for MetaTrader 5 terminals.**

Describe what a terminal should run as a folder of charts and compiled experts.
mt5ctl shows exactly what would change, applies it with a backup and automatic
restore, and touches nothing it did not put there. Built for headless terminals
under Wine and systemd on Linux and WSL2 — from a terminal session or from an AI
agent over MCP.

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

</div>

---

> **Status: pre-release.** The commands below work end to end against the test
> suite's simulated terminal. Chart-file details that MetaQuotes does not document
> (see [docs/chart-format.md](docs/chart-format.md)) are collected in one module,
> [`mt5spec.py`](src/mt5ctl/mt5spec.py), and are being checked against real
> terminal builds before the first release.

## Why

A portfolio of systematic strategies on MetaTrader 5 means dozens of charts per
terminal, each with one expert advisor and its inputs. Keeping that in sync by
hand — attach, set inputs, repeat on every terminal, remember which chart was
yours and which belongs to a watchdog — does not scale, and one stray click on a
live account is expensive.

mt5ctl treats a terminal like a deploy target:

- **A bundle is the desired state.** A folder that mirrors the terminal's data
  folder: chart files plus the `.ex5` experts they run.
- **A plan before every change.** `mt5ctl plan` diffs the bundle against the
  terminal and lists what would be added, updated and removed. It never writes.
- **Managed subset only.** mt5ctl records what it placed in
  `.mt5ctl/deployed.json` and only ever changes those files. Charts you attached
  by hand, a watchdog, a trade copier — all reported as *foreign* and left alone.
- **Compares what trades, not bytes.** The terminal keeps saving display state
  (scroll position, scale, window size) into chart files. mt5ctl compares a
  chart's symbol, timeframe, expert, expert mode and inputs, so scrolling a chart
  or restarting the terminal never shows up as a change.
- **Safe apply.** Stop the terminal, wait until its process has really exited,
  re-plan on the quiet files, back up, apply, start, verify. A failed apply is
  restored from the backup before the terminal starts again; a live account needs
  explicit confirmation.
- **Lossless chart editing.** Charts are read and written back byte for byte —
  UTF-16 and CRLF as the terminal writes them — so changing one input never
  disturbs the rest of the file.
- **Agent-ready.** `mt5ctl serve` exposes the same operations as MCP tools, with
  read-only and destructive tools marked as such.

## Install

```bash
pip install git+https://github.com/ak40u/mt5ctl
mt5ctl --version
```

Python 3.11 or newer on the machine you run mt5ctl from; dependencies are PyYAML
and the MCP SDK. What the terminal hosts need is listed under
[Supported hosts](#supported-hosts).

## Quickstart

**1. Look inside a chart file** — symbol, timeframe, expert and every input:

```console
$ mt5ctl chart show "MQL5/Profiles/Charts/Default/chart01.chr"
symbol     EURUSD
timeframe  H4
format     utf-16-le+bom, CRLF
expert     Trend EURUSD H4
path       Experts\Portfolio\Trend EURUSD H4.ex5
mode       33
inputs     6
  RiskPerTrade   100.0
  MagicNumber    1001
  ...
```

**2. Validate a bundle** — every chart parses and every expert it runs is present:

```console
$ mt5ctl bundle check bundles/demo1
bundle OK: profile 'Default', 2 charts, 2 experts
  chart01.chr  ->  MQL5/Experts/Portfolio/Trend EURUSD H4.ex5
  chart02.chr  ->  MQL5/Experts/Portfolio/Breakout GBPUSD H1.ex5
```

**3. Register your terminals** (see [Configuration](#configuration)), then plan:

```console
$ mt5ctl plan demo1 bundles/demo1
plan for demo1 (profile 'Default'):
  + MQL5/Experts/Portfolio/Breakout GBPUSD H1.ex5
  + MQL5/Profiles/Charts/Default/chart02.chr
  - MQL5/Profiles/Charts/Default/chart03.chr
  foreign MQL5/Profiles/Charts/Default/chart09.chr (not managed, left alone)
2 to add, 0 to update, 1 to remove, 2 unchanged
```

**4. Deploy** — stop, back up, apply, start, and wait until every expert has loaded:

```console
$ mt5ctl deploy demo1 bundles/demo1
deploy demo1:
  + MQL5/Experts/Portfolio/Breakout GBPUSD H1.ex5
  + MQL5/Profiles/Charts/Default/chart02.chr
  - MQL5/Profiles/Charts/Default/chart03.chr
2 to add, 0 to update, 1 to remove, 2 unchanged
backup: /home/trader/mt5/demo1/.mt5ctl/backups/20261115-120000-1a2b3c.tar
restarted
verify: healthy — service active; connected; 2/2 experts loaded
```

Running it again changes nothing and does not restart the terminal. For a
terminal that was set up by hand, run `mt5ctl adopt demo1 bundles/demo1` once
first: it records what the terminal already runs, so the first deploy does not
refuse every existing file. Details: [docs/deploy.md](docs/deploy.md).

Every command takes `--json` for scripting.

## Commands

| Command | What it does | Changes the terminal |
|---|---|---|
| `chart show <file>` | symbol, timeframe, expert and inputs of a `.chr` file | no |
| `bundle check <dir>` | validate a bundle | no |
| `list` | registered terminals | no |
| `plan <terminal> <bundle>` | what a deploy would change | no |
| `status [terminal…]` | service state, broker connection, journal age | no |
| `verify <terminal>` | service, connection, every chart's expert loaded | no |
| `deploy <terminal> <bundle>` | apply a bundle (see above) | yes |
| `adopt <terminal> <bundle>` | record a bundle the terminal already runs as managed | manifest only |
| `restart <terminal>` | restart through systemd, then verify | restarts it |
| `compile <terminal> <src> <out>` | build `.ex5` from `.mq5` with the terminal's MetaEditor | temporary folder |
| `serve` | run the MCP server (stdio) | — |

Commands that change a terminal with `env: live` require `--confirm-live`.

## Bundles

A bundle is a directory laid out like a terminal's data folder:

```text
bundles/demo1/
├── MQL5/Profiles/Charts/Default/
│   ├── chart01.chr          one chart per strategy, with its expert and inputs
│   └── chart02.chr
└── MQL5/Experts/Portfolio/
    ├── Trend EURUSD H4.ex5  the compiled experts the charts run
    └── Breakout GBPUSD H1.ex5
```

- Exactly one profile per bundle; it must match the terminal's `profile`.
- A chart's expert is found through its `path=` line (`Experts\Portfolio\…ex5`)
  and must be in the bundle.
- A chart without an expert is allowed — useful to put a symbol into Market
  Watch for currency conversion.
- Everything else in the folder (sources, notes, logs) is ignored. Symlinks,
  absolute paths and `..` are refused.

The easiest way to make a chart file is to set the chart up once in a terminal,
save it, and generate the rest from it with the `mt5ctl.chart` API. The format is
described in [docs/chart-format.md](docs/chart-format.md).

## Configuration

Terminals live in a YAML registry, looked up in this order: `--config PATH`,
`$MT5CTL_CONFIG`, `~/.config/mt5ctl/terminals.yaml`, `./terminals.yaml`.

```yaml
hosts:
  workstation:            # this machine: a Wine prefix, or Windows itself
    kind: local
  vps:                    # a Linux server over SSH (key-based login)
    kind: ssh
    ssh: trader@vps.example.com
  farm:                   # a Windows server running terminals inside WSL2
    kind: wsl
    ssh: farm.example.com
    wsl_distro: Ubuntu-24.04

terminals:
  demo1:
    host: farm
    data_dir: /home/trader/mt5/demo1   # the folder that contains MQL5/
    service: mt5-demo1.service         # systemd unit; needed by deploy, status, restart
    service_scope: system               # system (sudo -n systemctl) | user (systemctl --user)
    profile: Default                    # optional, default "Default"
    env: demo                           # demo | live
```

The full template is [examples/terminals.example.yaml](examples/terminals.example.yaml).
For a terminal started with `/portable`, the data folder is the install folder.

### Supported hosts

| Host | Read-only commands | deploy, adopt, restart, status | compile |
|---|---|---|---|
| Linux, Wine + systemd, over SSH (`kind: ssh`) | yes | yes | yes |
| WSL2 on a Windows server, over SSH (`kind: wsl`) | yes | yes | yes |
| This machine, Linux or macOS with bash (`kind: local`) | yes | with systemd | with Wine |
| This machine, Windows (`kind: local`) | `chart`, `bundle`, `plan` | no | no |
| A remote Windows host (OpenSSH + PowerShell) | not supported yet | | |

Hosts need `bash`, `tar`, `base64` and `sha256sum` (or `shasum`); `ss` lets
`status` attribute broker connections to each terminal. A system unit is
controlled with `sudo -n systemctl`, so the SSH user needs a password-less sudo
rule for `systemctl start|stop|restart <unit>`, or use a user unit.

### MCP

```bash
claude mcp add --scope user mt5ctl -- mt5ctl serve --config ~/.config/mt5ctl/terminals.yaml
```

Tools: `mt5_list`, `mt5_status`, `mt5_plan`, `mt5_verify`, `mt5_chart` (read-only);
`mt5_deploy`, `mt5_adopt`, `mt5_restart`, `mt5_compile` (change state; `confirm=true`
for live terminals).

## Exit codes

| Code | Meaning |
|---|---|
| `0` | success; for `plan`, the deploy can proceed (with or without changes) |
| `1` | error: bad bundle, unreachable host, corrupt manifest, failed compile… (message on stderr) |
| `2` | `plan` only: a deploy would refuse — a bundle file would overwrite a file mt5ctl does not manage |
| `3` | the terminal is not healthy: after `deploy` or `restart`, in `verify`, or in `status` |

## Safety model

- `plan`, `status`, `verify`, `chart show` and `bundle check` are read-only,
  locally and remotely.
- One deploy, adopt or restart at a time per terminal: a lock in the data folder
  refuses a second run until the first finishes.
- A terminal that was asked to stop is always started again — after an error, a
  dropped connection, or a cancelled MCP call.
- Nothing is written while the terminal runs: after `systemctl stop`, mt5ctl waits
  until no terminal process is left in the unit's cgroup.
- A failed apply puts every touched file back from the backup before the terminal
  starts again. A deploy whose verify fails is reported (exit code 3), not rolled
  back: the files are as intended, and the usual cause is a slow reconnect.
- A bundle file that would land on an existing unmanaged file is a **conflict**,
  and a deploy refuses outright. Typical cause: your bundle's `chart05.chr`
  collides with a chart you attached by hand. Renumber the bundle around it.
- A managed expert dropped from the bundle is removed only when no remaining
  chart runs it. If any foreign chart's expert cannot be resolved, no managed
  expert is removed at all.
- A corrupt or newer-version `deployed.json` stops everything; it is never
  treated as empty.
- Remote scripts are shipped base64-encoded over SSH's stdin and every embedded
  value is shell-quoted, so file names with spaces, quotes or `$` are safe.

## Roadmap

- Check every assumption in `mt5spec.py` against current terminal builds.
- Remote Windows hosts (OpenSSH + PowerShell).
- Market Watch and symbol-set management.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest && .venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests && .venv/bin/mypy
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE). MetaTrader and MQL5 are trademarks of MetaQuotes Ltd.; this
project is not affiliated with MetaQuotes.
