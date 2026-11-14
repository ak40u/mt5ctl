"""operations.plan over a remote host, with ssh replaced by local bash."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from builders import CHART1, EXPERT, chart_bytes, write_tree
from mt5ctl import operations, ssh
from mt5ctl.models import Host, HostKind, Registry, Terminal


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_remote_plan_runs_the_state_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def local_bash(
        host: Host, script: str, *, timeout: float = 60.0
    ) -> ssh.CommandResult:
        done = subprocess.run(["bash"], input=script, capture_output=True, text=True)
        return ssh.CommandResult(done.returncode, done.stdout, done.stderr)

    monkeypatch.setattr(ssh, "run", local_bash)
    bundle = write_tree(
        tmp_path / "b", charts={"chart01.chr": chart_bytes()}, experts={EXPERT: b"1"}
    )
    data = write_tree(tmp_path / "t", experts={EXPERT: b"1"})
    registry = Registry(
        hosts={"vps": Host("vps", HostKind.SSH, ssh="vps")},
        terminals={"t": Terminal("t", "vps", str(data))},
    )
    report = asyncio.run(operations.plan(registry, "t", bundle))
    assert report.plan.add == (CHART1,)
    assert report.plan.conflicts == (EXPERT,)  # present but not managed
