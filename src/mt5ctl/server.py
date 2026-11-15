"""MCP server: the CLI's operations as tools for AI agents.

Start it with ``mt5ctl serve`` (stdio transport, the form MCP clients launch).
Tools return the same text the CLI prints. Expected errors (a bad bundle, a
refused deploy, an unreachable host) are reported as tool errors carrying
mt5ctl's actionable message, never as a stack trace.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import ParamSpec

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import formatting as fmt
from . import operations
from .chart import ChartFile
from .config import load_registry
from .errors import Mt5ctlError
from .models import Registry

INSTRUCTIONS = """\
Deploy and inspect expert advisors on MetaTrader 5 terminals (Wine on Linux/WSL2).
Read-only: mt5_list (terminal ids), mt5_status, mt5_plan (what a deploy would change),
mt5_verify, mt5_chart (read a local .chr file).
Changing, and refused for a terminal with env=live unless confirm=true:
mt5_deploy (apply a local bundle: stop, back up, apply, start, verify),
mt5_adopt (record a bundle the terminal already runs, before the first deploy),
mt5_start, mt5_stop, mt5_restart, mt5_compile.
Always run mt5_plan before mt5_deploy and show the plan.
Bundles and sources are folders on the machine running this server, not on the host."""

P = ParamSpec("P")


class Tools:
    """Holds the registry path; the registry is loaded on first use."""

    def __init__(self, config: str | None) -> None:
        self._config = config

    @functools.cached_property
    def registry(self) -> Registry:
        return load_registry(self._config)


def _guard(fn: Callable[P, Awaitable[str]]) -> Callable[P, Awaitable[str]]:
    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> str:
        try:
            return await fn(*args, **kwargs)
        except (Mt5ctlError, ValueError) as exc:
            raise ToolError(str(exc)) from None

    return wrapper


READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
CHANGES = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


def build_server(config: str | None = None) -> MCPServer:
    tools = Tools(config)
    mcp = MCPServer("mt5ctl", instructions=INSTRUCTIONS)

    @mcp.tool(annotations=READ_ONLY)
    @_guard
    async def mt5_list() -> str:
        """List registered terminals: id, environment, host, data folder. Offline."""
        reg = tools.registry
        return "\n".join(
            f"{t.id}  env={t.env.value}  host={t.host}  "
            f"service={t.service or '-'}  {t.data_dir}"
            for t in reg.terminals.values()
        )

    @mcp.tool(annotations=READ_ONLY)
    @_guard
    async def mt5_status(terminal: str = "all") -> str:
        """Service state, broker connection and journal age of one or all terminals."""
        ids = None if terminal == "all" else [terminal]
        return "\n".join(fmt.status_lines(await operations.status(tools.registry, ids)))

    @mcp.tool(annotations=READ_ONLY)
    @_guard
    async def mt5_plan(terminal: str, bundle: str) -> str:
        """Show what deploying a local bundle folder would add, update and remove.

        Read-only. Charts are compared by symbol, timeframe, expert and inputs,
        so display changes never count. Conflicts (files mt5ctl does not manage
        at a bundle path) mean a deploy would refuse.
        """
        report = await operations.plan(tools.registry, terminal, bundle)
        return "\n".join(fmt.plan_lines(report.plan))

    @mcp.tool(annotations=CHANGES)
    @_guard
    async def mt5_deploy(
        terminal: str, bundle: str, confirm: bool = False, verify_timeout: float | None = None
    ) -> str:
        """Apply a local bundle: stop, back up, apply, start, verify.

        Only files mt5ctl placed earlier are updated or removed; foreign charts
        stay. A failed apply is restored from the backup before the restart.
        Nothing to change means no restart.

        A terminal with env=live is refused unless confirm=true.
        """
        result = await operations.deploy(
            tools.registry, terminal, bundle, confirm=confirm, verify_timeout=verify_timeout
        )
        return "\n".join(fmt.deploy_lines(result))

    @mcp.tool(annotations=CHANGES)
    @_guard
    async def mt5_adopt(terminal: str, bundle: str, confirm: bool = False) -> str:
        """Record a bundle the terminal already runs as managed. Changes no strategy file.

        Run once before the first mt5_deploy on a terminal set up by hand;
        every bundle file must already be on the terminal.

        A terminal with env=live is refused unless confirm=true.
        """
        result = await operations.adopt(tools.registry, terminal, bundle, confirm=confirm)
        return "\n".join(fmt.adopt_lines(result))

    @mcp.tool(annotations=CHANGES)
    @_guard
    async def mt5_restart(
        terminal: str, confirm: bool = False, verify_timeout: float | None = None
    ) -> str:
        """Restart a terminal through systemd and wait until it is healthy.

        A terminal with env=live is refused unless confirm=true.
        """
        result = await operations.restart(
            tools.registry, terminal, confirm=confirm, verify_timeout=verify_timeout
        )
        return "\n".join(fmt.restart_lines(result))

    @mcp.tool(annotations=CHANGES)
    @_guard
    async def mt5_start(
        terminal: str, confirm: bool = False, verify_timeout: float | None = None
    ) -> str:
        """Start a terminal through systemd and wait until it is healthy.

        A terminal with env=live is refused unless confirm=true.
        """
        result = await operations.start(
            tools.registry, terminal, confirm=confirm, verify_timeout=verify_timeout
        )
        return "\n".join(fmt.restart_lines(result, verb="started"))

    @mcp.tool(annotations=CHANGES)
    @_guard
    async def mt5_stop(terminal: str, confirm: bool = False) -> str:
        """Stop a terminal through systemd and wait until its process has exited.

        A terminal with env=live is refused unless confirm=true.
        """
        result = await operations.stop(tools.registry, terminal, confirm=confirm)
        return "\n".join(fmt.stop_lines(result))

    @mcp.tool(annotations=READ_ONLY)
    @_guard
    async def mt5_verify(terminal: str, timeout: float | None = None) -> str:
        """Check service, broker connection and that every chart's expert loaded."""
        report = await operations.verify(tools.registry, terminal, timeout=timeout)
        state = "healthy" if report.healthy else "NOT healthy"
        return f"{terminal}: {state} — {report.detail}"

    @mcp.tool(annotations=READ_ONLY)
    @_guard
    async def mt5_chart(path: str) -> str:
        """Read a local .chr file: symbol, timeframe, expert and its inputs."""
        chart = ChartFile.read(path)
        expert = chart.expert
        lines = [f"symbol={chart.symbol} timeframe={chart.timeframe}"]
        if expert is None:
            lines.append("no expert")
        else:
            lines.append(f"expert={expert.name} path={expert.path} mode={expert.mode}")
            lines.extend(f"  {k}={v}" for k, v in expert.inputs)
        return "\n".join(lines)

    @mcp.tool(annotations=CHANGES)
    @_guard
    async def mt5_compile(terminal: str, sources: str, out: str, confirm: bool = False) -> str:
        """Compile a local folder of .mq5 sources with a terminal's MetaEditor.

        The .ex5 files are written to the local folder *out*.

        A terminal with env=live is refused unless confirm=true.
        """
        result = await operations.compile_sources(
            tools.registry, terminal, sources, out, confirm=confirm
        )
        return "\n".join(fmt.compile_lines(result))

    return mcp


def serve(config: str | None = None) -> None:
    build_server(config).run()
