<div align="center">

# mt5ctl

**Idempotent expert-advisor deploys for MetaTrader 5 terminals.**

Describe what a terminal should run as a folder of charts and compiled experts.
mt5ctl shows exactly what would change, and touches nothing it did not put there.
Built for headless terminals under Wine (Linux, WSL2) and for Windows.

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

</div>

---

> **Status: early development.** Reading charts, validating bundles and planning a
> deploy (`mt5ctl plan`) work today. Applying a deploy, adopting a running terminal,
> status, restart and the MCP server are being built next; see [Roadmap](#roadmap).

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
- **Lossless chart editing.** Charts are read and written back byte for byte —
  UTF-16 and CRLF as the terminal writes them — so changing one input never
  disturbs the rest of the file.

## Install

```bash
pip install git+https://github.com/ak40u/mt5ctl
mt5ctl --version
```

Python 3.11 or newer. The only runtime dependency is PyYAML. Remote hosts need
`bash`, `base64` and `sha256sum` (or `shasum`) — standard on any Linux.

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

Every command takes `--json` for scripting.

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
    profile: Default                    # optional, default "Default"
    env: demo                           # demo | live
```

The full template is [examples/terminals.example.yaml](examples/terminals.example.yaml).
For a terminal started with `/portable`, the data folder is the install folder.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | success; for `plan`, the deploy can proceed (with or without changes) |
| `1` | error: bad bundle, unreachable host, corrupt manifest… (message on stderr) |
| `2` | `plan` only: a deploy would refuse — a bundle file would overwrite a file mt5ctl does not manage |

## Safety model

- `plan`, `chart show` and `bundle check` are read-only, locally and remotely.
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

- `deploy`: stop the terminal, wait for it to exit, back up, apply, start, with
  automatic rollback when the apply fails, and a health check afterwards.
- `adopt`: take an already-running terminal under management without touching it.
- `status` and `restart` for terminals supervised by systemd.
- Compile check: build `.ex5` from sources with MetaEditor and fail on errors.
- An MCP server exposing the same operations to AI agents.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest && .venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests && .venv/bin/mypy
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE). MetaTrader and MQL5 are trademarks of MetaQuotes Ltd.; this
project is not affiliated with MetaQuotes.
