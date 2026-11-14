"""Transport: argv construction and the run()/put_tar() paths."""

from __future__ import annotations

import asyncio
import io
import tarfile
from pathlib import Path

import pytest

from mt5ctl import transport
from mt5ctl.errors import RemoteCommandError
from mt5ctl.models import Host, HostKind

LOCAL = Host("here")


def test_ssh_host_argv() -> None:
    argv = transport.build_argv(Host("vps", HostKind.SSH, ssh="trader@vps"))
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    assert argv[-2:] == ["trader@vps", 'bash -c "set -o pipefail; base64 -d | bash"']


def test_wsl_host_argv() -> None:
    argv = transport.build_argv(
        Host("box", HostKind.WSL, ssh="box", wsl_distro="Ubuntu-24.04")
    )
    assert argv[-1] == 'wsl -d Ubuntu-24.04 -- bash -c "set -o pipefail; base64 -d | bash"'


def test_local_host_argv_runs_the_same_command() -> None:
    assert transport.build_argv(LOCAL) == ["bash", "-c", "set -o pipefail; base64 -d | bash"]


def test_put_argv_carries_only_base64() -> None:
    argv = transport.build_put_argv(Host("vps", HostKind.SSH, ssh="vps"), "/data/it's $HOME")
    remote = argv[-1]
    assert "it's" not in remote and "$HOME" not in remote
    assert remote.startswith('bash -c "bash <(echo ') and remote.endswith(' | base64 -d)"')


def test_run_executes_the_script() -> None:
    result = asyncio.run(transport.run(LOCAL, "echo \"$((6 * 7))\"; echo 'it''s'"))
    assert (result.returncode, result.stdout) == (0, "42\nits\n")


def test_run_raises_on_failure_unless_unchecked() -> None:
    with pytest.raises(RemoteCommandError, match=r"exit 3\): boom"):
        asyncio.run(transport.run(LOCAL, "echo boom >&2; exit 3"))
    result = asyncio.run(transport.run(LOCAL, "echo out; exit 3", check=False))
    assert (result.ok, result.stdout) == (False, "out\n")


def test_run_times_out() -> None:
    with pytest.raises(RemoteCommandError, match="timed out"):
        asyncio.run(transport.run(LOCAL, "sleep 5", timeout=0.2))


def test_missing_program_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(RemoteCommandError, match="cannot start bash"):
        asyncio.run(transport.run(LOCAL, "true"))


def test_put_tar_extracts_into_a_new_folder(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        data = b"payload"
        info = tarfile.TarInfo("MQL5/Experts/It's $x/A.ex5")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    dest = tmp_path / "data dir" / "staging"
    asyncio.run(transport.put_tar(LOCAL, buf.getvalue(), str(dest)))
    assert (dest / "MQL5/Experts/It's $x/A.ex5").read_bytes() == b"payload"


def test_put_tar_failure_is_raised(tmp_path: Path) -> None:
    with pytest.raises(RemoteCommandError, match="upload failed"):
        asyncio.run(transport.put_tar(LOCAL, b"not a tar", str(tmp_path / "x")))
