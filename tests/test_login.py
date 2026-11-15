"""login: one headless login with a temporary start config that is shredded after."""

from __future__ import annotations

import asyncio
import io
import json
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import Farm
from mt5ctl import operations, scripts
from mt5ctl.cli import EXIT_ERROR, EXIT_OK, main
from mt5ctl.errors import CredentialError, LoginError
from mt5ctl.models import ServiceScope
from mt5ctl.results import LoginResult
from mt5ctl.server import build_server

SECRET = "s3cr3t-'$(touch pwned)`x`\"pw"


def run_login(farm: Farm, **kw: object) -> LoginResult:
    args: dict[str, object] = {
        "server": "Broker-Demo",
        "account": "1001",
        "password": SECRET,
        "verify_timeout": 1,
    }
    args |= kw
    return asyncio.run(operations.login(farm.registry, "demo1", wait_s=10, **args))  # type: ignore[arg-type]


def leftovers(farm: Farm) -> list[Path]:
    return sorted((farm.data / ".mt5ctl").glob("login*"))


def test_login_ok(farm: Farm) -> None:
    farm.journal_on_start()
    result = run_login(farm)
    assert result.ok and result.restarted
    assert result.verify is not None and result.verify.healthy, result.verify
    assert (result.account, result.server) == ("1001", "Broker-Demo")
    calls = farm.calls
    assert calls[0] == "stop mt5-demo1.service"
    assert calls[-1] == "start mt5-demo1.service"
    terminal = next(c for c in calls if c.startswith("terminal "))
    assert "/portable" in terminal and "/config:.mt5ctl\\login." in terminal
    assert SECRET not in "\n".join(calls)
    assert any(c.startswith("taskkill") for c in calls)
    # what the terminal was given: CRLF, the login, and KeepPrivate
    seen = (farm.systemd / "login-seen.ini").read_bytes().decode()
    assert seen.splitlines() and all(line.endswith("\r") for line in seen.split("\n")[:-1])
    lines = seen.replace("\r", "").splitlines()
    assert lines[:4] == ["[Common]", "Login=1001", f"Password={SECRET}", "Server=Broker-Demo"]
    assert "KeepPrivate=1" in lines
    assert leftovers(farm) == []
    assert not (farm.data / "pwned").exists()


def test_login_refused_by_broker(farm: Farm) -> None:
    (farm.systemd / "login-refuse").touch()
    result = run_login(farm)
    assert result.outcome == "failed" and not result.ok
    assert "authorization on Broker-Demo failed" in result.detail
    assert result.restarted and result.verify is None
    assert leftovers(farm) == []


def test_login_ok_but_not_connected_after_restart(farm: Farm) -> None:
    """A start config without the account: the terminal waits at its login dialog."""
    farm.journal_on_start(authorized=False)
    result = run_login(farm)
    assert result.ok
    assert result.verify is not None and not result.verify.healthy
    from mt5ctl.formatting import login_lines

    assert "Login=<account> and Server=<server>" in "\n".join(login_lines(result))


def test_login_timeout_still_cleans_up_and_starts(farm: Farm) -> None:
    (farm.systemd / "login-silent").touch()
    result = asyncio.run(
        operations.login(
            farm.registry,
            "demo1",
            server="Broker-Demo",
            account="1001",
            password=SECRET,
            wait_s=2,
        )
    )
    assert result.outcome == "timeout"
    assert result.restarted and farm.calls[-1] == "start mt5-demo1.service"
    assert leftovers(farm) == []


def test_bad_values_never_stop_the_terminal(farm: Farm) -> None:
    with pytest.raises(LoginError, match="server"):
        run_login(farm, server="Broker Demo;rm")
    with pytest.raises(LoginError, match="login number"):
        run_login(farm, account="10a")
    with pytest.raises(LoginError, match="one non-empty line"):
        run_login(farm, password="a\nb")
    assert farm.calls == []


def test_account_from_registry(farm: Farm) -> None:
    term = replace(farm.registry.terminals["demo1"], account="2002")
    farm.registry.terminals["demo1"] = term
    result = run_login(farm, account=None)
    assert result.ok and result.account == "2002"


def test_no_account_anywhere(farm: Farm) -> None:
    with pytest.raises(LoginError, match="no account"):
        run_login(farm, account=None)
    assert farm.calls == []


