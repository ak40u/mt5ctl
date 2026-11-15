"""screenshot: the terminal's display as PNG, display found in the registry or the unit."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import Farm
from mt5ctl import operations
from mt5ctl.cli import EXIT_ERROR, EXIT_OK, main
from mt5ctl.errors import UsageError
from mt5ctl.server import build_server


def test_display_from_the_unit(farm: Farm) -> None:
    (farm.systemd / "mt5-demo1.service.env").write_text(
        "WINEPREFIX=/p DISPLAY=:91 XAUTHORITY=/home/t/.Xauthority\n"
    )
    shot = asyncio.run(operations.screenshot(farm.registry, "demo1"))
    assert shot.png.startswith(operations.PNG_MAGIC)
    assert farm.calls == ["import DISPLAY=:91 XAUTHORITY=/home/t/.Xauthority"]


def test_registry_wins_over_the_unit(farm: Farm) -> None:
    (farm.systemd / "mt5-demo1.service.env").write_text("DISPLAY=:91 XAUTHORITY=/u\n")
    term = replace(farm.registry.terminals["demo1"], display=":5", xauthority="/r")
    farm.registry.terminals["demo1"] = term
    asyncio.run(operations.screenshot(farm.registry, "demo1"))
    assert farm.calls == ["import DISPLAY=:5 XAUTHORITY=/r"]


def test_no_display_anywhere(farm: Farm) -> None:
    with pytest.raises(UsageError, match="no display"):
        asyncio.run(operations.screenshot(farm.registry, "demo1"))


def test_capture_fails(farm: Farm) -> None:
    (farm.systemd / "mt5-demo1.service.env").write_text("DISPLAY=:91\n")
    (farm.systemd / "import-fail").write_text("")
    with pytest.raises(UsageError, match="unable to open X server"):
        asyncio.run(operations.screenshot(farm.registry, "demo1"))


def test_cli_saves_the_file(
    farm: Farm, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (farm.systemd / "mt5-demo1.service.env").write_text("DISPLAY=:91\n")
    config = tmp_path / "t.yaml"
    config.write_text(
        "hosts:\n  here: {kind: local}\n"
        f"terminals:\n  demo1:\n    host: here\n    data_dir: '{farm.data}'\n"
        "    service: mt5-demo1.service\n    service_scope: user\n"
    )
    out = tmp_path / "shots"
    out.mkdir()
    assert main(["screenshot", "demo1", "-o", str(out), "--config", str(config)]) == EXIT_OK
    (saved,) = out.glob("demo1-*.png")
    assert saved.read_bytes().startswith(operations.PNG_MAGIC)
    assert str(saved) in capsys.readouterr().out
    (farm.systemd / "mt5-demo1.service.env").write_text("")
    assert main(["screenshot", "demo1", "--config", str(config)]) == EXIT_ERROR


def test_mcp_returns_the_image(farm: Farm, tmp_path: Path) -> None:
    (farm.systemd / "mt5-demo1.service.env").write_text("DISPLAY=:91\n")
    config = tmp_path / "t.yaml"
    config.write_text(
        "hosts:\n  here: {kind: local}\n"
        f"terminals:\n  demo1:\n    host: here\n    data_dir: '{farm.data}'\n"
        "    service: mt5-demo1.service\n    service_scope: user\n"
    )
    server = build_server(str(config))
    result = asyncio.run(server.call_tool("mt5_screenshot", {"terminal": "demo1"}))
    kinds = [b.type for b in result.content]
    assert kinds == ["text", "image"], kinds
    assert result.content[1].mime_type == "image/png"
    (farm.systemd / "mt5-demo1.service.env").write_text("")
    with pytest.raises(ToolError, match="no display"):
        asyncio.run(server.call_tool("mt5_screenshot", {"terminal": "demo1"}))
