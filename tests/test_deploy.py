"""deploy, adopt, restart and verify end to end against a local fake farm.

The scripts run in a real bash against a temporary data folder; only systemd
is faked (tests/fakes/systemctl), so stop/drain/apply/start are exercised as on
a host.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import anyio
import pytest

from builders import (
    CHART1,
    CHART2,
    EXPERT,
    FIXTURES,
    chart_bytes,
    plant_lock,
    write_manifest,
    write_tree,
)
from conftest import Farm, make_registry
from mt5ctl import operations
from mt5ctl.bundle import Bundle, parse_order, read_bundle
from mt5ctl.chart import ChartFile
from mt5ctl.errors import (
    ConfirmationRequiredError,
    DeployError,
    ManifestError,
    RemoteCommandError,
)
from mt5ctl.manifest import MANIFEST_RELPATH, parse_manifest
from mt5ctl.models import Env, Host, Terminal
from mt5ctl.results import DeployResult, TerminalStatus
from mt5ctl.transport import CommandResult

BREAKOUT = "MQL5/Experts/Portfolio/Breakout.ex5"
WATCHDOG = "MQL5/Experts/Tools/Watchdog.ex5"
WATCHDOG_CHART = "MQL5/Profiles/Charts/Default/chart03.chr"


def bundle_v1(root: Path) -> Path:
    return write_tree(
        root,
        charts={"chart01.chr": chart_bytes()},
        experts={EXPERT: b"trend v1"},
    )


def bundle_v1b(root: Path) -> Path:
    """Trend back to v1; chart02 keeps a symbol open without an expert."""
    return write_tree(
        root,
        charts={"chart01.chr": chart_bytes(), "chart02.chr": chart_bytes(expert_path=None)},
        experts={EXPERT: b"trend v1"},
    )


def bundle_v2(root: Path) -> Path:
    """Trend updated, Breakout added on chart02."""
    return write_tree(
        root,
        charts={
            "chart01.chr": chart_bytes(),
            "chart02.chr": chart_bytes(expert_path=r"Experts\Portfolio\Breakout.ex5"),
        },
        experts={EXPERT: b"trend v2", BREAKOUT: b"breakout v1"},
    )


def deploy(
    farm: Farm, bundle: Path, *, confirm: bool = False, verify_timeout: float = 2.0
) -> DeployResult:
    return asyncio.run(
        operations.deploy(
            farm.registry,
            "demo1",
            bundle,
            confirm=confirm,
            verify_timeout=verify_timeout,
            verify_interval=0.1,
        )
    )


def manifest(farm: Farm) -> dict[str, str]:
    parsed = parse_manifest((farm.data / MANIFEST_RELPATH).read_text())
    assert parsed is not None
    return dict(parsed.files)


def test_first_deploy_places_files_and_restarts(farm: Farm, tmp_path: Path) -> None:
    bundle = bundle_v1(tmp_path / "v1")
    farm.journal_on_start("Trend")
    result = deploy(farm, bundle)
    assert result.plan.add == (EXPERT, CHART1)
    assert result.restarted and result.backup is None
    assert result.verify is not None and result.verify.healthy, result.verify
    assert (farm.data / EXPERT).read_bytes() == b"trend v1"
    assert (farm.data / CHART1).read_bytes() == chart_bytes()
    assert manifest(farm) == dict(read_bundle(bundle).files)
    assert farm.calls == ["stop mt5-demo1.service", "start mt5-demo1.service"]
    assert not os.path.lexists(farm.data / ".mt5ctl/lock")
    assert not list((farm.data / ".mt5ctl/staging").glob("*"))


def test_redeploy_is_a_no_op_without_restart(farm: Farm, tmp_path: Path) -> None:
    bundle = bundle_v1(tmp_path / "v1")
    farm.journal_on_start("Trend")
    deploy(farm, bundle)
    result = deploy(farm, bundle)
    assert not result.plan.has_changes
    assert not result.restarted
    assert result.verify is not None and result.verify.healthy
    assert farm.calls == ["stop mt5-demo1.service", "start mt5-demo1.service"]


def test_update_add_remove_keep_foreign_and_back_up(farm: Farm, tmp_path: Path) -> None:
    farm.journal_on_start("Trend", "Breakout")
    # A watchdog attached by hand on chart03, foreign to mt5ctl.
    write_tree(
        farm.data,
        charts={"chart03.chr": chart_bytes(expert_path=r"Experts\Tools\Watchdog.ex5")},
        experts={WATCHDOG: b"wd"},
    )
    deploy(farm, bundle_v2(tmp_path / "v2"))
    result = deploy(farm, bundle_v1b(tmp_path / "v1b"))
    assert result.plan.update == (EXPERT, CHART2)
    assert result.plan.remove == (BREAKOUT,)
    assert result.plan.foreign == (WATCHDOG_CHART,)
    assert not (farm.data / BREAKOUT).exists()
    assert ChartFile.read(farm.data / CHART2).expert is None
    assert (farm.data / WATCHDOG_CHART).exists() and (farm.data / WATCHDOG).exists()
    assert (farm.data / EXPERT).read_bytes() == b"trend v1"
    assert result.backup is not None and Path(result.backup).is_file()


def test_removing_a_middle_chart_is_refused(farm: Farm, tmp_path: Path) -> None:
    """chart02 gone with a foreign chart03 left: the terminal would rename chart03."""
    farm.journal_on_start("Trend", "Breakout")
    write_tree(farm.data, charts={"chart03.chr": chart_bytes(expert_path=None)})
    deploy(farm, bundle_v2(tmp_path / "v2"))
    calls = list(farm.calls)
    with pytest.raises(DeployError, match=r"gaps in chart numbering \(chart02\.chr missing\)"):
        deploy(farm, bundle_v1(tmp_path / "v1"))
    assert farm.calls == calls
    assert (farm.data / CHART2).exists()


def test_charts_saved_on_stop_are_re_placed(farm: Farm, tmp_path: Path) -> None:
    """The terminal may save a chart without its expert while stopping; the
    deploy re-plans after the stop and puts the chart back."""
    bundle = bundle_v2(tmp_path / "v2")
    farm.journal_on_start("Trend", "Breakout")
    deploy(farm, bundle)
    lost = farm.systemd / "lost.chr"
    lost.write_bytes(chart_bytes(expert_path=None))
    farm.hook("on-stop", f"cp '{lost}' \"$DATA/{CHART2}\"")
    # Change something else so the deploy has a reason to stop the terminal.
    write_tree(bundle, experts={EXPERT: b"trend v3"})
    result = deploy(farm, bundle)
    assert set(result.plan.update) == {EXPERT, CHART2}
    chart = ChartFile.read(farm.data / CHART2)
    assert chart.expert is not None and chart.expert.path.endswith("Breakout.ex5")


def test_display_changes_saved_on_stop_are_not_changes(farm: Farm, tmp_path: Path) -> None:
    bundle = bundle_v1(tmp_path / "v1")
    farm.journal_on_start("Trend")
    deploy(farm, bundle)
    (farm.data / CHART1).write_bytes(chart_bytes(scroll=0))
    assert not deploy(farm, bundle).plan.has_changes


def test_conflict_refuses_before_stopping(farm: Farm, tmp_path: Path) -> None:
    write_tree(farm.data, charts={"chart01.chr": chart_bytes(expert_path=None)})
    with pytest.raises(DeployError, match="does not manage"):
        deploy(farm, bundle_v1(tmp_path / "v1"))
    assert farm.calls == []


def test_failed_apply_restores_and_restarts(
    farm: Farm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    farm.journal_on_start("Trend")
    deploy(farm, bundle_v1(tmp_path / "v1"))
    before = {p: (farm.data / p).read_bytes() for p in (EXPERT, CHART1, MANIFEST_RELPATH)}

    real = operations.build_upload

    def corrupt(
        bundle: Bundle, paths: Iterable[str], generated: Mapping[str, bytes] | None = None
    ) -> tuple[bytes, dict[str, str]]:
        tar, shas = real(bundle, paths, generated)
        return tar, dict.fromkeys(shas, "0" * 64)

    monkeypatch.setattr(operations, "build_upload", corrupt)
    with pytest.raises(DeployError, match="restored"):
        deploy(farm, bundle_v2(tmp_path / "v2"))
    assert {p: (farm.data / p).read_bytes() for p in before} == before
    assert not (farm.data / CHART2).exists() and not (farm.data / BREAKOUT).exists()
    assert farm.calls[-1] == "start mt5-demo1.service"


def test_failed_first_apply_leaves_no_manifest(
    farm: Farm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = operations.build_upload
    monkeypatch.setattr(
        operations,
        "build_upload",
        lambda b, p, g=None: (real(b, p, g)[0], dict.fromkeys([*p, *(g or {})], "0" * 64)),
    )
    with pytest.raises(DeployError):
        deploy(farm, bundle_v1(tmp_path / "v1"))
    assert not (farm.data / MANIFEST_RELPATH).exists()
    assert not (farm.data / EXPERT).exists()


def test_terminal_that_does_not_exit_aborts_before_writing(
    farm: Farm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(operations, "DRAIN_TIMEOUT_S", 2)
    farm.flag("stuck")
    with pytest.raises(DeployError, match="did not exit"):
        deploy(farm, bundle_v1(tmp_path / "v1"))
    assert not (farm.data / EXPERT).exists()
    assert farm.calls == ["stop mt5-demo1.service", "start mt5-demo1.service"]


def test_failed_stop_is_an_error_and_writes_nothing(farm: Farm, tmp_path: Path) -> None:
    farm.flag("fail-stop")
    with pytest.raises(DeployError, match="systemctl stop"):
        deploy(farm, bundle_v1(tmp_path / "v1"))
    assert not (farm.data / EXPERT).exists()


def test_unhealthy_start_is_reported_not_rolled_back(farm: Farm, tmp_path: Path) -> None:
    farm.journal_on_start(authorized=False)  # no expert loads
    result = deploy(farm, bundle_v1(tmp_path / "v1"), verify_timeout=0.3)
    assert result.verify is not None and not result.verify.healthy
    assert result.verify.pending == ("Trend",)
    assert (farm.data / EXPERT).exists()


def test_live_terminal_needs_confirmation(farm: Farm, tmp_path: Path) -> None:
    farm.registry = make_registry(farm.data, env=Env.LIVE)
    with pytest.raises(ConfirmationRequiredError):
        deploy(farm, bundle_v1(tmp_path / "v1"))
    farm.journal_on_start("Trend")
    assert deploy(farm, bundle_v1(tmp_path / "v1"), confirm=True).restarted


def test_deploy_needs_a_service(farm: Farm, tmp_path: Path) -> None:
    farm.registry = make_registry(farm.data, service=None)
    with pytest.raises(DeployError, match="systemd unit"):
        deploy(farm, bundle_v1(tmp_path / "v1"))


def test_held_lock_refuses(farm: Farm, tmp_path: Path) -> None:
    plant_lock(farm.data, "other:1:abc 9999999999")
    with pytest.raises(DeployError, match="holds the lock"):
        deploy(farm, bundle_v1(tmp_path / "v1"))
    assert farm.calls == []


def test_stale_lock_is_taken_over(farm: Farm, tmp_path: Path) -> None:
    plant_lock(farm.data, "other:1:abc 1000")
    farm.journal_on_start("Trend")
    assert deploy(farm, bundle_v1(tmp_path / "v1")).restarted


# -- adopt ------------------------------------------------------------------- #


def test_adopt_records_what_runs_and_deploy_continues_from_there(
    farm: Farm, tmp_path: Path
) -> None:
    bundle = bundle_v1(tmp_path / "v1")
    # The terminal already runs this bundle, with an older Trend and a watchdog.
    write_tree(
        farm.data,
        charts={
            "chart01.chr": chart_bytes(scroll=0),
            "chart02.chr": chart_bytes(expert_path=r"Experts\Tools\Watchdog.ex5"),
        },
        experts={EXPERT: b"trend v0", WATCHDOG: b"wd"},
    )
    result = asyncio.run(operations.adopt(farm.registry, "demo1", bundle))
    assert result.adopted == (EXPERT, CHART1)
    assert result.drifted == (EXPERT,)
    assert result.foreign == (CHART2,)
    assert farm.calls == []
    report = asyncio.run(operations.plan(farm.registry, "demo1", bundle))
    assert report.plan.update == (EXPERT,)
    assert report.plan.unchanged == (CHART1,)
    assert report.plan.foreign == (CHART2,)
    assert not report.plan.blocked


def test_adopt_refuses_a_bundle_the_terminal_does_not_run(farm: Farm, tmp_path: Path) -> None:
    write_tree(farm.data, charts={"chart01.chr": chart_bytes()})
    with pytest.raises(DeployError, match=r"missing: MQL5/Experts/Portfolio/Trend\.ex5"):
        asyncio.run(operations.adopt(farm.registry, "demo1", bundle_v1(tmp_path / "v1")))
    assert not (farm.data / MANIFEST_RELPATH).exists()


def test_adopt_refuses_another_profile(farm: Farm, tmp_path: Path) -> None:
    bundle = bundle_v1(tmp_path / "v1")
    write_tree(farm.data, charts={"chart01.chr": chart_bytes()}, experts={EXPERT: b"x"})
    write_manifest(farm.data, {}, profile="Live")
    with pytest.raises(DeployError, match="profile 'Live'"):
        asyncio.run(operations.adopt(farm.registry, "demo1", bundle))


# -- restart, verify, status ----------------------------------------------- #


def test_restart_waits_for_health(farm: Farm, tmp_path: Path) -> None:
    farm.journal_on_start("Trend")
    deploy(farm, bundle_v1(tmp_path / "v1"))
    result = asyncio.run(operations.restart(farm.registry, "demo1", verify_timeout=2.0))
    assert result.verify.healthy
    assert result.verify.loaded == ("Trend",)
    assert farm.calls[-1] == "restart mt5-demo1.service"


def test_restart_reports_failure(farm: Farm) -> None:
    farm.flag("fail-restart")
    with pytest.raises(DeployError, match="systemctl restart"):
        asyncio.run(operations.restart(farm.registry, "demo1"))


def test_restart_live_needs_confirmation(farm: Farm) -> None:
    farm.registry = make_registry(farm.data, env=Env.LIVE)
    with pytest.raises(ConfirmationRequiredError):
        asyncio.run(operations.restart(farm.registry, "demo1"))
    assert farm.calls == []


def test_status_reads_service_and_journal(farm: Farm, tmp_path: Path) -> None:
    farm.journal_on_start("Trend")
    deploy(farm, bundle_v1(tmp_path / "v1"))
    (st,) = asyncio.run(operations.status(farm.registry))
    assert st.service_state == "active"
    assert st.connected is True  # from the journal: sockets cannot be attributed here
    assert st.last_event is not None and "authorized on Broker-Demo" in st.last_event
    assert st.journal_age_seconds is not None and st.journal_age_seconds < 60
    assert st.healthy


def test_status_of_a_stopped_terminal_without_journal(farm: Farm) -> None:
    farm.set_state("inactive")
    (st,) = asyncio.run(operations.status(farm.registry, ["demo1"]))
    assert (st.service_state, st.connected, st.journal_age_seconds) == ("inactive", None, None)
    assert not st.healthy


def test_verify_without_restart(farm: Farm, tmp_path: Path) -> None:
    farm.journal_on_start("Trend")
    deploy(farm, bundle_v1(tmp_path / "v1"))
    report = asyncio.run(operations.verify(farm.registry, "demo1"))
    assert report.healthy and report.loaded == ("Trend",)


def test_evaluate_verify_flags_failed_experts() -> None:
    st = TerminalStatus("t", "h", Env.DEMO, "active", connected=None)
    journal = (
        "authorized on Broker\n"
        "expert Trend (EURUSD,H4) loaded successfully\n"
        "expert Breakout (GBPUSD,H1) loaded successfully\n"
        "initializing of Breakout (GBPUSD,H1) failed with code 1\n"
        "expert Breakout (GBPUSD,H1) removed\n"
    )
    report = operations.evaluate_verify(st, journal, {"Trend", "Breakout", "Missing"})
    assert report.loaded == ("Trend",)
    assert report.failed == ("Breakout",)
    assert report.pending == ("Missing",)
    assert report.connected is True and not report.healthy
    assert "failed: Breakout" in report.detail


def test_journal_connection_takes_the_latest_event() -> None:
    assert operations.journal_connection("authorized on A\nconnection to A lost\n")[0] is False
    assert operations.journal_connection("no connection\nauthorized on A\n")[0] is True
    assert operations.journal_connection("nothing relevant\n") == (None, None)


def test_manifest_written_by_deploy_is_canonical(farm: Farm, tmp_path: Path) -> None:
    farm.journal_on_start("Trend")
    deploy(farm, bundle_v1(tmp_path / "v1"))
    raw = json.loads((farm.data / MANIFEST_RELPATH).read_text())
    assert raw["version"] == 1 and raw["profile"] == "Default"


# -- regressions: failure and cancellation paths ----------------------------- #


def lock_held(farm: Farm) -> bool:
    return os.path.lexists(farm.data / ".mt5ctl/lock")


def test_cancelled_deploy_still_starts_the_terminal_and_unlocks(
    farm: Farm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An MCP client giving up mid-deploy cancels the call through an anyio scope."""
    real_drain = operations._drain
    scopes: list[anyio.CancelScope] = []

    async def drain_then_cancel(host: Host, term: Terminal) -> None:
        await real_drain(host, term)
        scopes[0].cancel()
        await anyio.sleep(1)

    monkeypatch.setattr(operations, "_drain", drain_then_cancel)
    farm.journal_on_start("Trend")

    async def main() -> None:
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            await operations.deploy(farm.registry, "demo1", bundle_v1(tmp_path / "v1"))

    anyio.run(main)
    assert farm.calls == ["stop mt5-demo1.service", "start mt5-demo1.service"]
    assert not lock_held(farm)


