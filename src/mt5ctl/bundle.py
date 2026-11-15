"""The deploy bundle: a local directory that mirrors a terminal's data folder.

A bundle holds exactly what mt5ctl manages on a terminal::

    <bundle>/
      MQL5/Profiles/Charts/<profile>/chart01.chr   one chart per strategy
      MQL5/Experts/<any/sub/path>/<Expert>.ex5      the compiled experts they run

Anything else in the directory (a README, the ``.mq5`` sources, build logs) is
ignored. Paths inside a bundle are POSIX-relative to the bundle root; the same
relative path names the file on the terminal, in the manifest and in a plan, so
every stage of a deploy keys on one identity.

Each file also gets a *digest*. Experts are compared by content (``sha256:``);
charts are compared by :meth:`~mt5ctl.chart.ChartFile.fingerprint`
(``chart:``), because the terminal keeps saving display state into chart files
and a byte hash would report change where nothing that trades has changed.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import mt5spec
from .chart import ChartFile
from .errors import BundleError, ChartFormatError
from .text import decode_lenient

MQL5_DIR = "MQL5"
EXPERTS_DIR = f"{MQL5_DIR}/Experts"
CHARTS_ROOT = f"{MQL5_DIR}/Profiles/Charts"
DEFAULT_PROFILE = "Default"

# Profile names become a directory on the terminal and part of shell scripts.
# Letters, digits, space, dot, underscore and hyphen cover what the terminal's
# "Save profile as" accepts in practice, without leading/trailing spaces.
_PROFILE_RE = re.compile(r"[A-Za-z0-9._-](?:[A-Za-z0-9 ._-]*[A-Za-z0-9._-])?")


def validate_profile(name: str) -> str:
    """Return *name* if it is a safe profile name, else raise :class:`BundleError`."""
    if not _PROFILE_RE.fullmatch(name) or set(name) == {"."}:
        raise BundleError(
            f"invalid profile name {name!r}: use letters, digits, spaces, '.', '_' or '-'"
        )
    return name


def charts_dir(profile: str) -> str:
    """Relative path of a profile's chart folder, e.g. ``MQL5/Profiles/Charts/Default``."""
    return f"{CHARTS_ROOT}/{validate_profile(profile)}"


def order_relpath(profile: str) -> str:
    """Relative path of a profile's window-order file (:data:`mt5spec.PROFILE_ORDER_FILE`)."""
    return f"{charts_dir(profile)}/{mt5spec.PROFILE_ORDER_FILE}"


def parse_order(data: bytes) -> tuple[str, ...]:
    """Chart file names listed in an order file, in order."""
    return tuple(line.strip() for line in decode_lenient(data).splitlines() if line.strip())


def render_order(names: Iterable[str]) -> bytes:
    """An order file as the terminal writes it: UTF-16LE, BOM, CRLF after each name."""
    return b"\xff\xfe" + "".join(f"{n}\r\n" for n in names).encode("utf-16-le")


def is_chart(relpath: str) -> bool:
    return relpath.endswith(".chr")


def classify(relpath: str, profile: str) -> str | None:
    """``"chart"`` or ``"expert"`` for a managed path, ``None`` for anything else.

    A managed path is a ``.chr`` directly inside the profile's chart folder, or
    a ``.ex5`` anywhere below ``MQL5/Experts``. Traversal is refused separately
    by :func:`validate_member`.
    """
    parts = PurePosixPath(relpath).parts
    chart_parts = tuple(charts_dir(profile).split("/"))
    if (
        len(parts) == len(chart_parts) + 1
        and parts[:-1] == chart_parts
        and relpath.endswith(".chr")
    ):
        return "chart"
    if parts[:2] == ("MQL5", "Experts") and len(parts) > 2 and relpath.endswith(".ex5"):
        return "expert"
    return None


def validate_member(relpath: str, profile: str) -> None:
    """Raise :class:`BundleError` unless *relpath* is a safe, managed path.

    The single allowlist for every stage that handles a path (bundle reading,
    upload, apply): absolute paths, backslashes, ``.``/``..`` components and
    anything outside the managed layout are refused identically everywhere.
    """
    if not relpath or relpath.startswith("/") or "\\" in relpath:
        raise BundleError(f"unsafe path (absolute, empty or with backslashes): {relpath!r}")
    if any(part in ("", ".", "..") for part in relpath.split("/")):
        raise BundleError(f"unsafe path (empty, '.' or '..' component): {relpath!r}")
    if any(ord(c) < 0x20 for c in relpath):
        raise BundleError(f"unsafe path (control character): {relpath!r}")
    if classify(relpath, profile) is None:
        raise BundleError(
            f"path outside the managed layout ({charts_dir(profile)}/*.chr or "
            f"{EXPERTS_DIR}/**/*.ex5): {relpath!r}"
        )


