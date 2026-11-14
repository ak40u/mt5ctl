"""Command-line interface.

Exit codes: ``0`` success; ``1`` an error (bad bundle, unreachable host,
corrupt manifest...); ``2`` a plan that a deploy would refuse (conflicts).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any

from . import __version__, operations
from .bundle import Bundle, read_bundle
from .chart import ChartFile
from .config import load_registry
from .errors import Mt5ctlError
from .reconcile import DeployPlan

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 2


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
    print(f"mode       {expert['mode'] if expert['mode'] is not None else '-'}")
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
    return EXIT_OK


# -- plan ------------------------------------------------------------------- #


def _plan_payload(report: operations.PlanReport) -> dict[str, Any]:
    plan = report.plan
    return {
        "terminal": report.terminal.id,
        "profile": report.bundle.profile,
        "add": list(plan.add),
        "update": list(plan.update),
        "remove": list(plan.remove),
        "unchanged": list(plan.unchanged),
        "keep": list(plan.keep),
        "foreign": list(plan.foreign),
        "conflicts": list(plan.conflicts),
        "notes": list(plan.notes),
        "has_changes": plan.has_changes,
        "blocked": plan.blocked,
    }


def format_plan(plan: DeployPlan) -> list[str]:
    lines: list[str] = []
    for marker, paths in (
        ("+", plan.add),
        ("~", plan.update),
        ("-", plan.remove),
        ("!", plan.conflicts),
    ):
        lines.extend(f"  {marker} {p}" for p in paths)
    lines.extend(f"  keep {p} (still used by a remaining chart)" for p in plan.keep)
    lines.extend(f"  foreign {p} (not managed, left alone)" for p in plan.foreign)
    if plan.notes:
        lines.append("notes:")
        lines.extend(f"  {n}" for n in plan.notes)
    summary = (
        f"{len(plan.add)} to add, {len(plan.update)} to update, "
        f"{len(plan.remove)} to remove, {len(plan.unchanged)} unchanged"
    )
    if plan.blocked:
        summary += f"; BLOCKED by {len(plan.conflicts)} conflict(s)"
    elif not plan.has_changes:
        summary += "; nothing to do"
    lines.append(summary)
    return lines


def _cmd_plan(args: argparse.Namespace) -> int:
    registry = load_registry(args.config)
    report = asyncio.run(operations.plan(registry, args.terminal, args.bundle))
    if args.json:
        _emit_json(_plan_payload(report))
    else:
        print(f"plan for {report.terminal.id} (profile {report.bundle.profile!r}):")
        print("\n".join(format_plan(report.plan)))
    return EXIT_BLOCKED if report.plan.blocked else EXIT_OK


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Mt5ctlError as exc:
        print(f"mt5ctl: error: {exc}", file=sys.stderr)
        return EXIT_ERROR


def entrypoint() -> None:
    sys.exit(main())
