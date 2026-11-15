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


# -- charts with an expert, attached in the terminal and saved on exit ------ #


ON = CHARTS / "terminal-expert-algo-on.chr"
OFF = CHARTS / "terminal-expert-algo-off.chr"


def test_real_expert_block() -> None:
    chart = ChartFile.read(ON)
    expert = chart.expert
    assert expert is not None
    assert (expert.name, expert.path, expert.mode) == (
        "MACD Sample",
        r"Experts\Examples\MACD\MACD Sample.ex5",
        1,
    )
    assert expert.input("InpLots") == "0.1" and len(expert.inputs) == 6
    # The expert block sits before the first window, where set_expert puts it.
    tags = [c.tag for c in chart.chart.children if hasattr(c, "tag")]
    assert tags.index("expert") < tags.index("window")


def test_algo_trading_bit() -> None:
    on, off = ChartFile.read(ON), ChartFile.read(OFF)
    assert on.expert is not None and on.expert.algo_trading is True
    assert off.expert is not None and off.expert.algo_trading is False
    # Unticking "Allow Algo Trading" changes expertmode and nothing else.
    differing = [
        (a, b)
        for a, b in zip(
            on.to_bytes().decode("utf-16").splitlines(),
            off.to_bytes().decode("utf-16").splitlines(),
            strict=True,
        )
        if a != b
    ]
    assert differing == [("expertmode=1", "expertmode=0")]
    assert on.fingerprint() != off.fingerprint()


def test_real_expert_chart_round_trips_and_edits() -> None:
    data = ON.read_bytes()
    chart = ChartFile.parse(data)
    assert chart.to_bytes() == data
    chart.set_input("InpLots", "0.2")
    edited = chart.to_bytes().decode("utf-16")
    assert edited == data.decode("utf-16").replace("InpLots=0.1", "InpLots=0.2")


def test_expert_log_name_matches_the_journal() -> None:
    name = mt5spec.expert_log_name(r"MQL5/Experts/Examples/MACD/MACD Sample.ex5")
    loaded = {m["name"] for m in mt5spec.LOG_EXPERT_LOADED_RE.finditer(JOURNAL)}
    assert name in loaded
