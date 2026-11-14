"""Result types returned by :mod:`mt5ctl.operations`."""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Env
from .reconcile import DeployPlan


@dataclass(frozen=True, slots=True)
class TerminalStatus:
    """A terminal's runtime state, as one status probe saw it."""

    terminal: str
    host: str
    env: Env
    service_state: str
    """``systemctl is-active``: ``active``, ``inactive``, ``failed``, ...; or ``unknown``."""
    active_seconds: int | None = None
    """Time since the unit became active; ``None`` when unknown."""
    journal_age_seconds: int | None = None
    """Time since the journal was last written; ``None`` without a journal."""
    connected: bool | None = None
    """Broker connection: from the terminal's sockets when they can be attributed,
    otherwise from the latest connect/disconnect line in the journal."""
    last_event: str | None = None
    """The latest connect or disconnect line of the journal."""
    error: str | None = None
    """Set when the host could not be queried; the other fields are then empty."""

    @property
    def healthy(self) -> bool:
        return self.service_state == "active" and self.connected is True


@dataclass(frozen=True, slots=True)
class VerifyReport:
    """Health after a start: service up, broker connected, every expert loaded."""

    healthy: bool
    service_state: str
    connected: bool | None
    loaded: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()

    @property
    def detail(self) -> str:
        conn = {True: "connected", False: "NOT connected", None: "connection unknown"}
        parts = [f"service {self.service_state}", conn[self.connected]]
        total = len(self.loaded) + len(self.pending) + len(self.failed)
        if total:
            parts.append(f"{len(self.loaded)}/{total} experts loaded")
        if self.failed:
            parts.append("failed: " + ", ".join(self.failed))
        return "; ".join(parts)


@dataclass(frozen=True, slots=True)
class DeployResult:
    terminal: str
    plan: DeployPlan
    restarted: bool
    backup: str | None = None
    """Backup of the managed files taken before the apply; ``None`` when nothing
    was replaced (first deploy, or no changes)."""
    verify: VerifyReport | None = None


@dataclass(frozen=True, slots=True)
class AdoptResult:
    terminal: str
    adopted: tuple[str, ...]
    drifted: tuple[str, ...]
    """Adopted files whose content differs from the bundle; the next deploy updates them."""
    foreign: tuple[str, ...]
    """Live charts outside the bundle, left unmanaged."""
    manifest_path: str


@dataclass(frozen=True, slots=True)
class RestartResult:
    terminal: str
    verify: VerifyReport


@dataclass(frozen=True, slots=True)
class CompileResult:
    compiled: tuple[str, ...]
    """Sources (relative to the source folder) that produced an ``.ex5``."""
    failed: tuple[str, ...]
    errors: int
    warnings: int
    logs: dict[str, str] = field(default_factory=dict)
    """MetaEditor log files by name, decoded."""

    @property
    def ok(self) -> bool:
        return not self.failed and self.errors == 0
