"""compile_sources end to end with a fake wine/MetaEditor (tests/fakes/wine)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from conftest import Farm, make_registry
from mt5ctl import operations
from mt5ctl.errors import CompileError, ConfirmationRequiredError
from mt5ctl.models import Env
from mt5ctl.results import CompileResult


def sources(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return root


@pytest.fixture
def editor(farm: Farm, tmp_path: Path) -> Farm:
    """The farm's terminal, moved to a data folder without spaces (MetaEditor needs it)."""
    data = tmp_path / "build-terminal"
    data.mkdir()
    (data / "MetaEditor64.exe").write_bytes(b"MZ")
    farm.data = data
    farm.registry = make_registry(data)
    return farm


def compile_(farm: Farm, src: Path, out: Path) -> CompileResult:
    return asyncio.run(operations.compile_sources(farm.registry, "demo1", src, out))


def test_compile_fetches_ex5_and_cleans_up(editor: Farm, tmp_path: Path) -> None:
    """A folder with a space is compiled under a space-free stand-in (MetaEditor
    skips such folders) and its .ex5 comes back under the original name."""
    src = sources(
        tmp_path / "src",
        {
            "Trend EURUSD H4.mq5": "// ok",
            "Sub dir/Breakout.mq5": "// ok",
            "Sub dir/Common.mqh": "// include",
            "notes.log": "ignored",
        },
    )
    result = compile_(editor, src, tmp_path / "out")
    assert result.ok
    assert result.compiled == ("Sub dir/Breakout.mq5", "Trend EURUSD H4.mq5")
    assert (tmp_path / "out/Trend EURUSD H4.ex5").read_bytes() == b"EX5"
    assert (tmp_path / "out/Sub dir/Breakout.ex5").exists()
    assert result.errors == 0 and result.logs
    assert not list((editor.data / "MQL5/Experts").glob("mt5ctl-build-*"))


def test_compile_errors_are_reported(editor: Farm, tmp_path: Path) -> None:
    src = sources(tmp_path / "src", {"Good.mq5": "// ok", "Bad.mq5": "#error broken"})
    result = compile_(editor, src, tmp_path / "out")
    assert not result.ok
    assert result.compiled == ("Good.mq5",)
    assert result.failed == ("Bad.mq5",)
    assert result.errors == 1
    assert (tmp_path / "out/Good.ex5").exists() and not (tmp_path / "out/Bad.ex5").exists()


def test_compile_needs_metaeditor(editor: Farm, tmp_path: Path) -> None:
    (editor.data / "MetaEditor64.exe").unlink()
    src = sources(tmp_path / "src", {"A.mq5": "// ok"})
    with pytest.raises(CompileError, match=r"metaeditor64\.exe"):
        compile_(editor, src, tmp_path / "out")
    assert not list((editor.data / "MQL5/Experts").glob("mt5ctl-build-*"))


def test_compile_needs_sources(editor: Farm, tmp_path: Path) -> None:
    with pytest.raises(CompileError, match=r"no \.mq5"):
        compile_(editor, sources(tmp_path / "src", {"A.mqh": ""}), tmp_path / "out")
    with pytest.raises(CompileError, match="not found"):
        compile_(editor, tmp_path / "missing", tmp_path / "out")


def test_compile_refuses_symlinks(editor: Farm, tmp_path: Path) -> None:
    src = sources(tmp_path / "src", {"A.mq5": "// ok"})
    (src / "link.mq5").symlink_to(src / "A.mq5")
    with pytest.raises(CompileError, match="symlink"):
        compile_(editor, src, tmp_path / "out")


def test_compile_on_live_needs_confirmation(editor: Farm, tmp_path: Path) -> None:
    editor.registry = make_registry(editor.data, env=Env.LIVE)
    with pytest.raises(ConfirmationRequiredError):
        compile_(editor, sources(tmp_path / "src", {"A.mq5": ""}), tmp_path / "out")


def test_metaeditor_that_never_ran_is_an_error(
    editor: Farm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "wine").write_text(
        '#!/bin/sh\n[ "$1" = winepath ] && { echo "$3"; exit 0; }\nexit 1\n'
    )
    (fake / "wine").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:/usr/bin:/bin")
    src = sources(tmp_path / "src", {"A.mq5": "// ok"})
    with pytest.raises(CompileError, match="did not run"):
        compile_(editor, src, tmp_path / "out")


def test_a_prefix_needs_its_own_wine(editor: Farm, tmp_path: Path) -> None:
    from dataclasses import replace

    term = editor.registry.terminals["demo1"]
    editor.registry.terminals["demo1"] = replace(term, wineprefix="/somewhere/prefix")
    with pytest.raises(CompileError, match="set 'wine'"):
        compile_(editor, sources(tmp_path / "src", {"A.mq5": ""}), tmp_path / "out")


def test_compile_aliases() -> None:
    aliases = operations.compile_aliases(
        ["Plain", "Sub dir", "a/b c"], ["Plain", "Sub dir", "Sub_dir", "a/b c"]
    )
    assert aliases == {"Sub dir": "Sub_dir_1", "a/b c": "a/b_c"}


def test_data_folder_with_a_space_is_refused(farm: Farm, tmp_path: Path) -> None:
    (farm.data / "MetaEditor64.exe").write_bytes(b"MZ")  # the farm's folder has a space
    with pytest.raises(CompileError, match="space in its path"):
        compile_(farm, sources(tmp_path / "src", {"A.mq5": ""}), tmp_path / "out")
