"""Operations on registered terminals, composed from the pure core.

Everything here is orchestration: read state, plan, run the scripts from
:mod:`mt5ctl.scripts` through :mod:`mt5ctl.transport`, interpret their output.
The decisions themselves (what to change, what is safe) live in the pure
modules and are tested there.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import os
import re
import socket
import tarfile
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TypeVar

import anyio

from . import mt5spec, scripts, transport
from .auth import resolve_password, secrets_file
from .bundle import (
    EXPERTS_DIR,
    Bundle,
    order_relpath,
    read_bundle,
    render_order,
    validate_member,
)
from .chart import ChartFile
from .errors import (
    ChartFormatError,
    CompileError,
    ConfirmationRequiredError,
    DeployError,
    LoginError,
    Mt5ctlError,
    RemoteCommandError,
    UsageError,
)
from .manifest import MANIFEST_RELPATH, Manifest
from .models import Env, Host, HostKind, Registry, Terminal
from .reconcile import DeployPlan, TerminalState, compute_plan
from .results import (
    AdoptResult,
    ChartExpert,
    Check,
    CompileResult,
    DeployResult,
    ExpertsReport,
    LoginResult,
    LogsResult,
    RestartResult,
    Screenshot,
    StopResult,
    TerminalInfo,
    TerminalStatus,
    VerifyReport,
)
from .state import (
    build_state_script,
    charts_from_output,
    parse_state_output,
    read_local_charts,
    read_local_state,
)
from .text import decode_lenient

SEP = scripts.SEP
T = TypeVar("T")

# Drain must outlast the unit's own stop timeout plus a slow Wine teardown.
DRAIN_TIMEOUT_S = 90
CONTROL_TIMEOUT_S = 180.0
# process mode: how long the stop waits for the terminal after stop_command.
PROCESS_STOP_WAIT_S = 50
# A terminal needs up to a minute or two to reconnect and load a large set of
# experts; verify polls instead of judging one snapshot.
VERIFY_TIMEOUT_S = 180.0
MAX_VERIFY_TIMEOUT_S = 600.0
VERIFY_INTERVAL_S = 6.0
# The lock has no heartbeat, so a takeover must wait longer than the longest
# possible run: stop, drain, every transport timeout and the longest verify
# (about 2,300 s in the worst case). A run whose lock was taken over anyway
# cannot write: apply checks that it still owns the lock first.
LOCK_STALE_S = 3600
JOURNAL_TAIL_BYTES = 64 * 1024
# Per journal file read by verify; start-up lines come first, so the head is kept.
JOURNAL_SINCE_LIMIT = 8 * 1024 * 1024
COMPILE_TIMEOUT_S = 600


def _lines(stdout: str, tag: str) -> list[list[str]]:
    return [line.split(SEP)[1:] for line in stdout.splitlines() if line.split(SEP)[0] == tag]


def _unb64(token: str) -> bytes:
    return base64.b64decode(token) if token else b""


def _require_live_confirmation(term: Terminal, action: str, confirm: bool) -> None:
    if term.env is Env.LIVE and not confirm:
        raise ConfirmationRequiredError(term.id, action)


def _require_supervisor(term: Terminal, action: str) -> None:
    if not term.supervised:
        raise DeployError(
            f"{action} needs to know how {term.id!r} is run: set 'service' (a systemd "
            "unit) or 'start_command' and 'stop_command' in the registry"
        )


# --------------------------------------------------------------------------- #
# State and plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PlanReport:
    terminal: Terminal
    bundle: Bundle
    plan: DeployPlan


async def collect_state(
    registry: Registry, terminal: Terminal, bundle_paths: Iterable[str]
) -> TerminalState:
    """Read *terminal*'s current state, locally or over SSH (read-only)."""
    host = registry.host_of(terminal)
    paths = sorted(bundle_paths)
    if host.kind is HostKind.LOCAL:
        return read_local_state(terminal.data_dir, terminal.profile, paths)
    script = build_state_script(terminal.data_dir, terminal.profile, paths)
    result = await transport.run(host, script, timeout=120.0)
    return parse_state_output(result.stdout, terminal.profile, paths)


async def plan(registry: Registry, terminal_id: str, bundle_dir: str | Path) -> PlanReport:
    """Dry-run a deploy: what would change on *terminal_id* to match *bundle_dir*.

    Read-only on both sides. The bundle is read for the terminal's configured
    profile, so a bundle built for another profile is refused up front.
    """
    terminal = registry.terminal(terminal_id)
    bundle = read_bundle(bundle_dir, profile=terminal.profile)
    state = await collect_state(registry, terminal, bundle.files)
    return PlanReport(terminal=terminal, bundle=bundle, plan=compute_plan(bundle, state))


# --------------------------------------------------------------------------- #
# Status and verify
# --------------------------------------------------------------------------- #


def _int_or_none(token: str) -> int | None:
    try:
        value = int(token)
    except ValueError:
        return None
    return value if value >= 0 else None


def journal_connection(text: str) -> tuple[bool | None, str | None]:
    """Connection state from journal text: the latest connect/disconnect line wins."""
    state: bool | None = None
    event: str | None = None
    for line in text.splitlines():
        if mt5spec.LOG_AUTHORIZED_RE.search(line):
            state, event = True, line.strip()
        elif mt5spec.LOG_CONNECTION_LOST_RE.search(line):
            state, event = False, line.strip()
    return state, event


def since_latest_start(text: str) -> str:
    """The part of journal text from the terminal's latest start line on."""
    starts = list(mt5spec.LOG_TERMINAL_STARTED_RE.finditer(text))
    return text[starts[-1].start() :] if starts else text


def journal_algo_trading(text: str) -> bool | None:
    """The terminal's Algo Trading button, if it was toggled in *text*.

    ``None`` when the journal says nothing: the state then is whatever the
    start configuration set (see :data:`mt5spec.LOG_ALGO_TRADING_RE`).
    """
    state: bool | None = None
    for match in mt5spec.LOG_ALGO_TRADING_RE.finditer(text):
        state = match.group(1).lower() == "enabled"
    return state


def journal_trading_disabled(text: str) -> str | None:
    """Why the account cannot trade, if the journal says so after the latest login.

    Each authorization starts afresh; a "trading has been disabled" line after
    it wins. Returns the reason (``investor mode``), or ``"disabled"`` when the
    line gives none.
    """
    reason: str | None = None
    for line in text.splitlines():
        if mt5spec.LOG_AUTHORIZED_RE.search(line):
            reason = None
        elif (match := mt5spec.LOG_TRADING_DISABLED_RE.search(line)) is not None:
            reason = (match["reason"] or "disabled").strip()
    return reason


def _parse_status_line(fields: list[str], term: Terminal, host: Host) -> TerminalStatus:
    _tid, state, active, age, estab, tail, procs = (fields + [""] * 7)[:7]
    process_count = _int_or_none(procs)
    journal = decode_lenient(_unb64(tail))
    session = since_latest_start(journal)
    journal_state, event = journal_connection(session)
    sockets = _int_or_none(estab)
    connected = (sockets > 0) if sockets is not None else journal_state
    running = state == "active"
    if not running:
        # A stopped terminal's journal describes its last session, not now.
        connected = None
    return TerminalStatus(
        terminal=term.id,
        host=host.id,
        env=term.env,
        service_state=state or "unknown",
        active_seconds=_int_or_none(active),
        journal_age_seconds=_int_or_none(age),
        connected=connected,
        last_event=event,
        trading_disabled=journal_trading_disabled(session) if running else None,
        terminal_running=None if process_count is None else process_count > 0,
        algo_trading=journal_algo_trading(session) if running else None,
    )


async def _status_for_host(host: Host, terms: list[Terminal]) -> list[TerminalStatus]:
    specs = [
        (
            t.id,
            t.service or "",
            t.service_scope,
            t.data_dir,
            "systemd" if t.service else "process",
        )
        for t in terms
    ]
    try:
        result = await transport.run(
            host, scripts.build_status_script(specs, JOURNAL_TAIL_BYTES), timeout=60.0
        )
    except RemoteCommandError as exc:
        return [TerminalStatus(t.id, host.id, t.env, "unknown", error=str(exc)) for t in terms]
    by_id = {fields[0]: fields for fields in _lines(result.stdout, "TERM") if fields}
    return [
        _parse_status_line(by_id[t.id], t, host)
        if t.id in by_id
        else TerminalStatus(t.id, host.id, t.env, "unknown", error="no status reported")
        for t in terms
    ]


