"""Collect a terminal's :class:`~mt5ctl.reconcile.TerminalState`.

Two collectors produce the same state:

* :func:`read_local_state` reads a data folder on this machine (a Wine prefix,
  or a Windows terminal when mt5ctl runs on that Windows host);
* :func:`build_state_script` + :func:`parse_state_output` do the same over SSH
  in a single round trip.

The remote script only *ships bytes*: the manifest, every chart in the profile,
and a sha256 per bundle expert. All parsing happens here in Python with the
same :mod:`mt5ctl.chart` code that reads bundles, so local and remote state can
never disagree about what a chart runs. Chart files are UTF-16 on a real
terminal, which shell tools handle poorly anyway.

Only bundle paths are probed for content. Paths that are managed but no longer
in the bundle are decided from the manifest and the live charts alone, so their
digests are never needed.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable, Mapping
from pathlib import Path

from .bundle import (
    chart_digest,
    charts_dir,
    expert_relpath,
    file_digest,
    is_chart,
    order_relpath,
    parse_order,
)
from .chart import ChartFile
from .errors import ChartFormatError, StateError
from .manifest import MANIFEST_RELPATH, parse_manifest
from .reconcile import TerminalState
from .scripts import HELPERS, SEP, sh_quote


def _chart_ref(chart: ChartFile) -> tuple[str | None, bool]:
    """``(expert relpath or None, unresolved?)`` for a live chart.

    Unresolved means the chart runs an expert we cannot locate (empty or
    escaping path), so it might be running any managed expert.
    """
    expert = chart.expert
    if expert is None:
        return None, False
    stored = expert.path.strip()
    if not stored or ".." in stored.replace("\\", "/").split("/"):
        return None, True
    # A path outside Experts (e.g. a Market product) is resolved: it is simply
    # not an expert mt5ctl manages.
    return expert_relpath(stored), False


def assemble_state(
    manifest_raw: str | None,
    charts: Mapping[str, bytes | None],
    expert_digests: Mapping[str, str | None],
    bundle_paths: Iterable[str],
    order: bytes | None = None,
) -> TerminalState:
    """Build a :class:`TerminalState` from raw collected data.

    Args:
        manifest_raw: ``deployed.json`` text, ``None`` when absent.
        charts: every live chart (relpath) -> its bytes, ``None`` if unreadable.
        expert_digests: present bundle experts (relpath) -> digest, ``None`` if
            unreadable.
        bundle_paths: the bundle's paths, to report which charts are present.
        order: the profile's order file, ``None`` when absent.
    """
    manifest = parse_manifest(manifest_raw)
    files: dict[str, str | None] = dict(expert_digests)
    refs: dict[str, str | None] = {}
    unresolved: set[str] = set()
    parsed: dict[str, ChartFile | None] = {}
    for rel, data in charts.items():
        try:
            parsed[rel] = ChartFile.parse(data) if data is not None else None
        except ChartFormatError:
            parsed[rel] = None
        chart = parsed[rel]
        if chart is None:
            refs[rel] = None
            unresolved.add(rel)
            continue
        refs[rel], is_unresolved = _chart_ref(chart)
        if is_unresolved:
            unresolved.add(rel)
    for rel in bundle_paths:
        if is_chart(rel) and rel in parsed:
            chart = parsed[rel]
            files[rel] = chart_digest(chart) if chart is not None else None
    return TerminalState(
        manifest=manifest,
        files=files,
        charts=refs,
        unresolved_charts=frozenset(unresolved),
        order=parse_order(order) if order is not None else None,
    )


# --------------------------------------------------------------------------- #
# Local
# --------------------------------------------------------------------------- #


def read_local_charts(data_dir: str | Path, profile: str) -> dict[str, bytes | None]:
    """The raw chart files of a profile on this machine (``None``: unreadable)."""
    charts: dict[str, bytes | None] = {}
    profile_dir = Path(data_dir) / charts_dir(profile)
    if profile_dir.is_dir():
        for item in sorted(profile_dir.glob("*.chr")):
            if not item.is_file():
                continue
            rel = f"{charts_dir(profile)}/{item.name}"
            try:
                charts[rel] = item.read_bytes()
            except OSError:
                charts[rel] = None
    return charts


def charts_from_output(stdout: str, profile: str) -> dict[str, bytes | None]:
    """The raw chart files in :func:`build_state_script` output."""
    charts: dict[str, bytes | None] = {}
    try:
        for line in stdout.splitlines():
            tag, _, rest = line.partition(SEP)
            if tag == "CHART":
                name_b64, _, content_b64 = rest.partition(SEP)
                name = _b64(name_b64).decode("utf-8", "replace")
                charts[f"{charts_dir(profile)}/{name}"] = (
                    _b64(content_b64) if content_b64 else None
                )
    except (binascii.Error, ValueError) as exc:
        raise StateError(f"unreadable state output from the terminal host: {exc}") from None
    return charts


def read_local_state(
    data_dir: str | Path, profile: str, bundle_paths: Iterable[str]
) -> TerminalState:
    """Collect the state of a terminal whose data folder is on this machine."""
    root = Path(data_dir)
    if not root.is_dir():
        raise StateError(f"terminal data folder not found: {root}")
    manifest_path = root / MANIFEST_RELPATH
    manifest_raw = (
        manifest_path.read_text(encoding="utf-8") if manifest_path.is_file() else None
    )

    charts = read_local_charts(root, profile)

    paths = list(bundle_paths)
    experts: dict[str, str | None] = {}
    for rel in paths:
        if is_chart(rel):
            continue
        target = root / rel
        if target.is_file():
            try:
                experts[rel] = file_digest(target.read_bytes())
            except OSError:
                experts[rel] = None
    order_path = root / order_relpath(profile)
    order = order_path.read_bytes() if order_path.is_file() else None
    return assemble_state(manifest_raw, charts, experts, paths, order)


# --------------------------------------------------------------------------- #
# Remote
# --------------------------------------------------------------------------- #


def build_state_script(data_dir: str, profile: str, bundle_paths: Iterable[str]) -> str:
    """A read-only bash script that prints a terminal's raw state.

    Output lines (``|``-separated; every free-form field is base64 so no file
    name or content can break the framing):

    * ``MANIFEST|<base64 of deployed.json>`` or ``MANIFEST|MISSING``
    * ``ORDER|<base64 of the profile's order file>`` or ``ORDER|MISSING``
    * ``CHART|<base64 file name>|<base64 content>`` for each ``*.chr`` in the profile
    * ``EXPERT|<index>|<sha256 hex>`` for each bundle expert that exists, where
      *index* is its position in the expert list passed in (an empty hash means
      the file exists but could not be read)

    Every value embedded in the script is single-quoted; nothing is
    interpolated into a double-quoted context.
    """
    experts = [p for p in bundle_paths if not is_chart(p)]
    probes = "\n".join(f"probe {i} {sh_quote(rel)}" for i, rel in enumerate(experts))
    return f"""\
