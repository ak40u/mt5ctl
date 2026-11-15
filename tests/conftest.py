"""A local "farm": one terminal data folder managed by a fake systemd."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from mt5ctl.models import Env, Host, Registry, ServiceScope, Terminal

FAKES = Path(__file__).parent / "fakes"
UNIT = "mt5-demo1.service"


@dataclass
class Farm:
    data: Path
    systemd: Path
    registry: Registry

    def hook(self, event: str, script: str) -> None:
        """Run *script* (bash) when the unit stops or starts: event is on-stop/on-start."""
        path = self.systemd / f"{UNIT}.{event}"
        path.write_text(f"#!/usr/bin/env bash\nset -e\nDATA={_q(self.data)}\n{script}\n")
        path.chmod(0o755)

    def journal_on_start(self, *experts: str, authorized: bool = True) -> None:
        """On start, append UTF-16LE journal lines like the terminal's."""
        lines = [
            "0\t12:00:00.000\tTerminal\tMetaTrader 5 x64 build 6320 started for MetaQuotes"
        ]
        if authorized:
            lines.append("0\t12:00:01.000\tNetwork\t'1001': authorized on Broker-Demo")
        lines += [
            f"0\t12:00:02.000\tExperts\texpert {e} (EURUSD,H4) loaded successfully"
            for e in experts
        ]
        blob = ("\r\n".join(lines) + "\r\n").encode("utf-16-le")
        (self.systemd / "journal.bin").write_bytes(blob)
        journal = _q(self.systemd / "journal.bin")
        self.hook(
            "on-start", f'mkdir -p "$DATA/logs"; cat {journal} >> "$DATA/logs/20261115.log"'
        )

    def set_state(self, state: str) -> None:
        (self.systemd / f"{UNIT}.state").write_text(state + "\n")

    def flag(self, name: str) -> None:
        (self.systemd / f"{UNIT}.{name}").write_text("")

    @property
    def calls(self) -> list[str]:
        path = self.systemd / "calls"
        return path.read_text().splitlines() if path.exists() else []


def _q(path: Path | str) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


def make_registry(data: Path, env: Env = Env.DEMO, service: str | None = UNIT) -> Registry:
    return Registry(
        hosts={"here": Host("here")},
        terminals={
            "demo1": Terminal(
                "demo1",
                "here",
                str(data),
                env=env,
                service=service,
                service_scope=ServiceScope.USER,
            )
        },
    )


@pytest.fixture
def farm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Farm:
    data = tmp_path / "terminal data"
    data.mkdir()
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    monkeypatch.setenv("FAKE_SYSTEMD", str(systemd))
    monkeypatch.setenv("PATH", f"{FAKES}{os.pathsep}{os.environ['PATH']}")
    farm = Farm(data=data, systemd=systemd, registry=make_registry(data))
    farm.set_state("active")
    return farm
