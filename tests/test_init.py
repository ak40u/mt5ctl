"""init: a private starter registry that loads as written."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mt5ctl.cli import EXIT_ERROR, EXIT_OK, main
from mt5ctl.config import STARTER_REGISTRY, load_registry
from mt5ctl.models import Env, ServiceScope


def test_init_writes_a_private_registry_that_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("MT5CTL_CONFIG", raising=False)
    assert main(["init"]) == EXIT_OK
    target = tmp_path / "xdg" / "mt5ctl" / "terminals.yaml"
    assert str(target) in capsys.readouterr().out
    assert target.read_text() == STARTER_REGISTRY
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600
    registry = load_registry()
    term = registry.terminal("demo1")
    assert (term.service_scope, term.env, term.host) == (ServiceScope.SYSTEM, Env.DEMO, "vps")


def test_init_never_overwrites(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "mine.yaml"
    target.write_text("hosts: {}\n")
    assert main(["init", str(target)]) == EXIT_ERROR
    assert "already exists" in capsys.readouterr().err
    assert target.read_text() == "hosts: {}\n"


def test_example_registry_loads() -> None:
    example = Path(__file__).parent.parent / "examples" / "terminals.example.yaml"
    assert load_registry(example).terminals
