"""info and logs: what the journal says, read the way the terminal writes it."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from conftest import Farm
from mt5ctl import operations
from mt5ctl.cli import EXIT_ERROR, EXIT_OK, main
from mt5ctl.errors import UsageError
from mt5ctl.server import build_server

# Lines as build 6320 writes them (names changed).
SESSION_1 = [
    "GL\t0\t09:00:00.100\tTerminal\tMetaTrader 5 x64 build 6310 started for MetaQuotes",
    "MF\t0\t09:00:00.110\tTerminal\tWindows 11 build 22000 on Wine 10.0 Linux 6.6, 4 x CPU",
    "LJ\t0\t09:00:00.111\tTerminal\tlaunched with Z:\\home\\trader\\mt5\\old.ini",
    "LJ\t0\t09:00:09.000\tNetwork\t'1001': authorized on Example-Demo through Access Point A",
]
SESSION_2 = [
    "GL\t0\t12:00:00.100\tTerminal\tMetaTrader 5 x64 build 6320 started for MetaQuotes",
    "MF\t0\t12:00:00.110\tTerminal\tWindows 11 build 22000 on Wine 11.20 Linux 6.6, 8 x CPU",
    "LJ\t0\t12:00:00.111\tTerminal\tlaunched with Z:\\home\\trader\\mt5\\cfg\\start.ini",
    "MM\t0\t12:00:09.000\tExperts\texpert Trend (EURUSD,H4) loaded successfully",
    "LJ\t0\t12:00:11.000\tNetwork\t'2002': authorized on Example-Live through Access Server B",
    "MK\t0\t12:00:11.300\tNetwork\t'2002': trading has been disabled - investor mode",
    "RJ\t0\t12:00:11.400\tNetwork\t'2002': terminal synchronized with Example Markets Ltd: "
    "3 positions, 1 orders, 837 symbols, 0 spreads",
    "QR\t2\t12:01:00.000\tTrades\tsomething went wrong",
]


def utf16(lines: list[str]) -> bytes:
    return b"\xff\xfe" + ("\r\n".join(lines) + "\r\n").encode("utf-16-le")


def test_journal_info_reads_the_latest_session() -> None:
    info = operations.journal_info("t", "\n".join(SESSION_1 + SESSION_2))
    assert info.build == 6320
    assert info.started == "12:00:00.100"
    assert info.system and info.system.startswith("Windows 11") and "Wine 11.20" in info.system
    assert info.start_config == "Z:\\home\\trader\\mt5\\cfg\\start.ini"
    assert (info.account, info.server, info.access_point) == (
        "2002",
        "Example-Live",
        "Access Server B",
    )
    assert info.ping_ms is None
    assert (info.company, info.positions, info.orders) == ("Example Markets Ltd", 3, 1)
    assert info.trading_disabled == "investor mode"


def test_journal_info_without_login_this_session() -> None:
    info = operations.journal_info("t", "\n".join(SESSION_1 + SESSION_2[:3]))
    assert info.build == 6320
    assert info.account is None and info.trading_disabled is None


def test_journal_info_takes_a_ping_if_written() -> None:
    line = "X\t0\t1\tNetwork\t'1': authorized on S-1 through Access Server C (ping: 12.5 ms)"
    info = operations.journal_info("t", line)
    assert (info.server, info.access_point, info.ping_ms) == ("S-1", "Access Server C", 12.5)


def test_info_reads_yesterdays_file_too(farm: Farm) -> None:
    """A terminal started before midnight logged in in yesterday's file."""
    logs = farm.data / "logs"
    logs.mkdir()
    (logs / "20261114.log").write_bytes(utf16(SESSION_2[:6]))
    (logs / "20261115.log").write_bytes(utf16(["A\t0\t00:00:01.000\tNetwork\tsomething"]))
    (logs / "metaeditor.log").write_bytes(utf16(["not the journal"]))
    (info,) = asyncio.run(operations.info(farm.registry))
    assert info.log_file == "20261115.log"
    assert (info.build, info.account, info.server) == (6320, "2002", "Example-Live")


