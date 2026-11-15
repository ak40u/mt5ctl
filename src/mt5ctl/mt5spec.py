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
# minutes (0) or hours (1); a day is 24 hours.
# Observed (build 6320): M30 = (0, 30), H1 = (1, 1), H4 = (1, 4), D1 = (1, 24).
# The other minute and hour timeframes follow the same scheme. W1 and MN1 have
# not been observed, so mt5ctl neither reads nor writes them.
PERIOD_CODES: dict[str, tuple[int, int]] = {
    **{f"M{n}": (0, n) for n in (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30)},
    **{f"H{n}": (1, n) for n in (1, 2, 3, 4, 6, 8, 12)},
    "D1": (1, 24),
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

# Process name of the terminal under Wine, as /proc/<pid>/comm shows it.
TERMINAL_PROCESS_NAMES = ("terminal64.exe",)

# Exit codes are not documented. Observed (build 6320, Wine 11): `taskkill
# /IM terminal64.exe` without /F makes the terminal close itself and save its
# profile, journal line "exit with code 0".

# TCP port broker trade servers listen on; an established connection to it from
# the terminal process counts as "connected".
BROKER_PORT = 443

# The journal: one file per day in <data dir>/logs (Help: "Journal"), named
# YYYYMMDD.log, UTF-16LE with BOM; lines are tab-separated:
# <code> <severity> <time> <source> <message>. Observed (build 6320).
JOURNAL_DIR = "logs"
JOURNAL_GLOB = "*.log"

# Journal lines (matched case-insensitively, anywhere in the line).
# Observed (build 6320, with and without a broker account):
#   Terminal  MetaTrader 5 x64 build 6320 started for MetaQuotes Ltd.
#   Experts   expert MACD Sample (EURUSD,H1) loaded successfully   (and "... removed")
#   Network   '<login>': authorized on <server>
#   Network   '<login>': authorized on <server> through Access Server ... (ping: ...)
#   Network   '<login>': connection to <server> lost
#   Network   '<login>': disconnected from <server>      (on a normal exit)
#   Network   '<login>': trading has been disabled - investor mode
# The second column is the level: 0 information, 1 warning, 2 error.
# The failed-load line is MT4 heritage and has not been observed.
LOG_TERMINAL_STARTED_RE = re.compile(r"MetaTrader 5 .*build \d+ started", re.IGNORECASE)
LOG_AUTHORIZED_RE = re.compile(r"authorized on (\S+)", re.IGNORECASE)
LOG_CONNECTION_LOST_RE = re.compile(
    r"connection to .* lost|no connection|disconnected from", re.IGNORECASE
)
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
    r"expert (?P<name>.+?) \([^)]*\).*(?:failed|cannot load|initializing.*error)",
    re.IGNORECASE,
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

# MetaEditor's exit code is not documented (and unreliable under Wine), so a
# compile succeeds only if every source has a fresh .ex5 and no log reports errors.
EX5_SUFFIX = ".ex5"
SOURCE_SUFFIX = ".mq5"
