"""Operations on registered terminals, composed from the pure core."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import ssh
from .bundle import Bundle, read_bundle
from .models import HostKind, Registry, Terminal
from .reconcile import DeployPlan, TerminalState, compute_plan
from .state import build_state_script, parse_state_output, read_local_state


@dataclass(frozen=True, slots=True)
class PlanReport:
    terminal: Terminal
    bundle: Bundle
    plan: DeployPlan


async def collect_state(
    registry: Registry, terminal: Terminal, bundle: Bundle
) -> TerminalState:
    """Read *terminal*'s current state, locally or over SSH (read-only)."""
    host = registry.host_of(terminal)
    paths = sorted(bundle.files)
    if host.kind is HostKind.LOCAL:
        return read_local_state(terminal.data_dir, terminal.profile, paths)
    script = build_state_script(terminal.data_dir, terminal.profile, paths)
    result = await ssh.run(host, script, timeout=120.0)
    return parse_state_output(result.stdout, terminal.profile, paths)


async def plan(registry: Registry, terminal_id: str, bundle_dir: str | Path) -> PlanReport:
    """Dry-run a deploy: what would change on *terminal_id* to match *bundle_dir*.

    Read-only on both sides. The bundle is read for the terminal's configured
    profile, so a bundle built for another profile is refused up front.
    """
    terminal = registry.terminal(terminal_id)
    bundle = read_bundle(bundle_dir, profile=terminal.profile)
    state = await collect_state(registry, terminal, bundle)
    return PlanReport(terminal=terminal, bundle=bundle, plan=compute_plan(bundle, state))
