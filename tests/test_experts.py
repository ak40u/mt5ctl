"""ea-list and autotrading: what each chart runs and whether it can trade."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from builders import chart_bytes, write_tree
from conftest import Farm
from mt5ctl import operations
from mt5ctl.cli import EXIT_OK, main
from mt5ctl.server import build_server

SECRET = "pw-never-shown"


def windows(path: Path) -> str:
    return "Z:" + str(path).replace("/", "\\")


@pytest.fixture
def desk(farm: Farm) -> Farm:
    write_tree(
        farm.data,
        charts={
            "chart01.chr": chart_bytes(),
            "chart02.chr": chart_bytes(
                symbol="GBPUSD", period=(1, 1), expert_path=r"Experts\Other.ex5", mode=0
            ),
            "chart03.chr": chart_bytes(symbol="USDJPY", expert_path=None),
        },
    )
    cfg = farm.data / "cfg"
    cfg.mkdir()
    text = f"[Common]\r\nLogin=1001\r\nPassword={SECRET}\r\n[Experts]\r\nEnabled=1\r\n"
    (cfg / "start.ini").write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    journal(farm, [])
    return farm


def journal(farm: Farm, extra: list[str]) -> None:
    lines = [
        "A\t0\t12:00:00.000\tTerminal\tMetaTrader 5 x64 build 6320 started for MetaQuotes",
        "A\t0\t12:00:00.010\tTerminal\tlaunched with "
        + windows(farm.data / "cfg" / "start.ini"),
        "A\t0\t12:00:05.000\tExperts\texpert Trend (EURUSD,H4) loaded successfully",
        "A\t0\t12:00:05.100\tExperts\tloading of Other (GBPUSD,H1) failed [533]",
        "A\t0\t12:00:09.000\tNetwork\t'1001': authorized on Broker-Demo",
        *extra,
    ]
    logs = farm.data / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "20261115.log").write_bytes(
        b"\xff\xfe" + ("\r\n".join(lines) + "\r\n").encode("utf-16-le")
    )


def test_charts_experts_and_states(desk: Farm) -> None:
    (report,) = asyncio.run(operations.experts(desk.registry))
    assert report.running is True and report.error is None
    assert (report.algo_trading, report.algo_source) == (
        True,
        "start file: [Experts] Enabled=1",
    )
    rows = {c.chart: c for c in report.charts}
    assert (rows["chart01.chr"].expert, rows["chart01.chr"].state) == ("Trend", "loaded")
    assert rows["chart01.chr"].allowed is True
    assert (rows["chart02.chr"].state, rows["chart02.chr"].allowed) == ("failed", False)
    assert rows["chart03.chr"].expert is None and rows["chart03.chr"].state == "-"
    assert [c.chart for c in report.can_trade] == ["chart01.chr"]
    assert desk.calls == []


def test_button_switched_in_the_session_wins(desk: Farm) -> None:
    journal(desk, ["A\t0\t12:05:00.000\tExperts\tautomated trading is disabled"])
    (report,) = asyncio.run(operations.experts(desk.registry))
    assert report.algo_trading is False and "switched" in report.algo_source
    assert report.can_trade == ()


def test_expert_removed_later(desk: Farm) -> None:
    journal(desk, ["A\t0\t12:06:00.000\tExperts\texpert Trend (EURUSD,H4) removed"])
    (report,) = asyncio.run(operations.experts(desk.registry))
    assert report.charts[0].state == "removed"


def test_stopped_terminal(desk: Farm) -> None:
    journal(desk, ["A\t0\t12:05:00.000\tExperts\tautomated trading is disabled"])
    desk.set_state("inactive")
    (report,) = asyncio.run(operations.experts(desk.registry))
    assert report.running is False
    # the next start decides: the start file
    assert report.algo_trading is True
    assert {c.state for c in report.charts} == {"-"}


def test_start_file_without_the_setting_or_missing(desk: Farm) -> None:
    (desk.data / "cfg" / "start.ini").write_text("[Common]\r\nLogin=1001\r\n")
    (report,) = asyncio.run(operations.experts(desk.registry))
    assert report.algo_trading is None and "keeps its last state" in report.algo_source
    (desk.data / "cfg" / "start.ini").unlink()
    (report,) = asyncio.run(operations.experts(desk.registry))
    assert report.algo_trading is None and "not found" in report.algo_source


@pytest.mark.parametrize(
    ("logged", "prefix", "expected"),
    [
        ("Z:\\home\\t\\cfg\\a b.ini", None, "/home/t/cfg/a b.ini"),
        ("c:\\mt5\\start.ini", "/p", "/p/drive_c/mt5/start.ini"),
        ("C:\\mt5\\start.ini", None, None),
        ("D:\\x.ini", "/p", None),
        ("cfg\\start.ini", "/p", None),
    ],
)
def test_host_path(logged: str, prefix: str | None, expected: str | None) -> None:
    assert operations.host_path(logged, prefix) == expected


def test_cli_and_mcp_never_show_the_start_file(
    desk: Farm, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "t.yaml"
    config.write_text(
        "hosts:\n  here: {kind: local}\n"
        f"terminals:\n  demo1:\n    host: here\n    data_dir: '{desk.data}'\n"
        "    service: mt5-demo1.service\n    service_scope: user\n"
    )
    assert main(["ea-list", "--config", str(config)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "chart01.chr  EURUSD H4      Trend  [loaded]" in out
    assert "ALGO TRADING NOT ALLOWED" in out
    assert main(["autotrading", "--config", str(config)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "1 of 2 experts can trade" in out and "Other has its own Algo Trading off" in out
    assert main(["ea-list", "--json", "--config", str(config)]) == EXIT_OK
    out += capsys.readouterr().out
    server = build_server(str(config))
    for tool in ("mt5_ea_list", "mt5_autotrading"):
        out += json.dumps(asyncio.run(server.call_tool(tool, {})), default=str)
    assert SECRET not in out
