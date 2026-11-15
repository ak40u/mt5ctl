"""State collection: local reader, remote script + parser, and their parity.

The remote script is executed here with the local bash against a temporary
data folder, so the shell code is tested for real, not only as text.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
from pathlib import Path

import pytest

from builders import CHART1, CHART2, EXPERT, FIXTURES, chart_bytes, write_manifest, write_tree
from mt5ctl.errors import ManifestError, StateError
from mt5ctl.manifest import Manifest
from mt5ctl.reconcile import TerminalState
from mt5ctl.scripts import sh_quote
from mt5ctl.state import (
    assemble_state,
    build_state_script,
    parse_state_output,
    read_local_state,
)

bash = shutil.which("bash")
needs_bash = pytest.mark.skipif(bash is None, reason="bash not available")


def run_script(script: str) -> str:
    assert bash is not None
    done = subprocess.run([bash], input=script, capture_output=True, text=True, check=True)
    return done.stdout


def remote_state(data_dir: Path, paths: list[str], profile: str = "Default") -> TerminalState:
    out = run_script(build_state_script(str(data_dir), profile, paths))
    return parse_state_output(out, profile, paths)


def terminal(root: Path) -> Path:
    write_tree(
        root,
        charts={
            "chart01.chr": chart_bytes(),
            "chart02.chr": chart_bytes(expert_path=r"Experts\Tools\Watchdog.ex5"),
            "chart03.chr": chart_bytes(expert_path=None),
            "chart04.chr": b"\xff\xfe<\x00",  # truncated: unparsable
        },
        experts={EXPERT: b"ex5", "MQL5/Experts/Tools/Watchdog.ex5": b"wd"},
    )
    write_manifest(root, {CHART1: "chart:x", EXPERT: "sha256:y"})
    (root / "MQL5/Profiles/Charts/Default/order.wnd").write_bytes(
        (FIXTURES / "profile/order.wnd").read_bytes()
    )
    return root


def test_local_state(tmp_path: Path) -> None:
    root = terminal(tmp_path)
    paths = [CHART1, EXPERT, "MQL5/Experts/Portfolio/Absent.ex5"]
    st = read_local_state(root, "Default", paths)
    assert st.manifest == Manifest("Default", {CHART1: "chart:x", EXPERT: "sha256:y"})
    assert set(st.files) == {CHART1, EXPERT}
    assert st.files[EXPERT] is not None and st.files[EXPERT].startswith("sha256:")
    assert st.files[CHART1] is not None and st.files[CHART1].startswith("chart:")
    assert st.charts == {
        CHART1: EXPERT,
        CHART2: "MQL5/Experts/Tools/Watchdog.ex5",
        "MQL5/Profiles/Charts/Default/chart03.chr": None,
        "MQL5/Profiles/Charts/Default/chart04.chr": None,
    }
    assert st.unresolved_charts == {"MQL5/Profiles/Charts/Default/chart04.chr"}
    assert st.order == ("chart04.chr", "chart03.chr", "chart02.chr", "chart01.chr")


def test_local_state_of_an_empty_terminal(tmp_path: Path) -> None:
    st = read_local_state(tmp_path, "Default", [CHART1, EXPERT])
    assert (st.manifest, dict(st.files), dict(st.charts)) == (None, {}, {})


def test_local_state_missing_data_dir(tmp_path: Path) -> None:
    with pytest.raises(StateError, match="not found"):
        read_local_state(tmp_path / "nope", "Default", [])


@needs_bash
def test_remote_script_matches_local_reader(tmp_path: Path) -> None:
    root = terminal(tmp_path)
    paths = [CHART1, CHART2, EXPERT, "MQL5/Experts/Portfolio/Absent.ex5"]
    assert remote_state(root, paths) == read_local_state(root, "Default", paths)


@needs_bash
def test_remote_script_survives_hostile_names(tmp_path: Path) -> None:
    root = tmp_path / "data dir with 'quotes' $HOME `id`"
    profile = "My Profile"
    expert = 'MQL5/Experts/Port folio/It\'s "$(id)" `x` |pipe|.ex5'
    write_tree(
        root,
        charts={"chart 01 |x|.chr": chart_bytes(expert_path=r"Experts\Port folio\Q.ex5")},
        experts={expert: b"payload"},
        profile=profile,
    )
    paths = [expert]
    remote = remote_state(root, paths, profile)
    assert remote == read_local_state(root, profile, paths)
    assert set(remote.files) == {expert}
    assert "MQL5/Profiles/Charts/My Profile/chart 01 |x|.chr" in remote.charts


@needs_bash
def test_remote_script_of_an_empty_terminal(tmp_path: Path) -> None:
    st = remote_state(tmp_path, [CHART1, EXPERT])
    assert (st.manifest, dict(st.files), dict(st.charts)) == (None, {}, {})


def test_parse_rejects_truncated_output() -> None:
    with pytest.raises(StateError, match="truncated"):
        parse_state_output("", "Default", [])


@pytest.mark.parametrize(
    "line", ["MANIFEST|not base64!", "EXPERT|9|abc", "EXPERT|x|abc", "CHART|@@|"]
)
def test_parse_rejects_garbage(line: str) -> None:
    with pytest.raises(StateError):
        parse_state_output(f"MANIFEST|MISSING\n{line}\n", "Default", [EXPERT])


def test_parse_reports_corrupt_manifest() -> None:
    token = base64.b64encode(b"{oops").decode()
    with pytest.raises(ManifestError, match="corrupt"):
        parse_state_output(f"MANIFEST|{token}\nEND|state\n", "Default", [])


def test_unreadable_expert_and_chart_map_to_none() -> None:
    name = base64.b64encode(b"chart01.chr").decode()
    out = f"MANIFEST|MISSING\nCHART|{name}|\nEXPERT|0|\nEND|state\n"
    st = parse_state_output(out, "Default", [CHART1, EXPERT])
    assert st.files == {EXPERT: None, CHART1: None}
    assert st.unresolved_charts == {CHART1}


def test_assemble_flags_expert_with_empty_or_escaping_path() -> None:
    charts = {
        CHART1: chart_bytes(expert_path=""),
        CHART2: chart_bytes(expert_path=r"Experts\..\..\X.ex5"),
        "MQL5/Profiles/Charts/Default/chart03.chr": chart_bytes(
            expert_path=r"Market\Paid.ex5"
        ),
    }
    st = assemble_state(None, charts, {}, [])
    assert st.unresolved_charts == {CHART1, CHART2}
    assert st.charts["MQL5/Profiles/Charts/Default/chart03.chr"] is None


@pytest.mark.parametrize("value", ["plain", "it's", "a b", "$x", "`x`", '"x"', "'", ""])
def test_sh_quote_round_trips_through_bash(value: str) -> None:
    if bash is None:
        pytest.skip("bash not available")
    assert run_script(f"printf '%s' {sh_quote(value)}") == value


def test_cut_off_state_output_is_refused() -> None:
    """Without the end marker, charts may be missing from the answer."""
    name = base64.b64encode(b"chart01.chr").decode()
    with pytest.raises(StateError, match="truncated"):
        parse_state_output(f"MANIFEST|MISSING\nCHART|{name}|\n", "Default", [])


@pytest.mark.parametrize(
    "ref",
    [
        r"Experts\.\Portfolio\Trend.ex5",
        r"experts\portfolio\TREND.EX5",
        r"Experts\\Portfolio\Trend",
    ],
)
def test_foreign_chart_keeps_its_expert_however_the_path_is_written(ref: str) -> None:
    """The terminal (Wine) finds these; a managed expert they run must stay."""
    from mt5ctl.bundle import Bundle
    from mt5ctl.manifest import Manifest
    from mt5ctl.reconcile import compute_plan

    st = assemble_state(
        Manifest("Default", {EXPERT: "sha256:x"}).to_json(),
        {CHART2: chart_bytes(expert_path=ref)},
        {},
        [],
    )
    bundle = Bundle(
        root=Path(),
        profile="Default",
        files={"MQL5/Experts/Other.ex5": "sha256:y"},
        chart_experts={},
    )
    plan = compute_plan(bundle, st)
    assert EXPERT not in plan.remove
    assert plan.keep == (EXPERT,)