def expert_relpath(stored_path: str) -> str | None:
    """Map a chart's ``<expert> path=`` value to the expert's relative path.

    The terminal stores the path relative to ``MQL5`` with backslashes
    (``Experts\\Folder\\EA.ex5``). Returns ``MQL5/Experts/Folder/EA.ex5``, or
    ``None`` when the value is empty, escapes the folder, or points outside
    ``MQL5/Experts`` (an expert mt5ctl cannot manage, e.g. a Market purchase).
    """
    parts = [p for p in stored_path.strip().replace("\\", "/").split("/") if p]
    if not parts or any(p in (".", "..") for p in parts) or parts[0] != "Experts":
        return None
    if len(parts) < 2:
        return None
    rel = "/".join([MQL5_DIR, *parts])
    if not rel.lower().endswith(".ex5"):
        rel += ".ex5"
    return rel


def chart_expert(chart: ChartFile) -> str | None:
    """Relative path of the expert a chart runs, or ``None`` if none/unmanageable."""
    expert = chart.expert
    return expert_relpath(expert.path) if expert is not None else None


def file_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def chart_digest(chart: ChartFile) -> str:
    return "chart:" + chart.fingerprint()


def digest(relpath: str, data: bytes) -> str:
    """The comparison digest for a managed file (see the module docstring)."""
    if is_chart(relpath):
        return chart_digest(ChartFile.parse(data))
    return file_digest(data)


@dataclass(frozen=True, slots=True)
class Bundle:
    """A validated bundle, read from disk.

    Attributes:
        root: the bundle directory.
        profile: the one chart profile the bundle targets.
        files: every managed relative path mapped to its digest.
        chart_experts: each chart mapped to the expert it runs (charts without
            an expert, e.g. a chart that only adds a symbol to Market Watch,
            are absent).
        algo_trading_off: charts whose expert has its own "Allow Algo Trading"
            unticked: they would load but not trade.
    """

    root: Path
    profile: str
    files: Mapping[str, str]
    chart_experts: Mapping[str, str]
    algo_trading_off: tuple[str, ...] = ()

    @property
    def charts(self) -> list[str]:
        return sorted(p for p in self.files if is_chart(p))

    @property
    def experts(self) -> list[str]:
        return sorted(p for p in self.files if not is_chart(p))


def _profiles_in(root: Path) -> list[str]:
    """Profile folders of the bundle that hold any file."""
    charts_root = root / CHARTS_ROOT
    if not charts_root.is_dir():
        return []
    return sorted(
        p.name
        for p in charts_root.iterdir()
        if p.is_dir() and any(f.is_file() for f in p.rglob("*"))
    )


def _discover_profile(root: Path) -> str:
    found = _profiles_in(root)
    if len(found) == 1:
        return found[0]
    if not found:
        return DEFAULT_PROFILE
    raise BundleError(
        f"bundle has several profiles ({', '.join(found)}); a bundle targets exactly one"
    )


def read_bundle(path: str | Path, profile: str | None = None) -> Bundle:
    """Read and validate a bundle directory.

    *profile* defaults to the single profile folder found in the bundle. The
    bundle is refused when it contains a symlink, holds several profiles, has
    neither charts nor experts (most likely the wrong directory, which would
    otherwise plan the removal of everything deployed), has a chart that cannot
    be parsed, or has a chart whose expert is missing from the bundle.
    """
    root = Path(path)
    if not root.is_dir():
        raise BundleError(f"bundle directory not found: {str(path)!r}")
    chosen = validate_profile(profile) if profile is not None else _discover_profile(root)
    others = [p for p in _profiles_in(root) if p != chosen]
    if others:
        # Filtering them out would read as a bundle without charts: the plan
        # would remove every managed chart.
        raise BundleError(
            f"the bundle holds charts for profile {', '.join(repr(p) for p in others)}, but "
            f"the terminal runs profile {chosen!r}; build the bundle for {chosen!r}"
        )

    files: dict[str, str] = {}
    chart_experts: dict[str, str] = {}
    algo_off: list[str] = []
    for item in sorted(root.rglob("*")):
        rel = item.relative_to(root).as_posix()
        if item.is_symlink():
            raise BundleError(f"bundle contains a symlink (refused): {rel!r}")
        if item.is_dir() or classify(rel, chosen) is None:
            continue
        validate_member(rel, chosen)
        data = item.read_bytes()
        if is_chart(rel):
            try:
                chart = ChartFile.parse(data)
            except ChartFormatError as exc:
                raise BundleError(f"{rel}: {exc}") from None
            files[rel] = chart_digest(chart)
            if chart.expert is not None:
                ref = chart_expert(chart)
                if ref is None:
                    raise BundleError(
                        f"{rel}: expert path {chart.expert.path!r} must point into "
                        "the Experts folder (Experts\\<folder>\\<name>.ex5)"
                    )
                chart_experts[rel] = ref
                if chart.expert.algo_trading is False:
                    algo_off.append(rel)
        else:
            files[rel] = file_digest(data)

    if not files:
        raise BundleError(
            f"{str(path)!r} is not a bundle: no {charts_dir(chosen)}/*.chr and no "
            f"{EXPERTS_DIR}/**/*.ex5"
        )
    for chart_rel, ref in chart_experts.items():
        if ref not in files:
            raise BundleError(
                f"chart {chart_rel!r} runs expert {ref!r}, which is missing from the bundle"
            )
    return Bundle(
        root=root,
        profile=chosen,
        files=files,
        chart_experts=chart_experts,
        algo_trading_off=tuple(algo_off),
    )