def test_interrupted_cli_deploy_still_starts_the_terminal(
    farm: Farm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C under asyncio.run cancels the task natively."""
    real_drain = operations._drain

    async def drain_then_cancel(host: Host, term: Terminal) -> None:
        await real_drain(host, term)
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await asyncio.sleep(1)

    monkeypatch.setattr(operations, "_drain", drain_then_cancel)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(operations.deploy(farm.registry, "demo1", bundle_v1(tmp_path / "v1")))
    assert farm.calls == ["stop mt5-demo1.service", "start mt5-demo1.service"]
    assert not lock_held(farm)


def test_stop_that_errors_after_stopping_is_followed_by_start(
    farm: Farm, tmp_path: Path
) -> None:
    farm.flag("fail-after-stop")
    with pytest.raises(DeployError, match="systemctl stop"):
        deploy(farm, bundle_v1(tmp_path / "v1"))
    assert farm.calls == ["stop mt5-demo1.service", "start mt5-demo1.service"]
    assert (farm.systemd / "mt5-demo1.service.state").read_text().strip() == "active"
    assert not (farm.data / EXPERT).exists() and not lock_held(farm)


def test_restart_refuses_while_a_deploy_holds_the_lock(farm: Farm) -> None:
    plant_lock(farm.data, "deploy:1:abc 9999999999")
    with pytest.raises(DeployError, match="holds the lock"):
        asyncio.run(operations.restart(farm.registry, "demo1"))
    assert farm.calls == []


def test_manifest_path_outside_the_layout_is_never_removed(farm: Farm, tmp_path: Path) -> None:
    victim = farm.data.parent / "victim.txt"
    victim.write_text("keep me")
    manifest_path = farm.data / MANIFEST_RELPATH
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {"version": 1, "profile": "Default", "files": {"../victim.txt": "sha256:x"}}
        )
    )
    with pytest.raises(ManifestError, match="outside the managed layout"):
        deploy(farm, bundle_v1(tmp_path / "v1"))
    assert victim.read_text() == "keep me"
    assert farm.calls == []


def test_kept_expert_stays_managed(farm: Farm, tmp_path: Path) -> None:
    farm.journal_on_start("Trend", "Breakout")
    v2 = bundle_v2(tmp_path / "v2")
    deploy(farm, v2)
    # A foreign chart starts running Breakout; v1b drops it from the bundle.
    write_tree(
        farm.data,
        charts={"chart03.chr": chart_bytes(expert_path=r"Experts\Portfolio\Breakout.ex5")},
    )
    result = deploy(farm, bundle_v1b(tmp_path / "v1b"))
    assert result.plan.keep == (BREAKOUT,)
    assert BREAKOUT in manifest(farm)
    # Bringing it back is an ordinary deploy, not a conflict.
    again = deploy(farm, v2)
    assert not again.plan.blocked
    assert BREAKOUT in again.plan.unchanged


def test_failed_restore_is_reported_as_such(
    farm: Farm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = operations.build_upload
    monkeypatch.setattr(
        operations,
        "build_upload",
        lambda b, p, g=None: (real(b, p, g)[0], dict.fromkeys([*p, *(g or {})], "0" * 64)),
    )
    monkeypatch.setattr(operations.scripts, "build_restore_script", lambda *a: "exit 1")
    with pytest.raises(DeployError, match="restore did not complete"):
        deploy(farm, bundle_v1(tmp_path / "v1"))
    assert farm.calls[-1] == "start mt5-demo1.service"


def test_unreadable_journal_cursor_does_not_block_the_start(
    farm: Farm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_run = operations.transport.run

    async def flaky(host: Host, script: str, **kwargs: Any) -> CommandResult:
        if "CURSOR|" in script:
            raise RemoteCommandError("here", 124, "timed out after 60s")
        return await real_run(host, script, **kwargs)

    monkeypatch.setattr(operations.transport, "run", flaky)
    farm.journal_on_start("Trend")
    assert deploy(farm, bundle_v1(tmp_path / "v1")).restarted
    assert farm.calls[-1] == "start mt5-demo1.service"


def test_no_change_deploy_after_the_journal_rolled_over(farm: Farm, tmp_path: Path) -> None:
    bundle = bundle_v1(tmp_path / "v1")
    farm.journal_on_start("Trend")
    deploy(farm, bundle)
    logs = farm.data / "logs"
    (logs / "20261115.log").rename(logs / "20261114.log")
    (logs / "20261115.log").write_bytes("0\t00:00:01\tNetwork\tping\r\n".encode("utf-16-le"))
    result = deploy(farm, bundle)
    assert not result.restarted
    assert result.verify is not None and result.verify.healthy, result.verify


def test_deploy_called_while_handling_an_exception_still_verifies(
    farm: Farm, tmp_path: Path
) -> None:
    farm.journal_on_start("Trend")
    try:
        raise ValueError("unrelated")
    except ValueError:
        result = deploy(farm, bundle_v1(tmp_path / "v1"))
    assert result.verify is not None and result.verify.healthy


def test_verify_timeout_is_bounded(farm: Farm, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="between 0 and"):
        deploy(farm, bundle_v1(tmp_path / "v1"), verify_timeout=10_000)
    assert farm.calls == []


# -- window order file --------------------------------------------------------- #


ORDER = "MQL5/Profiles/Charts/Default/order.wnd"


def test_deploy_keeps_the_order_file_in_step(farm: Farm, tmp_path: Path) -> None:
    # A real order file (it lists chart04 .. chart01) and a foreign chart03.
    write_tree(farm.data, charts={"chart03.chr": chart_bytes(expert_path=None)})
    (farm.data / ORDER).write_bytes((FIXTURES / "profile/order.wnd").read_bytes())
    farm.journal_on_start("Trend", "Breakout")
    deploy(farm, bundle_v2(tmp_path / "v2"))
    data = (farm.data / ORDER).read_bytes()
    assert data.startswith(b"\xff\xfe") and data.endswith("\r\n".encode("utf-16-le"))
    # The entry of a chart that does not exist is dropped; the rest keep their places.
    assert parse_order(data) == ("chart03.chr", "chart02.chr", "chart01.chr")


def test_order_file_lists_new_charts(farm: Farm, tmp_path: Path) -> None:
    farm.journal_on_start("Trend")
    deploy(farm, bundle_v1(tmp_path / "v1"))
    assert parse_order((farm.data / ORDER).read_bytes()) == ("chart01.chr",)
    farm.journal_on_start("Trend", "Breakout")
    deploy(farm, bundle_v2(tmp_path / "v2"))
    assert parse_order((farm.data / ORDER).read_bytes()) == ("chart01.chr", "chart02.chr")


def test_failed_apply_restores_the_order_file(
    farm: Farm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    farm.journal_on_start("Trend")
    deploy(farm, bundle_v1(tmp_path / "v1"))
    before = (farm.data / ORDER).read_bytes()
    real = operations.build_upload
    monkeypatch.setattr(
        operations,
        "build_upload",
        lambda b, p, g=None: (real(b, p, g)[0], dict.fromkeys([*p, *(g or {})], "0" * 64)),
    )
    with pytest.raises(DeployError, match="restored"):
        deploy(farm, bundle_v2(tmp_path / "v2"))
    assert (farm.data / ORDER).read_bytes() == before


# -- start, stop, and what verify requires after a start --------------------- #


def test_stop_then_start(farm: Farm) -> None:
    farm.journal_on_start()
    asyncio.run(operations.stop(farm.registry, "demo1"))
    assert (farm.systemd / "mt5-demo1.service.state").read_text().strip() == "inactive"
    result = asyncio.run(operations.start(farm.registry, "demo1", verify_timeout=2.0))
    assert result.verify.healthy and result.verify.started is True
    assert farm.calls == ["stop mt5-demo1.service", "start mt5-demo1.service"]
    assert not lock_held(farm)


def test_start_without_a_journaled_start_is_not_healthy(farm: Farm) -> None:
    """A unit that is active but whose terminal never logged its start."""
    farm.set_state("inactive")
    result = asyncio.run(operations.start(farm.registry, "demo1", verify_timeout=0.3))
    assert result.verify.started is False and not result.verify.healthy
    assert "start not in the journal yet" in result.verify.detail


def test_stop_live_needs_confirmation(farm: Farm) -> None:
    farm.registry = make_registry(farm.data, env=Env.LIVE)
    with pytest.raises(ConfirmationRequiredError):
        asyncio.run(operations.stop(farm.registry, "demo1"))
    assert farm.calls == []


def test_journal_errors_are_reported() -> None:
    st = TerminalStatus("t", "h", Env.DEMO, "active", connected=True)
    journal = (
        'QR\t2\t11:41:43.557\tTerminal\tcannot load config "cfg\\login.ini" at start\n'
        "HJ\t0\t11:41:45.227\tTerminal\tMetaTrader 5 x64 build 6320 started\n"
    )
    report = operations.evaluate_verify(st, journal, set(), require_start=True)
    assert report.started is True and report.healthy
    assert report.errors == ('Terminal\tcannot load config "cfg\\login.ini" at start',)
    assert "1 error(s) in the journal" in report.detail


def test_stop_reports_a_clean_exit(farm: Farm) -> None:
    result = asyncio.run(operations.stop(farm.registry, "demo1"))
    assert result.clean and result.exit_code == 0


def test_unclean_stop_is_reported(farm: Farm, tmp_path: Path) -> None:
    """systemd had to end the terminal: no exit line in its journal."""
    farm.flag("killed")
    (farm.systemd / "mt5-demo1.service.result").write_text("timeout\n")
    result = asyncio.run(operations.stop(farm.registry, "demo1"))
    assert not result.clean and result.exit_code is None
    assert result.unit_result == "timeout"
    farm.set_state("active")
    farm.journal_on_start("Trend")
    deployed = deploy(farm, bundle_v1(tmp_path / "v1"))
    assert deployed.stopped_cleanly is False
    from mt5ctl.formatting import deploy_lines

    assert any("wrote no exit line" in line for line in deploy_lines(deployed))


def test_clean_deploy_stop(farm: Farm, tmp_path: Path) -> None:
    farm.journal_on_start("Trend")
    deployed = deploy(farm, bundle_v1(tmp_path / "v1"))
    assert deployed.stopped_cleanly is True
    assert deployed.stop is not None and deployed.stop.exit_code == 0


def test_stopping_a_stopped_terminal_is_clean(farm: Farm, tmp_path: Path) -> None:
    farm.set_state("inactive")
    farm.flag("killed")  # it writes no exit line: it was not running
    result = asyncio.run(operations.stop(farm.registry, "demo1"))
    assert not result.was_running and result.clean
    farm.set_state("inactive")
    farm.journal_on_start("Trend")
    deployed = deploy(farm, bundle_v1(tmp_path / "v1"))
    assert deployed.stopped_cleanly is True


def test_verify_stops_waiting_when_the_outcome_is_settled(farm: Farm, tmp_path: Path) -> None:
    """Investor mode: every expert loaded, connected, trading disabled -> no point waiting."""
    import time as _time

    farm.journal_on_start("Trend")
    with (farm.systemd / "journal.bin").open("ab") as fh:
        line = "0\t12:00:03.000\tNetwork\t'1': trading has been disabled - investor mode\r\n"
        fh.write(line.encode("utf-16-le"))
    began = _time.monotonic()
    result = deploy(farm, bundle_v1(tmp_path / "v1"), verify_timeout=30.0)
    assert _time.monotonic() - began < 15
    assert result.verify is not None and result.verify.trading_disabled == "investor mode"
    assert not result.verify.healthy


def test_status_of_a_stopped_terminal_ignores_its_old_journal(
    farm: Farm, tmp_path: Path
) -> None:
    farm.journal_on_start("Trend")
    with (farm.systemd / "journal.bin").open("ab") as fh:
        line = "0\t12:00:03.000\tNetwork\t'1': trading has been disabled - investor mode\r\n"
        fh.write(line.encode("utf-16-le"))
    deploy(farm, bundle_v1(tmp_path / "v1"))
    farm.set_state("inactive")
    (st,) = asyncio.run(operations.status(farm.registry))
    assert (st.connected, st.trading_disabled, st.algo_trading) == (None, None, None)


def test_status_judges_the_connection_by_the_current_session(farm: Farm) -> None:
    """Without sockets to go by, an earlier session's login is not a connection now."""
    lines = [
        "0\t11:00:00.000\tTerminal\tMetaTrader 5 x64 build 6320 started for MetaQuotes",
        "0\t11:00:01.000\tNetwork\t'1': authorized on Broker-Demo",
        "0\t11:30:00.000\tTerminal\texit with code 0",
        "0\t12:00:00.000\tTerminal\tMetaTrader 5 x64 build 6320 started for MetaQuotes",
    ]
    logs = farm.data / "logs"
    logs.mkdir()
    (logs / "20261115.log").write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-16-le"))
    (st,) = asyncio.run(operations.status(farm.registry))
    assert st.connected is None
