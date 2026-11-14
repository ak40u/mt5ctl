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
- CLI: `chart show`, `bundle check`, `plan`, `list`, all with `--json`.
