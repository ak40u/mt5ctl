# Commands and MCP tools

Every operation is available both as a CLI command and as a tool of the MCP
server (`mt5ctl serve`), except the purely local `chart`, `bundle check`, `init`
and `serve`. Each CLI command takes `--config PATH` and `--json`.

On a terminal with `env: live`, every changing command is refused unless it is
given `--confirm` (MCP: `confirm=true`), before anything on the host is touched.

## Look

Read-only. Nothing on the host is changed.

| CLI | MCP tool | What it tells |
|---|---|---|
| `list` | `mt5_list` | registered terminals |
| `status [terminal…]` | `mt5_status(terminal="all")` | service or process state, uptime, broker connection (from the terminal's sockets, else the journal), journal age, trading disabled, Algo Trading off. One round trip per host, all hosts at once. Exit 3 when a terminal is not healthy |
| `info [terminal…]` | `mt5_info(terminal="all")` | from the journal's latest session: build, account, server, access point, broker company, open positions and orders, the start file, the Wine line |
| `ea-list [terminal…]` | `mt5_ea_list(terminal="all")` | each chart of the profile: symbol, timeframe, expert, loaded / failed / removed in this session, and the expert's own Algo Trading permission |
| `autotrading [terminal…]` | `mt5_autotrading(terminal="all")` | the terminal's Algo Trading button (from the journal when it was switched during the session, otherwise the start file's `[Experts] Enabled`) and how many experts can trade |
| `logs <terminal> [-n N] [--grep RE] [--experts]` | `mt5_logs(terminal, lines=50, grep=None, experts=false)` | the end of the newest journal, or of the experts' log (what `Print` writes); `RE` is a case-insensitive regular expression. At most the last 4 MB of the file is read |
| `screenshot <terminal> [-o PATH]` | `mt5_screenshot(terminal)` | a PNG of the terminal's whole virtual display; the MCP tool returns the image itself. Needs ImageMagick's `import` on the host and the display (`display`/`xauthority`, or the unit's environment) |
| `verify <terminal> [--timeout S]` | `mt5_verify(terminal, timeout=None)` | service up, broker connected, every chart's expert loaded, trading possible. Exit 3 when not |
| `plan <terminal> <bundle>` | `mt5_plan(terminal, bundle)` | what a deploy would add, update, remove, and conflicts. Exit 2 when a deploy would refuse |
| `doctor` | `mt5_doctor()` | every host and terminal: the tools the scripts need, units and their sudo rules, data folders, `terminal64.exe`, Wine and its prefix, journals, the credentials file's permissions, broker connections. Exit 1 on a failed check |
| `chart show <file>` | `mt5_chart(path)` | a local `.chr` file: symbol, timeframe, expert and inputs |
| `bundle check <dir>` | — | validate a bundle folder |

## Change

| CLI | MCP tool | What it does |
|---|---|---|
| `deploy <terminal> <bundle>` | `mt5_deploy(terminal, bundle, confirm, verify_timeout)` | lock, stop and wait for the exit, plan again, back up, upload with checksums, apply, start, verify; restores the backup when the apply fails ([deploy.md](deploy.md#the-deploy-sequence)) |
| `adopt <terminal> <bundle>` | `mt5_adopt(terminal, bundle, confirm)` | record a bundle the terminal already runs as managed, without touching it |
| `start` / `stop` / `restart <terminal>` | `mt5_start`, `mt5_stop`, `mt5_restart` | through systemd, or the terminal's own commands in process mode; `stop` reports whether the terminal closed itself; `start` and `restart` verify |
| `login <terminal> --server S [--account N] --password-stdin` | `mt5_login(terminal, server, account, password, confirm, verify_timeout)` | one headless login; the terminal keeps the password and logs in by itself afterwards ([deploy.md](deploy.md#logging-in)). The password is never in a command line, a log or the output |
| `compile <terminal> <src> <out>` | `mt5_compile(terminal, sources, out, confirm)` | build `.ex5` from local `.mq5` sources with the terminal's MetaEditor in a temporary folder on the host; the terminal itself is not touched |

## Set up

| CLI | What it does |
|---|---|
| `init [path]` | write a starter registry (mode 600, never overwriting) where it is looked up first |
| `serve` | run the MCP server on stdio |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | error (message on stderr), or a failed `doctor` check |
| `2` | `plan`: a deploy would refuse |
| `3` | a terminal is not healthy (`status`, `verify`, after `deploy`, `start`, `restart`) |
