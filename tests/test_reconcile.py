"""compute_plan: managed-subset reconcile rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from mt5ctl.bundle import Bundle
from mt5ctl.errors import ManifestError
from mt5ctl.manifest import Manifest
from mt5ctl.reconcile import DeployPlan, TerminalState, compute_plan

P = "MQL5/Profiles/Charts/Default"
C1, C2, C3, W = f"{P}/chart01.chr", f"{P}/chart02.chr", f"{P}/chart03.chr", f"{P}/chart09.chr"
A, B = "MQL5/Experts/S/A.ex5", "MQL5/Experts/S/B.ex5"
WATCHDOG = "MQL5/Experts/Tools/Watchdog.ex5"


def bundle(files: dict[str, str], experts: dict[str, str] | None = None) -> Bundle:
    return Bundle(
        root=Path("/bundle"), profile="Default", files=files, chart_experts=experts or {}
    )


def state(
    manifest: dict[str, str] | None = None,
    files: dict[str, str | None] | None = None,
    charts: dict[str, str | None] | None = None,
    unresolved: set[str] | None = None,
    profile: str = "Default",
) -> TerminalState:
    return TerminalState(
        manifest=Manifest(profile=profile, files=manifest) if manifest is not None else None,
        files=files or {},
        charts=charts or {},
        unresolved_charts=frozenset(unresolved or ()),
    )


def test_first_deploy_adds_everything() -> None:
    plan = compute_plan(bundle({C1: "chart:1", A: "sha256:a"}, {C1: A}), state())
    assert plan == DeployPlan(add=(A, C1))
    assert plan.has_changes and not plan.blocked


def test_identical_terminal_is_a_no_op() -> None:
    files = {C1: "chart:1", A: "sha256:a"}
    plan = compute_plan(
        bundle(files, {C1: A}), state(manifest=files, files=dict(files), charts={C1: A})
    )
    assert plan.unchanged == (A, C1)
    assert not plan.has_changes
    assert plan.notes == ()


def test_add_update_unchanged_remove() -> None:
    managed = {C1: "chart:1", C2: "chart:2", A: "sha256:a", B: "sha256:b"}
    plan = compute_plan(
        bundle({C1: "chart:1", A: "sha256:a2", C3: "chart:3"}, {C1: A, C3: A}),
        state(
            manifest=managed,
            files={C1: "chart:1", A: "sha256:a"},
            charts={C1: A, C2: B},
        ),
    )
    assert plan.add == (C3,)
    assert plan.update == (A,)
    assert plan.unchanged == (C1,)
    assert plan.remove == (B, C2)
    assert plan.keep == ()
    assert plan.foreign == ()


def test_drift_on_terminal_is_overwritten_and_noted() -> None:
    plan = compute_plan(
        bundle({A: "sha256:new"}),
        state(manifest={A: "sha256:old"}, files={A: "sha256:edited-by-hand"}),
    )
    assert plan.update == (A,)
    assert any("changed on the terminal" in n for n in plan.notes)


def test_planned_update_is_not_reported_as_drift() -> None:
    plan = compute_plan(
        bundle({A: "sha256:new"}), state(manifest={A: "sha256:old"}, files={A: "sha256:old"})
    )
    assert plan.update == (A,)
    assert plan.notes == ()


def test_managed_file_missing_on_terminal_is_placed_again() -> None:
    plan = compute_plan(bundle({A: "sha256:a"}), state(manifest={A: "sha256:a"}))
    assert plan.add == (A,)
    assert any("missing on the terminal" in n for n in plan.notes)


def test_unreadable_managed_file_is_updated() -> None:
    plan = compute_plan(
        bundle({A: "sha256:a"}), state(manifest={A: "sha256:a"}, files={A: None})
    )
    assert plan.update == (A,)


def test_unmanaged_file_at_bundle_path_is_a_conflict() -> None:
    plan = compute_plan(
        bundle({C1: "chart:1", A: "sha256:a"}, {C1: A}),
        state(files={C1: "chart:hand-made", A: "sha256:a"}, charts={C1: WATCHDOG}),
    )
    assert plan.conflicts == (A, C1)
    assert plan.blocked
    assert any("a live chart mt5ctl does not manage" in n for n in plan.notes)
    assert any("a file mt5ctl does not manage" in n for n in plan.notes)


def test_foreign_chart_is_reported_and_its_expert_kept() -> None:
    # A managed expert dropped from the bundle survives while a foreign chart
    # (e.g. a watchdog) still runs it.
    plan = compute_plan(
        bundle({C1: "chart:1", A: "sha256:a"}, {C1: A}),
        state(
            manifest={C1: "chart:1", A: "sha256:a", B: "sha256:b"},
            files={C1: "chart:1", A: "sha256:a"},
            charts={C1: A, W: B},
        ),
    )
    assert plan.foreign == (W,)
    assert plan.keep == (B,)
    assert plan.remove == ()


def test_shared_expert_stays_while_any_bundle_chart_runs_it() -> None:
    plan = compute_plan(
        bundle({C1: "chart:1", A: "sha256:a"}, {C1: A}),
        state(
            manifest={C1: "chart:1", C2: "chart:2", A: "sha256:a"},
            files={C1: "chart:1", A: "sha256:a"},
            charts={C1: A, C2: A},
        ),
    )
    assert plan.remove == (C2,)
    assert plan.unchanged == (A, C1)


def test_unresolved_foreign_chart_blocks_expert_removal() -> None:
    plan = compute_plan(
        bundle({C1: "chart:1"}),
        state(
            manifest={C1: "chart:1", C2: "chart:2", B: "sha256:b"},
            files={C1: "chart:1"},
            charts={C1: None, C2: B, W: None},
            unresolved={W},
        ),
    )
    assert plan.remove == (C2,)
    assert plan.keep == (B,)
    assert any("cannot be resolved" in n for n in plan.notes)


def test_unresolved_managed_chart_does_not_block_removal() -> None:
    # A managed chart is being replaced or removed, so its reference does not
    # survive the deploy, resolvable or not.
    plan = compute_plan(
        bundle({C1: "chart:1"}),
        state(
            manifest={C1: "chart:1", C2: "chart:2", B: "sha256:b"},
            files={C1: "chart:1"},
            charts={C1: None, C2: None},
            unresolved={C2},
        ),
    )
    assert plan.remove == (B, C2)


def test_same_expert_name_in_different_folders_is_distinct() -> None:
    other = "MQL5/Experts/Old/A.ex5"
    plan = compute_plan(
        bundle({C1: "chart:1", A: "sha256:a"}, {C1: A}),
        state(manifest={C1: "chart:0", other: "sha256:a"}, files={C1: "chart:0"}),
    )
    assert plan.add == (A,)
    assert plan.update == (C1,)
    assert plan.remove == (other,)


def test_profile_mismatch_is_refused() -> None:
    with pytest.raises(ManifestError, match="profile 'Live'"):
        compute_plan(bundle({A: "sha256:a"}), state(manifest={}, profile="Live"))


def test_empty_manifest_behaves_like_first_deploy_for_free_paths() -> None:
    plan = compute_plan(bundle({A: "sha256:a"}), state(manifest={}))
    assert plan.add == (A,)