def test_info_without_journal(farm: Farm) -> None:
    (info,) = asyncio.run(operations.info(farm.registry))
    assert info.error == "no journal yet"


def write_journal(farm: Farm, lines: list[str], sub: str = "logs") -> None:
    logs = farm.data / sub
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "20261115.log").write_bytes(utf16(lines))


def test_logs_tail_and_grep(farm: Farm) -> None:
    write_journal(farm, SESSION_1 + SESSION_2)
    result = asyncio.run(operations.logs(farm.registry, "demo1", lines=2))
    assert result.file == "20261115.log" and not result.partial
    assert [line.split("\t")[2] for line in result.lines] == ["12:00:11.400", "12:01:00.000"]
    result = asyncio.run(operations.logs(farm.registry, "demo1", pattern="AUTHORIZED on"))
    assert len(result.lines) == 2


def test_logs_of_the_experts(farm: Farm) -> None:
    write_journal(
        farm, ["A\t0\t12:00:00.000\tTrend (EURUSD,H4)\thello from OnInit"], "MQL5/logs"
    )
    result = asyncio.run(operations.logs(farm.registry, "demo1", source="experts"))
    assert result.lines and result.lines[0].endswith("hello from OnInit")
    assert asyncio.run(operations.logs(farm.registry, "demo1")).file is None


def test_logs_reads_only_the_end_of_a_big_file(
    farm: Farm, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_journal(farm, [f"A\t0\t12:00:{i:02d}.000\tTerminal\tline {i}" for i in range(60)])
    monkeypatch.setattr(operations, "LOGS_MAX_BYTES", 1001)
    result = asyncio.run(operations.logs(farm.registry, "demo1", lines=1000))
    assert result.partial and result.read_bytes == 1002
    assert result.lines[-1].endswith("line 59")
    # the first line read is cut, so it is dropped
    assert all(line.count("\t") == 4 for line in result.lines)


def test_logs_bad_pattern(farm: Farm) -> None:
    with pytest.raises(UsageError, match="bad pattern"):
        asyncio.run(operations.logs(farm.registry, "demo1", pattern="("))


def test_cli(farm: Farm, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_journal(farm, SESSION_2)
    config = tmp_path / "t.yaml"
    config.write_text(
        "hosts:\n  here: {kind: local}\n"
        f"terminals:\n  demo1:\n    host: here\n    data_dir: '{farm.data}'\n"
    )
    assert main(["logs", "demo1", "-n", "1", "--config", str(config)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "12:01:00.000  Trades  ERROR something went wrong" in out
    assert main(["info", "--json", "--config", str(config)]) == EXIT_OK
    (row,) = json.loads(capsys.readouterr().out)
    assert row["account"] == "2002" and row["company"] == "Example Markets Ltd"
    assert main(["info", "--config", str(config)]) == EXIT_OK
    assert "start file Z:\\home\\trader\\mt5\\cfg\\start.ini" in capsys.readouterr().out
    assert main(["logs", "demo1", "--grep", "(", "--config", str(config)]) == EXIT_ERROR


def test_mcp(farm: Farm, tmp_path: Path) -> None:
    write_journal(farm, SESSION_2)
    config = tmp_path / "t.yaml"
    config.write_text(
        "hosts:\n  here: {kind: local}\n"
        f"terminals:\n  demo1:\n    host: here\n    data_dir: '{farm.data}'\n"
    )
    server = build_server(str(config))
    out = json.dumps(asyncio.run(server.call_tool("mt5_info", {})), default=str)
    assert "Example-Live" in out
    out = json.dumps(
        asyncio.run(
            server.call_tool("mt5_logs", {"terminal": "demo1", "grep": "synchronized"})
        ),
        default=str,
    )
    assert "Example Markets Ltd" in out
