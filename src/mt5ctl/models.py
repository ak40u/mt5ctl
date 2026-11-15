"""The terminal registry: hosts and the MetaTrader 5 terminals on them."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .bundle import DEFAULT_PROFILE
from .errors import UnknownTargetError


class HostKind(StrEnum):
    """How mt5ctl reaches a host.

    ``LOCAL``
        This machine: data folders are read directly. Covers a Wine prefix on
        the machine running mt5ctl, and a Windows terminal when mt5ctl runs on
        that Windows host.
    ``SSH``
        A Linux host over SSH; scripts run in ``bash``.
    ``WSL``
        A Windows host over SSH whose terminals run in a WSL2 distribution;
        scripts run via ``wsl -d <distro> -- bash``.
    """

    LOCAL = "local"
    SSH = "ssh"
    WSL = "wsl"


class ServiceScope(StrEnum):
    """Which systemd instance supervises the terminal.

    ``SYSTEM`` units are controlled with ``sudo -n systemctl`` unless mt5ctl
    already runs as root; ``USER`` units with ``systemctl --user``.
    """

    SYSTEM = "system"
    USER = "user"


class Env(StrEnum):
    """Risk tier. Changing a ``live`` terminal requires explicit confirmation."""

    DEMO = "demo"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class Host:
    id: str
    kind: HostKind = HostKind.LOCAL
    ssh: str | None = None
    """SSH destination (an ``~/.ssh/config`` alias or ``user@host``); remote kinds only."""
    wsl_distro: str | None = None
    """WSL distribution name; ``wsl`` hosts only."""

    def __post_init__(self) -> None:
        if self.kind is not HostKind.LOCAL and not self.ssh:
            raise ValueError(
                f"host {self.id!r} is kind={self.kind} but has no ssh destination"
            )
        if self.kind is HostKind.WSL and not self.wsl_distro:
            raise ValueError(f"host {self.id!r} is kind=wsl but has no wsl_distro")


@dataclass(frozen=True, slots=True)
class Terminal:
    id: str
    host: str
    """Id of the owning :class:`Host`."""
    data_dir: str
    """The terminal's data folder (the one holding ``MQL5/``), as a path on its host."""
    profile: str = DEFAULT_PROFILE
    """Chart profile mt5ctl manages (``MQL5/Profiles/Charts/<profile>``)."""
    env: Env = Env.DEMO
    account: str | None = None
    """Login number, the default for ``login``."""
    service: str | None = None
    """systemd unit running the terminal. Either this or :attr:`start_command`
    is needed by deploy, start, stop, restart and status."""
    service_scope: ServiceScope = ServiceScope.SYSTEM
    wine: str = "wine"
    """Wine to run MetaEditor with (compile); a path when the terminal uses its own
    Wine build rather than the one on PATH."""
    wineprefix: str | None = None
    """``WINEPREFIX`` for running MetaEditor (compile); Wine's default when unset."""
    display: str | None = None
    """X display for MetaEditor under Wine, e.g. ``:99``."""
    start_command: str | None = None
    """Without systemd: the shell command that runs the terminal, started by
    mt5ctl in the data folder as a background process group (``process`` mode)."""
    stop_command: str | None = None
    """``process`` mode: the shell command that asks the terminal to close; it
    gets ``$MAINPID``. mt5ctl then waits, and ends the group if it lingers."""

    @property
    def supervised(self) -> bool:
        return bool(self.service or self.start_command)


@dataclass(frozen=True, slots=True)
class Registry:
    hosts: dict[str, Host] = field(default_factory=dict)
    terminals: dict[str, Terminal] = field(default_factory=dict)

    def terminal(self, terminal_id: str) -> Terminal:
        try:
            return self.terminals[terminal_id]
        except KeyError:
            known = ", ".join(sorted(self.terminals)) or "(none)"
            raise UnknownTargetError(
                f"unknown terminal {terminal_id!r}; known: {known}"
            ) from None

    def host_of(self, terminal: Terminal) -> Host:
        return self.hosts[terminal.host]
