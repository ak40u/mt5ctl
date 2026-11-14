"""CLI end to end against a local terminal folder."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from builders import CHART1, CHARTS, EXPERT, chart_bytes, write_manifest, write_tree
from mt5ctl import __version__
from mt5ctl.bundle import read_bundle
from mt5ctl.cli import EXIT_BLOCKED, EXIT_ERROR, EXIT_OK, main


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A bundle, a local terminal folder and a registry pointing at it."""
    bundle = write_tree(
        tmp_path / "bundle", charts={"chart01.chr": chart_bytes()}, experts={EXPERT: b"v2"}
    )
    data = tmp_path / "terminal"
    data.mkdir()
    config = tmp_path / "terminals.yaml"
    config.write_text(
        "hosts:\n  here:\n    kind: local\n"
        f"terminals:\n  demo1:\n    host: here\n    data_dir: '{data}'\n"
    )
    return bundle, data, config


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_chart_show(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["chart", "show", str(CHARTS / "expert-utf16.chr")]) == EXIT_OK
    out = capsys.readouterr().out
    assert "symbol     EURUSD" in out
    assert "timeframe  H4" in out
    assert "utf-16-le+bom, CRLF" in out
    assert "MagicNumber    1001" in out


def test_chart_show_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["chart", "show", "--json", str(CHARTS / "no-expert-utf16.chr")]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["expert"] is None
    assert payload["timeframe"] == "H4"


def test_chart_show_bad_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.chr"
    bad.write_bytes(b"<chart>\n")
    assert main(["chart", "show", str(bad)]) == EXIT_ERROR
    assert "unclosed section" in capsys.readouterr().err


def test_bundle_check(
    setup: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, _, _ = setup
    (bundle / "MQL5/Experts/Portfolio/Unused.ex5").write_bytes(b"x")
    assert main(["bundle", "check", str(bundle)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "bundle OK: profile 'Default', 1 charts, 2 experts" in out
    assert f"chart01.chr  ->  {EXPERT}" in out
    assert "Unused.ex5 is not used by any chart" in out


def test_bundle_check_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["bundle", "check", str(tmp_path)]) == EXIT_ERROR
    assert "not a bundle" in capsys.readouterr().err


def test_plan_first_deploy(
    setup: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, _, config = setup
    assert main(["plan", "demo1", str(bundle), "--config", str(config)]) == EXIT_OK
    out = capsys.readouterr().out
    assert f"  + {EXPERT}" in out
    assert f"  + {CHART1}" in out
    assert "2 to add, 0 to update, 0 to remove, 0 unchanged" in out


def test_plan_no_op_after_deploy_and_terminal_rewrite(
    setup: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, data, config = setup
    # Simulate a deploy, then the terminal rewriting the chart on exit.
    write_tree(data, charts={"chart01.chr": chart_bytes(scroll=0)}, experts={EXPERT: b"v2"})
    write_manifest(data, dict(read_bundle(bundle).files))
    assert main(["plan", "demo1", str(bundle), "--config", str(config), "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["has_changes"] is False
    assert payload["unchanged"] == [EXPERT, CHART1]


def test_plan_blocked_by_unmanaged_chart(
    setup: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, data, config = setup
    write_tree(data, charts={"chart01.chr": chart_bytes(expert_path=None)})
    assert main(["plan", "demo1", str(bundle), "--config", str(config)]) == EXIT_BLOCKED
    out = capsys.readouterr().out
    assert f"  ! {CHART1}" in out
    assert "BLOCKED by 1 conflict(s)" in out


def test_plan_unknown_terminal(
    setup: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, _, config = setup
    assert main(["plan", "demo9", str(bundle), "--config", str(config)]) == EXIT_ERROR
    assert "unknown terminal 'demo9'" in capsys.readouterr().err


def test_list(setup: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    _, data, config = setup
    assert main(["list", "--config", str(config)]) == EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("demo1  demo  here  local")
    assert str(data) in out
    assert main(["list", "--config", str(config), "--json"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)[0]["terminal"] == "demo1"
