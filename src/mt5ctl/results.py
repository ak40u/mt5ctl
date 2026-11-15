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
    algo_trading: bool | None = None
    """The terminal's Algo Trading button, when the journal shows it was switched
    since the latest start; ``None`` when it was not (the start config decides)."""
    terminal_running: bool | None = None
    """Whether the terminal's own process is in the unit; ``None`` when unknown.
    ``False`` with an active unit: something (a Wine dialog, a crash at start)
    keeps the terminal from running."""
    error: str | None = None
    """Set when the host could not be queried; the other fields are then empty."""

    @property
    def healthy(self) -> bool:
        return (
            self.service_state == "active"
            and self.terminal_running is not False
            and self.connected is True
            and self.trading_disabled is None
            and self.algo_trading is not False
        )


@dataclass(frozen=True, slots=True)
class VerifyReport:
    """Health after a start: service up, broker connected, every expert loaded."""

    healthy: bool
    service_state: str
    connected: bool | None
    terminal_running: bool | None = None
    loaded: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    trading_disabled: str | None = None
    """Why the account cannot trade, from the journal; ``None`` when it can."""
    algo_trading: bool | None = None
    """The terminal's Algo Trading button, if switched since the start."""
    started: bool | None = None
    """After mt5ctl started the terminal: whether the journal shows the start.
    ``None`` when not checked (verifying a terminal that was already running)."""
    errors: tuple[str, ...] = ()
    """Error-level journal messages in the judged part of the journal."""

    @property
    def detail(self) -> str:
        conn = {True: "connected", False: "NOT connected", None: "connection unknown"}
        parts = [f"service {self.service_state}"]
        if self.terminal_running is False:
            parts.append("NO TERMINAL PROCESS")
        if self.started is False:
            parts.append("start not in the journal yet")
        parts.append(conn[self.connected])
        if self.trading_disabled is not None:
            parts.append(f"TRADING DISABLED ({self.trading_disabled})")
        if self.algo_trading is False:
            parts.append("ALGO TRADING OFF")
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
    stop: StopResult | None = None
    """How the terminal stopped; ``None`` when the deploy did not stop it."""

    @property
    def stopped_cleanly(self) -> bool | None:
        return None if self.stop is None else self.stop.clean


@dataclass(frozen=True, slots=True)
class StopResult:
    terminal: str
    unit_result: str
    """systemd's ``Result`` for the unit, for information (see :attr:`clean`)."""
    exit_code: int | None = None
    """The code in the terminal's own "exit with code N" journal line; ``None``
    when it wrote none, i.e. it did not close itself and systemd ended it."""
    was_running: bool = True
    """``False`` when the unit was already stopped: there was nothing to save."""

    @property
    def clean(self) -> bool:
        """Whether the terminal closed itself, saving its profile on the way out."""
        return not self.was_running or self.exit_code is not None


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
class ChartExpert:
    """One chart of the profile and the expert it runs."""

    chart: str
    """The chart file's name, e.g. ``chart01.chr``."""
    symbol: str | None
    timeframe: str | None
    expert: str | None
    """The expert's name; ``None`` for a chart without one."""
    path: str | None = None
    allowed: bool | None = None
    """The expert's own "Allow Algo Trading" (bit 0 of ``expertmode``)."""
    state: str = "-"
    """From the journal's latest session: ``loaded``, ``failed``, ``removed`` or
    ``not loaded``; ``-`` for a chart without an expert or a stopped terminal."""


@dataclass(frozen=True, slots=True)
class ExpertsReport:
    """A terminal's experts and whether they can trade."""

    terminal: str
    running: bool | None = None
    algo_trading: bool | None = None
    """The terminal's Algo Trading button; ``None`` when nothing tells."""
    algo_source: str = ""
    """What :attr:`algo_trading` comes from: the journal or the start file."""
    trading_disabled: str | None = None
    charts: tuple[ChartExpert, ...] = ()
    error: str | None = None

    @property
    def can_trade(self) -> tuple[ChartExpert, ...]:
        """Experts that are loaded and allowed, on a terminal whose button is on."""
        if self.algo_trading is False or self.trading_disabled or not self.running:
            return ()
        return tuple(c for c in self.charts if c.state == "loaded" and c.allowed is not False)


@dataclass(frozen=True, slots=True)
class Screenshot:
    terminal: str
    png: bytes = field(repr=False)

    @property
    def size(self) -> int:
        return len(self.png)


@dataclass(frozen=True, slots=True)
class Check:
    """One line of ``doctor``: ``ok``, ``warn`` (works, with less) or ``fail``."""

    label: str
    status: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class TerminalInfo:
    """What the journal tells about a terminal's latest session."""

    terminal: str
    build: int | None = None
    started: str | None = None
    """Time of the latest start line (journal time, ``HH:MM:SS.mmm``)."""
    system: str | None = None
    """The OS / Wine line the terminal logs at start."""
    start_config: str | None = None
    """The ``/config:`` start file it was launched with, as it logged it."""
    account: str | None = None
    server: str | None = None
    access_point: str | None = None
    ping_ms: float | None = None
    company: str | None = None
    """The broker company named on synchronization."""
    positions: int | None = None
    orders: int | None = None
    trading_disabled: str | None = None
    log_file: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class LogsResult:
    terminal: str
    source: str
    """``journal`` or ``experts``."""
    file: str | None
    lines: tuple[str, ...]
    size: int = 0
    """Size of the file read, in bytes."""
    read_bytes: int = 0
    """How much of its end was read (the whole file when equal to ``size``)."""

    @property
    def partial(self) -> bool:
        return self.read_bytes < self.size


@dataclass(frozen=True, slots=True)
class LoginResult:
    terminal: str
    account: str
    server: str
    outcome: str
    """``ok``, ``failed`` (the broker refused), ``timeout`` or ``error``."""
    detail: str = ""
    """The journal line of a refusal, or what went wrong."""
    restarted: bool = False
    verify: VerifyReport | None = None
    """Health after the terminal was started again, when the login went through."""

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


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
