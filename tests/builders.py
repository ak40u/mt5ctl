"""Shared builders for charts, bundles and terminal data folders."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from mt5ctl.bundle import charts_dir
from mt5ctl.manifest import MANIFEST_RELPATH, Manifest

FIXTURES = Path(__file__).parent / "fixtures"
CHARTS = FIXTURES / "charts"


def chart_text(
    symbol: str = "EURUSD",
    period: tuple[int, int] = (1, 4),
    expert_path: str | None = r"Experts\Portfolio\Trend.ex5",
    inputs: Sequence[tuple[str, str]] = (("MagicNumber", "1001"),),
    mode: int = 1,
    scroll: int = 1,
) -> str:
    """A small but structurally complete chart (``scroll`` varies display only)."""
    lines = [
        "<chart>",
        "id=133402917718373291",
        f"symbol={symbol}",
        f"period_type={period[0]}",
        f"period_size={period[1]}",
        f"scroll={scroll}",
        "",
    ]
    if expert_path is not None:
        name = expert_path.rsplit("\\", 1)[-1].removesuffix(".ex5")
        lines += [
            "<expert>",
            f"name={name}",
            f"path={expert_path}",
            f"expertmode={mode}",
            "<inputs>",
            *(f"{k}={v}" for k, v in inputs),
            "</inputs>",
            "</expert>",
            "",
        ]
    lines += ["<window>", "height=100.000000", "</window>", "</chart>"]
    return "\r\n".join(lines) + "\r\n"


def chart_bytes(**kwargs: Any) -> bytes:
    """:func:`chart_text` encoded the way the terminal writes it (UTF-16LE + BOM)."""
    return b"\xff\xfe" + chart_text(**kwargs).encode("utf-16-le")


def write_tree(
    root: Path,
    charts: Mapping[str, bytes] | None = None,
    experts: Mapping[str, bytes] | None = None,
    profile: str = "Default",
) -> Path:
    """Write charts (by file name) and experts (by relpath) under *root*."""
    chart_root = root / charts_dir(profile)
    chart_root.mkdir(parents=True, exist_ok=True)
    for name, data in (charts or {}).items():
        (chart_root / name).write_bytes(data)
    for rel, data in (experts or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


def write_manifest(data_dir: Path, files: Mapping[str, str], profile: str = "Default") -> None:
    target = data_dir / MANIFEST_RELPATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(Manifest(profile=profile, files=dict(files)).to_json(), encoding="utf-8")


EXPERT = "MQL5/Experts/Portfolio/Trend.ex5"
CHART1 = "MQL5/Profiles/Charts/Default/chart01.chr"
CHART2 = "MQL5/Profiles/Charts/Default/chart02.chr"


def plant_lock(data: Path, target: str) -> Path:
    """A terminal lock as another mt5ctl run leaves it: ``<holder> <epoch>``."""
    lock = data / ".mt5ctl" / "lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.symlink_to(target)
    return lock