async def status(
    registry: Registry, terminal_ids: list[str] | None = None
) -> list[TerminalStatus]:
    """Status of the given terminals (all when ``None``), one round trip per host."""
    ids = list(registry.terminals) if terminal_ids is None else terminal_ids
    terms = [registry.terminal(i) for i in ids]
    by_host: dict[str, list[Terminal]] = {}
    for term in terms:
        by_host.setdefault(term.host, []).append(term)
    groups = await asyncio.gather(
        *(_status_for_host(registry.hosts[h], ts) for h, ts in by_host.items())
    )
    found = {s.terminal: s for group in groups for s in group}
    return [found[i] for i in ids]


@dataclass(frozen=True, slots=True)
class JournalCursor:
    name: str
    size: int


async def _journal_cursor(host: Host, term: Terminal) -> JournalCursor:
    """Where the journal ends now. Never raises: a cursor only narrows verify,
    so failing to read one must not stop a terminal from being started."""
    try:
        result = await transport.run(
            host, scripts.build_log_cursor_script(term.data_dir), check=False
        )
    except RemoteCommandError:
        return JournalCursor("", 0)
    for fields in _lines(result.stdout, "CURSOR"):
        if len(fields) >= 2:
            name = _unb64(fields[0]).decode("utf-8", "replace")
            return JournalCursor(name, _int_or_none(fields[1]) or 0)
    return JournalCursor("", 0)


def _previous_day(name: str) -> str:
    """The journal file of the day before *name* (``YYYYMMDD.log``), or *name*."""
    stem, _, suffix = name.partition(".")
    try:
        day = time.strptime(stem, "%Y%m%d")
    except ValueError:
        return name
    previous = time.gmtime(time.mktime(day) - 12 * 3600)
    return time.strftime("%Y%m%d", previous) + (f".{suffix}" if suffix else "")


async def _recent_journal(host: Host, term: Terminal) -> str:
    """Journal text since the terminal's latest start (verify without a restart).

    Reads yesterday's and today's files, so a terminal started before midnight
    is judged on its own start, then cuts at the last start line (see
    :data:`mt5spec.LOG_TERMINAL_STARTED_RE`). Without a start line the text is
    returned whole.
    """
    newest = await _journal_cursor(host, term)
    if not newest.name:
        return ""
    since = JournalCursor(_previous_day(newest.name), 0)
    text = await _journal_since(host, term, since)
    return since_latest_start(text)


async def _journal_since(host: Host, term: Terminal, cursor: JournalCursor) -> str:
    script = scripts.build_log_since_script(
        term.data_dir, cursor.name, cursor.size, JOURNAL_SINCE_LIMIT
    )
    result = await transport.run(host, script, check=False)
    return "\n".join(
        decode_lenient(_unb64(fields[1]))
        for fields in _lines(result.stdout, "LOG")
        if len(fields) >= 2
    )


def expected_experts(experts: Iterable[str]) -> set[str]:
    """The names experts are logged under, for a set of expert relpaths."""
    return {mt5spec.expert_log_name(rel) for rel in experts}


def journal_errors(text: str) -> tuple[str, ...]:
    """Messages of the error-level lines (source column included)."""
    out = []
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) > 3 and cols[mt5spec.JOURNAL_LEVEL_COLUMN] == mt5spec.JOURNAL_LEVEL_ERROR:
            out.append("\t".join(cols[3:]).strip())
    return tuple(out)


def evaluate_verify(
    st: TerminalStatus, journal: str, expected: set[str], *, require_start: bool = False
) -> VerifyReport:
    """Judge one health snapshot (pure).

    With *require_start* (after mt5ctl started the terminal), the journal must
    show the start: a unit that is "active" may still be a terminal that never
    got going, and a terminal with no experts and an unknown connection would
    otherwise pass on the service state alone.
    """
    loaded = {
        m["name"].strip().lower() for m in mt5spec.LOG_EXPERT_LOADED_RE.finditer(journal)
    }
    failed_names = {
        m["name"].strip().lower() for m in mt5spec.LOG_EXPERT_FAILED_RE.finditer(journal)
    }
    journal_state, _ = journal_connection(journal)
    connected = st.connected if st.connected is not None else journal_state
    ok_names = sorted(
        n for n in expected if n.lower() in loaded and n.lower() not in failed_names
    )
    failed = sorted(n for n in expected if n.lower() in failed_names)
    pending = sorted(n for n in expected if n not in ok_names and n not in failed)
    trading_disabled = journal_trading_disabled(journal) or st.trading_disabled
    algo_trading = journal_algo_trading(since_latest_start(journal))
    if algo_trading is None:
        algo_trading = st.algo_trading
    started = (
        mt5spec.LOG_TERMINAL_STARTED_RE.search(journal) is not None if require_start else None
    )
    healthy = (
        st.service_state == "active"
        and st.terminal_running is not False
        and started is not False
        and connected is True
        and trading_disabled is None
        and algo_trading is not False
        and not failed
        and not pending
    )
    return VerifyReport(
        healthy=healthy,
        service_state=st.service_state,
        terminal_running=st.terminal_running,
        connected=connected,
        loaded=tuple(ok_names),
        pending=tuple(pending),
        failed=tuple(failed),
        trading_disabled=trading_disabled,
        algo_trading=algo_trading,
        started=started,
        errors=journal_errors(journal),
    )


def _settled(report: VerifyReport) -> bool:
    """An unhealthy outcome that waiting will not change.

    The terminal has started, is connected and every expert has loaded or
    failed; what is left (trading disabled for the account, a failed expert)
    stays as it is however long verify polls.
    """
    return (
        report.started is not False
        and report.connected is True
        and not report.pending
        and (
            report.trading_disabled is not None
            or report.algo_trading is False
            or bool(report.failed)
        )
    )


def _verify_window(timeout: float | None, default: float) -> float:
    value = default if timeout is None else timeout
    if not 0 <= value <= MAX_VERIFY_TIMEOUT_S:
        raise ValueError(f"verify timeout must be between 0 and {MAX_VERIFY_TIMEOUT_S:.0f}s")
    return value


async def _verify(
    registry: Registry,
    term: Terminal,
    journal: Callable[[], Awaitable[str]],
    expected: set[str],
    *,
    timeout: float,
    interval: float,
    require_start: bool = False,
) -> VerifyReport:
    """Poll until healthy or *timeout*; report the last snapshot (never changes anything)."""
    deadline = time.monotonic() + timeout
    while True:
        st = (await status(registry, [term.id]))[0]
        report = evaluate_verify(st, await journal(), expected, require_start=require_start)
        if report.healthy or _settled(report) or time.monotonic() + interval > deadline:
            return report
        await asyncio.sleep(interval)


def _since(host: Host, term: Terminal, cursor: JournalCursor) -> Callable[[], Awaitable[str]]:
    async def read() -> str:
        return await _journal_since(host, term, cursor)

    return read


def _since_latest_start(host: Host, term: Terminal) -> Callable[[], Awaitable[str]]:
    async def read() -> str:
        return await _recent_journal(host, term)

    return read


async def _live_experts(registry: Registry, term: Terminal) -> set[str]:
    state = await collect_state(registry, term, [])
    return expected_experts(ref for ref in state.charts.values() if ref is not None)


async def verify(
    registry: Registry, terminal_id: str, *, timeout: float | None = None
) -> VerifyReport:
    """Check that a running terminal is healthy and every chart's expert is loaded.

    Judges the journal since the terminal's latest start, so it works however
    long ago that was (within yesterday's and today's journal files).
    """
    term = registry.terminal(terminal_id)
    host = registry.host_of(term)
    return await _verify(
        registry,
        term,
        _since_latest_start(host, term),
        await _live_experts(registry, term),
        timeout=_verify_window(timeout, 0.0),
        interval=VERIFY_INTERVAL_S,
    )


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


def _process_script(term: Terminal, action: str) -> str:
    assert term.start_command is not None and term.stop_command is not None
    return scripts.build_process_script(
        term.data_dir, action, term.start_command, term.stop_command, PROCESS_STOP_WAIT_S
    )


