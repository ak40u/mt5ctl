"""Interruptions: what must not be cut short finishes, whatever interrupts it."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from builders import EXPERT, chart_bytes, write_tree
from conftest import Farm
from mt5ctl import operations, transport
from mt5ctl.cli import EXIT_INTERRUPTED, main
from mt5ctl.errors import RemoteCommandError
from mt5ctl.manifest import MANIFEST_RELPATH
from mt5ctl.models import Host

BREAKOUT = "MQL5/Experts/Portfolio/Breakout.ex5"


def bundles(tmp_path: Path) -> tuple[Path, Path]:
    v1 = write_tree(
        tmp_path / "v1", charts={"chart01.chr": chart_bytes()}, experts={EXPERT: b"v1"}
    )
    v2 = write_tree(
        tmp_path / "v2",
        charts={
            "chart01.chr": chart_bytes(),
            "chart02.chr": chart_bytes(expert_path=r"Experts\Portfolio\Breakout.ex5"),
        },
        experts={EXPERT: b"v2", BREAKOUT: b"b1"},
    )
    return v1, v2


def slow_apply(monkeypatch: pytest.MonkeyPatch, started: Path) -> None:
    """The apply pauses after its first move (half applied) for a second."""
    real = operations.scripts.build_apply_script

    def slow(*args: Any, **kwargs: Any) -> str:
        move = 'mv -f "$STG/$f" "$DIR/$f"'
        script = real(*args, **kwargs).replace(move, f"{move}; touch '{started}'; sleep 2", 1)
        return script + f"\ntouch '{started}.done'\n"

    monkeypatch.setattr(operations.scripts, "build_apply_script", slow)


def applied_completely(farm: Farm, started: Path) -> bool:
    """The apply finished, and finished before the terminal was started."""
    done = Path(f"{started}.done")
    manifest = (farm.data / MANIFEST_RELPATH).read_text()
    state = farm.systemd / "mt5-demo1.service.state"
    return (
        done.exists()
        and done.stat().st_mtime <= state.stat().st_mtime
        and (farm.data / BREAKOUT).exists()
        and BREAKOUT in manifest
    )


def test_timeout_ends_the_whole_command(tmp_path: Path) -> None:
    late = tmp_path / "LATE"
    with pytest.raises(RemoteCommandError, match="timed out"):
        asyncio.run(transport.run(Host("here"), f"sleep 1.5; touch '{late}'", timeout=0.5))
    time.sleep(2)
    assert not late.exists()


def test_native_cancel_lets_the_apply_finish(
    farm: Farm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """asyncio.run cancels the task outright on Ctrl-C; the apply still completes
    before the terminal is started, and the lock is released."""
    v1, v2 = bundles(tmp_path)
    farm.journal_on_start("Trend")
    asyncio.run(operations.deploy(farm.registry, "demo1", v1, verify_timeout=0))
    started = tmp_path / "moving"
    slow_apply(monkeypatch, started)

    async def run() -> None:
        task = asyncio.ensure_future(
            operations.deploy(farm.registry, "demo1", v2, verify_timeout=0)
        )
        while not started.exists():
            await asyncio.sleep(0.05)
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())
    assert applied_completely(farm, started)
    assert farm.calls[-2:] == ["stop mt5-demo1.service", "start mt5-demo1.service"]
    assert not os.path.lexists(farm.data / ".mt5ctl/lock")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_ctrl_c_in_the_cli_lets_the_apply_finish(
    farm: Farm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v1, v2 = bundles(tmp_path)
    farm.journal_on_start("Trend")
    config = tmp_path / "t.yaml"
    config.write_text(
        "hosts:\n  here: {kind: local}\n"
        f"terminals:\n  demo1:\n    host: here\n    data_dir: '{farm.data}'\n"
        "    service: mt5-demo1.service\n    service_scope: user\n"
    )
    assert main(
        ["deploy", "demo1", str(v1), "--verify-timeout", "0", "--config", str(config)]
    ) in (0, 3)
    started = tmp_path / "moving"
    slow_apply(monkeypatch, started)

    def interrupt() -> None:
        while not started.exists():
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.2)
        os.kill(os.getpid(), signal.SIGINT)  # a second Ctrl-C changes nothing

    threading.Thread(target=interrupt, daemon=True).start()
    code = main(["deploy", "demo1", str(v2), "--config", str(config)])
    assert code == EXIT_INTERRUPTED
    assert applied_completely(farm, started)
    assert farm.calls[-1] == "start mt5-demo1.service"
    assert not os.path.lexists(farm.data / ".mt5ctl/lock")


def test_start_after_a_deploy_survives_a_dropped_connection(
    farm: Farm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(operations, "START_RETRY_DELAYS_S", (0.1, 0.1))
    v1, _ = bundles(tmp_path)
    farm.journal_on_start("Trend")
    farm.flag("drop-start")
    result = asyncio.run(operations.deploy(farm.registry, "demo1", v1, verify_timeout=0))
    assert result.restarted
    assert (farm.systemd / "mt5-demo1.service.state").read_text().strip() == "active"