set -u
{HELPERS}
DIR={sh_quote(data_dir)}
PROFILE_DIR="$DIR/"{sh_quote(charts_dir(profile))}
MF="$DIR/"{sh_quote(MANIFEST_RELPATH)}
if [ -f "$MF" ]; then echo "MANIFEST{SEP}$(b64 "$MF")"; else echo "MANIFEST{SEP}MISSING"; fi
OF="$DIR/"{sh_quote(order_relpath(profile))}
if [ -f "$OF" ]; then echo "ORDER{SEP}$(b64 "$OF")"; else echo "ORDER{SEP}MISSING"; fi
if [ -d "$PROFILE_DIR" ]; then
  for f in "$PROFILE_DIR"/*.chr; do
    [ -f "$f" ] || continue
    name=$(printf '%s' "${{f##*/}}" | base64 | tr -d '\\n')
    echo "CHART{SEP}$name{SEP}$(b64 "$f" 2>/dev/null)"
  done
fi
probe() {{
  if [ -f "$DIR/$2" ]; then echo "EXPERT{SEP}$1{SEP}$(sha "$DIR/$2" 2>/dev/null)"; fi
}}
{probes}
"""


def _b64(token: str) -> bytes:
    return base64.b64decode(token.strip(), validate=True)


def parse_state_output(
    stdout: str, profile: str, bundle_paths: Iterable[str]
) -> TerminalState:
    """Turn :func:`build_state_script` output into a :class:`TerminalState`."""
    paths = list(bundle_paths)
    experts = [p for p in paths if not is_chart(p)]
    manifest_raw: str | None = None
    order: bytes | None = None
    seen_manifest = False
    charts: dict[str, bytes | None] = {}
    digests: dict[str, str | None] = {}
    try:
        for line in stdout.splitlines():
            tag, _, rest = line.partition(SEP)
            if tag == "MANIFEST":
                seen_manifest = True
                manifest_raw = None if rest == "MISSING" else _b64(rest).decode("utf-8")
            elif tag == "ORDER":
                order = None if rest == "MISSING" else _b64(rest)
            elif tag == "CHART":
                name_b64, _, content_b64 = rest.partition(SEP)
                name = _b64(name_b64).decode("utf-8", "replace")
                rel = f"{charts_dir(profile)}/{name}"
                charts[rel] = _b64(content_b64) if content_b64 else None
            elif tag == "EXPERT":
                index, _, sha = rest.partition(SEP)
                rel = experts[int(index)]
                digests[rel] = f"sha256:{sha}" if sha else None
    except (binascii.Error, ValueError, IndexError) as exc:
        raise StateError(f"unreadable state output from the terminal host: {exc}") from None
    if not seen_manifest:
        raise StateError("state output from the terminal host is truncated")
    return assemble_state(manifest_raw, charts, digests, paths, order)
