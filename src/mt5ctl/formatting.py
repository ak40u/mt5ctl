"""Human-readable and JSON renderings shared by the CLI and the MCP server."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .reconcile import DeployPlan
from .results import (
    AdoptResult,
    CompileResult,
    DeployResult,
    RestartResult,
    StopResult,
    TerminalStatus,
    VerifyReport,
)

UNCLEAN_STOP = (
    "the terminal wrote no exit line to its journal: systemd ended it, so it may not "
    "have saved its profile. Check that ExecStop asks it to close and waits, on its "
    "own DISPLAY, and that no dialog was open (see docs/deploy.md)"
)


def plan_lines(plan: DeployPlan) -> list[str]:
    lines: list[str] = []
    for marker, paths in (
        ("+", plan.add),
        ("~", plan.update),
        ("-", plan.remove),
        ("!", plan.conflicts),
    ):
        lines.extend(f"  {marker} {p}" for p in paths)
    lines.extend(f"  ! gap: no {name}" for name in plan.gaps)
    lines.extend(f"  keep {p} (still used by a remaining chart)" for p in plan.keep)
    lines.extend(f"  foreign {p} (not managed, left alone)" for p in plan.foreign)
    if plan.order is not None and plan.has_changes:
        lines.append(f"  window order: {len(plan.order)} charts listed")
    if plan.notes:
        lines.append("notes:")
        lines.extend(f"  {n}" for n in plan.notes)
    summary = (
        f"{len(plan.add)} to add, {len(plan.update)} to update, "
        f"{len(plan.remove)} to remove, {len(plan.unchanged)} unchanged"
    )
    if plan.blocked:
        problems = []
        if plan.conflicts:
            problems.append(f"{len(plan.conflicts)} conflict(s)")
        if plan.gaps:
            problems.append(f"{len(plan.gaps)} numbering gap(s)")
        summary += "; BLOCKED by " + " and ".join(problems)
    elif not plan.has_changes:
        summary += "; nothing to do"
    lines.append(summary)
    return lines


def plan_payload(plan: DeployPlan) -> dict[str, Any]:
    payload: dict[str, Any] = {
        k: list(v) if v is not None else None for k, v in asdict(plan).items()
    }
    payload["has_changes"] = plan.has_changes
    payload["blocked"] = plan.blocked
    return payload


def verify_payload(report: VerifyReport | None) -> dict[str, Any] | None:
    if report is None:
        return None
    payload = asdict(report)
    payload["detail"] = report.detail
    return payload


def deploy_lines(result: DeployResult) -> list[str]:
    lines = [f"deploy {result.terminal}:", *plan_lines(result.plan)]
    if result.backup:
        lines.append(f"backup: {result.backup}")
    if result.stop is not None and not result.stop.clean:
        lines.append(f"warning: {UNCLEAN_STOP}")
    lines.append("restarted" if result.restarted else "not restarted (nothing changed)")
    if result.verify is not None:
        state = "healthy" if result.verify.healthy else "NOT healthy"
        lines.append(f"verify: {state} — {result.verify.detail}")
        if result.verify.pending:
            lines.append("  not loaded yet: " + ", ".join(result.verify.pending))
        lines.extend(f"  journal error: {e}" for e in result.verify.errors[:5])
    return lines


def deploy_payload(result: DeployResult) -> dict[str, Any]:
    return {
        "terminal": result.terminal,
        "plan": plan_payload(result.plan),
        "restarted": result.restarted,
        "backup": result.backup,
        "verify": verify_payload(result.verify),
    }


def adopt_lines(result: AdoptResult) -> list[str]:
    lines = [f"adopted {len(result.adopted)} files on {result.terminal}"]
    lines.extend(
        f"  drifted {p} (differs from the bundle; deploy will update it)"
        for p in result.drifted
    )
    lines.extend(f"  foreign {p} (left unmanaged)" for p in result.foreign)
    lines.append(f"manifest: {result.manifest_path}")
    return lines


def duration(seconds: int) -> str:
    """Compact human duration: ``45s``, ``12m``, ``3h05m``, ``2d04h``."""
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


def status_lines(rows: list[TerminalStatus]) -> list[str]:
    conn = {True: "connected", False: "DISCONNECTED", None: "unknown"}
    out = []
    for st in rows:
        if st.error:
            out.append(f"{st.terminal:<14} ERROR {st.error}")
            continue
        if st.service_state != "active":
            uptime = "-"
        elif st.active_seconds is None:
            uptime = "up ?"
        else:
            uptime = f"up {duration(st.active_seconds)}"
        age = (
            f"journal {duration(st.journal_age_seconds)} ago"
            if st.journal_age_seconds is not None
            else "no journal"
        )
        line = (
            f"{st.terminal:<14} {st.env.value:<4} {st.service_state:<10} "
            f"{conn[st.connected]:<12} {uptime:<12} {age}"
        )
        if st.trading_disabled is not None:
            line += f"  TRADING DISABLED ({st.trading_disabled})"
        out.append(line)
    return out


def status_payload(rows: list[TerminalStatus]) -> list[dict[str, Any]]:
    return [asdict(st) | {"healthy": st.healthy} for st in rows]


def restart_lines(result: RestartResult, verb: str = "restarted") -> list[str]:
    state = "healthy" if result.verify.healthy else "NOT healthy"
    lines = [f"{verb} {result.terminal}: {state} — {result.verify.detail}"]
    if result.verify.pending:
        lines.append("  not loaded yet: " + ", ".join(result.verify.pending))
    lines.extend(f"  journal error: {e}" for e in result.verify.errors[:5])
    return lines


def stop_lines(result: StopResult) -> list[str]:
    if result.clean:
        return [
            f"stopped {result.terminal} (the terminal exited with code {result.exit_code})"
        ]
    return [
        f"stopped {result.terminal}, but the terminal did not close itself "
        f"(systemd Result={result.unit_result})",
        f"warning: {UNCLEAN_STOP}",
    ]


def compile_lines(result: CompileResult) -> list[str]:
    lines = [f"  ok     {s}" for s in result.compiled]
    lines += [f"  FAILED {s}" for s in result.failed]
    lines.append(
        f"{len(result.compiled)} compiled, {len(result.failed)} failed; "
        f"{result.errors} errors, {result.warnings} warnings"
    )
    if not result.ok:
        for name, text in sorted(result.logs.items()):
            errors = [ln.strip() for ln in text.splitlines() if "error" in ln.lower()]
            if errors:
                lines.append(f"{name}:")
                lines.extend(f"  {ln}" for ln in errors[:20])
    return lines


def compile_payload(result: CompileResult) -> dict[str, Any]:
    payload = asdict(result)
    payload["ok"] = result.ok
    return payload
