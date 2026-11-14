"""The assumptions in mt5spec, checked against files written by a real terminal.

The fixtures under tests/fixtures/{charts,profile,logs} named terminal-*,
order.wnd, journal.log and compile.log were saved by MetaTrader 5 build 6320
under Wine (home folder renamed, host details removed).
"""

from __future__ import annotations

from builders import CHARTS, FIXTURES
from mt5ctl import mt5spec
from mt5ctl.chart import ChartFile, Timeframe
from mt5ctl.text import TextFormat, decode, decode_lenient

JOURNAL = decode_lenient((FIXTURES / "logs/journal.log").read_bytes())


def test_terminal_chart_format_matches_the_default() -> None:
    for name in ("terminal-h1.chr", "terminal-d1.chr"):
        _, fmt = decode((CHARTS / name).read_bytes())
        assert fmt == TextFormat.terminal_default()


def test_period_codes_match_real_charts() -> None:
    for name, expected in (("terminal-h1.chr", "H1"), ("terminal-d1.chr", "D1")):
        chart = ChartFile.read(CHARTS / name)
        codes = (
            int(chart.chart.get("period_type") or -1),
            int(chart.chart.get("period_size") or -1),
        )
        assert mt5spec.PERIOD_CODES[expected] == codes
        assert chart.timeframe == Timeframe(expected)


def test_order_file_format() -> None:
    data = (FIXTURES / "profile/order.wnd").read_bytes()
    text, fmt = decode(data)
    assert (fmt.encoding, fmt.bom, fmt.newline, fmt.final_newline) == (
        "utf-16-le",
        True,
        "\r\n",
        True,
    )
    assert all(mt5spec.CHART_FILE_RE.fullmatch(n) for n in text.split())


def test_journal_start_lines() -> None:
    starts = mt5spec.LOG_TERMINAL_STARTED_RE.findall(JOURNAL)
    assert starts and all("build 6320" in s for s in starts)
    assert "\tTerminal\t" in JOURNAL  # tab-separated columns


def test_loaded_line_shape() -> None:
    # The real journal shows a script loading; an expert logs the same shape
    # under its own source column.
    script_line = next(ln for ln in JOURNAL.splitlines() if "loaded successfully" in ln)
    expert_line = script_line.replace("Scripts\tscript", "Experts\texpert")
    match = mt5spec.LOG_EXPERT_LOADED_RE.search(expert_line)
    assert match is not None
    assert (match["name"], match["symbol"], match["period"]) == (
        "ImportQdmTicks",
        "EURUSD",
        "H1",
    )
    assert mt5spec.LOG_EXPERT_LOADED_RE.search(script_line) is None


def test_compile_result_line() -> None:
    log = decode_lenient((FIXTURES / "logs/compile.log").read_bytes())
    match = mt5spec.COMPILE_RESULT_RE.search(log)
    assert match is not None and match.groups() == ("0", "0")
