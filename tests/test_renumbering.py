"""The terminal closes gaps in chart numbering when it saves the profile: a deploy
must never take a chart that moved for the one that used to be there."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from builders import EXPERT, chart_bytes, write_tree
from conftest import Farm
from mt5ctl import operations
from mt5ctl.errors import DeployError
from mt5ctl.reconcile import chart_moves
from mt5ctl.results import DeployResult

CHARTS = "MQL5/Profiles/Charts/Default"
WATCHDOG = r"Experts\Tools\Watchdog.ex5"


def deploy(farm: Farm, bundle: Path) -> DeployResult:
    return asyncio.run(operations.deploy(farm.registry, "demo1", bundle, verify_timeout=0))


def test_moves_are_followed_by_what_a_chart_trades() -> None:
    before = {"c1": "A", "c2": "B", "c3": "C"}
    assert chart_moves(before, {"c1": "B", "c2": "C"}) == (("c2", "c1"), ("c3", "c2"))
    assert chart_moves(before, before) == ()
    assert chart_moves(before, {"c1": "A", "c2": "B"}) == ()  # c3 closed: not a move
    # identical charts cannot be told apart: no claim either way
    assert chart_moves({"c1": "A", "c2": "A"}, {"c1": "A", "c3": "A"}) == ()
    assert chart_moves({"c1": None}, {"c2": None}) == ()


def test_renumbered_while_stopping_changes_nothing(farm: Farm, tmp_path: Path) -> None:
    """Managed Trend on chart01, a foreign watchdog on chart02. Trend was closed in
    the terminal; on the way out it saves the watchdog as chart01."""
    write_tree(farm.data, charts={"chart02.chr": chart_bytes(expert_path=WATCHDOG)})
    v1 = write_tree(
        tmp_path / "v1", charts={"chart01.chr": chart_bytes()}, experts={EXPERT: b"1"}
    )
    farm.journal_on_start("Trend")
    deploy(farm, v1)
    farm.hook("on-stop", f'cd "$DATA/{CHARTS}"; mv -f chart02.chr chart01.chr')
    v2 = write_tree(
        tmp_path / "v2", charts={"chart01.chr": chart_bytes()}, experts={EXPERT: b"2"}
    )
    with pytest.raises(DeployError, match=r"renumbered its charts while it shut down"):
        deploy(farm, v2)
    watchdog = (farm.data / CHARTS / "chart01.chr").read_bytes()
    assert watchdog == chart_bytes(expert_path=WATCHDOG)
    assert (farm.data / EXPERT).read_bytes() == b"1"
    assert farm.calls[-1] == "start mt5-demo1.service"


def test_renumbered_since_the_last_deploy_blocks_the_plan(farm: Farm, tmp_path: Path) -> None:
    """Foreign watchdog chart01, managed A chart02 and B chart03. The watchdog was
    closed and the terminal saved A as chart01, B as chart02: a deploy by file
    name would run A twice (chart01 and a new chart02) with one magic number."""
    write_tree(farm.data, charts={"chart01.chr": chart_bytes(expert_path=WATCHDOG)})
    a = chart_bytes(inputs=(("MagicNumber", "1001"),))
    b = chart_bytes(symbol="GBPUSD", inputs=(("MagicNumber", "1002"),))
    bundle = write_tree(
        tmp_path / "b", charts={"chart02.chr": a, "chart03.chr": b}, experts={EXPERT: b"1"}
    )
    farm.journal_on_start("Trend")
    deploy(farm, bundle)
    charts = farm.data / CHARTS
    (charts / "chart01.chr").unlink()
    (charts / "chart02.chr").rename(charts / "chart01.chr")
    (charts / "chart03.chr").rename(charts / "chart02.chr")
    calls = len(farm.calls)
    report = asyncio.run(operations.plan(farm.registry, "demo1", bundle))
    assert report.plan.blocked
    assert report.plan.renumbered == (
        (f"{CHARTS}/chart02.chr", f"{CHARTS}/chart01.chr"),
        (f"{CHARTS}/chart03.chr", f"{CHARTS}/chart02.chr"),
    )
    with pytest.raises(DeployError, match="renumbered"):
        deploy(farm, bundle)
    assert len(farm.calls) == calls  # refused before the terminal was stopped
    assert (charts / "chart01.chr").read_bytes() == a


def test_adopt_after_a_renumbering_unblocks(farm: Farm, tmp_path: Path) -> None:
    write_tree(farm.data, charts={"chart01.chr": chart_bytes(expert_path=WATCHDOG)})
    a = chart_bytes(inputs=(("MagicNumber", "1001"),))
    old = write_tree(tmp_path / "old", charts={"chart02.chr": a}, experts={EXPERT: b"1"})
    farm.journal_on_start("Trend")
    deploy(farm, old)
    charts = farm.data / CHARTS
    (charts / "chart01.chr").unlink()
    (charts / "chart02.chr").rename(charts / "chart01.chr")
    renumbered = write_tree(
        tmp_path / "new", charts={"chart01.chr": a}, experts={EXPERT: b"1"}
    )
    asyncio.run(operations.adopt(farm.registry, "demo1", renumbered))
    report = asyncio.run(operations.plan(farm.registry, "demo1", renumbered))
    assert not report.plan.blocked and not report.plan.has_changes


def test_manifest_without_a_layout_is_not_blocked(farm: Farm, tmp_path: Path) -> None:
    """Manifests written before the layout was recorded keep working."""
    import json

    from mt5ctl.manifest import MANIFEST_RELPATH

    v1 = write_tree(
        tmp_path / "v1", charts={"chart01.chr": chart_bytes()}, experts={EXPERT: b"1"}
    )
    farm.journal_on_start("Trend")
    deploy(farm, v1)
    path = farm.data / MANIFEST_RELPATH
    data = json.loads(path.read_text())
    assert "charts" in data
    del data["charts"]
    path.write_text(json.dumps(data))
    report = asyncio.run(operations.plan(farm.registry, "demo1", v1))
    assert not report.plan.blocked
