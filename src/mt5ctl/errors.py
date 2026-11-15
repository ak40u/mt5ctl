"""Typed errors with actionable messages.

Every error mt5ctl raises on purpose derives from :class:`Mt5ctlError`. The CLI
prints the message verbatim, so each one should say how to recover, not only
what went wrong.
"""

from __future__ import annotations


class Mt5ctlError(Exception):
    """Base class for all expected, user-facing errors."""


class ConfigError(Mt5ctlError):
    """The terminal registry is missing, malformed, or internally inconsistent."""


class UnknownTargetError(Mt5ctlError):
    """A referenced terminal or host does not exist in the registry."""


class ChartFormatError(Mt5ctlError):
    """A ``.chr`` file cannot be decoded or its sections do not nest."""


class BundleError(Mt5ctlError):
    """A local bundle is malformed: a chart references a missing expert, or a
    file sits outside the managed layout (absolute path, ``..``, symlink)."""


class ManifestError(Mt5ctlError):
    """The terminal's ``deployed.json`` is corrupt or was written by an
    incompatible mt5ctl version. Raised before anything is changed."""


class StateError(Mt5ctlError):
    """A terminal's current state could not be collected or read back."""


class CredentialError(Mt5ctlError):
    """No password could be found for a login."""


class LoginError(Mt5ctlError):
    """A login could not be carried out."""


class DeployError(Mt5ctlError):
    """A deploy, adopt or restart cannot proceed safely, or its apply failed."""


class CompileError(Mt5ctlError):
    """MetaEditor reported errors or produced no ``.ex5`` for a source."""


class ConfirmationRequiredError(Mt5ctlError):
    """A change to a live terminal was requested without explicit confirmation."""

    def __init__(self, terminal_id: str, action: str) -> None:
        self.terminal_id = terminal_id
        self.action = action
        super().__init__(
            f"refusing to {action} live terminal {terminal_id!r} without confirmation; "
            "pass --confirm (CLI) or confirm=true (MCP)"
        )


class RemoteCommandError(Mt5ctlError):
    """A command executed over SSH exited non-zero or timed out."""

    def __init__(self, host: str, returncode: int, stderr: str) -> None:
        self.host = host
        self.returncode = returncode
        self.stderr = stderr.strip()
        detail = self.stderr or "(no stderr)"
        super().__init__(f"remote command on {host!r} failed (exit {returncode}): {detail}")
