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
