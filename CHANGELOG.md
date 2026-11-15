# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Lossless reader and writer for MetaTrader 5 chart files (`mt5ctl.chart`):
  encoding detection (UTF-16LE/BE, UTF-8, Windows-1252), byte-exact round trip,
  typed view of symbol, timeframe, expert and inputs, safe in-place edits.
- Deploy bundles (`mt5ctl.bundle`): one profile of charts plus the `.ex5`
  experts they run, with path allowlisting and integrity checks.
- Managed-subset reconcile (`mt5ctl.reconcile`) that compares charts by what
  they trade rather than by bytes.
- Terminal state collection from a local data folder or over SSH (plain Linux
  and WSL2 hosts) in one read-only round trip.
- `deploy`: lock, stop, drain, re-plan, backup, staged upload with sha256
  checks, apply, start and verify; automatic restore when the apply fails; no
  restart when nothing changes.
- `adopt`: record a bundle a running terminal already has, without touching it.
- `status`, `restart` and `verify` for terminals run by systemd (system or user
  units); broker connection from the unit's sockets or the journal.
- `compile`: build `.ex5` with a terminal's MetaEditor under Wine, judged by
  output rather than exit code.
- MCP server (`mt5ctl serve`) with read-only and destructive tools annotated.
- `mt5spec`: every assumption about undocumented terminal behaviour in one
  module, checked against files saved by MetaTrader 5 build 6320 (chart and
  expert blocks, timeframe codes, the order file, journal and compile logs).
- A deploy that would leave gaps in chart numbering is refused: the terminal
  renumbers charts to close gaps when it saves the profile.
- `Expert.algo_trading`; `bundle check` warns about experts that would load
  without trading.
- `status` and `verify` report an account whose trading is disabled (investor
  password) as not healthy.
- `start` and `stop` commands (CLI and MCP); `stop` and `deploy` tell from the
  terminal's journal whether it closed itself or systemd had to end it.
- `status` and `verify` report an active unit whose terminal process is not
  running, and need a confirmed broker connection for "healthy".
- Terminals take a `wine` setting; `compile` requires it when a `wineprefix` is set.
- `status` and `verify` report the terminal's Algo Trading button switched off.
- W1 and MN1 timeframes; failed expert loads recognised from the journal; `plan`
  explains a chart the terminal saved without its expert.
- `compile` handles folders with spaces through a space-free stand-in.
- Process mode: `start_command` / `stop_command` run a terminal without systemd.
- CLI: `chart show`, `bundle check`, `plan`, `list`, `deploy`, `adopt`, `status`,
  `restart`, `verify`, `compile`, `serve`, all with `--json`.
