"""Bundle reading: layout, path safety, integrity, digests."""

from __future__ import annotations

from pathlib import Path

import pytest

from builders import CHART1, EXPERT, chart_bytes, write_tree
from mt5ctl.bundle import (
    classify,
    digest,
    expert_relpath,
    read_bundle,
    validate_member,
    validate_profile,
)
from mt5ctl.errors import BundleError


def make(
    root: Path,
    charts: dict[str, bytes] | None = None,
    experts: dict[str, bytes] | None = None,
    profile: str = "Default",
) -> Path:
    """A one-chart bundle by default; pass ``{}`` to leave charts or experts out."""
    return write_tree(
        root,
        charts={"chart01.chr": chart_bytes()} if charts is None else charts,
        experts={EXPERT: b"ex5-binary"} if experts is None else experts,
        profile=profile,
    )


def test_reads_a_valid_bundle(tmp_path: Path) -> None:
    bundle = read_bundle(make(tmp_path))
    assert bundle.profile == "Default"
    assert bundle.charts == [CHART1]
    assert bundle.experts == [EXPERT]
    assert bundle.chart_experts == {CHART1: EXPERT}
    assert bundle.files[EXPERT].startswith("sha256:")
    assert bundle.files[CHART1].startswith("chart:")


def test_ignores_files_outside_the_layout(tmp_path: Path) -> None:
    root = make(tmp_path)
    (root / "README.md").write_text("notes")
    (root / "MQL5/Experts/Portfolio/Trend.mq5").write_text("source")
    (root / "MQL5/Profiles/Charts/Default/order.wnd").write_bytes(b"x")
    assert sorted(read_bundle(root).files) == [EXPERT, CHART1]


def test_chart_without_expert_is_allowed(tmp_path: Path) -> None:
    root = make(tmp_path, charts={"chart01.chr": chart_bytes(expert_path=None)}, experts={})
    bundle = read_bundle(root)
    assert bundle.chart_experts == {}
    assert bundle.charts == [CHART1]


def test_missing_expert_is_refused(tmp_path: Path) -> None:
    root = make(tmp_path, experts={})
    with pytest.raises(BundleError, match="missing from the bundle"):
        read_bundle(root)


def test_expert_outside_experts_folder_is_refused(tmp_path: Path) -> None:
    root = make(tmp_path, charts={"chart01.chr": chart_bytes(expert_path=r"Market\Paid.ex5")})
    with pytest.raises(BundleError, match="Experts folder"):
        read_bundle(root)


def test_unparsable_chart_is_refused_with_its_path(tmp_path: Path) -> None:
    root = make(tmp_path, charts={"chart01.chr": b"<chart>\n<expert>\n"})
    with pytest.raises(BundleError, match=r"chart01\.chr"):
        read_bundle(root)


def test_symlink_is_refused(tmp_path: Path) -> None:
    root = make(tmp_path)
    (root / "MQL5/Experts/Portfolio/Link.ex5").symlink_to(root / EXPERT)
    with pytest.raises(BundleError, match="symlink"):
        read_bundle(root)


def test_empty_directory_is_not_a_bundle(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    with pytest.raises(BundleError, match="not a bundle"):
        read_bundle(tmp_path)


def test_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(BundleError, match="not found"):
        read_bundle(tmp_path / "nope")


def test_profile_discovery_and_selection(tmp_path: Path) -> None:
    root = make(tmp_path, profile="Portfolio A")
    assert read_bundle(root).profile == "Portfolio A"
    write_tree(root, charts={"chart01.chr": chart_bytes()}, profile="Other")
    with pytest.raises(BundleError, match="several profiles"):
        read_bundle(root)
    chosen = read_bundle(root, profile="Other")
    assert chosen.charts == ["MQL5/Profiles/Charts/Other/chart01.chr"]


def test_bundle_with_only_experts_defaults_to_default_profile(tmp_path: Path) -> None:
    root = make(tmp_path, charts={})
    (root / "MQL5/Profiles/Charts/Default").rmdir()
    bundle = read_bundle(root)
    assert (bundle.profile, bundle.experts) == ("Default", [EXPERT])


# -- path rules -------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("rel", "kind"),
    [
        (CHART1, "chart"),
        (EXPERT, "expert"),
        ("MQL5/Experts/Deep/Nested/X.ex5", "expert"),
        ("MQL5/Experts/X.ex5", "expert"),
        ("MQL5/Profiles/Charts/Default/sub/chart01.chr", None),
        ("MQL5/Profiles/Charts/Other/chart01.chr", None),
        ("MQL5/Experts/X.mq5", None),
        ("MQL5/Indicators/X.ex5", None),
        ("profiles/default/chart01.chr", None),
    ],
)
def test_classify(rel: str, kind: str | None) -> None:
    assert classify(rel, "Default") == kind


@pytest.mark.parametrize(
    "rel",
    [
        "",
        "/MQL5/Experts/X.ex5",
        "MQL5/Experts/../../etc/X.ex5",
        "MQL5/Experts/./X.ex5",
        "MQL5//Experts/X.ex5",
        "MQL5\\Experts\\X.ex5",
        "MQL5/Experts/X\n.ex5",
        "MQL5/Files/X.ex5",
    ],
)
def test_validate_member_refuses_unsafe_paths(rel: str) -> None:
    with pytest.raises(BundleError):
        validate_member(rel, "Default")


def test_validate_member_accepts_spaces_and_quotes() -> None:
    validate_member('MQL5/Experts/Port folio/It\'s "$HOME" `x`.ex5', "Default")


@pytest.mark.parametrize("name", ["Default", "Portfolio A", "demo-1", "v1.2_x"])
def test_valid_profiles(name: str) -> None:
    assert validate_profile(name) == name


@pytest.mark.parametrize(
    "name", ["", " lead", "trail ", "..", ".", "a/b", "a\\b", "x'y", "$x"]
)
def test_invalid_profiles(name: str) -> None:
    with pytest.raises(BundleError):
        validate_profile(name)


@pytest.mark.parametrize(
    ("stored", "rel"),
    [
        (r"Experts\Portfolio\Trend.ex5", "MQL5/Experts/Portfolio/Trend.ex5"),
        (r"Experts\Trend", "MQL5/Experts/Trend.ex5"),
        ("Experts/Trend.ex5", "MQL5/Experts/Trend.ex5"),
        (r"  Experts\\A\\B.ex5 ", "MQL5/Experts/A/B.ex5"),
        ("", None),
        ("Experts", None),
        (r"Market\Paid.ex5", None),
        (r"Experts\..\Indicators\X.ex5", None),
    ],
)
def test_expert_relpath(stored: str, rel: str | None) -> None:
    assert expert_relpath(stored) == rel


def test_digest_dispatches_on_file_kind() -> None:
    assert digest(CHART1, chart_bytes()) == digest(CHART1, chart_bytes(scroll=0))
    assert digest(EXPERT, b"a") != digest(EXPERT, b"b")
    assert digest(EXPERT, b"a").startswith("sha256:")
