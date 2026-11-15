"""What mt5ctl assumes about MetaTrader 5 behaviour that MetaQuotes does not document.

The chart-file format, the terminal's log lines and MetaEditor's command-line
output are not published specifications. Every such assumption mt5ctl relies on
is collected in this one module, each with where it comes from, so a terminal
build that behaves differently is handled by changing this file alone.

Sources referred to below:

* *MQL5 Book* — the "Charts / templates" chapter of the MQL5 programming book
  on mql5.com, which shows the chart/template file syntax;
* *Help* — the official MetaTrader 5 and MetaEditor user guides;
* *MT4 heritage* — behaviour of the MetaTrader 4 terminal, from which the
  MetaTrader 5 file layout descends.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

# --------------------------------------------------------------------------- #
# Chart files
# --------------------------------------------------------------------------- #

# Encoding the terminal writes chart files in, used for charts mt5ctl creates
# from scratch. Existing files always keep their own encoding.
# Observed (build 6320): UTF-16LE with BOM, CRLF, no line break after </chart>.
CHART_ENCODING = "utf-16-le"
CHART_BOM = True
CHART_NEWLINE = "\r\n"
CHART_FINAL_NEWLINE = False

# How a chart stores its timeframe: (period_type, period_size). The unit is
# minutes (0), hours (1; a day is 24 hours), weeks (2) or months (3).
# Observed (build 6320): M30 = (0, 30), H1 = (1, 1), H4 = (1, 4), D1 = (1, 24),
# W1 = (2, 1), MN1 = (3, 1). The other minute and hour timeframes follow the
# same scheme.
PERIOD_CODES: dict[str, tuple[int, int]] = {
    **{f"M{n}": (0, n) for n in (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30)},
    **{f"H{n}": (1, n) for n in (1, 2, 3, 4, 6, 8, 12)},
    "D1": (1, 24),
    "W1": (2, 1),
    "MN1": (3, 1),
}

# A chart's expert: an <expert> block directly in <chart>, before the first
# <window>, with name=, path= (relative to MQL5, backslashes), expertmode= and
# an <inputs> block of name=value lines. Observed (build 6320, expert attached
# in the terminal and saved on a normal exit). Indicator windows carry their own
# expertmode=0 lines, unrelated to the expert.
#
# expertmode bit 0 is the expert's "Allow Algo Trading" checkbox: unticking it
# changed expertmode=1 to 0 and nothing else. Observed (build 6320). Other bits
# (DLL imports, ...) have not been observed.
EXPERT_MODE_ALGO_TRADING = 0x1

# Chart files are named chart01.chr, chart02.chr, ... in a profile folder.
# Observed (build 6320).
CHART_FILE_RE = re.compile(r"chart(\d+)\.chr")

# Each profile folder also holds order.wnd: the chart file names, one per line,
# in window order (UTF-16LE with BOM, CRLF, final CRLF). Observed (build 6320):
# the terminal opens a chart file that order.wnd does not list, and rewrites
# order.wnd when it saves the profile. Deploys still keep it in step (removed
# charts dropped, added ones listed, other entries kept), so the file never
# lists a chart that no longer exists.
PROFILE_ORDER_FILE = "order.wnd"

# The terminal closes gaps in chart numbering when it saves a profile.
# Observed (build 6320): with chart05 and chart06 missing, chart07.chr was
# saved as chart05.chr. A managed chart would move to a file name the manifest
# does not know, so a deploy that would leave gaps is refused.
# Also observed: a chart id that two files share is replaced by max(id) + 1.
CHARTS_CONTIGUOUS = True

# --------------------------------------------------------------------------- #
# Terminal process and journal
# --------------------------------------------------------------------------- #

# The terminal's executable, as it appears in the process's command line. Its
# /proc/<pid>/comm is not reliable: under Wine 11 it is the name of the main
# thread, "main". Observed (build 6320, Wine 11.20): command line
# "Z:\\...\\terminal\\terminal64.exe /portable ...", comm "main"; broker
# sockets are owned by that pid (and by wineserver).
TERMINAL_EXECUTABLES = ("terminal64.exe",)

# Exit codes are not documented. Observed (build 6320, Wine 11): `taskkill
# /IM terminal64.exe` without /F makes the terminal close itself and save its
# profile, journal line "exit with code 0" -- provided taskkill runs with the
# terminal's DISPLAY and XAUTHORITY, and no modal window is open.

# TCP port broker trade servers listen on; an established connection to it from
# the terminal process counts as "connected".
BROKER_PORT = 443

# The journal: one file per day in <data dir>/logs (Help: "Journal"), named
# YYYYMMDD.log, UTF-16LE with BOM; lines are tab-separated:
# <code> <severity> <time> <source> <message>. Observed (build 6320).
# The same folder also holds other logs (MetaEditor writes metaeditor.log
# there, observed), so only dated files are the journal.
JOURNAL_DIR = "logs"
JOURNAL_GLOB = "[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9].log"
# The experts' own log (what Print() writes, the terminal's "Experts" tab): the
# same dated UTF-16LE files with a BOM, in MQL5/Logs. Observed under Wine
# (build 6320): the folder is created as "MQL5/logs", so both are looked in.
EXPERTS_LOG_DIRS = ("MQL5/Logs", "MQL5/logs")

# Journal lines (matched case-insensitively, anywhere in the line).
# Observed (build 6320, with and without a broker account):
#   Terminal  MetaTrader 5 x64 build 6320 started for MetaQuotes Ltd.
#   Experts   expert MACD Sample (EURUSD,H1) loaded successfully   (and "... removed")
#   Network   '<login>': authorized on <server>
#   Network   '<login>': authorized on <server> through Access Server ... (ping: ...)
#   Network   '<login>': connection to <server> lost
#   Network   '<login>': disconnected from <server>      (on a normal exit)
#   Network   '<login>': trading has been disabled - investor mode
# The second column is the level: 0 information, 1 warning, 2 error. The
# journal reaches the disk some seconds after the events (observed: about 13 s
# after start), so a freshly started terminal may have no journal yet.
# An expert that cannot be loaded or initialised (observed, build 6320):
#   Experts   loading of <name> (<symbol>,<period>) failed [533]       (damaged .ex5)
#   Experts   expert <name> (...) loaded successfully, then
#   Experts   initializing of <name> (...) failed with code 1         (OnInit failed)
#   Experts   expert <name> (...) removed
# A file of random bytes named .ex5 logs nothing at all; it simply never loads.
# The terminal then saves that chart without its <expert> block.
# The terminal's own exit line: it closed itself (and saved its profile).
# Observed (build 6320): "Terminal  exit with code 0", then "stopped with 0" and
# "shutdown with 0". A terminal ended by a signal writes none of them. systemd's
# Result for the unit is no guide: it reads "exit-code" even after a clean exit.
LOG_TERMINAL_EXIT_RE = re.compile(r"exit with code (-?\d+)", re.IGNORECASE)

JOURNAL_LEVEL_COLUMN = 1
JOURNAL_LEVEL_ERROR = "2"
LOG_TERMINAL_STARTED_RE = re.compile(r"MetaTrader 5 .*build \d+ started", re.IGNORECASE)
LOG_AUTHORIZED_RE = re.compile(r"authorized on ([^ ]+)", re.IGNORECASE)
# A rejected login. MT4 heritage ("authorization on <server> failed"); the
# MetaTrader 5 wording has not been observed.
# These two are also run by grep -E on the host, so they stay POSIX ERE.
LOG_AUTH_FAILED_RE = re.compile(
    r"authorization on [^ ]+ failed|invalid account", re.IGNORECASE
)
# Logging in without a password on disk. Observed (build 6320): once the
# terminal has logged in with KeepPrivate=1 (the password saved in
# Config/accounts.dat), a start config naming the account and server, without
# a password, logs it in by itself. Started with /login:<account>, or with no
# config at all, it opens the login dialog instead and waits there.
START_CONFIG_LOGIN = "[Common]\r\nLogin={account}\r\nServer={server}\r\n"
# The start file a terminal was launched with is read for one value only, the
# [Experts] Enabled key (Algo Trading at start); the rest of it, which may hold
# a password, never leaves the host. Its logged path is a Windows path: drive
# Z: is the host's root (Wine's default), drive C: the prefix's drive_c.
START_FILE_EXPERTS_SECTION = "experts"
START_FILE_ALGO_KEY = "enabled"

# What `info` reads from the journal. Observed (build 6320):
#   Terminal  MetaTrader 5 x64 build 6320 started for MetaQuotes Ltd.
#   Terminal  Windows 11 build 22000 on Wine 11.20 Linux 6.6..., 64 x <cpu>, ...
#   Terminal  launched with <the /config: start file, as a Windows path>
#   Network   '<login>': authorized on <server> through <access point>
#   Network   '<login>': terminal synchronized with <company>: 0 positions, 0 orders, ...
# No ping is written with the authorization (MetaTrader 4 wrote one); the
# pattern takes one if a build adds it.
LOG_BUILD_RE = re.compile(r"MetaTrader 5 .*?build (\d+) started", re.IGNORECASE)
LOG_SYSTEM_RE = re.compile(r"\t(Windows .* on Wine [^,]+)", re.IGNORECASE)
LOG_LAUNCHED_WITH_RE = re.compile(r"launched with (.+?)\s*$", re.IGNORECASE)
LOG_AUTHORIZED_DETAIL_RE = re.compile(
    r"'(?P<account>[^']+)': authorized on (?P<server>\S+)"
    r"(?: through (?P<access>.+?))?(?: \(ping: (?P<ping>[0-9.]+) ?ms\))?\s*$",
    re.IGNORECASE,
)
LOG_SYNCHRONIZED_RE = re.compile(
    r"terminal synchronized with (?P<company>.+?): (?P<positions>\d+) positions?, "
    r"(?P<orders>\d+) orders?",
    re.IGNORECASE,
)
LOG_CONNECTION_LOST_RE = re.compile(
    r"connection to .* lost|no connection|disconnected from", re.IGNORECASE
)
# The terminal's "Algo Trading" button. Observed (build 6320): toggling it logs
#   Experts   automated trading is disabled   /   automated trading is enabled
# and nothing is logged at start. Its state is kept in Config/settings.ini,
# which is binary and not readable. `[Experts] Enabled=1` in the /config: start
# file switches it on at every start, overriding a manual "off".
LOG_ALGO_TRADING_RE = re.compile(r"automated trading is (enabled|disabled)", re.IGNORECASE)

# The account cannot trade (investor password, or trading disabled by the
# broker); experts load but every order is refused. The reason follows the dash.
LOG_TRADING_DISABLED_RE = re.compile(
    r"trading has been disabled(?:\s*-\s*(?P<reason>.+))?", re.IGNORECASE
)
LOG_EXPERT_LOADED_RE = re.compile(
    r"expert (?P<name>.+?) \((?P<symbol>[^,()]+),(?P<period>[^)]+)\) loaded successfully",
    re.IGNORECASE,
)
LOG_EXPERT_FAILED_RE = re.compile(
    r"(?:loading|initializing) of (?P<name>.+?) "
    r"\((?P<symbol>[^,()]*),?(?P<period>[^)]*)\) failed",
    re.IGNORECASE,
)
LOG_EXPERT_REMOVED_RE = re.compile(
    r"expert (?P<name>.+?) \((?P<symbol>[^,()]+),(?P<period>[^)]+)\) removed", re.IGNORECASE
)


def expert_log_name(relpath: str) -> str:
    """The name the journal uses for an expert: its file name without extension.

    Observed (build 6320): ``Experts\\Examples\\MACD\\MACD Sample.ex5`` logs as
    ``expert MACD Sample (EURUSD,H1) loaded successfully``.
    """
    return PurePosixPath(relpath).stem


# --------------------------------------------------------------------------- #
# MetaEditor
# --------------------------------------------------------------------------- #

# Matched case-insensitively: installations ship it as MetaEditor64.exe.
METAEDITOR_EXE = "metaeditor64.exe"

# Final summary line of a compile log, written by /log:<file> as UTF-16LE.
# Observed (build 6320): "Result: 0 errors, 0 warnings, 780 ms elapsed, cpu='X64 Regular'".
COMPILE_RESULT_RE = re.compile(
    r"result:\s*(\d+)\s+errors?,\s*(\d+)\s+warnings?", re.IGNORECASE
)

# MetaEditor silently skips a /compile: folder whose path contains a space: no
# log, no .ex5. File names with spaces inside a space-free folder compile fine.
# Observed (build 6320, Wine 11.20).
COMPILE_PATH_NO_SPACES = True

# MetaEditor's exit code is not documented (and unreliable under Wine), so a
# compile succeeds only if every source has a fresh .ex5 and no log reports errors.
EX5_SUFFIX = ".ex5"
SOURCE_SUFFIX = ".mq5"
