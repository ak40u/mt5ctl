"""What verify reads: the right start, however long ago, and only after it."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from builders import chart_bytes, write_tree
from conftest import Farm
from mt5ctl import operations, scripts


def utf16(lines: list[str]) -> bytes:
    return b"\xff\xfe" + ("\r\n".join(lines) + "\r\n").encode("utf-16-le")


START = "A\t0\t09:00:00.000\tTerminal\tMetaTrader 5 x64 build 6320 started for MetaQuotes"
AUTH = "A\t0\t09:00:05.000\tNetwork\t'1': authorized on Broker-Demo"
LOADED = "A\t0\t09:00:06.000\tExperts\texpert Trend (EURUSD,H4) loaded successfully"


@pytest.fixture
def running(farm: Farm) -> Farm:
    write_tree(farm.data, charts={"chart01.chr": chart_bytes()})
    return farm


def test_start_of_a_running_terminal_starts_nothing(running: Farm) -> None:
    logs = running.data / "logs"
    logs.mkdir()
    (logs / "20261115.log").write_bytes(utf16([START, AUTH, LOADED]))
    result = asyncio.run(operations.start(running.registry, "demo1", verify_timeout=0))
    assert result.already_running
    assert result.verify.healthy, result.verify.detail
    assert running.calls == []


def test_verify_finds_a_start_days_back(running: Farm) -> None:
    logs = running.data / "logs"
    logs.mkdir()
    (logs / "20261110.log").write_bytes(utf16([START, AUTH, LOADED]))
    for day in ("20261111", "20261112", "20261113", "20261114", "20261115"):
        (logs / f"{day}.log").write_bytes(utf16([f"A\t0\t00:00:01.000\tNetwork\t{day} ping"]))
    report = asyncio.run(operations.verify(running.registry, "demo1"))
    assert report.healthy, report.detail
    assert report.loaded == ("Trend (EURUSD,H4)",)


def test_unreadable_journal_end_is_never_healthy(
    running: Farm, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without knowing where the journal ended, an old start could pass for the new one."""
    running.journal_on_start("Trend")
    logs = running.data / "logs"
    logs.mkdir()
    (logs / "20261115.log").write_bytes(utf16([START, AUTH, LOADED]))
    monkeypatch.setattr(scripts, "build_log_cursor_script", lambda data_dir: "exit 1")
    monkeypatch.setattr(operations.asyncio, "sleep", _no_sleep)
    result = asyncio.run(operations.restart(running.registry, "demo1", verify_timeout=0))
    assert not result.verify.healthy
    assert result.verify.note == operations.CURSOR_UNKNOWN
    assert running.calls == ["restart mt5-demo1.service"]


async def _no_sleep(_: float) -> None:
    return None


def test_cursor_read_once_more_after_a_failure(
    running: Farm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    running.journal_on_start("Trend")
    real = scripts.build_log_cursor_script
    tries = tmp_path / "tries"

    def flaky(data_dir: str) -> str:
        return f"echo x >> '{tries}'; [ $(wc -l < '{tries}') -gt 1 ] || exit 1\n" + real(
            data_dir
        )

    monkeypatch.setattr(scripts, "build_log_cursor_script", flaky)
    monkeypatch.setattr(operations.asyncio, "sleep", _no_sleep)
    result = asyncio.run(operations.restart(running.registry, "demo1", verify_timeout=1))
    assert result.verify.note is None and result.verify.healthy
