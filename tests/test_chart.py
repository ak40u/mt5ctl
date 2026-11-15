"""The chart parser/writer: decoding, lossless round trip, typed view, edits."""

from __future__ import annotations

from pathlib import Path

import pytest

from builders import CHARTS, chart_bytes, chart_text
from mt5ctl.chart import (
    ChartFile,
    Entry,
    Expert,
    Section,
    Text,
    Timeframe,
    decode,
    parse_nodes,
)
from mt5ctl.errors import ChartFormatError

FIXTURE_FILES = sorted(CHARTS.glob("*.chr"))


# -- decoding ---------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("data", "encoding", "bom"),
    [
        (b"\xff\xfe" + "<chart>\r\n".encode("utf-16-le"), "utf-16-le", True),
        (b"\xfe\xff" + "<chart>\r\n".encode("utf-16-be"), "utf-16-be", True),
        ("<chart>\r\n".encode("utf-16-le"), "utf-16-le", False),
        (b"\xef\xbb\xbf<chart>\n", "utf-8", True),
        ("<chart>\nname=Größe\n".encode(), "utf-8", False),
        ("<chart>\nname=Größe\n".encode("cp1252"), "cp1252", False),
    ],
)
def test_decode_detects_encoding(data: bytes, encoding: str, bom: bool) -> None:
    text, fmt = decode(data)
    assert text.startswith("<chart>")
    assert (fmt.encoding, fmt.bom) == (encoding, bom)


def test_decode_detects_newline_and_final_newline() -> None:
    _, crlf = decode(b"<chart>\r\n</chart>\r\n")
    _, lf = decode(b"<chart>\n</chart>")
    assert (crlf.newline, crlf.final_newline) == ("\r\n", True)
    assert (lf.newline, lf.final_newline) == ("\n", False)


def test_parse_rejects_broken_utf16() -> None:
    with pytest.raises(ChartFormatError, match="utf-16-le"):
        ChartFile.parse(b"\xff\xfe<\x00c")  # odd byte count after the BOM


# -- round trip -------------------------------------------------------------- #


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.name)
def test_fixture_round_trips_byte_exact(path: Path) -> None:
    data = path.read_bytes()
    assert ChartFile.parse(data).to_bytes() == data


def test_round_trip_keeps_missing_final_newline_and_odd_lines() -> None:
    data = b"<chart>\nsymbol=EURUSD\n  indented line\n\n</chart>"
    assert ChartFile.parse(data).to_bytes() == data


def test_fixture_formats() -> None:
    utf16 = ChartFile.read(CHARTS / "expert-utf16.chr").format
    utf8 = ChartFile.read(CHARTS / "expert-utf8-lf.chr").format
    assert (utf16.encoding, utf16.bom, utf16.newline) == ("utf-16-le", True, "\r\n")
    assert (utf8.encoding, utf8.bom, utf8.newline) == ("utf-8", False, "\n")


# -- structure --------------------------------------------------------------- #


def test_parse_nodes_builds_tree() -> None:
    nodes = parse_nodes("<chart>\na=1\n\n<expert>\nname=x=y\n</expert>\n</chart>\n")
    assert len(nodes) == 1
    chart = nodes[0]
    assert isinstance(chart, Section)
    assert chart.children[0] == Entry("a", "1")
    assert chart.children[1] == Text("")
    expert = chart.children[2]
    assert isinstance(expert, Section)
    assert expert.get("name") == "x=y"  # split on the first '=' only


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("<chart>\n</expert>\n</chart>\n", "does not close"),
        ("<chart>\n<expert>\n", "unclosed section <expert>"),
        ("</chart>\n", "no open section"),
    ],
)
def test_unbalanced_sections_are_rejected(text: str, message: str) -> None:
    with pytest.raises(ChartFormatError, match=message):
        parse_nodes(text)


@pytest.mark.parametrize("text", ["a=1\n", "<chart>\n</chart>\n<chart>\n</chart>\n"])
def test_exactly_one_chart_section_required(text: str) -> None:
    with pytest.raises(ChartFormatError, match="exactly one <chart>"):
        ChartFile.parse(text.encode())


def test_read_names_the_file_in_errors(tmp_path: Path) -> None:
    bad = tmp_path / "chart01.chr"
    bad.write_bytes(b"<chart>\n")
    with pytest.raises(ChartFormatError, match=r"chart01\.chr"):
        ChartFile.read(bad)


# -- typed view -------------------------------------------------------------- #


