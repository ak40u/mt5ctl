"""process mode: a terminal run by mt5ctl itself, without systemd."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from builders import CHART1, EXPERT, chart_bytes, write_tree
from conftest import FAKES
from mt5ctl import operations
from mt5ctl.models import Host, Registry, Terminal


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    data = tmp_path / "terminal"
    data.mkdir()
    return Registry(
        hosts={"here": Host("here")},
        terminals={
            "t1": Terminal(
                "t1",
                "here",
                str(data),
                start_command=f"FAKE_EXPERTS=Trend '{FAKES}/fake-terminal'",
                stop_command="touch .close",
            )
        },
    )


def data_dir(registry: Registry) -> Path:
    return Path(registry.terminals["t1"].data_dir)


def stop(registry: Registry) -> operations.StopResult:
    return asyncio.run(operations.stop(registry, "t1"))


def test_start_status_stop(registry: Registry) -> None:
    started = asyncio.run(operations.start(registry, "t1", verify_timeout=5.0))
    try:
        assert started.verify.started is True and started.verify.connected is True
        (st,) = asyncio.run(operations.status(registry))
        assert st.service_state == "active"
        assert (data_dir(registry) / ".mt5ctl/terminal.pid").exists()
    finally:
        result = stop(registry)
    assert result.clean and result.exit_code == 0 and result.was_running
    assert not (data_dir(registry) / ".mt5ctl/terminal.pid").exists()
    (st,) = asyncio.run(operations.status(registry))
    assert st.service_state == "inactive"


def test_stop_when_not_running(registry: Registry) -> None:
    result = stop(registry)
    assert not result.was_running and result.clean


def test_a_terminal_that_ignores_the_close_request_is_ended(
    registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(operations, "PROCESS_STOP_WAIT_S", 2)
    asyncio.run(operations.start(registry, "t1", verify_timeout=5.0))
    (data_dir(registry) / ".hang").write_text("")
    result = stop(registry)
    assert result.unit_result == "timeout" and not result.clean
    (st,) = asyncio.run(operations.status(registry))
    assert st.service_state == "inactive"


def test_deploy_without_systemd(registry: Registry, tmp_path: Path) -> None:
    bundle = write_tree(
        tmp_path / "b", charts={"chart01.chr": chart_bytes()}, experts={EXPERT: b"v1"}
    )
    asyncio.run(operations.start(registry, "t1", verify_timeout=5.0))
    try:
        result = asyncio.run(
            operations.deploy(registry, "t1", bundle, verify_timeout=5.0, verify_interval=0.2)
        )
        assert result.plan.add == (EXPERT, CHART1)
        assert result.stopped_cleanly is True and result.restarted
        assert result.verify is not None and result.verify.healthy, result.verify
        assert (data_dir(registry) / EXPERT).read_bytes() == b"v1"
    finally:
        stop(registry)
