"""Load and validate the terminal registry (``terminals.yaml``).

The registry path is resolved in this order:

1. an explicit path argument (``--config``) — must exist;
2. ``MT5CTL_CONFIG`` — must exist; a typo fails loudly instead of falling back
   to some other file;
3. the first existing of ``$XDG_CONFIG_HOME/mt5ctl/terminals.yaml`` (or
   ``~/.config/mt5ctl/terminals.yaml``) and ``./terminals.yaml``.

Keep the real file out of version control: it names your hosts and accounts.
``examples/terminals.example.yaml`` is the template.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .bundle import DEFAULT_PROFILE, validate_profile
from .errors import BundleError, ConfigError
from .models import Env, Host, HostKind, Registry, ServiceScope, Terminal

# Ids end up in file names, messages and scripts: keep them boring.
_ID_RE = re.compile(r"[A-Za-z0-9._-]+")
# systemd unit names (instance units included) and X display names.
_UNIT_RE = re.compile(r"[A-Za-z0-9@._:-]+")
_DISPLAY_RE = re.compile(r":[0-9]+(\.[0-9]+)?")


def candidate_paths() -> list[Path]:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return [base / "mt5ctl" / "terminals.yaml", Path.cwd() / "terminals.yaml"]


def resolve_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path is not None:
        resolved = Path(path).expanduser()
        if not resolved.is_file():
            raise ConfigError(f"registry file not found: {resolved}")
        return resolved
    env = os.environ.get("MT5CTL_CONFIG")
    if env:
        resolved = Path(env).expanduser()
        if not resolved.is_file():
            raise ConfigError(
                f"MT5CTL_CONFIG points to {resolved}, which does not exist; fix the path "
                "or unset MT5CTL_CONFIG to use the default locations"
            )
        return resolved
    for candidate in candidate_paths():
        if candidate.is_file():
            return candidate
    searched = "\n  ".join(str(p) for p in candidate_paths())
    raise ConfigError(
        "no terminal registry found. Copy examples/terminals.example.yaml to "
        "~/.config/mt5ctl/terminals.yaml (or set MT5CTL_CONFIG). Searched:\n  " + searched
    )


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a mapping, got {type(value).__name__}")
    return value


def _required(raw: dict[str, Any], key: str, where: str) -> str:
    if raw.get(key) in (None, ""):
        raise ConfigError(f"{where}: missing required field {key!r}")
    return str(raw[key])


def _plain(value: str, field: str, where: str) -> str:
    if any(c in value for c in "\r\n\0"):
        raise ConfigError(f"{where}: {field} must not contain line breaks or NUL")
    return value


def _check_id(kind: str, value: str) -> str:
    if not _ID_RE.fullmatch(value):
        raise ConfigError(
            f"{kind} id {value!r} is invalid: use letters, digits, '.', '_', '-'"
        )
    return value


def _build_host(host_id: str, raw: dict[str, Any]) -> Host:
    where = f"host {host_id!r}"
    try:
        kind = HostKind(raw.get("kind", "local"))
    except ValueError:
        raise ConfigError(
            f"{where}: invalid kind {raw.get('kind')!r} (use local, ssh or wsl)"
        ) from None
    ssh = raw.get("ssh")
    if ssh is not None and (str(ssh).startswith("-") or any(c.isspace() for c in str(ssh))):
        raise ConfigError(f"{where}: ssh destination must be one word not starting with '-'")
    distro = raw.get("wsl_distro")
    if distro is not None and not _ID_RE.fullmatch(str(distro)):
        raise ConfigError(f"{where}: wsl_distro {distro!r} has invalid characters")
    try:
        return Host(
            id=host_id,
            kind=kind,
            ssh=str(ssh) if ssh is not None else None,
            wsl_distro=str(distro) if distro is not None else None,
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from None


def _build_terminal(term_id: str, raw: dict[str, Any], hosts: dict[str, Host]) -> Terminal:
    where = f"terminal {term_id!r}"
    host_id = _required(raw, "host", where)
    if host_id not in hosts:
        raise ConfigError(f"{where}: unknown host {host_id!r}")
    try:
        env = Env(raw.get("env", "demo"))
    except ValueError:
        raise ConfigError(
            f"{where}: invalid env {raw.get('env')!r} (use demo or live)"
        ) from None
    profile = str(raw.get("profile", DEFAULT_PROFILE))
    try:
        validate_profile(profile)
    except BundleError as exc:
        raise ConfigError(f"{where}: {exc}") from None
    service = raw.get("service")
    if service is not None and not _UNIT_RE.fullmatch(str(service)):
        raise ConfigError(f"{where}: service {service!r} is not a systemd unit name")
    try:
        scope = ServiceScope(raw.get("service_scope", "system"))
    except ValueError:
        raise ConfigError(
            f"{where}: invalid service_scope {raw.get('service_scope')!r} (use system or user)"
        ) from None
    display = raw.get("display")
    if display is not None and not _DISPLAY_RE.fullmatch(str(display)):
        raise ConfigError(f"{where}: display {display!r} must look like ':99'")
    wineprefix = raw.get("wineprefix")
    wine = str(raw.get("wine", "wine"))
    start_command = raw.get("start_command")
    stop_command = raw.get("stop_command")
    if start_command is not None and service is not None:
        raise ConfigError(
            f"{where}: set either 'service' (systemd) or 'start_command', not both"
        )
    if (start_command is None) != (stop_command is None):
        raise ConfigError(f"{where}: 'start_command' and 'stop_command' go together")
    return Terminal(
        id=term_id,
        host=host_id,
        data_dir=_plain(_required(raw, "data_dir", where), "data_dir", where),
        profile=profile,
        env=env,
        service=str(service) if service is not None else None,
        service_scope=scope,
        wine=_plain(wine, "wine", where),
        start_command=_plain(str(start_command), "start_command", where)
        if start_command is not None
        else None,
        stop_command=_plain(str(stop_command), "stop_command", where)
        if stop_command is not None
        else None,
        wineprefix=_plain(str(wineprefix), "wineprefix", where) if wineprefix else None,
        display=str(display) if display is not None else None,
    )


def parse_registry(data: Any) -> Registry:
    """Build a :class:`Registry` from parsed YAML."""
    root = _mapping(data, "registry")
    raw_hosts = _mapping(root.get("hosts") or {}, "hosts")
    raw_terms = _mapping(root.get("terminals") or {}, "terminals")
    if not raw_terms:
        raise ConfigError("registry defines no terminals")
    hosts = {
        _check_id("host", str(hid)): _build_host(str(hid), _mapping(raw, f"host {hid!r}"))
        for hid, raw in raw_hosts.items()
    }
    terminals = {
        _check_id("terminal", str(tid)): _build_terminal(
            str(tid), _mapping(raw, f"terminal {tid!r}"), hosts
        )
        for tid, raw in raw_terms.items()
    }
    return Registry(hosts=hosts, terminals=terminals)


def load_registry(path: str | os.PathLike[str] | None = None) -> Registry:
    resolved = resolve_path(path)
    try:
        data = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{resolved}: invalid YAML: {exc}") from None
    if data is None:
        raise ConfigError(f"{resolved}: file is empty")
    return parse_registry(data)
