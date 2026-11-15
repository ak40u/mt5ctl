"""The assumptions in mt5spec, checked against files written by a real terminal.

The fixtures under tests/fixtures/{charts,profile,logs} named terminal-*,
order.wnd, journal.log and compile.log were saved by MetaTrader 5 build 6320
under Wine (home folder renamed, host details removed).
"""

from __future__ import annotations

from builders import CHARTS, FIXTURES
from mt5ctl import mt5spec, operations
from mt5ctl.chart import ChartFile, Timeframe
from mt5ctl.models import Env
from mt5ctl.results import TerminalStatus
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


# -- broker journal (a real account logged in; login and server renamed) -- #


INVESTOR = decode_lenient((FIXTURES / "logs/journal-investor.log").read_bytes())
RECONNECT = decode_lenient((FIXTURES / "logs/journal-reconnect.log").read_bytes())


def until(text: str, marker: str) -> str:
    """The journal up to and including the first line containing *marker*."""
    lines = text.splitlines()
    end = next(i for i, ln in enumerate(lines) if marker in ln)
    return "\n".join(lines[: end + 1])


def test_authorization_lines() -> None:
    assert len(mt5spec.LOG_AUTHORIZED_RE.findall(RECONNECT)) == 2  # login, then reconnect
    assert operations.journal_connection(until(INVESTOR, "synchronized"))[0] is True


def test_lost_connection_then_reconnect() -> None:
    assert operations.journal_connection(until(RECONNECT, "lost"))[0] is False
    state, event = operations.journal_connection(until(RECONNECT, "Access Server"))
    assert state is True and event is not None and "Access Server" in event


def test_normal_exit_logs_a_disconnect() -> None:
    assert operations.journal_connection(RECONNECT)[0] is False


def test_investor_mode_means_trading_disabled() -> None:
    assert operations.journal_trading_disabled(until(INVESTOR, "authorized")) is None
    assert operations.journal_trading_disabled(INVESTOR) == "investor mode"
    st = TerminalStatus("t", "h", Env.DEMO, "active", connected=True)
    report = operations.evaluate_verify(st, INVESTOR, {"MACD Sample"})
    assert report.loaded == ("MACD Sample",)
    assert not report.healthy
    assert "TRADING DISABLED (investor mode)" in report.detail


def test_level_column() -> None:
    lost = next(ln for ln in RECONNECT.splitlines() if "lost" in ln)
    assert lost.split("\t")[1] == "1"  # warning


def test_failed_expert_lines() -> None:
    """Real lines: a damaged .ex5 and an expert whose OnInit returned INIT_FAILED."""
    failed = {m["name"] for m in mt5spec.LOG_EXPERT_FAILED_RE.finditer(JOURNAL)}
    assert failed == {"ProbeTruncated", "ProbeInitFailed"}
    st = TerminalStatus("t", "h", Env.DEMO, "active", connected=True)
    expected = {"ProbeOpenCharts", "ProbeInitFailed", "ProbeTruncated", "ProbeCorrupt"}
    report = operations.evaluate_verify(st, JOURNAL, expected)
    assert report.loaded == ("ProbeOpenCharts",)
    assert report.failed == ("ProbeInitFailed", "ProbeTruncated")
    assert report.pending == ("ProbeCorrupt",)  # random bytes: nothing is logged


def test_algo_trading_toggle_lines() -> None:
    """Real lines from switching the terminal's Algo Trading button off and on."""
    assert operations.journal_algo_trading(JOURNAL) is True  # last: enabled
    off = until(JOURNAL, "automated trading is disabled")
    assert operations.journal_algo_trading(off) is False
    st = TerminalStatus("t", "h", Env.DEMO, "active", connected=True)
    report = operations.evaluate_verify(st, off, set())
    assert report.algo_trading is False and not report.healthy
    assert "ALGO TRADING OFF" in report.detail


def test_algo_trading_unknown_without_a_toggle() -> None:
    assert operations.journal_algo_trading("MetaTrader 5 x64 build 6320 started\n") is None
