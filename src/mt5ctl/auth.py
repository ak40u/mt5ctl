"""Where a login password comes from.

Resolution order, first hit wins:

1. a password passed by the caller (the CLI reads it from stdin with
   ``--password-stdin``, so it never appears in a command line);
2. the ``MT5CTL_PASSWORD_<account>`` environment variable;
3. the ``<account>`` key of a JSON secrets file, ``$MT5CTL_CREDENTIALS`` or
   ``$XDG_CONFIG_HOME/mt5ctl/credentials.json``, which must not be readable by
   group or others.

A password is never logged or printed. It leaves this machine only inside the
login script, over the SSH connection's stdin, and on the host lives only in a
temporary login file that the script shreds when it ends.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .errors import CredentialError


def secrets_file() -> Path:
    """The JSON secrets file (it may not exist)."""
    if env := os.environ.get("MT5CTL_CREDENTIALS"):
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "mt5ctl" / "credentials.json"


def resolve_password(account: str, explicit: str | None = None) -> str:
    """The password for *account*; :class:`CredentialError` when there is none."""
    if explicit:
        return explicit
    if env := os.environ.get(f"MT5CTL_PASSWORD_{account}"):
        return env
    path = secrets_file()
    if path.is_file():
        if os.name == "posix" and path.stat().st_mode & 0o077:
            raise CredentialError(
                f"secrets file {path} is readable by group or others; run: chmod 600 {path}"
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise CredentialError(f"cannot read secrets file {path}: {exc}") from None
        if isinstance(data, dict) and account in data:
            return str(data[account])
    raise CredentialError(
        f"no password for account {account!r}: pass it on stdin (--password-stdin), set "
        f"MT5CTL_PASSWORD_{account}, or add the {account!r} key to {path}"
    )
