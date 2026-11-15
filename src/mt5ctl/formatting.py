"""Human-readable and JSON renderings shared by the CLI and the MCP server."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .reconcile import DeployPlan
from .results import (
    AdoptResult,
    Check,
    CompileResult,
    DeployResult,
    ExpertsReport,
    LoginResult,
    LogsResult,
    RestartResult,
    StopResult,
    TerminalInfo,
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
    lines.extend(
        f"  ! renumbered: {a.rsplit('/', 1)[-1]} is now {b.rsplit('/', 1)[-1]}"
        for a, b in plan.renumbered
    )
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
        if plan.renumbered:
            problems.append("charts renumbered by the terminal")
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
    if result.left_stopped:
        lines.append(
            "the terminal was stopped before the deploy and is left stopped; "
            f"start it with: mt5ctl start {result.terminal} (or deploy with --start)"
        )
    else:
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
        "left_stopped": result.left_stopped,
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
        if st.service_state == "active" and st.terminal_running is False:
            line += "  NO TERMINAL PROCESS"
        if st.trading_disabled is not None:
            line += f"  TRADING DISABLED ({st.trading_disabled})"
        if st.algo_trading is False:
            line += "  ALGO TRADING OFF"
        out.append(line)
    return out


def status_payload(rows: list[TerminalStatus]) -> list[dict[str, Any]]:
    return [asdict(st) | {"healthy": st.healthy} for st in rows]


def restart_lines(result: RestartResult, verb: str = "restarted") -> list[str]:
    state = "healthy" if result.verify.healthy else "NOT healthy"
    if result.already_running:
        verb = "already running:"
    lines = [f"{verb} {result.terminal}: {state} — {result.verify.detail}"]
    if result.verify.pending:
        lines.append("  not loaded yet: " + ", ".join(result.verify.pending))
    lines.extend(f"  journal error: {e}" for e in result.verify.errors[:5])
    return lines


def stop_lines(result: StopResult) -> list[str]:
    if not result.was_running:
        return [f"{result.terminal} was not running"]
    if result.clean:
        return [
            f"stopped {result.terminal} (the terminal exited with code {result.exit_code})"
        ]
    return [
        f"stopped {result.terminal}, but the terminal did not close itself "
        f"(systemd Result={result.unit_result})",
        f"warning: {UNCLEAN_STOP}",
    ]


def _algo(report: ExpertsReport) -> str:
    state = {True: "on", False: "OFF", None: "unknown"}[report.algo_trading]
    return f"Algo Trading {state} ({report.algo_source})"


def _running(report: ExpertsReport) -> str:
    return {True: "running", False: "stopped", None: "state unknown"}[report.running]


def ea_list_lines(reports: list[ExpertsReport]) -> list[str]:
    out: list[str] = []
    for r in reports:
        if r.error:
            out.append(f"{r.terminal}: ERROR {r.error}")
            continue
        out.append(f"{r.terminal}: {_running(r)}; {_algo(r)}")
        if r.trading_disabled:
            out.append(f"  TRADING DISABLED ({r.trading_disabled})")
        if not r.charts:
            out.append("  no charts in the profile")
        for c in r.charts:
            where = f"{c.symbol or '?'} {c.timeframe or '?'}"
            if c.expert is None:
                out.append(f"  {c.chart:<12} {where:<14} (no expert)")
                continue
            allowed = "" if c.allowed is not False else "  ALGO TRADING NOT ALLOWED"
            out.append(f"  {c.chart:<12} {where:<14} {c.expert}  [{c.state}]{allowed}")
    return out


def autotrading_lines(reports: list[ExpertsReport]) -> list[str]:
    out: list[str] = []
    for r in reports:
        if r.error:
            out.append(f"{r.terminal}: ERROR {r.error}")
            continue
        with_expert = [c for c in r.charts if c.expert]
        blocked = [c for c in with_expert if c.allowed is False]
        line = f"{r.terminal}: {_algo(r)}; {_running(r)}"
        if r.running:
            line += f"; {len(r.can_trade)} of {len(with_expert)} experts can trade"
        out.append(line)
        if r.trading_disabled:
            out.append(f"  TRADING DISABLED ({r.trading_disabled})")
        for c in blocked:
            out.append(f"  {c.chart}: {c.expert} has its own Algo Trading off")
    return out


def doctor_lines(checks: list[Check]) -> list[str]:
    glyph = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}
    width = max((len(c.label) for c in checks), default=0)
    lines = [
        f"{glyph.get(c.status, '?')}  {c.label:<{width}}  {c.detail}".rstrip() for c in checks
    ]
    fails = sum(c.status == "fail" for c in checks)
    warns = sum(c.status == "warn" for c in checks)
    lines.append(
        "all checks passed" if not fails and not warns else f"{fails} failed, {warns} warnings"
    )
    return lines


def journal_line(raw: str) -> str:
    """A journal line for reading: time, source and message, errors marked."""
    cols = raw.split("\t")
    if len(cols) < 5:
        return raw
    mark = {"1": "WARN ", "2": "ERROR "}.get(cols[1].strip(), "")
    return f"{cols[2]}  {cols[3]}  {mark}{chr(9).join(cols[4:])}".replace("\t", " ")


def logs_lines(result: LogsResult) -> list[str]:
    if result.file is None:
        return [f"{result.terminal}: no {result.source} log yet"]
    head = f"# {result.terminal} {result.source} {result.file}"
    if result.partial:
        head += f" (last {mib(result.read_bytes)} of {mib(result.size)})"
    return [head, *(journal_line(line) for line in result.lines)]


def mib(n: int) -> str:
    return f"{n / 1048576:.1f} MB"


def info_lines(rows: list[TerminalInfo]) -> list[str]:
    out: list[str] = []
    for i in rows:
        if i.error:
            out.append(f"{i.terminal}: {i.error}")
            continue
        build = f"build {i.build}" if i.build else "build unknown"
        started = f", started {i.started}" if i.started else ""
        out.append(f"{i.terminal}: {build}{started}  ({i.log_file})")
        if i.account:
            where = f"account {i.account} on {i.server}"
            if i.access_point:
                where += f" through {i.access_point}"
            if i.ping_ms is not None:
                where += f", ping {i.ping_ms:g} ms"
            if i.company:
                where += f" ({i.company})"
            out.append("  " + where)
        else:
            out.append("  not logged in this session")
        facts = []
        if i.positions is not None:
            facts.append(f"{i.positions} positions, {i.orders} orders")
        if i.trading_disabled:
            facts.append(f"TRADING DISABLED ({i.trading_disabled})")
        if facts:
            out.append("  " + "; ".join(facts))
        if i.start_config:
            out.append(f"  start file {i.start_config}")
        if i.system:
            out.append(f"  {i.system}")
    return out


def login_lines(result: LoginResult) -> list[str]:
    head = f"login {result.terminal}: account {result.account} on {result.server}: "
    text = {
        "ok": "logged in; the terminal keeps the password",
        "failed": f"REFUSED by the broker ({result.detail})",
        "timeout": "no authorization in the journal in time; check the server name",
        "error": f"could not log in ({result.detail})",
    }.get(result.outcome, result.outcome)
    lines = [head + text]
    if not result.restarted:
        lines.append("WARNING: terminal not started")
    elif result.verify is None:
        lines.append("terminal started again")
    else:
        lines.append(f"started again: {result.verify.detail}")
        if result.verify.connected is not True:
            lines.append(
                "  to log in by itself, its start config (/config:) must name the account: "
                "[Common] Login=<account> and Server=<server>, without a password"
            )
    return lines


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