def test_fixture_expert_view() -> None:
    chart = ChartFile.read(CHARTS / "expert-utf16.chr")
    assert chart.symbol == "EURUSD"
    assert chart.timeframe == Timeframe("H4")
    expert = chart.expert
    assert expert is not None
    assert expert.name == "Trend EURUSD H4"
    assert expert.path == r"Experts\Portfolio\Trend EURUSD H4.ex5"
    assert expert.mode == 1
    assert expert.input("MagicNumber") == "1001"
    assert expert.input("CustomComment") == "Trend EURUSD H4 = v2"
    assert expert.input("Note") == "Größe €"
    assert expert.input("InpAtrPeriod") is None  # an indicator input, not the expert's
    assert expert.input("Missing") is None


def test_chart_without_expert() -> None:
    chart = ChartFile.read(CHARTS / "terminal-h1.chr")
    assert chart.expert is None
    assert chart.remove_expert() is False


def test_unparsable_mode_and_period_are_none() -> None:
    text = chart_text().replace("expertmode=1", "expertmode=x").replace("period_size=4", "")
    chart = ChartFile.parse(text.encode())
    assert chart.expert is not None and chart.expert.mode is None
    assert chart.timeframe is None


def test_invalid_timeframe_combination_reads_as_none() -> None:
    chart = ChartFile.parse(chart_bytes(period=(1, 5)))  # there is no H5
    assert chart.timeframe is None


# -- timeframes -------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "codes"),
    [
        ("M1", (0, 1)),
        ("M15", (0, 15)),
        ("m30", (0, 30)),
        ("H1", (1, 1)),
        ("h4", (1, 4)),
        ("D1", (1, 24)),
        ("W1", (2, 1)),
        ("mn1", (3, 1)),
    ],
)
def test_timeframe_codes(name: str, codes: tuple[int, int]) -> None:
    tf = Timeframe.parse(name)
    assert tf.codes == codes
    assert str(tf) == name.upper()
    assert Timeframe.from_codes(*codes) == tf


def test_timeframes_of_real_terminal_charts() -> None:
    for name, tf in (
        ("terminal-h1.chr", "H1"),
        ("terminal-d1.chr", "D1"),
        ("terminal-w1.chr", "W1"),
        ("terminal-mn1.chr", "MN1"),
    ):
        assert str(ChartFile.read(CHARTS / name).timeframe) == tf


@pytest.mark.parametrize("codes", [(2, 2), (3, 6), (4, 1), (1, 5), (0, 7)])
def test_unobserved_codes_read_as_none(codes: tuple[int, int]) -> None:
    assert Timeframe.from_codes(*codes) is None


@pytest.mark.parametrize("name", ["H5", "M7", "D2", "W2", "MN3", "X1", "", "4H"])
def test_timeframe_rejects_unknown(name: str) -> None:
    with pytest.raises(ValueError):
        Timeframe.parse(name)


# -- edits ------------------------------------------------------------------- #


def test_set_input_changes_only_that_line() -> None:
    data = (CHARTS / "expert-utf16.chr").read_bytes()
    chart = ChartFile.parse(data)
    chart.set_input("RiskPerTrade", "50.0")
    edited = chart.to_bytes()
    assert edited.decode("utf-16") == data.decode("utf-16").replace(
        "RiskPerTrade=100.0", "RiskPerTrade=50.0"
    )
    assert edited.startswith(b"\xff\xfe")


def test_set_input_unknown_key_raises() -> None:
    chart = ChartFile.parse(chart_bytes())
    with pytest.raises(KeyError):
        chart.set_input("NoSuchInput", "1")


def test_set_symbol_and_timeframe() -> None:
    chart = ChartFile.parse(chart_bytes())
    chart.symbol = "USDJPY"
    chart.timeframe = Timeframe.parse("D1")
    again = ChartFile.parse(chart.to_bytes())
    assert again.symbol == "USDJPY"
    assert str(again.timeframe) == "D1"


def test_set_expert_replaces_in_place_and_keeps_unknown_keys() -> None:
    text = chart_text().replace("expertmode=1", "expertmode=1\r\ncustom_key=kept")
    chart = ChartFile.parse(text.encode())
    chart.set_expert(
        Expert(
            name="Breakout",
            path=r"Experts\Portfolio\Breakout.ex5",
            mode=1,
            inputs=(("Lots", "0.10"), ("Comment", "a=b $x `y` 'z' \"q\"")),
        )
    )
    again = ChartFile.parse(chart.to_bytes())
    expert = again.expert
    assert expert is not None
    assert (expert.name, expert.mode) == ("Breakout", 1)
    assert expert.inputs == (("Lots", "0.10"), ("Comment", "a=b $x `y` 'z' \"q\""))
    section = again.chart.section("expert")
    assert section is not None
    assert section.get("custom_key") == "kept"
    assert len(again.chart.sections("expert")) == 1


