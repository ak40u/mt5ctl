"""doctor: read-only checks of hosts, terminals and connections."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import Farm
from mt5ctl import operations, scripts
from mt5ctl.cli import EXIT_ERROR, EXIT_OK, main
from mt5ctl.models import Host, HostKind, ServiceScope
from mt5ctl.results import Check
from mt5ctl.server import build_server


def by_label(checks: list[Check]) -> dict[str, Check]:
    return {c.label: c for c in checks}


@pytest.fixture
def ready(farm: Farm) -> Farm:
    (farm.data / "MQL5").mkdir()
    (farm.data / "terminal64.exe").write_bytes(b"MZ")
    farm.journal_on_start()
    (farm.data / "logs").mkdir()
    (farm.data / "logs" / "20261115.log").write_bytes(b"\xff\xfe")
    return farm


def test_ready_terminal(ready: Farm) -> None:
    checks = by_label(asyncio.run(operations.doctor(ready.registry)))
    assert checks["host here"].status == "ok"
    assert checks["terminal demo1"].status == "ok", checks["terminal demo1"]
    assert checks["registry"].detail == "1 terminals on 1 hosts"
    assert checks["broker connections"].status == "ok"
    # read-only: nothing was started or stopped
    assert ready.calls == []


def test_missing_unit_and_folders(ready: Farm) -> None:
    (ready.systemd / "mt5-demo1.service.missing").write_text("")
    term = replace(ready.registry.terminals["demo1"], data_dir=str(ready.data / "nope"))
    ready.registry.terminals["demo1"] = term
    check = by_label(asyncio.run(operations.doctor(ready.registry)))["terminal demo1"]
    assert check.status == "fail"
    assert "unit mt5-demo1.service not found" in check.detail
    assert "no data folder" in check.detail


def test_wine_and_prefix(ready: Farm, tmp_path: Path) -> None:
    term = replace(
        ready.registry.terminals["demo1"],
        wine=str(tmp_path / "no-wine"),
        wineprefix=str(tmp_path / "no-prefix"),
    )
    ready.registry.terminals["demo1"] = term
    check = by_label(asyncio.run(operations.doctor(ready.registry)))["terminal demo1"]
    assert check.status == "fail"
    assert "wine" in check.detail and "no Wine prefix" in check.detail


def test_not_a_portable_install_is_a_warning(ready: Farm) -> None:
    (ready.data / "terminal64.exe").unlink()
    check = by_label(asyncio.run(operations.doctor(ready.registry)))["terminal demo1"]
    assert check.status == "warn" and "terminal64.exe" in check.detail


def test_not_connected_is_a_warning(ready: Farm) -> None:
    ready.journal_on_start(authorized=False)
    (ready.data / "logs" / "20261115.log").write_bytes(
        "\r\n".join(
            [
                "0\t12:00:00.000\tTerminal\tMetaTrader 5 build 6320 started for MetaQuotes",
                "0\t12:00:05.000\tNetwork\t'1': connection to Broker-Demo lost",
            ]
        ).encode("utf-16-le")
    )
    check = by_label(asyncio.run(operations.doctor(ready.registry)))["broker connections"]
    assert check.status == "warn" and "demo1" in check.detail


def test_unreachable_host(ready: Farm) -> None:
    ready.registry.hosts["here"] = Host("here", HostKind.SSH, ssh="nobody@host.invalid")
    checks = by_label(asyncio.run(operations.doctor(ready.registry)))
    assert checks["host here"].status == "fail"
    assert "broker connections" not in checks


def test_credentials_file_permissions(
    ready: Farm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    creds.chmod(0o644)
    monkeypatch.setenv("MT5CTL_CREDENTIALS", str(creds))
    check = by_label(asyncio.run(operations.doctor(ready.registry)))["credentials file"]
    assert check.status == "fail" and "chmod 600" in check.detail


def test_system_units_need_a_sudo_rule() -> None:
    script = scripts.build_doctor_script(
        [("t", "/d", "systemd", ServiceScope.SYSTEM, "mt5-t.service", "", "")]
    )
    for action in ("start", "stop", "restart"):
        assert f'sudo -n -l systemctl {action} "$svc"' in script


def test_cli_and_mcp(ready: Farm, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "t.yaml"
    config.write_text(
        "hosts:\n  here: {kind: local}\n"
        f"terminals:\n  demo1:\n    host: here\n    data_dir: '{ready.data}'\n"
        "    service: mt5-demo1.service\n    service_scope: user\n"
    )
    assert main(["doctor", "--config", str(config)]) == EXIT_OK
    assert "terminal demo1" in capsys.readouterr().out
    out = json.dumps(
        asyncio.run(build_server(str(config)).call_tool("mt5_doctor", {})), default=str
    )
    assert "terminal demo1" in out
    (ready.systemd / "mt5-demo1.service.missing").write_text("")
    assert main(["doctor", "--json", "--config", str(config)]) == EXIT_ERROR
    rows = {r["label"]: r["status"] for r in json.loads(capsys.readouterr().out)}
    assert rows["terminal demo1"] == "fail"
