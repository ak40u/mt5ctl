"""status: one round trip per host, hosts queried at the same time."""

from __future__ import annotations

import asyncio

import pytest

from mt5ctl import operations, transport
from mt5ctl.errors import RemoteCommandError
from mt5ctl.models import Host, HostKind, Registry, Terminal
from mt5ctl.transport import CommandResult


def registry() -> Registry:
    hosts = {h: Host(h, HostKind.SSH, ssh=f"{h}.example.com") for h in ("a", "b", "c")}
    terminals = {
        f"{h}{n}": Terminal(f"{h}{n}", h, f"/data/{h}{n}", service=f"mt5-{h}{n}.service")
        for h in hosts
        for n in (1, 2)
    }
    return Registry(hosts=hosts, terminals=terminals)


def test_one_trip_per_host_all_hosts_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    running = 0
    peak = 0

    async def fake_run(host: Host, script: str, **_: object) -> CommandResult:
        nonlocal running, peak
        calls.append(host.id)
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        if host.id == "b":
            raise RemoteCommandError("b", 255, "ssh: connect to host b: timed out")
        lines = [
            f"TERM|{tid}|active|60|5|1||1"
            for tid in ("a1", "a2", "c1", "c2")
            if f"'{tid}'" in script
        ]
        return CommandResult(returncode=0, stdout="\n".join(lines) + "\n", stderr="")

    monkeypatch.setattr(transport, "run", fake_run)
    rows = asyncio.run(operations.status(registry()))
    assert sorted(calls) == ["a", "b", "c"]
    assert peak == 3
    by_id = {r.terminal: r for r in rows}
    assert [r.terminal for r in rows] == ["a1", "a2", "b1", "b2", "c1", "c2"]
    assert by_id["a1"].service_state == "active" and by_id["c2"].connected is True
    assert by_id["b1"].error and "timed out" in by_id["b1"].error
    assert by_id["b2"].service_state == "unknown"
