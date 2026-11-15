"""Command-line interface.

Exit codes: ``0`` success; ``1`` an error (bad bundle, unreachable host,
corrupt manifest, failed compile...); ``2`` a plan that a deploy would refuse
(conflicts); ``3`` a terminal that is not healthy (after deploy, restart,
verify, or in status).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
from collections.abc import Coroutine, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any, TypeVar, cast

import anyio

from . import __version__, operations
from . import formatting as fmt
from .bundle import Bundle, read_bundle
from .chart import ChartFile
from .config import load_registry, write_starter
from .errors import Mt5ctlError

T = TypeVar("T")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 2
EXIT_UNHEALTHY = 3


def _emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


# -- chart show ------------------------------------------------------------- #


def _chart_payload(chart: ChartFile) -> dict[str, Any]:
    expert = chart.expert
    fmt = chart.format
    return {
        "symbol": chart.symbol,
        "timeframe": str(chart.timeframe) if chart.timeframe else None,
        "expert": None
        if expert is None
        else {
            "name": expert.name,
            "path": expert.path,
            "mode": expert.mode,
            "algo_trading": expert.algo_trading,
            "inputs": dict(expert.inputs),
        },
        "encoding": fmt.encoding + ("+bom" if fmt.bom else ""),
        "newline": "CRLF" if fmt.newline == "\r\n" else "LF",
    }


def _cmd_chart_show(args: argparse.Namespace) -> int:
    chart = ChartFile.read(args.file)
    payload = _chart_payload(chart)
    if args.json:
        _emit_json(payload)
        return EXIT_OK
    print(f"symbol     {payload['symbol'] or '-'}")
    print(f"timeframe  {payload['timeframe'] or '-'}")
    print(f"format     {payload['encoding']}, {payload['newline']}")
    expert = payload["expert"]
    if expert is None:
        print("expert     (none)")
        return EXIT_OK
    print(f"expert     {expert['name']}")
    print(f"path       {expert['path']}")
    algo = {True: " (algo trading allowed)", False: " (algo trading OFF)", None: ""}
    mode = expert["mode"] if expert["mode"] is not None else "-"
    print(f"mode       {mode}{algo[chart.expert.algo_trading if chart.expert else None]}")
    print(f"inputs     {len(expert['inputs'])}")
    width = max((len(k) for k in expert["inputs"]), default=0)
    for key, value in expert["inputs"].items():
        print(f"  {key:<{width}}  {value}")
    return EXIT_OK


# -- bundle check ----------------------------------------------------------- #


def _bundle_payload(bundle: Bundle) -> dict[str, Any]:
    return {
        "profile": bundle.profile,
        "charts": {c: bundle.chart_experts.get(c) for c in bundle.charts},
        "experts": bundle.experts,
        "algo_trading_off": list(bundle.algo_trading_off),
    }


def _cmd_bundle_check(args: argparse.Namespace) -> int:
    bundle = read_bundle(args.bundle, profile=args.profile)
    payload = _bundle_payload(bundle)
    if args.json:
        _emit_json(payload)
        return EXIT_OK
    print(
        f"bundle OK: profile {bundle.profile!r}, {len(bundle.charts)} charts, "
        f"{len(bundle.experts)} experts"
    )
    for chart, expert in payload["charts"].items():
        print(f"  {chart.rsplit('/', 1)[-1]}  ->  {expert or '(no expert)'}")
    unused = sorted(set(bundle.experts) - set(bundle.chart_experts.values()))
    for expert in unused:
        print(f"  note: {expert} is not used by any chart")
    for chart in bundle.algo_trading_off:
        name = chart.rsplit("/", 1)[-1]
        print(f"  warning: {name} has algo trading turned off; its expert will not trade")
    return EXIT_OK


# -- plan ------------------------------------------------------------------- #


def _cmd_plan(args: argparse.Namespace) -> int:
    registry = load_registry(args.config)
    report = _run(operations.plan(registry, args.terminal, args.bundle))
    if args.json:
        _emit_json(
            {
                "terminal": report.terminal.id,
                "profile": report.bundle.profile,
                **fmt.plan_payload(report.plan),
            }
        )
    else:
        print(f"plan for {report.terminal.id} (profile {report.bundle.profile!r}):")
        print("\n".join(fmt.plan_lines(report.plan)))
    return EXIT_BLOCKED if report.plan.blocked else EXIT_OK


# -- deploy, adopt, lifecycle, compile ---------------------------------------- #


def _output(args: argparse.Namespace, payload: Any, lines: list[str]) -> None:
    if args.json:
        _emit_json(payload)
    else:
        print("\n".join(lines))


def _cmd_deploy(args: argparse.Namespace) -> int:
    registry = load_registry(args.config)
    result = _run(
        operations.deploy(
            registry,
            args.terminal,
            args.bundle,
            confirm=args.confirm_live,
            verify_timeout=args.verify_timeout,
        )
    )
    _output(args, fmt.deploy_payload(result), fmt.deploy_lines(result))
    return (
        EXIT_UNHEALTHY if result.verify is not None and not result.verify.healthy else EXIT_OK
    )


def _cmd_adopt(args: argparse.Namespace) -> int:
    registry = load_registry(args.config)
    result = _run(
        operations.adopt(registry, args.terminal, args.bundle, confirm=args.confirm_live)
    )
    _output(args, asdict(result), fmt.adopt_lines(result))
    return EXIT_OK


def _cmd_status(args: argparse.Namespace) -> int:
    registry = load_registry(args.config)
    rows = _run(operations.status(registry, args.terminals or None))
    _output(args, fmt.status_payload(rows), fmt.status_lines(rows))
    return EXIT_OK if all(r.healthy for r in rows) else EXIT_UNHEALTHY


def _cmd_restart(args: argparse.Namespace) -> int:
    registry = load_registry(args.config)
    result = _run(
        operations.restart(
            registry,
            args.terminal,
            confirm=args.confirm_live,
            verify_timeout=args.verify_timeout,
        )
    )
    _output(
        args,
        {"terminal": result.terminal, "verify": fmt.verify_payload(result.verify)},
        fmt.restart_lines(result),
    )
    return EXIT_OK if result.verify.healthy else EXIT_UNHEALTHY


def _cmd_start(args: argparse.Namespace) -> int:
    registry = load_registry(args.config)
    result = _run(
        operations.start(
            registry,
            args.terminal,
            confirm=args.confirm_live,
            verify_timeout=args.verify_timeout,
        )
    )
    _output(
        args,
        {"terminal": result.terminal, "verify": fmt.verify_payload(result.verify)},
        fmt.restart_lines(result, verb="started"),
    )
    return EXIT_OK if result.verify.healthy else EXIT_UNHEALTHY


def _cmd_stop(args: argparse.Namespace) -> int:
    registry = load_registry(args.config)
    result = _run(operations.stop(registry, args.terminal, confirm=args.confirm_live))
    _output(
        args,
        {
            "terminal": result.terminal,
            "clean": result.clean,
            "exit_code": result.exit_code,
            "unit_result": result.unit_result,
        },
        fmt.stop_lines(result),
    )
    return EXIT_OK


def _cmd_verify(args: argparse.Namespace) -> int:
    registry = load_registry(args.config)
    report = _run(operations.verify(registry, args.terminal, timeout=args.timeout))
    state = "healthy" if report.healthy else "NOT healthy"
    _output(args, fmt.verify_payload(report), [f"{args.terminal}: {state} — {report.detail}"])
    return EXIT_OK if report.healthy else EXIT_UNHEALTHY


def _cmd_compile(args: argparse.Namespace) -> int:
    registry = load_registry(args.config)
    result = _run(
        operations.compile_sources(
            registry, args.terminal, args.sources, args.out, confirm=args.confirm_live
        )
    )
    _output(args, fmt.compile_payload(result), fmt.compile_lines(result))
    return EXIT_OK if result.ok else EXIT_ERROR


def _cmd_experts(args: argparse.Namespace) -> int:
    registry = load_registry(args.config)
    reports = _run(operations.experts(registry, args.terminals or None))
    lines = (fmt.autotrading_lines if args.command == "autotrading" else fmt.ea_list_lines)(
        reports
    )
    _output(args, [asdict(r) for r in reports], lines)
    return EXIT_ERROR if any(r.error for r in reports) else EXIT_OK


def _cmd_screenshot(args: argparse.Namespace) -> int:
    registry = load_registry(args.config)
    shot = _run(operations.screenshot(registry, args.terminal))
    target = (
        Path(args.output)
        if args.output
        else Path(f"{args.terminal}-{time.strftime('%Y%m%d-%H%M%S')}.png")
    )
    if target.is_dir():
        target = target / f"{args.terminal}-{time.strftime('%Y%m%d-%H%M%S')}.png"
    target.write_bytes(shot.png)
    _output(
        args,
        {"terminal": shot.terminal, "path": str(target), "size": shot.size},
        [f"saved {target} ({shot.size} bytes)"],
    )
    return EXIT_OK


def _cmd_init(args: argparse.Namespace) -> int:
    target = write_starter(args.path)
    print(f"wrote {target}\nedit it, then check it with: mt5ctl doctor")
    return EXIT_OK


def _cmd_doctor(args: argparse.Namespace) -> int:
    registry = load_registry(args.config)
    checks = _run(operations.doctor(registry))
    _output(args, [asdict(c) for c in checks], fmt.doctor_lines(checks))
    return EXIT_ERROR if any(c.status == "fail" for c in checks) else EXIT_OK


def _cmd_logs(args: argparse.Namespace) -> int:
    registry = load_registry(args.config)
    result = _run(
        operations.logs(
            registry,
            args.terminal,
            source="experts" if args.experts else "journal",
            pattern=args.grep,
            lines=args.lines,
        )
    )
    _output(args, asdict(result), fmt.logs_lines(result))
    return EXIT_OK


def _cmd_info(args: argparse.Namespace) -> int:
    registry = load_registry(args.config)
    rows = _run(operations.info(registry, args.terminals or None))
    _output(args, [asdict(r) for r in rows], fmt.info_lines(rows))
    return EXIT_ERROR if any(r.error for r in rows) else EXIT_OK


def _cmd_login(args: argparse.Namespace) -> int:
    password = None
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    registry = load_registry(args.config)
    result = _run(
        operations.login(
            registry,
            args.terminal,
            server=args.server,
            account=args.account,
            password=password,
            confirm=args.confirm_live,
            verify_timeout=args.verify_timeout,
        )
    )
    _output(args, asdict(result), fmt.login_lines(result))
    return EXIT_OK if result.ok else EXIT_ERROR


def _cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(args.config)
    return EXIT_OK


# -- list ------------------------------------------------------------------- #


def _cmd_list(args: argparse.Namespace) -> int:
    registry = load_registry(args.config)
    rows = [
        (t.id, t.env.value, registry.host_of(t).id, registry.host_of(t).kind.value, t.data_dir)
        for t in registry.terminals.values()
    ]
    if args.json:
        keys = ("terminal", "env", "host", "kind", "data_dir")
        _emit_json([dict(zip(keys, row, strict=True)) for row in rows])
        return EXIT_OK
    widths = [max(len(str(r[i])) for r in rows) for i in range(4)]
    for row in rows:
        print("  ".join(f"{row[i]:<{widths[i]}}" for i in range(4)) + f"  {row[4]}")
    return EXIT_OK


# -- parser ----------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mt5ctl",
        description="Deploy and inspect expert advisors on MetaTrader 5 terminals.",
    )
    parser.add_argument("--version", action="version", version=f"mt5ctl {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    def with_json(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--json", action="store_true", help="machine-readable output")
        return p

    def with_config(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--config", metavar="PATH", help="terminal registry (terminals.yaml)")
        return p

    chart = sub.add_parser("chart", help="inspect chart (.chr) files")
    chart_sub = chart.add_subparsers(dest="chart_command", required=True, metavar="<action>")
    show = with_json(
        chart_sub.add_parser("show", help="print a chart's symbol, expert, inputs")
    )
    show.add_argument("file", help="path to a .chr file")
    show.set_defaults(func=_cmd_chart_show)

    bundle = sub.add_parser("bundle", help="validate deploy bundles")
    bundle_sub = bundle.add_subparsers(
        dest="bundle_command", required=True, metavar="<action>"
    )
    check = with_json(bundle_sub.add_parser("check", help="validate a bundle directory"))
    check.add_argument("bundle", help="bundle directory")
    check.add_argument("--profile", help="chart profile (default: the one in the bundle)")
    check.set_defaults(func=_cmd_bundle_check)

    plan = with_config(
        with_json(sub.add_parser("plan", help="show what a deploy would change (read-only)"))
    )
    plan.add_argument("terminal", help="terminal id from the registry")
    plan.add_argument("bundle", help="bundle directory")
    plan.set_defaults(func=_cmd_plan)

    listing = with_config(with_json(sub.add_parser("list", help="list registered terminals")))
    listing.set_defaults(func=_cmd_list)

    def with_live(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument(
            "--confirm",
            "--confirm-live",
            dest="confirm_live",
            action="store_true",
            help="required to change a terminal with env: live",
        )
        return p

    def with_verify(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument(
            "--verify-timeout",
            type=float,
            metavar="SECONDS",
            help="how long to wait for a healthy start "
            f"(default {operations.VERIFY_TIMEOUT_S:.0f})",
        )
        return p

    def terminal_command(name: str, help_: str) -> argparse.ArgumentParser:
        p = with_config(with_json(sub.add_parser(name, help=help_)))
        p.add_argument("terminal", help="terminal id from the registry")
        return p

    deploy = with_verify(with_live(terminal_command("deploy", "apply a bundle to a terminal")))
    deploy.add_argument("bundle", help="bundle directory")
    deploy.set_defaults(func=_cmd_deploy)

    adopt = with_live(
        terminal_command(
            "adopt", "take a terminal that already runs a bundle under management"
        )
    )
    adopt.add_argument("bundle", help="bundle directory")
    adopt.set_defaults(func=_cmd_adopt)

    status = with_config(
        with_json(sub.add_parser("status", help="service, connection, journal"))
    )
    status.add_argument("terminals", nargs="*", metavar="terminal", help="default: all")
    status.set_defaults(func=_cmd_status)

    for name, help_ in (
        ("ea-list", "each chart's expert: loaded, allowed to trade"),
        ("autotrading", "the Algo Trading button and which experts can trade"),
    ):
        p = with_config(with_json(sub.add_parser(name, help=help_)))
        p.add_argument("terminals", nargs="*", metavar="terminal", help="default: all")
        p.set_defaults(func=_cmd_experts)

    shot = terminal_command("screenshot", "save a picture of the terminal's screen (PNG)")
    shot.add_argument(
        "-o", "--output", help="file or folder (default: <terminal>-<time>.png here)"
    )
    shot.set_defaults(func=_cmd_screenshot)

    init = sub.add_parser("init", help="write a starter registry (terminals.yaml)")
    init.add_argument(
        "path", nargs="?", help="where (default: ~/.config/mt5ctl/terminals.yaml)"
    )
    init.set_defaults(func=_cmd_init)

    doctor = with_config(
        with_json(sub.add_parser("doctor", help="check hosts, units, folders and connections"))
    )
    doctor.set_defaults(func=_cmd_doctor)

    info = with_config(
        with_json(
            sub.add_parser("info", help="build, account, server and session from the journal")
        )
    )
    info.add_argument("terminals", nargs="*", metavar="terminal", help="default: all")
    info.set_defaults(func=_cmd_info)

    logs = terminal_command("logs", "the end of the newest journal (or experts' log)")
    logs.add_argument(
        "-n", "--lines", type=int, default=50, help="how many lines (default 50)"
    )
    logs.add_argument("--grep", metavar="REGEX", help="only lines matching (case-insensitive)")
    logs.add_argument(
        "--experts", action="store_true", help="the experts' log (Print output) instead"
    )
    logs.set_defaults(func=_cmd_logs)

    restart = with_verify(
        with_live(terminal_command("restart", "restart the terminal, then verify"))
    )
    restart.set_defaults(func=_cmd_restart)

    start = with_verify(
        with_live(terminal_command("start", "start the terminal, then verify"))
    )
    start.set_defaults(func=_cmd_start)

    stop = with_live(terminal_command("stop", "stop the terminal and wait for its exit"))
    stop.set_defaults(func=_cmd_stop)

    verify = terminal_command("verify", "check service, connection and loaded experts")
    verify.add_argument(
        "--timeout", type=float, metavar="SECONDS", help="keep checking this long"
    )
    verify.set_defaults(func=_cmd_verify)

    login = with_verify(
        with_live(terminal_command("login", "log a terminal in once, headless"))
    )
    login.add_argument(
        "--server", required=True, help="broker server, e.g. ExampleBroker-Demo"
    )
    login.add_argument("--account", help="login number (default: the registry's account)")
    login.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from stdin (otherwise MT5CTL_PASSWORD_<account> or "
        "the credentials file)",
    )
    login.set_defaults(func=_cmd_login)

    comp = with_live(
        terminal_command("compile", "compile .mq5 sources with the terminal's MetaEditor")
    )
    comp.add_argument("sources", help="local folder with .mq5 sources (and includes)")
    comp.add_argument("out", help="local folder to write the .ex5 files to")
    comp.set_defaults(func=_cmd_compile)

    serve = with_config(sub.add_parser("serve", help="run the MCP server on stdio"))
    serve.set_defaults(func=_cmd_serve)
    return parser


EXIT_INTERRUPTED = 130
_SIGNALS = tuple(
    getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGHUP") if hasattr(signal, name)
)


def _run(coro: Coroutine[Any, Any, T]) -> T:
    """Run one operation, turning Ctrl-C, SIGTERM and SIGHUP into a cancellation.

    Under a plain ``asyncio.run`` a signal cancels the task outright, and a
    second Ctrl-C kills the process in the middle of whatever it does. Here a
    signal cancels the operation the way an MCP client does: the steps that
    must not be cut short (an apply, starting the terminal again, releasing
    the lock) finish first. Further signals only say so.
    """
    if not _SIGNALS or sys.platform == "win32":
        return asyncio.run(coro)
    state: dict[str, Any] = {}

    async def guarded() -> None:
        async with anyio.create_task_group() as tg:

            async def work() -> None:
                try:
                    with anyio.CancelScope() as scope:
                        state["scope"] = scope
                        state["result"] = await coro
                except Exception as exc:  # re-raised below, outside the task group
                    state["error"] = exc
                finally:
                    tg.cancel_scope.cancel()

            async def watch() -> None:
                with anyio.open_signal_receiver(*_SIGNALS) as signals:
                    async for signum in signals:
                        first = "signal" not in state
                        state["signal"] = signum
                        if first:
                            print(
                                "mt5ctl: interrupted; finishing the steps that must not be "
                                "cut short (starting the terminal again, releasing the lock)",
                                file=sys.stderr,
                            )
                            if "scope" in state:
                                state["scope"].cancel()
                        else:
                            print("mt5ctl: still finishing, please wait", file=sys.stderr)

            tg.start_soon(work)
            tg.start_soon(watch)

    anyio.run(guarded)
    if "error" in state:
        raise state["error"]
    if "result" not in state:
        raise KeyboardInterrupt
    return cast(T, state["result"])


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Mt5ctlError as exc:
        print(f"mt5ctl: error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("mt5ctl: interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except (OSError, ValueError) as exc:
        # A missing file, a bad argument: say so, without a traceback.
        print(f"mt5ctl: error: {exc}", file=sys.stderr)
        return EXIT_ERROR


def entrypoint() -> None:
    sys.exit(main())
