"""A symbolic link inside the data folder never lets a deploy write outside it."""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
from pathlib import Path

import pytest

from builders import EXPERT, chart_bytes, write_tree
from conftest import Farm, make_registry
from mt5ctl import operations, scripts
from mt5ctl.errors import DeployError


def bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash"], input=script, capture_output=True, text=True, check=False)


def bundle(root: Path) -> Path:
    return write_tree(root, charts={"chart01.chr": chart_bytes()}, experts={EXPERT: b"trend"})


def files_in(folder: Path) -> list[str]:
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())


def test_apply_refuses_a_linked_folder(tmp_path: Path) -> None:
    data, outside = tmp_path / "t", tmp_path / "outside"
    (data / "MQL5").mkdir(parents=True)
    outside.mkdir()
    (data / "MQL5/Experts").symlink_to(outside)
    staged = data / ".mt5ctl/staging/s" / EXPERT
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"new")
    script = scripts.build_apply_script(
        str(data),
        ".mt5ctl/staging/s",
        {EXPERT: hashlib.sha256(b"new").hexdigest()},
        [],
        "{}",
        "",
    )
    out = bash(script)
    assert out.returncode != 0 and "APPLY|link|" in out.stdout
    assert files_in(outside) == []


def test_deploy_refuses_before_stopping_the_terminal(farm: Farm, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (farm.data / "MQL5").mkdir(exist_ok=True)
    (farm.data / "MQL5/Experts").symlink_to(outside)
    with pytest.raises(DeployError, match=r"MQL5/Experts is a symbolic link"):
        asyncio.run(operations.deploy(farm.registry, "demo1", bundle(tmp_path / "b")))
    assert farm.calls == []
    assert files_in(outside) == []


def test_linked_state_folder_is_refused(farm: Farm, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (farm.data / ".mt5ctl").symlink_to(outside)
    with pytest.raises(DeployError, match=r"\.mt5ctl is a symbolic link"):
        asyncio.run(operations.deploy(farm.registry, "demo1", bundle(tmp_path / "b")))
    assert farm.calls == []
    assert not [p for p in files_in(outside) if "staging" in p or "backups" in p]


def test_restore_refuses_a_linked_folder(tmp_path: Path) -> None:
    data, outside = tmp_path / "t", tmp_path / "outside"
    (data / ".mt5ctl").mkdir(parents=True)
    (data / ".mt5ctl/applying").write_text("999999")
    (data / "MQL5").mkdir()
    outside.mkdir()
    (outside / "Portfolio").mkdir()
    (outside / "Portfolio/Trend.ex5").write_bytes(b"theirs")
    (data / "MQL5/Experts").symlink_to(outside)
    out = bash(scripts.build_restore_script(str(data), None, [EXPERT], ""))
    assert "RESTORE|failed|" in out.stdout
    assert (outside / "Portfolio/Trend.ex5").read_bytes() == b"theirs"


def test_data_folder_itself_may_be_a_link(farm: Farm, tmp_path: Path) -> None:
    link = tmp_path / "terminal-link"
    link.symlink_to(farm.data)
    farm.registry = make_registry(link)
    farm.journal_on_start("Trend")
    result = asyncio.run(
        operations.deploy(farm.registry, "demo1", bundle(tmp_path / "b"), verify_timeout=0)
    )
    assert result.restarted
    assert (farm.data / EXPERT).read_bytes() == b"trend"
