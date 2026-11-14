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
  module.
- CLI: `chart show`, `bundle check`, `plan`, `list`, `deploy`, `adopt`, `status`,
  `restart`, `verify`, `compile`, `serve`, all with `--json`.