async def _control(host: Host, term: Terminal, action: str) -> None:
    _require_supervisor(term, action)
    if term.service:
        script = scripts.build_control_script(term.service, term.service_scope, action)
        what = f"systemctl {action} {term.service}"
    else:
        script = _process_script(term, action)
        what = f"{action} of {term.id} (start_command / stop_command)"
    result = await transport.run(host, script, check=False, timeout=CONTROL_TIMEOUT_S)
    if not result.ok:
        raise DeployError(
            f"{what} failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


async def _drain(host: Host, term: Terminal) -> str:
    """Wait for the terminal process to be gone; return the unit's ``Result``.

    ``success`` means the terminal closed itself (and saved its profile);
    anything else means systemd had to end it.
    """
    assert term.service is not None
    script = scripts.build_drain_script(term.service, term.service_scope, DRAIN_TIMEOUT_S)
    result = await transport.run(host, script, check=False, timeout=DRAIN_TIMEOUT_S + 30)
    for fields in _lines(result.stdout, "DRAIN"):
        if fields[:1] == ["ok"]:
            return fields[1] if len(fields) > 1 and fields[1] else "unknown"
    raise DeployError(
        f"the terminal did not exit within {DRAIN_TIMEOUT_S}s of stop; nothing was "
        "changed and it is being started again"
    )


def _holder() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


async def _uncancellable(aw: Awaitable[T]) -> T:
    """Await *aw* to its end, whatever cancels the caller meanwhile.

    Two kinds of cancellation reach a run: an anyio scope (an MCP client giving
    up) and asyncio's own ``Task.cancel()`` (``asyncio.run`` on Ctrl-C, or a
    library caller). An anyio shield stops only the first. Here *aw* runs as
    its own task that neither can interrupt; the caller is still cancelled
    afterwards, once *aw* is done.
    """
    task = asyncio.ensure_future(aw)
    interrupted = False
    with anyio.CancelScope(shield=True):
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                interrupted = True
    if interrupted:
        raise asyncio.CancelledError
    return result


START_RETRY_DELAYS_S = (5.0, 15.0, 30.0, 60.0)


async def _start_again(host: Host, term: Terminal) -> None:
    """Start a terminal mt5ctl stopped, retrying while the host cannot be reached.

    A dropped connection right after the stop must not leave the terminal down
    for want of one attempt; an answer from the host (the unit failed) is final.
    """
    for delay in (*START_RETRY_DELAYS_S, None):
        try:
            await _control(host, term, "start")
            return
        except RemoteCommandError:
            if delay is None:
                raise
            await asyncio.sleep(delay)
        except DeployError as exc:
            if delay is None or "exit 255" not in str(exc):
                raise
            await asyncio.sleep(delay)


async def _release(host: Host, term: Terminal, holder: str) -> None:
    await transport.run(host, scripts.build_lock_release_script(term.data_dir, holder))


@contextlib.asynccontextmanager
async def _locked(host: Host, term: Terminal) -> AsyncIterator[str]:
    """Hold the terminal's lock for the body; yields the holder id.

    One deploy, adopt, start, stop or restart per terminal at a time. Taking
    the lock and releasing it run shielded from cancellation: a cancelled call
    whose acquire already reached the host would otherwise leave the lock
    behind. If taking it fails in any way, a lock this run may have created is
    released (release only removes a lock that names this holder).
    """
    holder = _holder()
    try:
        result = await _uncancellable(
            transport.run(
                host,
                scripts.build_lock_acquire_script(term.data_dir, holder, LOCK_STALE_S),
                check=False,
            )
        )
    except BaseException:
        with contextlib.suppress(Exception):
            await _uncancellable(_release(host, term, holder))
        raise
    for fields in _lines(result.stdout, "LOCK"):
        if fields and fields[0] == "held":
            owner = _unb64(fields[1]).decode("utf-8", "replace") if len(fields) > 1 else ""
            age = _int_or_none(fields[2]) if len(fields) > 2 else None
            raise DeployError(
                f"another mt5ctl run holds the lock on {term.id!r} ({owner or 'unknown'}, "
                f"taken {age // 60 if age is not None else '?'} min ago); retry when it "
                f"finishes. A lock older than {LOCK_STALE_S // 60} minutes is taken over; "
                f"if no run is going on, remove {term.data_dir}/{scripts.LOCK_PATH}"
            )
    if not any(f"LOCK{SEP}{w}" in result.stdout for w in ("acquired", "takeover")):
        raise RemoteCommandError(host.id, result.returncode, result.stderr or "lock not taken")
    body_ok = False
    try:
        yield holder
        body_ok = True
    finally:
        try:
            await _uncancellable(_release(host, term, holder))
        except RemoteCommandError as exc:
            # Never mask the body's own error; otherwise say what is left behind.
            if body_ok:
                raise DeployError(
                    f"the operation on {term.id} completed, but its lock could not be "
                    f"released ({exc}); it expires after {LOCK_STALE_S // 60} minutes, "
                    f"or remove {term.data_dir}/{scripts.LOCK_PATH}"
                ) from None


async def restart(
    registry: Registry,
    terminal_id: str,
    *,
    confirm: bool = False,
    verify_timeout: float | None = None,
) -> RestartResult:
    """Restart a terminal through systemd and wait until it is healthy again.

    Takes the terminal's lock, so it never starts the terminal in the middle of
    a deploy's apply.
    """
    term = registry.terminal(terminal_id)
    host = registry.host_of(term)
    _require_live_confirmation(term, "restart", confirm)
    _require_supervisor(term, "restart")
    timeout = _verify_window(verify_timeout, VERIFY_TIMEOUT_S)
    async with _locked(host, term):
        expected = await _live_experts(registry, term)
        cursor = await _journal_cursor(host, term)
        await _uncancellable(_control(host, term, "restart"))
        report = await _verify(
            registry,
            term,
            _since(host, term, cursor),
            expected,
            timeout=timeout,
            interval=VERIFY_INTERVAL_S,
            require_start=True,
        )
    return RestartResult(terminal=term.id, verify=report)


async def _stop_and_drain(host: Host, term: Terminal) -> StopResult:
    """``systemctl stop``, wait for the process to be gone, tell how it went.

    Whether the terminal closed itself is read from its own journal: the exit
    line it writes on the way out (see :data:`mt5spec.LOG_TERMINAL_EXIT_RE`).
    """
    _require_supervisor(term, "stop")
    cursor = await _journal_cursor(host, term)
    if term.service:
        probe = await transport.run(
            host, scripts.build_is_active_script(term.service, term.service_scope), check=False
        )
        states = _lines(probe.stdout, "ACTIVE")
        was_running = not states or states[0][:1] not in (["inactive"], ["failed"])
        await _control(host, term, "stop")
        unit_result = await _drain(host, term)
    else:
        # The process-mode stop script itself asks, waits and, if need be, ends the group.
        result = await transport.run(
            host,
            _process_script(term, "stop"),
            check=False,
            timeout=PROCESS_STOP_WAIT_S + 60,
        )
        was_running = _lines(result.stdout, "WAS")[:1] != [["0"]]
        results = _lines(result.stdout, "RESULT")
        unit_result = results[0][0] if results and results[0] else "unknown"
    if not was_running:
        return StopResult(terminal=term.id, unit_result=unit_result, was_running=False)
    exit_code: int | None = None
    try:
        journal = await _journal_since(host, term, cursor)
    except RemoteCommandError:
        journal = ""
    for match in mt5spec.LOG_TERMINAL_EXIT_RE.finditer(journal):
        exit_code = int(match.group(1))
    return StopResult(terminal=term.id, unit_result=unit_result, exit_code=exit_code)


async def stop(registry: Registry, terminal_id: str, *, confirm: bool = False) -> StopResult:
    """Stop a terminal through systemd and wait until its process has exited."""
    term = registry.terminal(terminal_id)
    host = registry.host_of(term)
    _require_live_confirmation(term, "stop", confirm)
    _require_supervisor(term, "stop")
    async with _locked(host, term):
        result = await _uncancellable(_stop_and_drain(host, term))
    return result


async def start(
    registry: Registry,
    terminal_id: str,
    *,
    confirm: bool = False,
    verify_timeout: float | None = None,
) -> RestartResult:
    """Start a terminal through systemd and wait until it is healthy."""
    term = registry.terminal(terminal_id)
    host = registry.host_of(term)
    _require_live_confirmation(term, "start", confirm)
    _require_supervisor(term, "start")
    timeout = _verify_window(verify_timeout, VERIFY_TIMEOUT_S)
    async with _locked(host, term):
        expected = await _live_experts(registry, term)
        cursor = await _journal_cursor(host, term)
        await _uncancellable(_control(host, term, "start"))
        report = await _verify(
            registry,
            term,
            _since(host, term, cursor),
            expected,
            timeout=timeout,
            interval=VERIFY_INTERVAL_S,
            require_start=True,
        )
    return RestartResult(terminal=term.id, verify=report)


# --------------------------------------------------------------------------- #
# Deploy and adopt
# --------------------------------------------------------------------------- #


async def _preflight(host: Host, term: Terminal, paths: Iterable[str] = ()) -> str:
    """Check deploy prerequisites (and that no folder on the way to *paths* is a
    symbolic link); return the unit's user."""
    script = scripts.build_preflight_script(
        term.data_dir, term.profile, term.service, term.service_scope, paths
    )
    result = await transport.run(host, script, check=False)
    problems = []
    for f in _lines(result.stdout, "PRE"):
        if f[1:2] == ["ok"]:
            continue
        if f[:1] == ["links"]:
            folder = _unb64(f[1]).decode("utf-8", "replace") if len(f) > 1 else "?"
            problems.append(
                f"{folder} is a symbolic link: writing through it would leave the data folder"
            )
        else:
            problems.append(f"{f[0]}: {f[1] if len(f) > 1 else '?'}")
    if problems:
        raise DeployError("deploy prerequisites not met on the host: " + "; ".join(problems))
    users = _lines(result.stdout, "USER")
    return users[0][0] if users and users[0] else "root"


def _conflict_message(plan: DeployPlan) -> str:
    reasons = []
    if plan.conflicts:
        reasons.append(
            "refusing to overwrite files mt5ctl does not manage: "
            + ", ".join(plan.conflicts)
            + " — renumber the bundle's charts around them, or adopt the terminal first"
        )
    if plan.gaps:
        reasons.append(
            "refusing to leave gaps in chart numbering ("
            + ", ".join(plan.gaps)
            + " missing) — the terminal would renumber the charts after them"
        )
    return "; ".join(reasons)


def build_upload(
    bundle: Bundle, paths: Iterable[str], generated: Mapping[str, bytes] | None = None
) -> tuple[bytes, dict[str, str]]:
    """Pack bundle files (and generated ones) into a tar, with each file's sha256.

    *generated* carries files mt5ctl writes itself rather than taking from the
    bundle; the only one is the profile's order file.
    """
    extra = dict(generated or {})
    if set(extra) - {order_relpath(bundle.profile)}:
        raise ValueError(f"unexpected generated files: {sorted(extra)}")
    buf = io.BytesIO()
    shas: dict[str, str] = {}
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel in [*paths, *extra]:
            if rel in extra:
                data = extra[rel]
            else:
                validate_member(rel, bundle.profile)
                data = (bundle.root / rel).read_bytes()
            shas[rel] = hashlib.sha256(data).hexdigest()
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue(), shas


def next_manifest(bundle: Bundle, plan: DeployPlan, state: TerminalState) -> Manifest:
    """The manifest after applying *plan*: the bundle, plus experts kept in place.

    A managed expert dropped from the bundle but still run by a foreign chart
    stays on disk, so it stays managed: it is removed by a later deploy once
    nothing runs it, and a bundle that brings it back does not conflict with it.
    """
    files = dict(bundle.files)
    previous = state.manifest.files if state.manifest else {}
    files.update({p: previous[p] for p in plan.keep if p in previous})
    return Manifest(profile=bundle.profile, files=files)


@dataclass(frozen=True, slots=True)
class _ApplyOutcome:
    backup: str | None
    error: RemoteCommandError | None = None
    restore: str = "done"
    """What the restore found: ``untouched`` (nothing had been moved),
    ``applied`` (the apply finished, only its answer was lost), ``done``,
    ``failed``, ``busy`` or ``lost``; see :func:`scripts.build_restore_script`."""
    restore_detail: str = ""


async def deploy(
    registry: Registry,
    terminal_id: str,
    bundle_dir: str | Path,
    *,
    confirm: bool = False,
    verify_timeout: float | None = None,
    verify_interval: float | None = None,
) -> DeployResult:
    """Make the terminal's managed files match *bundle_dir*.

    Sequence: lock → plan → (if anything changes) preflight → stop → drain →
    plan again on the quiet files → backup → upload → apply → start → verify →
    unlock. The second plan matters: the terminal saves its charts while it
    shuts down, so the files at apply time are not the ones seen while it ran.

    Once the terminal has been asked to stop, it is started again whatever
    happens next — an error, or the caller cancelling — and the apply and the
    start run shielded from cancellation. If the apply fails, the touched files
    are restored from the backup before the start, and :class:`DeployError` is
    raised. A failed verify is reported, not rolled back: the files are as
    intended, and the usual cause (a slow reconnect) resolves itself. A deploy
    with nothing to change does not restart the terminal.
    """
    term = registry.terminal(terminal_id)
    host = registry.host_of(term)
    _require_live_confirmation(term, "deploy to", confirm)
    _require_supervisor(term, "deploy")
    bundle = read_bundle(bundle_dir, profile=term.profile)
    timeout = _verify_window(verify_timeout, VERIFY_TIMEOUT_S)
    interval = VERIFY_INTERVAL_S if verify_interval is None else verify_interval

    async with _locked(host, term) as holder:
        stop_requested = False
        stop_result: StopResult | None = None
        outcome: _ApplyOutcome | None = None
        cursor = JournalCursor("", 0)
        current = compute_plan(bundle, await collect_state(registry, term, bundle.files))
        if current.blocked:
            raise DeployError(_conflict_message(current))
        if not current.has_changes:
            # Nothing to change: no restart; judge the terminal on its latest start.
            report = await _verify(
                registry,
                term,
                _since_latest_start(host, term),
                expected_experts(bundle.chart_experts.values()),
                timeout=0.0,
                interval=interval,
            )
            return DeployResult(terminal=term.id, plan=current, restarted=False, verify=report)
        try:
            unit_user = await _preflight(
                host,
                term,
                [
                    *current.add,
                    *current.update,
                    *current.remove,
                    order_relpath(bundle.profile),
                ],
            )
            # Counted as stopped before asking: a stop that reports an error (a
            # dropped connection, a slow ExecStop) may still complete on the host,
            # and starting a unit that is already running does nothing.
            stop_requested = True
            stop_result = await _stop_and_drain(host, term)
            state = await collect_state(registry, term, bundle.files)
            current = compute_plan(bundle, state)
            if current.blocked:
                raise DeployError(_conflict_message(current))
            if current.has_changes:
                outcome = await _uncancellable(
                    _apply(host, term, bundle, current, state, unit_user, holder)
                )
        finally:
            if stop_requested:

                async def start_again() -> JournalCursor:
                    mark = await _journal_cursor(host, term)
                    await _start_again(host, term)
                    return mark

                cursor = await _uncancellable(start_again())

        if outcome is not None and outcome.error is not None and outcome.restore != "applied":
            raise DeployError(_apply_failure(term, outcome))
        report = await _verify(
            registry,
            term,
            _since(host, term, cursor),
            expected_experts(bundle.chart_experts.values()),
            timeout=timeout,
            interval=interval,
            require_start=True,
        )
    return DeployResult(
        terminal=term.id,
        plan=current,
        restarted=True,
        backup=outcome.backup if outcome else None,
        verify=report,
        stop=stop_result,
    )


async def _apply(
    host: Host,
    term: Terminal,
    bundle: Bundle,
    plan: DeployPlan,
    state: TerminalState,
    unit_user: str,
    holder: str | None = None,
) -> _ApplyOutcome:
    """Back up, upload, apply; restore on failure."""
    order_file = order_relpath(bundle.profile)
    managed = sorted(state.manifest.files) if state.manifest else []
    result = await transport.run(
        host,
        scripts.build_backup_script(term.data_dir, [*managed, order_file], _timestamp()),
    )
    tokens = [f[0] for f in _lines(result.stdout, "BACKUP") if f]
    backup = (
        _unb64(tokens[0]).decode("utf-8", "surrogateescape")
        if tokens and tokens[0] != "none"
        else None
    )

    place = sorted({*plan.add, *plan.update})
    generated = {order_file: render_order(plan.order)} if plan.order is not None else {}
    staging = f"{scripts.STAGING_DIR}/{uuid.uuid4().hex}"
    tar_bytes, shas = build_upload(bundle, place, generated)
    manifest = next_manifest(bundle, plan, state)
    manifest_json = manifest.to_json()
    previous = state.manifest.files if state.manifest else {}
    interim = Manifest(profile=bundle.profile, files={**previous, **manifest.files})
    try:
        await transport.put_tar(host, tar_bytes, f"{term.data_dir}/{staging}")
        await transport.run(
            host,
            scripts.build_apply_script(
                term.data_dir,
                staging,
                shas,
                plan.remove,
                manifest_json,
                unit_user,
                holder,
                interim_manifest_json=interim.to_json(),
            ),
        )
    except RemoteCommandError as exc:
        touched = sorted({*plan.add, *plan.update, *plan.remove, *generated})
        restore = scripts.build_restore_script(
            term.data_dir,
            backup,
            touched,
            unit_user,
            new_manifest_sha=hashlib.sha256(manifest_json.encode("utf-8")).hexdigest(),
            holder=holder,
        )
        try:
            done = await transport.run(host, restore, check=False, timeout=300)
        except RemoteCommandError as again:
            return _ApplyOutcome(
                backup=backup, error=exc, restore="failed", restore_detail=str(again)
            )
        word, detail = "failed", (done.stderr.strip() or "no answer from the restore")
        for fields in _lines(done.stdout, "RESTORE"):
            if fields:
                word = fields[0]
                detail = (
                    _unb64(fields[1]).decode("utf-8", "replace") if len(fields) > 1 else ""
                )
        return _ApplyOutcome(backup=backup, error=exc, restore=word, restore_detail=detail)
    return _ApplyOutcome(backup=backup)


def _apply_failure(term: Terminal, outcome: _ApplyOutcome) -> str:
    backup = outcome.backup or "(none: first deploy)"
    head = f"apply failed ({outcome.error})"
    if outcome.restore == "untouched":
        return f"{head} before any file was changed; {term.id} was started again as it was"
    if outcome.restore == "done":
        return f"{head}; {term.id} was restored to its previous files and started again"
    if outcome.restore == "lost":
        return (
            f"{head}, and another run took the lock over, so nothing was restored; "
            f"check {term.id} against its backup {backup}"
        )
    why = (
        "the apply was still running" if outcome.restore == "busy" else outcome.restore_detail
    )
    return (
        f"{head}, and the restore did NOT complete ({why}); {term.id} was started on partly "
        "applied files. The manifest covers both versions, so running the deploy again "
        f"completes it; or unpack the backup {backup} into the data folder"
    )


async def adopt(
    registry: Registry, terminal_id: str, bundle_dir: str | Path, *, confirm: bool = False
) -> AdoptResult:
    """Take a terminal that already runs *bundle_dir* under management.

    Records the bundle's paths in the manifest with the digests they have on
    the terminal now. Nothing else is touched and the terminal keeps running.
    Every bundle file must already be present: adopting a bundle the terminal
    does not run would record a state that does not exist. Files whose content
    differs from the bundle are reported as drifted; the next deploy updates them.
    """
    term = registry.terminal(terminal_id)
    host = registry.host_of(term)
    _require_live_confirmation(term, "adopt", confirm)
    bundle = read_bundle(bundle_dir, profile=term.profile)
    async with _locked(host, term):
        state = await collect_state(registry, term, bundle.files)
        missing = sorted(p for p in bundle.files if state.files.get(p) is None)
        if missing:
            raise DeployError(
                "adopt needs every bundle file present and readable on the terminal; "
                "missing: " + ", ".join(missing)
            )
        if state.manifest is not None and state.manifest.profile != bundle.profile:
            raise DeployError(
                f"the terminal is already managed in profile {state.manifest.profile!r}"
            )
        recorded = {p: d for p in bundle.files if (d := state.files[p]) is not None}
        unit_user = await _preflight(host, term)
        await transport.run(
            host,
            scripts.build_manifest_put_script(
                term.data_dir,
                Manifest(profile=bundle.profile, files=recorded).to_json(),
                unit_user,
            ),
        )
    return AdoptResult(
        terminal=term.id,
        adopted=tuple(sorted(recorded)),
        drifted=tuple(sorted(p for p, d in recorded.items() if d != bundle.files[p])),
        foreign=tuple(sorted(c for c in state.charts if c not in bundle.files)),
        manifest_path=f"{term.data_dir}/{MANIFEST_RELPATH}",
    )


# --------------------------------------------------------------------------- #
# Logs and info
# --------------------------------------------------------------------------- #

LOGS_MAX_BYTES = 4 * 1024 * 1024
INFO_TAIL_BYTES = 1024 * 1024
INFO_PREVIOUS_BYTES = 256 * 1024
LOG_SOURCES = {"journal": (mt5spec.JOURNAL_DIR,), "experts": mt5spec.EXPERTS_LOG_DIRS}


def _tails(stdout: str) -> dict[str, tuple[str, int, bytes, bytes]]:
    """``TAIL`` lines by terminal id: (file name, size, tail, previous file's tail)."""
    out = {}
    for fields in _lines(stdout, "TAIL"):
        tid, name, size, tail, prev = (fields + [""] * 5)[:5]
        out[tid] = (
            _unb64(name).decode("utf-8", "replace"),
            _int_or_none(size) or 0,
            _unb64(tail),
            _unb64(prev),
        )
    return out


async def logs(
    registry: Registry,
    terminal_id: str,
    *,
    source: str = "journal",
    pattern: str | None = None,
    lines: int = 50,
) -> LogsResult:
    """The last *lines* of a terminal's newest journal (or experts' log) file.

    With *pattern* (a Python regular expression, case-insensitive), only the
    matching lines. At most :data:`LOGS_MAX_BYTES` of the file's end is read.
    """
    if source not in LOG_SOURCES:
        raise UsageError(f"source must be one of {sorted(LOG_SOURCES)}, got {source!r}")
    regex = None
    if pattern:
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise UsageError(f"bad pattern {pattern!r}: {exc}") from None
    term = registry.terminal(terminal_id)
    host = registry.host_of(term)
    script = scripts.build_log_tails_script(
        [(term.id, term.data_dir)], LOG_SOURCES[source], LOGS_MAX_BYTES
    )
    result = await transport.run(host, script)
    name, size, tail, _ = _tails(result.stdout).get(term.id, ("", 0, b"", b""))
    text = decode_lenient(tail).lstrip("\ufeff")
    rows = [line for line in text.replace("\r", "").split("\n") if line.strip()]
    if size > len(tail) and rows:
        rows = rows[1:]  # the first line read is cut
    if regex is not None:
        rows = [line for line in rows if regex.search(line)]
    keep = max(1, min(lines, 10_000))
    return LogsResult(
        terminal=term.id,
        source=source,
        file=name or None,
        lines=tuple(rows[-keep:]),
        size=size,
        read_bytes=min(size, len(tail)),
    )


def journal_info(terminal: str, text: str) -> TerminalInfo:
    """What a journal says about the latest session (pure).

    Build, system line and start file come from the latest start; account,
    server and access point from the latest authorization; trading state is
    judged after it.
    """
    lines = text.splitlines()
    starts = [
        n for n, line in enumerate(lines) if mt5spec.LOG_TERMINAL_STARTED_RE.search(line)
    ]
    session_lines = lines[starts[-1] :] if starts else lines
    session = "\n".join(session_lines)
    build = system = start_config = start_time = None
    if starts:
        first = session_lines[0]
        if (m := mt5spec.LOG_BUILD_RE.search(first)) is not None:
            build = int(m.group(1))
        cols = first.split("\t")
        start_time = cols[2].strip() if len(cols) > 3 else None
        for line in session_lines[1:6]:
            if system is None and (m := mt5spec.LOG_SYSTEM_RE.search(line)) is not None:
                system = m.group(1).strip()
            if (
                start_config is None
                and (m := mt5spec.LOG_LAUNCHED_WITH_RE.search(line)) is not None
            ):
                start_config = m.group(1)
    account = server = access = company = None
    ping: float | None = None
    positions = orders = None
    for line in session.splitlines():
        if (m := mt5spec.LOG_AUTHORIZED_DETAIL_RE.search(line)) is not None:
            account, server = m["account"], m["server"]
            access = m["access"].strip() if m["access"] else None
            ping = float(m["ping"]) if m["ping"] else None
        elif (m := mt5spec.LOG_SYNCHRONIZED_RE.search(line)) is not None:
            company = m["company"].strip()
            positions, orders = int(m["positions"]), int(m["orders"])
    return TerminalInfo(
        terminal=terminal,
        build=build,
        started=start_time,
        system=system,
        start_config=start_config,
        account=account,
        server=server,
        access_point=access,
        ping_ms=ping,
        company=company,
        positions=positions,
        orders=orders,
        trading_disabled=journal_trading_disabled(session),
    )


async def _journals(host: Host, terms: list[Terminal]) -> dict[str, tuple[str, str]] | str:
    """Per terminal: (newest journal file name, the end of the file before it and
    of that file, decoded); ``("", "")`` without a journal. An error message
    instead when the host cannot be read."""
    script = scripts.build_log_tails_script(
        [(t.id, t.data_dir) for t in terms],
        (mt5spec.JOURNAL_DIR,),
        INFO_TAIL_BYTES,
        INFO_PREVIOUS_BYTES,
    )
    try:
        result = await transport.run(host, script)
    except RemoteCommandError as exc:
        return str(exc)
    tails = _tails(result.stdout)
    out = {}
    for term in terms:
        name, _size, tail, prev = tails.get(term.id, ("", 0, b"", b""))
        text = decode_lenient(prev).lstrip("\ufeff") + "\n" + decode_lenient(tail)
        out[term.id] = (name, text.replace("\r", "") if name else "")
    return out


async def _info_for_host(host: Host, terms: list[Terminal]) -> list[TerminalInfo]:
    journals = await _journals(host, terms)
    if isinstance(journals, str):
        return [TerminalInfo(terminal=t.id, error=journals) for t in terms]
    out = []
    for term in terms:
        name, text = journals[term.id]
        if not name:
            out.append(TerminalInfo(terminal=term.id, error="no journal yet"))
            continue
        out.append(replace(journal_info(term.id, text), log_file=name))
    return out


async def info(
    registry: Registry, terminal_ids: list[str] | None = None
) -> list[TerminalInfo]:
    """Build, account, server and session facts from the journals, one trip per host."""
    ids = list(registry.terminals) if terminal_ids is None else terminal_ids
    terms = [registry.terminal(i) for i in ids]
    by_host: dict[str, list[Terminal]] = {}
    for term in terms:
        by_host.setdefault(term.host, []).append(term)
    groups = await asyncio.gather(
        *(_info_for_host(registry.hosts[h], ts) for h, ts in by_host.items())
    )
    found = {i.terminal: i for group in groups for i in group}
    return [found[i] for i in ids]


# --------------------------------------------------------------------------- #
# Experts and Algo Trading
# --------------------------------------------------------------------------- #

_WINDOWS_PATH_RE = re.compile(r"([A-Za-z]):\\(.*)")


def host_path(windows_path: str, wineprefix: str | None) -> str | None:
    """Where a path the terminal logged lives on the host; ``None`` when unknown.

    Drive Z: is the host's root (Wine's default mapping); drive C: is the
    prefix's ``drive_c``, known only when the terminal's ``wineprefix`` is set.
    """
    m = _WINDOWS_PATH_RE.fullmatch(windows_path.strip())
    if m is None:
        return None
    drive, rest = m[1].upper(), m[2].replace("\\", "/")
    if drive == "Z":
        return "/" + rest
    if drive == "C" and wineprefix:
        return f"{wineprefix.rstrip('/')}/drive_c/{rest}"
    return None


def journal_chart_states(session: str) -> dict[tuple[str, str, str], str]:
    """The latest ``loaded`` / ``failed`` / ``removed`` per (expert, symbol, period)."""
    events = []
    for state, regex in (
        ("loaded", mt5spec.LOG_EXPERT_LOADED_RE),
        ("failed", mt5spec.LOG_EXPERT_FAILED_RE),
        ("removed", mt5spec.LOG_EXPERT_REMOVED_RE),
    ):
        events += [(m.start(), state, m) for m in regex.finditer(session)]
    out: dict[tuple[str, str, str], str] = {}
    for _pos, state, m in sorted(events, key=lambda e: e[0]):
        key = (
            m["name"].strip().lower(),
            m["symbol"].strip().lower(),
            m["period"].strip().upper(),
        )
        out[key] = state
    return out


async def _profile_charts(registry: Registry, term: Terminal) -> dict[str, bytes | None]:
    host = registry.host_of(term)
    if host.kind is HostKind.LOCAL:
        return read_local_charts(term.data_dir, term.profile)
    result = await transport.run(
        host, build_state_script(term.data_dir, term.profile, []), timeout=120.0
    )
    return charts_from_output(result.stdout, term.profile)


def _chart_experts(
    charts: Mapping[str, bytes | None], states: dict[tuple[str, str, str], str] | None
) -> tuple[ChartExpert, ...]:
    out = []
    for rel, data in sorted(charts.items()):
        name = rel.rsplit("/", 1)[-1]
        try:
            chart = ChartFile.parse(data) if data is not None else None
        except ChartFormatError:
            chart = None
        if chart is None:
            out.append(ChartExpert(name, None, None, None, state="unreadable"))
            continue
        symbol = chart.symbol
        tf = chart.timeframe.name if chart.timeframe else None
        expert = chart.expert
        if expert is None:
            out.append(ChartExpert(name, symbol, tf, None))
            continue
        state = "-"
        if states is not None:
            key = (expert.name.strip().lower(), (symbol or "").lower(), tf or "")
            state = states.get(key, "not loaded")
        out.append(
            ChartExpert(
                name,
                symbol,
                tf,
                expert.name,
                path=expert.path,
                allowed=expert.algo_trading,
                state=state,
            )
        )
    return tuple(out)


def _algo_from_start_file(
    start_config: str | None, value: list[str] | None
) -> tuple[bool | None, str]:
    if start_config is None:
        return None, "the journal names no start file"
    if value is None:
        return None, f"start file not readable from the host: {start_config}"
    if value[0] == "missing":
        return None, f"start file not found: {start_config}"
    setting = value[1] if len(value) > 1 else ""
    if setting == "1":
        return True, "start file: [Experts] Enabled=1"
    if setting == "0":
        return False, "start file: [Experts] Enabled=0"
    return (
        None,
        "the start file does not set [Experts] Enabled; the terminal keeps its last state",
    )


async def _experts_for_host(
    registry: Registry, host: Host, terms: list[Terminal]
) -> list[ExpertsReport]:
    journals = await _journals(host, terms)
    if isinstance(journals, str):
        return [ExpertsReport(terminal=t.id, error=journals) for t in terms]
    sessions = {t.id: since_latest_start(journals[t.id][1]) for t in terms}
    infos = {t.id: journal_info(t.id, journals[t.id][1]) for t in terms}
    wanted = []
    for t in terms:
        logged = infos[t.id].start_config
        if logged and (path := host_path(logged, t.wineprefix)):
            wanted.append((t.id, path))
    start_values: dict[str, list[str]] = {}
    if wanted:
        result = await transport.run(
            host, scripts.build_start_file_script(wanted), check=False
        )
        start_values = {f[0]: f[1:] for f in _lines(result.stdout, "START") if f}
    rows = {s.terminal: s for s in await status(registry, [t.id for t in terms])}
    charts = await asyncio.gather(
        *(_profile_charts(registry, t) for t in terms), return_exceptions=True
    )
    out = []
    for term, profile in zip(terms, charts, strict=True):
        if isinstance(profile, BaseException):
            if not isinstance(profile, Mt5ctlError):
                raise profile
            out.append(ExpertsReport(terminal=term.id, error=str(profile)))
            continue
        st = rows.get(term.id)
        running = None if st is None or st.error else st.service_state == "active"
        session = sessions[term.id]
        toggled = journal_algo_trading(session) if running else None
        algo: bool | None
        if toggled is not None:
            algo, source = toggled, "switched in the terminal during this session"
        else:
            algo, source = _algo_from_start_file(
                infos[term.id].start_config, start_values.get(term.id)
            )
        states = journal_chart_states(session) if running else None
        out.append(
            ExpertsReport(
                terminal=term.id,
                running=running,
                algo_trading=algo,
                algo_source=source,
                trading_disabled=journal_trading_disabled(session) if running else None,
                charts=_chart_experts(profile, states),
            )
        )
    return out


async def experts(
    registry: Registry, terminal_ids: list[str] | None = None
) -> list[ExpertsReport]:
    """Each terminal's charts and experts, whether each is loaded and allowed to
    trade, and the terminal's Algo Trading button. Read-only."""
    ids = list(registry.terminals) if terminal_ids is None else terminal_ids
    terms = [registry.terminal(i) for i in ids]
    by_host: dict[str, list[Terminal]] = {}
    for term in terms:
        by_host.setdefault(term.host, []).append(term)
    groups = await asyncio.gather(
        *(_experts_for_host(registry, registry.hosts[h], ts) for h, ts in by_host.items())
    )
    found = {r.terminal: r for group in groups for r in group}
    return [found[i] for i in ids]


# --------------------------------------------------------------------------- #
# Screenshot
# --------------------------------------------------------------------------- #

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


async def screenshot(registry: Registry, terminal_id: str) -> Screenshot:
    """Capture the terminal's whole X display as PNG (read-only)."""
    term = registry.terminal(terminal_id)
    host = registry.host_of(term)
    script = scripts.build_screenshot_script(
        term.display, term.xauthority, term.service, term.service_scope
    )
    result = await transport.run(host, script, timeout=90)
    for fields in _lines(result.stdout, "SHOT"):
        if fields and fields[0] == "ok" and len(fields) > 1:
            png = _unb64(fields[1])
            if png.startswith(PNG_MAGIC):
                return Screenshot(terminal=term.id, png=png)
            raise UsageError(f"{term.id}: the capture is not a PNG image")
        if fields and fields[0] == "error":
            reason = _unb64(fields[1]).decode("utf-8", "replace") if len(fields) > 1 else ""
            raise UsageError(f"{term.id}: no screenshot: {reason.strip()}")
    raise UsageError(f"{term.id}: no screenshot: the host printed nothing")


# --------------------------------------------------------------------------- #
# Doctor
# --------------------------------------------------------------------------- #


def _credentials_check() -> Check:
    path = secrets_file()
    if not path.is_file():
        return Check("credentials file", "ok", f"none ({path})")
    if os.name == "posix" and path.stat().st_mode & 0o077:
        return Check("credentials file", "fail", f"{path} is readable by others: chmod 600")
    return Check("credentials file", "ok", str(path))


def _mode(term: Terminal) -> str:
    return "systemd" if term.service else "process" if term.start_command else "none"


async def _doctor_host(host: Host, terms: list[Terminal]) -> list[Check]:
    specs = [
        (
            t.id,
            t.data_dir,
            _mode(t),
            t.service_scope,
            t.service or "",
            t.wine if (t.wineprefix or t.wine != "wine") else "",
            t.wineprefix or "",
        )
        for t in terms
    ]
    try:
        result = await transport.run(host, scripts.build_doctor_script(specs), timeout=60)
    except RemoteCommandError as exc:
        return [Check(f"host {host.id}", "fail", f"unreachable: {exc}")]
    tools = {f[0]: f[1] for f in _lines(result.stdout, "TOOL") if len(f) >= 2}
    if not tools:
        return [Check(f"host {host.id}", "fail", "the check printed nothing")]
    missing = [t for t in scripts.DOCTOR_REQUIRED if tools.get(t) != "ok"]
    if tools.get("sha256sum") != "ok" and tools.get("shasum") != "ok":
        missing.append("sha256sum (or shasum)")
    if any(_mode(t) == "systemd" for t in terms) and tools.get("systemctl") != "ok":
        missing.append("systemctl")
    checks = [
        Check(
            f"host {host.id}",
            "fail" if missing else "ok",
            "missing: " + ", ".join(missing) if missing else "reachable, tools present",
        )
    ]
    for tool, why in scripts.DOCTOR_OPTIONAL.items():
        if tools.get(tool) != "ok":
            checks.append(Check(f"host {host.id}: {tool}", "warn", f"not installed; {why}"))
    rows = {f[0]: f[1:] for f in _lines(result.stdout, "TERM") if len(f) >= 9}
    for term in terms:
        row = rows.get(term.id)
        if row is None:
            checks.append(Check(f"terminal {term.id}", "fail", "not reported by the check"))
            continue
        d, m, e, u, s, w, p, j = row[:8]
        fails, warns = [], []
        if d != "ok":
            fails.append(f"no data folder {term.data_dir}")
        elif m != "ok":
            fails.append("no MQL5 folder in the data folder")
        if d == "ok" and e != "ok":
            warns.append("no terminal64.exe in the data folder (not a /portable install?)")
        mode = _mode(term)
        if mode == "none":
            warns.append("no service or start_command: read-only commands only")
        if u == "missing":
            fails.append(f"unit {term.service} not found")
        if s == "missing":
            fails.append(
                f"no password-less sudo for systemctl start|stop|restart {term.service}"
            )
        if w == "missing":
            fails.append(f"wine {term.wine} not found")
        if p == "missing":
            fails.append(f"no Wine prefix at {term.wineprefix}")
        if d == "ok" and j != "ok":
            warns.append("no journal yet (never started?)")
        status = "fail" if fails else "warn" if warns else "ok"
        detail = "; ".join(fails + warns) or (
            f"{mode}"
            + (" (sudo ok)" if s == "ok" else "")
            + ", data folder and journal present"
        )
        checks.append(Check(f"terminal {term.id}", status, detail))
    return checks


async def doctor(registry: Registry) -> list[Check]:
    """Check what mt5ctl needs, everywhere, read-only: hosts, terminals, connections."""
    checks = [
        Check(
            "registry",
            "ok",
            f"{len(registry.terminals)} terminals on {len(registry.hosts)} hosts",
        ),
        _credentials_check(),
    ]
    by_host: dict[str, list[Terminal]] = {h: [] for h in registry.hosts}
    for term in registry.terminals.values():
        by_host.setdefault(term.host, []).append(term)
    groups = await asyncio.gather(
        *(_doctor_host(registry.hosts[h], ts) for h, ts in by_host.items())
    )
    for group in groups:
        checks.extend(group)
    reachable = [
        t.id
        for t in registry.terminals.values()
        if t.supervised
        and not any(c.label == f"host {t.host}" and c.status == "fail" for c in checks)
    ]
    if reachable:
        rows = await status(registry, reachable)
        running = [r for r in rows if r.service_state == "active"]
        down = [r.terminal for r in running if r.connected is False]
        if down:
            checks.append(
                Check(
                    "broker connections", "warn", "running, not connected: " + ", ".join(down)
                )
            )
        else:
            n = sum(r.connected is True for r in running)
            checks.append(
                Check("broker connections", "ok", f"{n} of {len(running)} running connected")
            )
    return checks


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #

LOGIN_WAIT_S = 120


async def login(
    registry: Registry,
    terminal_id: str,
    *,
    server: str,
    account: str | None = None,
    password: str | None = None,
    confirm: bool = False,
    wait_s: int = LOGIN_WAIT_S,
    verify_timeout: float | None = None,
) -> LoginResult:
    """Log a terminal in once, headless; afterwards it logs in by itself.

    The terminal is stopped, started once with a temporary start config that
    holds the login (the terminal keeps the password, ``KeepPrivate=1``), and
    closed as soon as its journal shows the authorization; then it is started
    again as usual and verified. To log in by itself from then on, its own
    start config must name the account and server without a password (see
    :data:`mt5spec.START_CONFIG_LOGIN`). The temporary config is shredded
    whatever happens, and the terminal is always started again.
    """
    term = registry.terminal(terminal_id)
    host = registry.host_of(term)
    _require_live_confirmation(term, "log in", confirm)
    _require_supervisor(term, "login")
    login_account = account or term.account
    if not login_account:
        raise LoginError(f"{term.id!r} has no account in the registry; pass the account")
    secret = resolve_password(login_account, password)
    # Built (and validated) before anything is stopped: a bad value must never
    # leave the terminal down.
    try:
        script = scripts.build_login_script(
            term.data_dir,
            account=login_account,
            server=server,
            password=secret,
            wine=term.wine,
            wineprefix=term.wineprefix,
            service=term.service,
            scope=term.service_scope,
            wait_s=wait_s,
        )
    except ValueError as exc:
        raise LoginError(str(exc)) from None
    outcome, detail = "error", ""
    restarted = False
    report: VerifyReport | None = None
    timeout = _verify_window(verify_timeout, VERIFY_TIMEOUT_S)
    async with _locked(host, term):
        expected = await _live_experts(registry, term)
        await _uncancellable(_stop_and_drain(host, term))
        try:
            # Not cut short either: ended by a signal, the script could not shred
            # the file that holds the password.
            result = await _uncancellable(
                transport.run(host, script, check=False, timeout=wait_s + 120)
            )
            for fields in _lines(result.stdout, "LOGIN"):
                outcome = fields[0] if fields else "error"
                detail = (
                    _unb64(fields[1]).decode("utf-8", "replace") if len(fields) > 1 else ""
                )
        except RemoteCommandError as exc:
            # Never echo the script: it holds the password. stderr does not.
            outcome, detail = "error", str(exc)
        finally:
            await _uncancellable(_start_again(host, term))
            restarted = True
        if outcome == "ok":
            report = await _verify(
                registry,
                term,
                _since_latest_start(host, term),
                expected,
                timeout=timeout,
                interval=VERIFY_INTERVAL_S,
                require_start=True,
            )
    return LoginResult(
        terminal=term.id,
        account=login_account,
        server=server,
        outcome=outcome,
        detail=detail,
        restarted=restarted,
        verify=report,
    )


# --------------------------------------------------------------------------- #
# Compile
# --------------------------------------------------------------------------- #

_SKIPPED_SOURCES = (mt5spec.EX5_SUFFIX, ".log")


def collect_sources(src_dir: str | Path) -> list[str]:
    """Relative POSIX paths of the files to upload for a compile."""
    root = Path(src_dir)
    if not root.is_dir():
        raise CompileError(f"source folder not found: {str(src_dir)!r}")
    files: list[str] = []
    for item in sorted(root.rglob("*")):
        rel = item.relative_to(root).as_posix()
        if item.is_symlink():
            raise CompileError(f"source folder contains a symlink (refused): {rel!r}")
        if item.is_file() and not rel.lower().endswith(_SKIPPED_SOURCES):
            files.append(rel)
    if not any(f.lower().endswith(mt5spec.SOURCE_SUFFIX) for f in files):
        raise CompileError(f"no {mt5spec.SOURCE_SUFFIX} sources in {str(src_dir)!r}")
    return files


def compile_aliases(source_dirs: Iterable[str], all_dirs: Iterable[str]) -> dict[str, str]:
    """A space-free stand-in for each source folder whose path has a space.

    MetaEditor silently skips a ``/compile:`` folder whose path contains a space
    (see :data:`mt5spec.COMPILE_PATH_NO_SPACES`). Such a folder is also uploaded
    under a stand-in name, at the same depth so relative ``#include "..\\x.mqh"``
    still resolve, and compiled there.
    """
    if not mt5spec.COMPILE_PATH_NO_SPACES:
        return {}
    taken = set(all_dirs)
    aliases: dict[str, str] = {}
    for folder in sorted(source_dirs):
        if " " not in folder:
            continue
        base = "/".join(part.replace(" ", "_") for part in folder.split("/"))
        alias, n = base, 1
        while alias in taken:
            alias, n = f"{base}_{n}", n + 1
        taken.add(alias)
        aliases[folder] = alias
    return aliases


def _pack(root: Path, rels: Iterable[str], extra: Mapping[str, str] | None = None) -> bytes:
    """Tar *rels* from *root*; *extra* maps more member names to source files."""
    members = [(rel, rel) for rel in rels] + list((extra or {}).items())
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel, source in members:
            data = (root / source).read_bytes()
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _unpack_ex5(tar_bytes: bytes, out_dir: Path, wanted: Mapping[str, str]) -> None:
    """Write the wanted ``.ex5`` members of a fetched tar under *out_dir*.

    *wanted* maps a member name to the path it is written to.
    """
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r") as tar:
        for member in tar.getmembers():
            name = PurePosixPath(member.name).as_posix().removeprefix("./")
            if name not in wanted or not member.isfile():
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            target = out_dir / wanted[name]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(extracted.read())


async def compile_sources(
    registry: Registry,
    terminal_id: str,
    src_dir: str | Path,
    out_dir: str | Path,
    *,
    confirm: bool = False,
) -> CompileResult:
    """Compile ``.mq5`` sources with a terminal's MetaEditor; fetch the ``.ex5``.

    The sources are uploaded to a temporary folder under the terminal's
    ``MQL5/Experts`` (so ``#include`` resolves against its ``MQL5/Include``),
    compiled there under Wine, and the folder is removed afterwards. Use a
    dedicated, never-started copy of a terminal for this: compiling does not
    need the terminal running. Success is judged by output, not exit code: every
    source must yield an ``.ex5`` and no log may report errors.
    """
    term = registry.terminal(terminal_id)
    host = registry.host_of(term)
    _require_live_confirmation(term, "compile on", confirm)
    if term.wineprefix and term.wine == "wine":
        raise CompileError(
            f"set 'wine' for {term.id!r} to the Wine that runs the terminal: another Wine "
            "build updates the prefix, and the terminal then stops at a Wine dialog on "
            "its next start"
        )
    if mt5spec.COMPILE_PATH_NO_SPACES and " " in term.data_dir:
        raise CompileError(
            f"the data folder of {term.id!r} has a space in its path, and MetaEditor skips "
            "folders whose path has one; compile on a terminal installed without spaces"
        )
    root = Path(src_dir)
    files = collect_sources(root)
    sources = [f for f in files if f.lower().endswith(mt5spec.SOURCE_SUFFIX)]
    source_dirs = sorted({str(PurePosixPath(s).parent) for s in sources})
    aliases = compile_aliases(source_dirs, {str(PurePosixPath(f).parent) for f in files})

    def compiled_as(rel: str) -> str:
        folder = str(PurePosixPath(rel).parent)
        return f"{aliases[folder]}/{PurePosixPath(rel).name}" if folder in aliases else rel

    stand_ins = {compiled_as(f): f for f in files if str(PurePosixPath(f).parent) in aliases}
    targets = {compiled_as(s): s for s in sources}  # what MetaEditor compiles -> source
    work = f"{EXPERTS_DIR}/mt5ctl-build-{uuid.uuid4().hex[:12]}"
    out = Path(out_dir)
    try:
        await transport.put_tar(host, _pack(root, files, stand_ins), f"{term.data_dir}/{work}")
        result = await transport.run(
            host,
            scripts.build_compile_script(
                term.data_dir,
                work,
                sorted({str(PurePosixPath(t).parent) for t in targets}),
                sorted(targets),
                wine=term.wine,
                wineprefix=term.wineprefix,
                display=term.display,
                timeout_s=COMPILE_TIMEOUT_S,
            ),
            check=False,
            timeout=COMPILE_TIMEOUT_S * max(1, len(source_dirs)) + 60,
        )
        problems = [
            _unb64(f[0]).decode("utf-8", "replace") for f in _lines(result.stdout, "ERROR")
        ]
        if problems:
            raise CompileError("; ".join(problems))
        built = {
            _unb64(f[0]).decode("utf-8"): f[1] == "1"
            for f in _lines(result.stdout, "EX5")
            if len(f) >= 2
        }
        logs = {
            _unb64(f[0]).decode("utf-8", "replace"): decode_lenient(_unb64(f[1]))
            for f in _lines(result.stdout, "LOG")
            if len(f) >= 2
        }
        if not logs and not any(built.values()):
            raise CompileError(
                "MetaEditor wrote no log and no .ex5: it did not run. Check the terminal's "
                "'wine' (MetaTrader 5 needs a recent Wine), 'wineprefix' and 'display' in "
                "the registry"
            )
        compiled = sorted(src for target, src in targets.items() if built.get(target))
        ex5 = {
            str(PurePosixPath(target).with_suffix(mt5spec.EX5_SUFFIX)): str(
                PurePosixPath(src).with_suffix(mt5spec.EX5_SUFFIX)
            )
            for target, src in targets.items()
            if built.get(target)
        }
        if ex5:
            fetched = await transport.run(
                host, scripts.build_fetch_script(term.data_dir, work, sorted(ex5))
            )
            for fields in _lines(fetched.stdout, "TAR"):
                _unpack_ex5(_unb64(fields[0]), out, ex5)
    finally:
        await transport.run(
            host, scripts.build_remove_dir_script(term.data_dir, work), check=False
        )
    errors = warnings = 0
    for text in logs.values():
        for match in mt5spec.COMPILE_RESULT_RE.finditer(text):
            errors += int(match.group(1))
            warnings += int(match.group(2))
    return CompileResult(
        compiled=tuple(compiled),
        failed=tuple(sorted(set(sources) - set(compiled))),
        errors=errors,
        warnings=warnings,
        logs=logs,
    )
