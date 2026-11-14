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
import socket
import tarfile
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import anyio

from . import mt5spec, scripts, transport
from .bundle import (
    EXPERTS_DIR,
    Bundle,
    order_relpath,
    read_bundle,
    render_order,
    validate_member,
)
from .errors import (
    CompileError,
    ConfirmationRequiredError,
    DeployError,
    RemoteCommandError,
)
from .manifest import MANIFEST_RELPATH, Manifest
from .models import Env, Host, HostKind, Registry, Terminal
from .reconcile import DeployPlan, TerminalState, compute_plan
from .results import (
    AdoptResult,
    CompileResult,
    DeployResult,
    RestartResult,
    TerminalStatus,
    VerifyReport,
)
from .state import build_state_script, parse_state_output, read_local_state
from .text import decode_lenient

SEP = scripts.SEP

# Drain must outlast the unit's own stop timeout plus a slow Wine teardown.
DRAIN_TIMEOUT_S = 90
CONTROL_TIMEOUT_S = 180.0
# A terminal needs up to a minute or two to reconnect and load a large set of
# experts; verify polls instead of judging one snapshot.
VERIFY_TIMEOUT_S = 180.0
MAX_VERIFY_TIMEOUT_S = 600.0
VERIFY_INTERVAL_S = 6.0
# The lock has no heartbeat, so a takeover must wait longer than the longest
# possible run: stop, drain, every transport timeout and the longest verify.
LOCK_STALE_S = 1800
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


def _require_service(term: Terminal, action: str) -> str:
    if not term.service:
        raise DeployError(
            f"{action} needs the terminal's systemd unit; set 'service' for "
            f"{term.id!r} in the registry"
        )
    return term.service


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


def _parse_status_line(fields: list[str], term: Terminal, host: Host) -> TerminalStatus:
    _tid, state, active, age, estab, tail = (fields + [""] * 6)[:6]
    journal_state, event = journal_connection(decode_lenient(_unb64(tail)))
    sockets = _int_or_none(estab)
    connected = (sockets > 0) if sockets is not None else journal_state
    return TerminalStatus(
        terminal=term.id,
        host=host.id,
        env=term.env,
        service_state=state or "unknown",
        active_seconds=_int_or_none(active),
        journal_age_seconds=_int_or_none(age),
        connected=connected,
        last_event=event,
    )


async def _status_for_host(host: Host, terms: list[Terminal]) -> list[TerminalStatus]:
    specs = [(t.id, t.service or "", t.service_scope, t.data_dir) for t in terms]
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
    starts = list(mt5spec.LOG_TERMINAL_STARTED_RE.finditer(text))
    return text[starts[-1].start() :] if starts else text


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


def evaluate_verify(st: TerminalStatus, journal: str, expected: set[str]) -> VerifyReport:
    """Judge one health snapshot (pure)."""
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
    healthy = (
        st.service_state == "active" and connected is not False and not failed and not pending
    )
    return VerifyReport(
        healthy=healthy,
        service_state=st.service_state,
        connected=connected,
        loaded=tuple(ok_names),
        pending=tuple(pending),
        failed=tuple(failed),
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
) -> VerifyReport:
    """Poll until healthy or *timeout*; report the last snapshot (never changes anything)."""
    deadline = time.monotonic() + timeout
    while True:
        st = (await status(registry, [term.id]))[0]
        report = evaluate_verify(st, await journal(), expected)
        if report.healthy or time.monotonic() + interval > deadline:
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


