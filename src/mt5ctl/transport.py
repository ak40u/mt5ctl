"""Run bash scripts on a terminal's host: over SSH, or on this machine.

The script never appears on a command line. It is base64-encoded and streamed
over the child process's stdin, and the host runs a fixed command,
``base64 -d | bash``. Two problems disappear at once:

* quoting: nothing but the base64 alphabet crosses the
  ``ssh -> Windows cmd.exe -> wsl.exe -> bash`` chain of a WSL host;
* size: a script that grows with the bundle never hits the ~8 KB command-line
  limit of ``cmd.exe``.

A ``local`` host runs the very same command through the local ``bash``, so the
test suite exercises the exact decode-and-run path a remote host uses. Child
processes are always started from an argv list, never through a local shell.
"""

from __future__ import annotations

import asyncio
import base64
import shlex
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
_RUN_COMMAND = "set -o pipefail; base64 -d | bash"


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _argv(host: Host, inner: str) -> list[str]:
    if host.kind is HostKind.LOCAL:
        return ["bash", "-c", inner]
    if host.ssh is None:  # guaranteed by Host.__post_init__; keeps mypy precise
        raise ValueError(f"host {host.id!r} has no ssh destination")
    if host.kind is HostKind.WSL:
        remote = f'wsl -d {host.wsl_distro} -- bash -c "{inner}"'
    else:
        remote = f'bash -c "{inner}"'
    return ["ssh", *SSH_OPTIONS, host.ssh, remote]


def build_argv(host: Host) -> list[str]:
    """The argv that runs a base64 script fed on stdin. Pure, for unit tests."""
    return _argv(host, _RUN_COMMAND)


def build_put_argv(host: Host, dest_dir: str) -> list[str]:
    """The argv that extracts a tar fed on stdin into *dest_dir* (created if absent).

    stdin carries the raw tar, so the extraction script cannot travel there.
    It is base64-encoded into the command and decoded into a process
    substitution (``bash <(echo … | base64 -d)``); *dest_dir* is quoted inside
    the encoded script, so still only the base64 alphabet crosses the chain.
    ``--no-same-owner`` extracts as the invoking user; members were validated
    against the bundle allowlist before the tar was built.
    """
    script = (
        "set -o pipefail\n"
        f"mkdir -p {shlex.quote(dest_dir)}\n"
        f"tar --no-same-owner -x -f - -C {shlex.quote(dest_dir)}\n"
    )
    payload = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return _argv(host, f"bash <(echo {payload} | base64 -d)")


async def _exec(host: Host, argv: list[str], stdin: bytes, timeout: float) -> CommandResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise RemoteCommandError(host.id, 127, f"cannot start {argv[0]}: {exc}") from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RemoteCommandError(host.id, 124, f"timed out after {timeout:.0f}s") from None
    return CommandResult(
        returncode=proc.returncode or 0,
        stdout=out.decode("utf-8", "replace"),
        stderr=err.decode("utf-8", "replace"),
    )


async def run(
    host: Host, script: str, *, timeout: float = 60.0, check: bool = True
) -> CommandResult:
    """Run *script* on *host*.

    With ``check`` (the default) a non-zero exit raises
    :class:`RemoteCommandError`; scripts that report their outcome on stdout
    pass ``check=False``. A timeout always raises.
    """
    payload = base64.b64encode(script.encode("utf-8"))
    result = await _exec(host, build_argv(host), payload, timeout)
    if check and not result.ok:
        raise RemoteCommandError(host.id, result.returncode, result.stderr)
    return result


async def put_tar(
    host: Host, tar_bytes: bytes, dest_dir: str, *, timeout: float = 300.0
) -> None:
    """Upload *tar_bytes* and extract it into *dest_dir* on *host*."""
    result = await _exec(host, build_put_argv(host, dest_dir), tar_bytes, timeout)
    if not result.ok:
        raise RemoteCommandError(
            host.id, result.returncode, f"upload failed: {result.stderr.strip()}"
        )
