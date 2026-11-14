"""MCP server: tool surface, annotations, error mapping."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from builders import CHARTS
from mt5ctl.server import build_server

TOOLS = {
    "mt5_list": True,
    "mt5_status": True,
    "mt5_plan": True,
    "mt5_verify": True,
    "mt5_chart": True,
    "mt5_deploy": False,
    "mt5_adopt": False,
    "mt5_restart": False,
    "mt5_compile": False,
}


def text(result: CallToolResult | object) -> str:
    assert isinstance(result, CallToolResult)
    return "\n".join(c.text for c in result.content if isinstance(c, TextContent))


def test_tools_and_read_only_annotations() -> None:
    tools = asyncio.run(build_server(None).list_tools())
    found = {t.name: t.annotations.read_only_hint for t in tools if t.annotations}
    assert found == TOOLS
    assert all(t.description for t in tools)


def test_chart_tool_needs_no_registry() -> None:
    server = build_server("/nonexistent.yaml")
    out = text(
        asyncio.run(server.call_tool("mt5_chart", {"path": str(CHARTS / "expert-utf16.chr")}))
    )
    assert "symbol=EURUSD timeframe=H4" in out
    assert "MagicNumber=1001" in out


def test_expected_errors_become_tool_errors() -> None:
    server = build_server("/nonexistent.yaml")
    with pytest.raises(ToolError, match="registry file not found"):
        asyncio.run(server.call_tool("mt5_list", {}))


def test_plan_tool(tmp_path: Path) -> None:
    config = tmp_path / "terminals.yaml"
    data = tmp_path / "data"
    data.mkdir()
    config.write_text(
        "hosts:\n  here: {kind: local}\n"
        f"terminals:\n  t1: {{host: here, data_dir: '{data}'}}\n"
    )
    bundle = tmp_path / "bundle/MQL5/Experts/X"
    bundle.mkdir(parents=True)
    (bundle / "A.ex5").write_bytes(b"a")
    server = build_server(str(config))
    out = text(
        asyncio.run(
            server.call_tool(
                "mt5_plan", {"terminal": "t1", "bundle": str(tmp_path / "bundle")}
            )
        )
    )
    assert "+ MQL5/Experts/X/A.ex5" in out
    assert "t1" in text(asyncio.run(server.call_tool("mt5_list", {})))