async def _control(host: Host, term: Terminal, action: str) -> None:
    service = _require_service(term, action)
    script = scripts.build_control_script(service, term.service_scope, action)
    result = await transport.run(host, script, check=False, timeout=CONTROL_TIMEOUT_S)
    if not result.ok:
        raise DeployError(
            f"systemctl {action} {service} failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


async def _drain(host: Host, term: Terminal) -> None:
    service = _require_service(term, "stop")
    script = scripts.build_drain_script(service, term.service_scope, DRAIN_TIMEOUT_S)
    result = await transport.run(host, script, check=False, timeout=DRAIN_TIMEOUT_S + 30)
    if f"DRAIN{SEP}ok" not in result.stdout:
        raise DeployError(
            f"the terminal did not exit within {DRAIN_TIMEOUT_S}s of stop; nothing was "
            "changed and it is being started again"
        )


def _holder() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


@contextlib.asynccontextmanager
async def _locked(host: Host, term: Terminal) -> AsyncIterator[None]:
    """Hold the terminal's lock for the body; release it even when cancelled.

    One deploy, adopt or restart per terminal at a time. Release runs in a
    shielded scope: an MCP client that gives up mid-deploy cancels the call,
    and the cleanup must still run to completion.
    """
    holder = _holder()
    result = await transport.run(
        host,
        scripts.build_lock_acquire_script(term.data_dir, holder, LOCK_STALE_S),
        check=False,
    )
    if f"LOCK{SEP}held" in result.stdout:
        raise DeployError(
            f"another mt5ctl run holds the lock on {term.id!r}; retry when it finishes"
        )
    if not any(f"LOCK{SEP}{w}" in result.stdout for w in ("acquired", "takeover")):
        raise RemoteCommandError(host.id, result.returncode, result.stderr or "lock not taken")
    body_ok = False
    try:
        yield
        body_ok = True
    finally:
        with anyio.CancelScope(shield=True):
            try:
                await transport.run(
                    host, scripts.build_lock_release_script(term.data_dir, holder)
                )
            except RemoteCommandError as exc:
                # Never mask the body's own error; otherwise say what is left behind.
                if body_ok:
                    raise DeployError(
                        f"the operation on {term.id} completed, but its lock could not be "
                        f"released ({exc}); it expires after {LOCK_STALE_S // 60} minutes, "
                        f"or remove {term.data_dir}/{scripts.LOCK_DIR}"
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
    _require_service(term, "restart")
    timeout = _verify_window(verify_timeout, VERIFY_TIMEOUT_S)
    async with _locked(host, term):
        expected = await _live_experts(registry, term)
        cursor = await _journal_cursor(host, term)
        with anyio.CancelScope(shield=True):
            await _control(host, term, "restart")
        report = await _verify(
            registry,
            term,
            _since(host, term, cursor),
            expected,
            timeout=timeout,
            interval=VERIFY_INTERVAL_S,
        )
    return RestartResult(terminal=term.id, verify=report)


# --------------------------------------------------------------------------- #
# Deploy and adopt
# --------------------------------------------------------------------------- #


async def _preflight(host: Host, term: Terminal) -> str:
    """Check deploy prerequisites; return the unit's user."""
    script = scripts.build_preflight_script(
        term.data_dir, term.profile, term.service, term.service_scope
    )
    result = await transport.run(host, script, check=False)
    problems = [f"{f[0]}: {f[1]}" for f in _lines(result.stdout, "PRE") if f[1:2] != ["ok"]]
    if problems:
        raise DeployError("deploy prerequisites not met on the host: " + "; ".join(problems))
    users = _lines(result.stdout, "USER")
    return users[0][0] if users and users[0] else "root"


def _conflict_message(plan: DeployPlan) -> str:
    return (
        "refusing to overwrite files mt5ctl does not manage: "
        + ", ".join(plan.conflicts)
        + " — renumber the bundle's charts around them, or adopt the terminal first"
    )


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
    restored: bool = True


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
    _require_service(term, "deploy")
    bundle = read_bundle(bundle_dir, profile=term.profile)
    timeout = _verify_window(verify_timeout, VERIFY_TIMEOUT_S)
    interval = VERIFY_INTERVAL_S if verify_interval is None else verify_interval

    async with _locked(host, term):
        stop_requested = False
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
            unit_user = await _preflight(host, term)
            # Counted as stopped before asking: a stop that reports an error (a
            # dropped connection, a slow ExecStop) may still complete on the host,
            # and starting a unit that is already running does nothing.
            stop_requested = True
            await _control(host, term, "stop")
            await _drain(host, term)
            state = await collect_state(registry, term, bundle.files)
            current = compute_plan(bundle, state)
            if current.blocked:
                raise DeployError(_conflict_message(current))
            if current.has_changes:
                with anyio.CancelScope(shield=True):
                    outcome = await _apply(host, term, bundle, current, state, unit_user)
        finally:
            if stop_requested:
                with anyio.CancelScope(shield=True):
                    cursor = await _journal_cursor(host, term)
                    await _control(host, term, "start")

        if outcome is not None and outcome.error is not None:
            if outcome.restored:
                raise DeployError(
                    f"apply failed ({outcome.error}); {term.id} was restored to its previous "
                    "files and started again"
                )
            raise DeployError(
                f"apply failed ({outcome.error}) and the restore did not complete; {term.id} "
                "was started on partly applied files. Run the deploy again, or unpack the "
                f"backup {outcome.backup or '(none: first deploy)'} into the data folder"
            )
        report = await _verify(
            registry,
            term,
            _since(host, term, cursor),
            expected_experts(bundle.chart_experts.values()),
            timeout=timeout,
            interval=interval,
        )
    return DeployResult(
        terminal=term.id,
        plan=current,
        restarted=True,
        backup=outcome.backup if outcome else None,
        verify=report,
    )


async def _apply(
    host: Host,
    term: Terminal,
    bundle: Bundle,
    plan: DeployPlan,
    state: TerminalState,
    unit_user: str,
) -> _ApplyOutcome:
    """Back up, upload, apply; restore on failure."""
    order_file = order_relpath(bundle.profile)
    managed = sorted(state.manifest.files) if state.manifest else []
    result = await transport.run(
        host,
        scripts.build_backup_script(term.data_dir, [*managed, order_file], _timestamp()),
    )
    tokens = [f[0] for f in _lines(result.stdout, "BACKUP") if f]
    backup = tokens[0] if tokens and tokens[0] != "none" else None

    place = sorted({*plan.add, *plan.update})
    generated = {order_file: render_order(plan.order)} if plan.order is not None else {}
    staging = f"{scripts.STAGING_DIR}/{uuid.uuid4().hex}"
    tar_bytes, shas = build_upload(bundle, place, generated)
    manifest = next_manifest(bundle, plan, state)
    try:
        await transport.put_tar(host, tar_bytes, f"{term.data_dir}/{staging}")
        await transport.run(
            host,
            scripts.build_apply_script(
                term.data_dir, staging, shas, plan.remove, manifest.to_json(), unit_user
            ),
        )
    except RemoteCommandError as exc:
        touched = sorted({*plan.add, *plan.update, *plan.remove, *generated})
        restore = scripts.build_restore_script(term.data_dir, backup, touched, unit_user)
        try:
            done = await transport.run(host, restore, check=False)
            restored = f"RESTORE{SEP}done" in done.stdout
        except RemoteCommandError:
            restored = False
        return _ApplyOutcome(backup=backup, error=exc, restored=restored)
    return _ApplyOutcome(backup=backup)


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


def _pack(root: Path, rels: Iterable[str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel in rels:
            data = (root / rel).read_bytes()
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _unpack_ex5(tar_bytes: bytes, out_dir: Path, allowed: set[str]) -> None:
    """Write the expected ``.ex5`` members of a fetched tar under *out_dir*."""
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r") as tar:
        for member in tar.getmembers():
            name = PurePosixPath(member.name).as_posix().removeprefix("./")
            if name not in allowed or not member.isfile():
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            target = out_dir / name
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
    root = Path(src_dir)
    files = collect_sources(root)
    sources = [f for f in files if f.lower().endswith(mt5spec.SOURCE_SUFFIX)]
    source_dirs = sorted({str(PurePosixPath(s).parent) for s in sources})
    work = f"{EXPERTS_DIR}/mt5ctl-build-{uuid.uuid4().hex[:12]}"
    out = Path(out_dir)
    try:
        await transport.put_tar(host, _pack(root, files), f"{term.data_dir}/{work}")
        result = await transport.run(
            host,
            scripts.build_compile_script(
                term.data_dir,
                work,
                source_dirs,
                sources,
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
        compiled = sorted(s for s in sources if built.get(s))
        ex5 = {str(PurePosixPath(s).with_suffix(mt5spec.EX5_SUFFIX)) for s in compiled}
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