def test_no_password_anywhere(
    farm: Farm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MT5CTL_CREDENTIALS", str(tmp_path / "none.json"))
    with pytest.raises(CredentialError, match="MT5CTL_PASSWORD_1001"):
        run_login(farm, password=None)
    assert farm.calls == []


def test_password_from_env(farm: Farm, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MT5CTL_PASSWORD_1001", SECRET)
    assert run_login(farm, password=None).ok
    seen = (farm.systemd / "login-seen.ini").read_text()
    assert f"Password={SECRET}" in seen


def test_secrets_file_must_be_private(
    farm: Farm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"1001": SECRET}))
    creds.chmod(0o644)
    monkeypatch.setenv("MT5CTL_CREDENTIALS", str(creds))
    with pytest.raises(CredentialError, match="chmod 600"):
        run_login(farm, password=None)
    creds.chmod(0o600)
    assert run_login(farm, password=None).ok


def test_requires_a_supervisor(farm: Farm) -> None:
    term = replace(farm.registry.terminals["demo1"], service=None)
    farm.registry.terminals["demo1"] = term
    with pytest.raises(Exception, match="login"):
        run_login(farm)
    assert farm.calls == []


def test_script_has_no_password_outside_the_config() -> None:
    script = scripts.build_login_script(
        "/data",
        account="1001",
        server="Broker-Demo",
        password=SECRET,
        wine="wine",
        wineprefix=None,
        service="mt5.service",
        scope=ServiceScope.USER,
        wait_s=5,
    )
    # once, inside the quoted heredoc that writes the config
    assert script.count(SECRET) == 1
    head, _, tail = script.partition("<<'MT5CTL_LOGIN_CONFIG'\n")
    assert SECRET not in head
    assert tail.index(SECRET) < tail.index("\nMT5CTL_LOGIN_CONFIG\n")
    with pytest.raises(ValueError):
        scripts.build_login_script(
            "/d",
            account="1",
            server="S",
            password="MT5CTL_LOGIN_CONFIG",
            wine="wine",
            wineprefix=None,
            service=None,
            scope=ServiceScope.USER,
            wait_s=1,
        )


def test_cli_password_stdin(
    farm: Farm,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "t.yaml"
    config.write_text(
        "hosts:\n  here: {kind: local}\n"
        f"terminals:\n  demo1:\n    host: here\n    data_dir: '{farm.data}'\n"
        "    service: mt5-demo1.service\n    service_scope: user\n    account: '1001'\n"
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET + "\n"))
    code = main(
        [
            "login",
            "demo1",
            "--server",
            "Broker-Demo",
            "--password-stdin",
            "--verify-timeout",
            "1",
            "--config",
            str(config),
        ]
    )
    out = capsys.readouterr()
    assert code == EXIT_OK, out.err
    assert "logged in" in out.out and "started again" in out.out
    assert SECRET not in out.out + out.err
    (farm.systemd / "login-refuse").touch()
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET + "\n"))
    code = main(
        [
            "login",
            "demo1",
            "--server",
            "Broker-Demo",
            "--password-stdin",
            "--verify-timeout",
            "1",
            "--json",
            "--config",
            str(config),
        ]
    )
    out = capsys.readouterr()
    assert code == EXIT_ERROR
    assert json.loads(out.out)["outcome"] == "failed"
    assert SECRET not in out.out + out.err


def test_mcp_login(farm: Farm, tmp_path: Path) -> None:
    config = tmp_path / "t.yaml"
    config.write_text(
        "hosts:\n  here: {kind: local}\n"
        f"terminals:\n  demo1:\n    host: here\n    data_dir: '{farm.data}'\n"
        "    service: mt5-demo1.service\n    service_scope: user\n"
    )
    server = build_server(str(config))
    out = asyncio.run(
        server.call_tool(
            "mt5_login",
            {
                "terminal": "demo1",
                "server": "Broker-Demo",
                "account": "1001",
                "password": SECRET,
                "verify_timeout": 1,
            },
        )
    )
    text = json.dumps(out, default=str)
    assert "logged in" in text
    assert SECRET not in text


def test_mcp_refused_login_is_a_failed_call(farm: Farm, tmp_path: Path) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    (farm.systemd / "login-refuse").touch()
    config = tmp_path / "t.yaml"
    config.write_text(
        "hosts:\n  here: {kind: local}\n"
        f"terminals:\n  demo1:\n    host: here\n    data_dir: '{farm.data}'\n"
        "    service: mt5-demo1.service\n    service_scope: user\n"
    )
    args = {
        "terminal": "demo1",
        "server": "Broker-Demo",
        "account": "1001",
        "password": SECRET,
    }
    with pytest.raises(ToolError, match="REFUSED") as err:
        asyncio.run(build_server(str(config)).call_tool("mt5_login", args))
    assert SECRET not in str(err.value)
