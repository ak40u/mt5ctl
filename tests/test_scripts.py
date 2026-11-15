"""Script builders: quoting, privilege handling, and behaviour run in a real bash."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest

from mt5ctl import scripts
from mt5ctl.models import ServiceScope


def run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash"], input=script, capture_output=True, text=True)


def test_control_uses_sudo_only_for_system_units() -> None:
    system = scripts.build_control_script("mt5.service", ServiceScope.SYSTEM, "restart")
    user = scripts.build_control_script("mt5.service", ServiceScope.USER, "restart")
    assert "sudo -n systemctl" in system and "sudo" not in user
    assert "systemctl --user restart" in user


def test_control_rejects_unknown_actions() -> None:
    with pytest.raises(ValueError, match="action"):
        scripts.build_control_script("mt5.service", ServiceScope.USER, "reload; rm -rf /")


def test_every_value_is_single_quoted() -> None:
    hostile = "x'; touch /tmp/pwned; echo '"
    script = scripts.build_lock_acquire_script(hostile, hostile, 10)
    assert "'x'\\''; touch /tmp/pwned; echo '\\'''" in script


def test_lock_round_trip(tmp_path: Path) -> None:
    data = str(tmp_path / "it's here")
    assert "LOCK|acquired" in run(scripts.build_lock_acquire_script(data, "a", 600)).stdout
    held = run(scripts.build_lock_acquire_script(data, "b", 600))
    assert (held.returncode, held.stdout.strip()) == (1, "LOCK|held|a")
    assert "notowner" in run(scripts.build_lock_release_script(data, "b")).stdout
    assert "released" in run(scripts.build_lock_release_script(data, "a")).stdout
    assert "acquired" in run(scripts.build_lock_acquire_script(data, "b", 600)).stdout


def test_backup_keeps_the_newest(tmp_path: Path) -> None:
    (tmp_path / "MQL5/Experts").mkdir(parents=True)
    (tmp_path / "MQL5/Experts/A.ex5").write_bytes(b"a")
    for i in range(scripts.BACKUPS_KEPT + 2):
        out = run(scripts.build_backup_script(str(tmp_path), ["MQL5/Experts/A.ex5"], f"t{i}"))
        assert out.returncode == 0, out.stderr
    assert len(list((tmp_path / scripts.BACKUP_DIR).glob("*.tar"))) == scripts.BACKUPS_KEPT


def test_backup_of_nothing(tmp_path: Path) -> None:
    out = run(scripts.build_backup_script(str(tmp_path), ["MQL5/Experts/A.ex5"], "t"))
    assert out.stdout.strip() == "BACKUP|none"


def test_preflight_reports_the_running_user_without_a_unit(tmp_path: Path) -> None:
    out = run(
        scripts.build_preflight_script(str(tmp_path), "Default", None, ServiceScope.USER)
    )
    assert "PRE|tar|ok" in out.stdout and "PRE|sha256|ok" in out.stdout
    assert "PRE|filesystem|ok" in out.stdout
    assert "USER|" in out.stdout


def test_journal_cursor_and_since(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "20261114.log").write_bytes(b"old day\n")
    (logs / "20261115.log").write_bytes(b"before\n")
    cursor = run(scripts.build_log_cursor_script(str(tmp_path))).stdout.strip().split("|")
    assert cursor[0] == "CURSOR" and cursor[2] == "7"
    with (logs / "20261115.log").open("ab") as fh:
        fh.write(b"after\n")
    (logs / "20261116.log").write_bytes(b"next day\n")
    name = base64.b64decode(cursor[1]).decode()
    out = run(scripts.build_log_since_script(str(tmp_path), name, 7, 1 << 20)).stdout
    chunks = [base64.b64decode(line.split("|")[2]) for line in out.splitlines()]
    assert chunks == [b"after\n", b"next day\n"]


def test_remove_dir_refuses_escapes() -> None:
    for bad in ("", "/etc", "MQL5/../.."):
        with pytest.raises(ValueError):
            scripts.build_remove_dir_script("/data", bad)


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="needs Linux /proc")
def test_is_terminal_matches_the_command_line_not_comm() -> None:
    """Under Wine 11 the terminal's comm is "main"; its command line names the exe."""
    func = scripts._is_terminal_function()
    script = f"""
{func}
bash -c 'exec -a "Z:\\\\home\\\\t\\\\terminal\\\\terminal64.exe /portable" sleep 5' &
T=$!
sleep 5 &
O=$!
sleep 0.2
is_terminal $T && echo "T=yes" || echo "T=no"
is_terminal $O && echo "O=yes" || echo "O=no"
kill $T $O
"""
    out = run(script).stdout
    assert "T=yes" in out and "O=no" in out


def test_journal_ignores_other_logs_in_the_folder(tmp_path: Path) -> None:
    """MetaEditor writes metaeditor.log next to the journal; it sorts after it."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "20261115.log").write_bytes(b"journal\n")
    (logs / "metaeditor.log").write_bytes(b"compile log\n")
    cursor = run(scripts.build_log_cursor_script(str(tmp_path))).stdout.strip().split("|")
    assert base64.b64decode(cursor[1]) == b"20261115.log"
    out = run(scripts.build_log_since_script(str(tmp_path), "", 0, 1 << 20)).stdout
    names = [base64.b64decode(line.split("|")[1]) for line in out.splitlines()]
    assert names == [b"20261115.log"]


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="needs Linux /proc")
def test_is_terminal_main_skips_launchers() -> None:
    """xvfb-run and start.exe mention terminal64.exe in their arguments; only the
    process whose first argument is the executable is the terminal itself."""
    func = scripts._is_terminal_function()
    script = f"""
{func}
bash -c 'exec -a "Z:\\\\home\\\\t\\\\terminal\\\\terminal64.exe" sleep 5' &
T=$!
sh -c 'sleep 5' terminal64.exe &
L=$!
sleep 0.2
is_terminal_main $T && echo "T=main" || echo "T=no"
is_terminal_main $L && echo "L=main" || echo "L=no"
is_terminal $L && echo "L=related" || echo "L=unrelated"
kill $T $L
"""
    out = run(script).stdout
    assert "T=main" in out and "L=no" in out and "L=related" in out
