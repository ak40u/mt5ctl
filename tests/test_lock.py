"""The per-terminal lock: atomic with its owner, data never executed, one taker."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import anyio
import pytest

from builders import plant_lock
from conftest import Farm
from mt5ctl import operations, scripts

LOCK = ".mt5ctl/lock"


def bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash"], input=script, capture_output=True, text=True, check=False)


def owner(data: Path) -> str:
    return str((data / LOCK).readlink())


def test_lock_data_is_never_evaluated(tmp_path: Path) -> None:
    """A timestamp is data: shell arithmetic would run $(...) hidden in it."""
    marker = tmp_path / "MARKER"
    for ts in (
        f"x[$(touch${{IFS}}{marker})]",
        f"$(touch {marker})",
        "1e9",
        "-5",
        "99999999999999",
    ):
        data = tmp_path / f"t{abs(hash(ts))}"
        (data / "MQL5").mkdir(parents=True)
        plant_lock(data, f"evil {ts}")
        out = bash(scripts.build_lock_acquire_script(str(data), "me", 3600))
        assert not marker.exists(), ts
        # an unreadable timestamp makes the lock stale: taken over, by this run only
        assert "LOCK|takeover" in out.stdout, (ts, out)
        assert owner(data).startswith("me ")


def test_fresh_lock_is_held_and_names_its_owner(tmp_path: Path) -> None:
    (tmp_path / "MQL5").mkdir()
    plant_lock(tmp_path, f"other:1:abc {int(time.time())}")
    out = bash(scripts.build_lock_acquire_script(str(tmp_path), "me", 3600))
    assert out.returncode == 1
    tag, state, _who, age = out.stdout.strip().split("|")
    assert (tag, state) == ("LOCK", "held") and int(age) < 5
    assert owner(tmp_path).startswith("other:1:abc ")


def test_two_takers_of_a_stale_lock_one_wins(tmp_path: Path) -> None:
    """B judged the lock stale, then A took it over: B must not take A's lock."""
    (tmp_path / "MQL5").mkdir()
    plant_lock(tmp_path, "old 1000")
    slow = scripts.build_lock_acquire_script(str(tmp_path), "B", 3600).replace(
        "age=$((now - ts))\n", "age=$((now - ts))\nsleep 1\n", 1
    )
    results: dict[str, str] = {}
    b = threading.Thread(target=lambda: results.update(B=bash(slow).stdout))
    b.start()
    time.sleep(0.3)
    results["A"] = bash(scripts.build_lock_acquire_script(str(tmp_path), "A", 3600)).stdout
    b.join()
    assert "LOCK|takeover" in results["A"]
    assert "LOCK|held" in results["B"], results
    assert owner(tmp_path).startswith("A ")


def test_dead_takeover_mutex_expires(tmp_path: Path) -> None:
    (tmp_path / "MQL5").mkdir()
    plant_lock(tmp_path, "old 1000")
    mutex = tmp_path / ".mt5ctl/lock.takeover"
    mutex.mkdir()
    os.utime(mutex, (1000, 1000))
    out = bash(scripts.build_lock_acquire_script(str(tmp_path), "me", 3600))
    assert "LOCK|takeover" in out.stdout and not mutex.exists()


def test_release_only_by_its_owner(tmp_path: Path) -> None:
    (tmp_path / "MQL5").mkdir()
    assert "acquired" in bash(scripts.build_lock_acquire_script(str(tmp_path), "a", 60)).stdout
    assert "notowner" in bash(scripts.build_lock_release_script(str(tmp_path), "b")).stdout
    assert "notowner" in bash(scripts.build_lock_release_script(str(tmp_path), "a 1")).stdout
    assert "released" in bash(scripts.build_lock_release_script(str(tmp_path), "a")).stdout
    assert not os.path.lexists(tmp_path / LOCK)


def test_a_run_that_lost_the_lock_does_not_write(tmp_path: Path) -> None:
    data = tmp_path
    (data / ".mt5ctl/staging/s").mkdir(parents=True)
    (data / ".mt5ctl/staging/s/A.ex5").write_bytes(b"new")
    plant_lock(data, f"taker {int(time.time())}")
    import hashlib

    sha = hashlib.sha256(b"new").hexdigest()
    script = scripts.build_apply_script(
        str(data), ".mt5ctl/staging/s", {"A.ex5": sha}, [], "{}", "", holder="me"
    )
    out = bash(script)
    assert "APPLY|lost" in out.stdout and out.returncode != 0
    assert not (data / "A.ex5").exists()


def test_cancel_while_taking_the_lock_leaves_none(
    farm: Farm, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acquire reached the host, the caller gave up before the answer."""
    real = scripts.build_lock_acquire_script

    def slow(data_dir: str, holder: str, stale_s: int) -> str:
        return real(data_dir, holder, stale_s).replace(
            'echo "LOCK|acquired"', 'sleep 1; echo "LOCK|acquired"', 1
        )

    monkeypatch.setattr(scripts, "build_lock_acquire_script", slow)

    async def main() -> None:
        with anyio.move_on_after(0.3):
            await operations.restart(farm.registry, "demo1")

    anyio.run(main)
    state = farm.data / ".mt5ctl"
    assert not [p.name for p in state.iterdir() if "lock" in p.name]
    assert farm.calls == []


def test_no_lock_and_nothing_created_outside_a_data_folder(tmp_path: Path) -> None:
    """A wrong data_dir must not grow a .mt5ctl tree (and then a whole bundle)."""
    wrong = tmp_path / "typo"
    out = bash(scripts.build_lock_acquire_script(str(wrong), "me", 3600))
    assert out.returncode == 1 and "LOCK|nodata" in out.stdout
    assert not wrong.exists()
