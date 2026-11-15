# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed (from two independent reviews)

- The lock: its owner is written atomically with it, its timestamp is never
  evaluated by the shell, two runs can no longer both take over a stale one,
  and a run whose lock was taken over cannot write.
- A failed apply is restored honestly: nothing is removed before the backup is
  known to be readable, success is reported only when every file is back, and a
  deploy cut short completes when run again.
- Interruptions (Ctrl-C, SIGTERM, SIGHUP, a cancelled MCP call, a timeout) never
  cut an apply short, never leave a script writing behind, and the terminal is
  started again; the start is retried while the host cannot be reached.
- A symbolic link inside the data folder can no longer make a deploy write
  outside it.
- Charts the terminal renumbered (after one was closed by hand) block the
  deploy instead of being overwritten or run twice.
- A bundle for another profile is refused instead of planning the removal of
  every managed chart; the deploy uploads the bytes it checked.
- Verify counts the expert of every chart (an expert on three charts must load
  three times), finds a start however long ago, and is never healthy when it
  cannot tell this start from an earlier one.
- A foreign chart's expert path is read the way Wine follows it, so the expert
  it runs is not removed.
- A stopped terminal is left stopped by a deploy; adopt never stops managing
  files without --replace; a wrong data_dir is refused; a terminal left running
  on the data folder stops the deploy; files and new folders go to the unit's
  user.
- compile mirrors the whole include tree of a folder with a space, skips hidden
  files, and a failed compile or login is a failed MCP call.
- Smaller: cut-off scripts and answers are refused, CLI errors print without a
  traceback, chart names and line endings are checked in bundles, and a
  long-running MCP server notices a terminal marked live.

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
- Changing commands on a terminal with `env: live` are refused without
  `--confirm` (`confirm=true` over MCP).
- `login` (CLI and MCP `mt5_login`): one headless login with the password from
  stdin, the environment or a private credentials file; the temporary start
  file is shredded and the terminal started again and verified. Terminals take
  an `account` setting.
- `ea-list` and `autotrading` (CLI and MCP): each chart's expert, loaded or
  failed, its own permission, and the terminal's Algo Trading button from the
  journal or the start file.
- `screenshot` (CLI and MCP, which returns the image): the terminal's display
  as PNG; terminals take an `xauthority` setting, and the display falls back to
  the unit's environment.
- `init`: a starter registry, mode 600, never overwriting.
- `doctor` (CLI and MCP): tools on each host, units and their sudo rules,
  data folders, Wine and its prefix, journals and broker connections, read-only.
- `info` (build, account, server, access point, broker, positions and orders,
  start file) and `logs` (journal or experts' log, with `--grep`), CLI and MCP.
- CLI: `chart show`, `bundle check`, `plan`, `list`, `deploy`, `adopt`, `status`,
  `info`, `logs`, `restart`, `verify`, `compile`, `login`, `serve`, all with `--json`.
