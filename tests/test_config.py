"""Registry loading and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from mt5ctl.config import load_registry, parse_registry, resolve_path
from mt5ctl.errors import ConfigError, UnknownTargetError
from mt5ctl.models import Env, HostKind, Registry, ServiceScope

EXAMPLE = Path(__file__).parents[1] / "examples" / "terminals.example.yaml"


def registry(hosts: dict[str, Any], terminals: dict[str, Any]) -> Registry:
    return parse_registry({"hosts": hosts, "terminals": terminals})


def test_example_registry_parses() -> None:
    reg = parse_registry(yaml.safe_load(EXAMPLE.read_text()))
    assert reg.terminals
    kinds = {reg.host_of(t).kind for t in reg.terminals.values()}
    assert kinds == {HostKind.LOCAL, HostKind.SSH, HostKind.WSL}


def test_lifecycle_fields() -> None:
    reg = registry(
        {"here": {}},
        {
            "t1": {
                "host": "here",
                "data_dir": "/d",
                "service": "mt5@demo1.service",
                "service_scope": "user",
                "wineprefix": "/home/t/.wine",
                "display": ":99",
            }
        },
    )
    term = reg.terminal("t1")
    assert (term.service, term.service_scope) == ("mt5@demo1.service", ServiceScope.USER)
    assert (term.wineprefix, term.display) == ("/home/t/.wine", ":99")


def test_defaults() -> None:
    reg = registry({"here": {}}, {"t1": {"host": "here", "data_dir": "/data"}})
    term = reg.terminal("t1")
    assert (term.profile, term.env) == ("Default", Env.DEMO)
    assert reg.host_of(term).kind is HostKind.LOCAL


def test_unknown_terminal_lists_known() -> None:
    reg = registry({"here": {}}, {"t1": {"host": "here", "data_dir": "/d"}})
    with pytest.raises(UnknownTargetError, match="known: t1"):
        reg.terminal("t2")


@pytest.mark.parametrize(
    ("hosts", "terminals", "message"),
    [
        ({"h": {}}, {}, "no terminals"),
        ({"h": {"kind": "ftp"}}, {"t": {"host": "h", "data_dir": "/d"}}, "invalid kind"),
        ({"h": {"kind": "ssh"}}, {"t": {"host": "h", "data_dir": "/d"}}, "no ssh destination"),
        (
            {"h": {"kind": "wsl", "ssh": "box"}},
            {"t": {"host": "h", "data_dir": "/d"}},
            "no wsl_distro",
        ),
        (
            {"h": {"kind": "ssh", "ssh": "-oProxyCommand=x"}},
            {"t": {"host": "h", "data_dir": "/d"}},
            "must be one word",
        ),
        (
            {"h": {"kind": "wsl", "ssh": "box", "wsl_distro": "Ubuntu; rm"}},
            {"t": {"host": "h", "data_dir": "/d"}},
            "invalid characters",
        ),
        ({"h": {}}, {"t": {"host": "other", "data_dir": "/d"}}, "unknown host"),
        ({"h": {}}, {"t": {"host": "h"}}, "data_dir"),
        ({"h": {}}, {"t": {"host": "h", "data_dir": "/d\n/e"}}, "line breaks"),
        ({"h": {}}, {"t": {"host": "h", "data_dir": "/d", "env": "prod"}}, "invalid env"),
        ({"h": {}}, {"t": {"host": "h", "data_dir": "/d", "profile": "../x"}}, "profile"),
        ({"h": {}}, {"bad id": {"host": "h", "data_dir": "/d"}}, "invalid"),
        ({"h": []}, {"t": {"host": "h", "data_dir": "/d"}}, "expected a mapping"),
        ({"h": {}}, {"t": {"host": "h", "data_dir": "/d", "service": "a b"}}, "unit name"),
        (
            {"h": {}},
            {"t": {"host": "h", "data_dir": "/d", "service_scope": "global"}},
            "service_scope",
        ),
        ({"h": {}}, {"t": {"host": "h", "data_dir": "/d", "display": "99"}}, "display"),
        (
            {"h": {}},
            {"t": {"host": "h", "data_dir": "/d", "service": "u", "start_command": "x"}},
            "not both",
        ),
        ({"h": {}}, {"t": {"host": "h", "data_dir": "/d", "start_command": "x"}}, "together"),
    ],
)
def test_invalid_registries(
    hosts: dict[str, Any], terminals: dict[str, Any], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        registry(hosts, terminals)


def test_resolve_path_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("MT5CTL_CONFIG", raising=False)
    with pytest.raises(ConfigError, match="no terminal registry"):
        resolve_path()
    local = tmp_path / "terminals.yaml"
    local.write_text("x")
    assert resolve_path() == local
    xdg = tmp_path / "xdg" / "mt5ctl" / "terminals.yaml"
    xdg.parent.mkdir(parents=True)
    xdg.write_text("x")
    assert resolve_path() == xdg
    monkeypatch.setenv("MT5CTL_CONFIG", str(tmp_path / "missing.yaml"))
    with pytest.raises(ConfigError, match="MT5CTL_CONFIG"):
        resolve_path()
    with pytest.raises(ConfigError, match="not found"):
        resolve_path(tmp_path / "also-missing.yaml")


@pytest.mark.parametrize(("text", "message"), [("", "empty"), ("a: [", "invalid YAML")])
def test_load_registry_file_errors(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "terminals.yaml"
    path.write_text(text)
    with pytest.raises(ConfigError, match=message):
        load_registry(path)
