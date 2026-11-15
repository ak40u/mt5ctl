"""Live terminals: every changing action is refused without explicit confirmation.

Checked for every changing CLI command and every changing MCP tool, against a
terminal with env: live on the fake farm: the refusal happens before anything
on the host is touched.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from builders import EXPERT, chart_bytes, write_tree
from conftest import UNIT, Farm
from mt5ctl.cli import EXIT_ERROR, main
from mt5ctl.server import build_server

CHANGING_TOOLS = (
    "mt5_deploy",
    "mt5_adopt",
    "mt5_start",
    "mt5_stop",
    "mt5_restart",
    "mt5_compile",
)


@pytest.fixture
def live(farm: Farm, tmp_path: Path) -> tuple[Path, Path, Path]:
    """(registry, bundle, sources) for a live terminal on the fake farm."""
    config = tmp_path / "live.yaml"
    config.write_text(
        "hosts:\n  here: {kind: local}\n"
        f"terminals:\n  live1:\n    host: here\n    data_dir: '{farm.data}'\n"
        f"    service: {UNIT}\n    service_scope: user\n    env: live\n"
    )
    bundle = write_tree(
        tmp_path / "b", charts={"chart01.chr": chart_bytes()}, experts={EXPERT: b"1"}
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "A.mq5").write_text("// ok")
    return config, bundle, src


def cli_args(command: str, bundle: Path, src: Path, tmp_path: Path) -> list[str]:
    extra = {
        "deploy": [str(bundle)],
        "adopt": [str(bundle)],
        "compile": [str(src), str(tmp_path / "out")],
    }.get(command, [])
    return [command, "live1", *extra]


@pytest.mark.parametrize("command", ["deploy", "adopt", "start", "stop", "restart", "compile"])
def test_cli_refuses_without_confirm(
    command: str,
    farm: Farm,
    live: tuple[Path, Path, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, bundle, src = live
    code = main([*cli_args(command, bundle, src, tmp_path), "--config", str(config)])
    assert code == EXIT_ERROR
    assert "--confirm" in capsys.readouterr().err
    assert farm.calls == []
    assert not (farm.data / ".mt5ctl").exists()


def test_cli_proceeds_with_confirm(farm: Farm, live: tuple[Path, Path, Path]) -> None:
    config, _, _ = live
    assert main(["stop", "live1", "--confirm", "--config", str(config)]) == 0
    assert farm.calls == ["stop mt5-demo1.service"]


def mcp_args(tool: str, bundle: Path, src: Path, tmp_path: Path) -> dict[str, object]:
    args: dict[str, object] = {"terminal": "live1"}
    if tool in ("mt5_deploy", "mt5_adopt"):
        args["bundle"] = str(bundle)
    if tool == "mt5_compile":
        args |= {"sources": str(src), "out": str(tmp_path / "out")}
    return args


@pytest.mark.parametrize("tool", CHANGING_TOOLS)
def test_mcp_refuses_without_confirm(
    tool: str, farm: Farm, live: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    config, bundle, src = live
    server = build_server(str(config))
    with pytest.raises(ToolError, match="confirm=true"):
        asyncio.run(server.call_tool(tool, mcp_args(tool, bundle, src, tmp_path)))
    assert farm.calls == []


def test_every_changing_tool_says_so() -> None:
    tools = {t.name: t for t in asyncio.run(build_server(None).list_tools())}
    for name in CHANGING_TOOLS:
        assert "env=live is refused unless confirm=true" in (tools[name].description or "")
        assert "confirm" in tools[name].input_schema["properties"]
