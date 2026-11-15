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
    trading_disabled: str | None = None
    """Why the account cannot trade (e.g. ``investor mode``), when the journal
    says so after the latest authorization; ``None`` otherwise."""
    error: str | None = None
    """Set when the host could not be queried; the other fields are then empty."""

    @property
    def healthy(self) -> bool:
        return (
            self.service_state == "active"
            and self.connected is True
            and self.trading_disabled is None
        )


@dataclass(frozen=True, slots=True)
class VerifyReport:
    """Health after a start: service up, broker connected, every expert loaded."""

    healthy: bool
    service_state: str
    connected: bool | None
    loaded: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    trading_disabled: str | None = None
    """Why the account cannot trade, from the journal; ``None`` when it can."""
    started: bool | None = None
    """After mt5ctl started the terminal: whether the journal shows the start.
    ``None`` when not checked (verifying a terminal that was already running)."""
    errors: tuple[str, ...] = ()
    """Error-level journal messages in the judged part of the journal."""

    @property
    def detail(self) -> str:
        conn = {True: "connected", False: "NOT connected", None: "connection unknown"}
        parts = [f"service {self.service_state}"]
        if self.started is False:
            parts.append("start not in the journal yet")
        parts.append(conn[self.connected])
        if self.trading_disabled is not None:
            parts.append(f"TRADING DISABLED ({self.trading_disabled})")
        total = len(self.loaded) + len(self.pending) + len(self.failed)
        if total:
            parts.append(f"{len(self.loaded)}/{total} experts loaded")
        if self.failed:
            parts.append("failed: " + ", ".join(self.failed))
        if self.errors:
            parts.append(f"{len(self.errors)} error(s) in the journal")
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
    stop_result: str | None = None
    """systemd's ``Result`` for the stop: ``success`` when the terminal closed
    itself; ``None`` when the deploy did not stop it."""

    @property
    def stopped_cleanly(self) -> bool | None:
        return None if self.stop_result is None else self.stop_result == "success"


@dataclass(frozen=True, slots=True)
class StopResult:
    terminal: str
    unit_result: str
    """systemd's ``Result``: ``success`` when the terminal closed itself;
    ``timeout``, ``signal`` and the like when systemd had to end it."""

    @property
    def clean(self) -> bool:
        return self.unit_result == "success"


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
