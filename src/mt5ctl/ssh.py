"""Run bash scripts on remote hosts over SSH.

The script never appears on the command line. It is base64-encoded and streamed
over the ssh process's stdin, and the remote side runs a fixed command,
``base64 -d | bash``. Two problems disappear at once:

* quoting: nothing but the base64 alphabet crosses the
  ``ssh -> Windows cmd.exe -> wsl.exe -> bash`` chain of a WSL host;
* size: a script that grows with the bundle never hits the ~8 KB command-line
  limit of ``cmd.exe``.

``ssh`` itself is started from an argv list, never through a local shell.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass

from .errors import RemoteCommandError
from .models import Host, HostKind

SSH_OPTIONS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "ServerAliveInterval=5",
    "-o",
    "ServerAliveCountMax=3",
)

# pipefail: if base64 is missing or the decode fails, fail instead of running
# an empty script and reporting success.
_REMOTE_COMMAND = "set -o pipefail; base64 -d | bash"


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def build_argv(host: Host) -> list[str]:
    """The ``ssh`` argv for *host*. Pure, so it is unit-tested without a network."""
    if host.kind is HostKind.LOCAL or host.ssh is None:
        raise ValueError(f"host {host.id!r} is local; it has no SSH command")
    if host.kind is HostKind.WSL:
        remote = f'wsl -d {host.wsl_distro} -- bash -c "{_REMOTE_COMMAND}"'
    else:
        remote = f'bash -c "{_REMOTE_COMMAND}"'
    return ["ssh", *SSH_OPTIONS, host.ssh, remote]


async def run(host: Host, script: str, *, timeout: float = 60.0) -> CommandResult:
    """Run *script* on *host*; raise :class:`RemoteCommandError` on failure or timeout."""
    argv = build_argv(host)
    payload = base64.b64encode(script.encode("utf-8"))
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise RemoteCommandError(host.id, 127, f"cannot start ssh: {exc}") from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(payload), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RemoteCommandError(host.id, 124, f"timed out after {timeout:.0f}s") from None
    result = CommandResult(
        returncode=proc.returncode or 0,
        stdout=out.decode("utf-8", "replace"),
        stderr=err.decode("utf-8", "replace"),
    )
    if result.returncode != 0:
        raise RemoteCommandError(host.id, result.returncode, result.stderr)
    return result
