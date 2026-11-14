"""SSH argv construction and the run() error paths (no network)."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest

from mt5ctl import ssh
from mt5ctl.errors import RemoteCommandError
from mt5ctl.models import Host, HostKind


def test_ssh_host_argv() -> None:
    argv = ssh.build_argv(Host("vps", HostKind.SSH, ssh="trader@vps"))
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    assert argv[-2:] == ["trader@vps", 'bash -c "set -o pipefail; base64 -d | bash"']


def test_wsl_host_argv() -> None:
    argv = ssh.build_argv(Host("box", HostKind.WSL, ssh="box", wsl_distro="Ubuntu-24.04"))
    assert argv[-1] == 'wsl -d Ubuntu-24.04 -- bash -c "set -o pipefail; base64 -d | bash"'


def test_local_host_has_no_argv() -> None:
    with pytest.raises(ValueError, match="local"):
        ssh.build_argv(Host("here"))


def fake_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """Put an executable ``ssh`` stub first on PATH."""
    stub = tmp_path / "ssh"
    stub.write_text(f"#!/bin/sh\n{body}\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")


def test_run_streams_the_script_over_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The stub decodes stdin exactly as the remote side would, and runs it.
    fake_ssh(tmp_path, monkeypatch, "base64 -d | sh")
    host = Host("vps", HostKind.SSH, ssh="vps")
    result = asyncio.run(ssh.run(host, "echo \"$((6 * 7))\"; echo 'it''s'"))
    assert result.stdout == "42\nits\n"


def test_run_raises_on_non_zero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ssh(tmp_path, monkeypatch, "cat >/dev/null; echo boom >&2; exit 3")
    with pytest.raises(RemoteCommandError, match=r"exit 3\): boom"):
        asyncio.run(ssh.run(Host("vps", HostKind.SSH, ssh="vps"), "true"))


def test_run_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ssh(tmp_path, monkeypatch, "exec sleep 5")
    with pytest.raises(RemoteCommandError, match="timed out"):
        asyncio.run(ssh.run(Host("vps", HostKind.SSH, ssh="vps"), "true", timeout=0.2))


def test_payload_is_plain_base64() -> None:
    script = "echo 'quoted' \"$HOME\" `id`"
    payload = base64.b64encode(script.encode())
    assert set(payload) <= set(
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
    )