def test_set_expert_without_mode_keeps_existing_mode() -> None:
    chart = ChartFile.parse(chart_bytes())
    chart.set_expert(Expert(name="X", path=r"Experts\X.ex5"))
    assert chart.expert is not None and chart.expert.mode == 1
    assert chart.expert.inputs == ()


def test_set_expert_inserts_before_first_window() -> None:
    chart = ChartFile.read(CHARTS / "terminal-h1.chr")
    chart.set_expert(Expert(name="X", path=r"Experts\X.ex5", mode=1, inputs=(("A", "1"),)))
    tags = [c.tag for c in chart.chart.children if isinstance(c, Section)]
    assert tags[0] == "expert"
    assert tags[1:] == ["window"]
    again = ChartFile.parse(chart.to_bytes())
    assert again.expert == Expert(
        name="X", path=r"Experts\X.ex5", mode=1, inputs=(("A", "1"),)
    )


def test_new_expert_block_is_followed_by_a_blank_line() -> None:
    chart = ChartFile.read(CHARTS / "terminal-h1.chr")
    chart.set_expert(Expert(name="X", path=r"Experts\X.ex5", mode=1))
    text = chart.to_bytes().decode("utf-16")
    assert "</expert>\r\n\r\n<window>" in text


def test_remove_expert() -> None:
    chart = ChartFile.parse(chart_bytes())
    assert chart.remove_expert() is True
    assert ChartFile.parse(chart.to_bytes()).expert is None


@pytest.mark.parametrize(
    "expert",
    [
        Expert(name="X", path=r"Experts\X.ex5", inputs=(("A", "1\r\n</inputs>"),)),
        Expert(name="X\n<chart>", path=r"Experts\X.ex5"),
        Expert(name="X", path=r"Experts\X.ex5", inputs=(("A=B", "1"),)),
        Expert(name="X", path=r"Experts\X.ex5", inputs=(("<inputs>", "1"),)),
    ],
)
def test_set_expert_refuses_values_that_would_break_the_structure(expert: Expert) -> None:
    chart = ChartFile.parse(chart_bytes())
    with pytest.raises(ValueError):
        chart.set_expert(expert)


def test_section_set_appends_after_last_entry() -> None:
    chart = ChartFile.parse(chart_bytes())
    chart.chart.set("new_key", "1")
    children = chart.chart.children
    index = children.index(Entry("new_key", "1"))
    assert all(not isinstance(c, Section) for c in children[:index])


def test_unencodable_text_is_reported() -> None:
    chart = ChartFile.parse(b"<chart>\nsymbol=EURUSD\n</chart>\n".replace(b"\n", b"\r\n"))
    chart.format = type(chart.format)("cp1252", False, "\r\n", True)
    chart.symbol = "日本"
    with pytest.raises(ChartFormatError, match="cp1252"):
        chart.to_bytes()


# -- fingerprint ------------------------------------------------------------- #


def test_fingerprint_ignores_display_and_encoding() -> None:
    base = ChartFile.parse(chart_bytes())
    scrolled = ChartFile.parse(chart_bytes(scroll=0))
    as_utf8 = ChartFile.parse(chart_text().encode())
    assert base.fingerprint() == scrolled.fingerprint() == as_utf8.fingerprint()


@pytest.mark.parametrize(
    "change",
    [
        {"symbol": "GBPUSD"},
        {"period": (1, 1)},
        {"expert_path": r"Experts\Portfolio\Other.ex5"},
        {"inputs": (("MagicNumber", "1002"),)},
        {"inputs": ()},
        {"mode": 0},
        {"expert_path": None},
    ],
)
def test_fingerprint_tracks_what_trades(change: dict[str, object]) -> None:
    base = ChartFile.parse(chart_bytes())
    changed = ChartFile.parse(chart_bytes(**change))
    assert base.fingerprint() != changed.fingerprint()


def test_mixed_line_endings_are_refused_not_rewritten() -> None:
    """Everything parse accepts is written back byte for byte; this could not be."""
    data = b"<chart>\r\nsymbol=EURUSD\nperiod_type=1\r\nperiod_size=4\r\n</chart>"
    with pytest.raises(ChartFormatError, match="mixed line endings"):
        ChartFile.parse(data)
    uniform = data.replace(b"EURUSD\n", b"EURUSD\r\n")
    assert ChartFile.parse(uniform).to_bytes() == uniform
