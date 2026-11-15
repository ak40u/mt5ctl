"""Lossless reader and writer for MetaTrader 5 chart files (``chartNN.chr``).

A chart file lives in ``MQL5/Profiles/Charts/<profile>/`` and describes one open
chart: symbol, timeframe, the attached expert with its inputs, and the
indicator windows. The format is line-oriented:

* ``<tag>`` and ``</tag>`` on their own line open and close a section
  (``<chart>``, ``<expert>``, ``<inputs>``, ``<window>``, ``<indicator>`` ...);
* ``key=value`` is an entry, split on the *first* ``=`` so values may contain
  ``=`` themselves;
* any other line (blank lines included) is kept verbatim.

The terminal writes these files as UTF-16 text (see :mod:`mt5ctl.mt5spec`), but
files written by other tools may be UTF-8. :class:`ChartFile`
remembers the encoding, BOM, line ending and trailing newline it was read with,
and writes them back unchanged, so ``parse(data).to_bytes() == data`` holds for
every well-formed file. That property is what makes editing safe: changing one
input never disturbs the hundreds of display settings around it.

Only the parts mt5ctl reasons about get a typed view (:attr:`ChartFile.symbol`,
:attr:`ChartFile.timeframe`, :attr:`ChartFile.expert`). Everything else stays an
ordered tree of :class:`Entry`, :class:`Text` and :class:`Section` nodes.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import mt5spec
from .errors import ChartFormatError
from .text import TextDecodeError, TextFormat, decode

__all__ = [
    "ChartFile",
    "Entry",
    "Expert",
    "Node",
    "Section",
    "Text",
    "TextFormat",
    "Timeframe",
    "decode",
    "parse_nodes",
]

_TAG_RE = re.compile(r"<(/?)([A-Za-z_][A-Za-z0-9_]*)>")


# --------------------------------------------------------------------------- #
# Timeframes
# --------------------------------------------------------------------------- #

_TIMEFRAME_RE = re.compile(r"(MN|M|H|D|W)([0-9]+)")


@dataclass(frozen=True, slots=True)
class Timeframe:
    """A chart timeframe by its usual name (``M15``, ``H4``, ``D1``).

    Only timeframes whose chart-file encoding is known are accepted; see
    :data:`mt5ctl.mt5spec.PERIOD_CODES`.
    """

    name: str

    def __post_init__(self) -> None:
        if self.name not in mt5spec.PERIOD_CODES:
            known = ", ".join(mt5spec.PERIOD_CODES)
            raise ValueError(f"unsupported timeframe {self.name!r} (known: {known})")

    @classmethod
    def parse(cls, text: str) -> Timeframe:
        """Parse a name such as ``m15`` or ``H4``."""
        name = text.strip().upper()
        if not _TIMEFRAME_RE.fullmatch(name):
            raise ValueError(f"not a timeframe: {text!r} (expected e.g. M15, H1, H4, D1)")
        return cls(name)

    @classmethod
    def from_codes(cls, period_type: int, period_size: int) -> Timeframe | None:
        """The timeframe stored as ``period_type``/``period_size``, if known."""
        codes = (period_type, period_size)
        return next(
            (cls(name) for name, value in mt5spec.PERIOD_CODES.items() if value == codes), None
        )

    @property
    def codes(self) -> tuple[int, int]:
        return mt5spec.PERIOD_CODES[self.name]

    def __str__(self) -> str:
        return self.name


# --------------------------------------------------------------------------- #
# Tree
# --------------------------------------------------------------------------- #


def _check_line_value(what: str, value: str) -> None:
    if "\n" in value or "\r" in value:
        raise ValueError(f"{what} must not contain a line break: {value!r}")


def _check_key(key: str) -> None:
    if not key or "=" in key or key.startswith("<") or "\n" in key or "\r" in key:
        raise ValueError(f"invalid entry key: {key!r}")


@dataclass(slots=True)
class Entry:
    """A ``key=value`` line. Both sides are kept exactly as written."""

    key: str
    value: str

    def render(self) -> str:
        return f"{self.key}={self.value}"


@dataclass(slots=True)
class Text:
    """A line that is neither a tag nor an entry (blank lines, mostly)."""

    text: str

    def render(self) -> str:
        return self.text


@dataclass(slots=True)
class Section:
    """A ``<tag> ... </tag>`` block and its ordered children."""

    tag: str
    children: list[Node] = field(default_factory=list)

    def entries(self) -> list[Entry]:
        """Direct ``key=value`` children, in file order."""
        return [c for c in self.children if isinstance(c, Entry)]

    def sections(self, tag: str) -> list[Section]:
        """Direct child sections named *tag*, in file order."""
        return [c for c in self.children if isinstance(c, Section) and c.tag == tag]

    def section(self, tag: str) -> Section | None:
        """The first direct child section named *tag*, if any."""
        found = self.sections(tag)
        return found[0] if found else None

    def get(self, key: str) -> str | None:
        """Value of the first direct entry named *key*."""
        for entry in self.entries():
            if entry.key == key:
                return entry.value
        return None

    def set(self, key: str, value: str) -> None:
        """Replace the first entry named *key*, or add one after the last entry.

        Adding after the last direct entry (rather than at the very end) keeps
        new keys ahead of nested sections, where the terminal puts its own.
        """
        _check_key(key)
        _check_line_value(f"value of {key!r}", value)
        for entry in self.entries():
            if entry.key == key:
                entry.value = value
                return
        last = max(
            (i for i, c in enumerate(self.children) if isinstance(c, Entry)), default=-1
        )
        self.children.insert(last + 1, Entry(key, value))

    def lines(self) -> list[str]:
        out = [f"<{self.tag}>"]
        for child in self.children:
            out.extend(child.lines() if isinstance(child, Section) else [child.render()])
        out.append(f"</{self.tag}>")
        return out


Node = Entry | Text | Section


def parse_nodes(text: str) -> list[Node]:
    """Parse decoded chart text into a node list (top level: usually one ``<chart>``)."""
    body = text.replace("\r\n", "\n")
    raw_lines = body.split("\n")
    if body.endswith("\n"):
        raw_lines.pop()

    top: list[Node] = []
    stack: list[Section] = []
    for number, line in enumerate(raw_lines, start=1):
        current = stack[-1].children if stack else top
        tag = _TAG_RE.fullmatch(line)
        if tag is not None and not tag.group(1):
            section = Section(tag.group(2))
            current.append(section)
            stack.append(section)
        elif tag is not None:
            name = tag.group(2)
            if not stack or stack[-1].tag != name:
                expected = f"</{stack[-1].tag}>" if stack else "no open section"
                raise ChartFormatError(f"line {number}: </{name}> does not close ({expected})")
            stack.pop()
        elif "=" in line and not line.startswith("<"):
            key, value = line.split("=", 1)
            current.append(Entry(key, value))
        else:
            current.append(Text(line))
    if stack:
        raise ChartFormatError(f"unclosed section <{stack[-1].tag}> at end of file")
    return top


# --------------------------------------------------------------------------- #
# Expert view
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Expert:
    """The expert advisor attached to a chart (its ``<expert>`` block).

    ``path`` is stored relative to the ``MQL5`` folder with backslashes, for
    example ``Experts\\Portfolio\\TrendFollower.ex5``. ``mode`` is the raw
    ``expertmode`` bitmask (``None`` when absent or not a number). ``inputs`` are
    the ``<inputs>`` entries in file order, values exactly as the terminal stores
    them.
    """

    name: str
    path: str
    mode: int | None = None
    inputs: tuple[tuple[str, str], ...] = ()

    def input(self, key: str) -> str | None:
        """Value of one input, or ``None`` when the expert has no such input."""
        return next((v for k, v in self.inputs if k == key), None)

    @property
    def algo_trading(self) -> bool | None:
        """Whether the expert's own "Allow Algo Trading" is ticked; ``None`` if unknown."""
        if self.mode is None:
            return None
        return bool(self.mode & mt5spec.EXPERT_MODE_ALGO_TRADING)


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Chart file
# --------------------------------------------------------------------------- #


class ChartFile:
    """One ``.chr`` file: an editable node tree plus the on-disk text format."""

    __slots__ = ("format", "nodes")

    def __init__(self, nodes: list[Node], fmt: TextFormat) -> None:
        self.nodes = nodes
        self.format = fmt
        charts = [n for n in nodes if isinstance(n, Section) and n.tag == "chart"]
        if len(charts) != 1:
            raise ChartFormatError(
                f"a chart file must hold exactly one <chart> section, found {len(charts)}"
            )

    # -- I/O ---------------------------------------------------------------- #

    @classmethod
    def parse(cls, data: bytes) -> ChartFile:
        try:
            text, fmt = decode(data)
        except TextDecodeError as exc:
            raise ChartFormatError(f"cannot decode chart file: {exc}") from None
        return cls(parse_nodes(text), fmt)

    @classmethod
    def read(cls, path: str | Path) -> ChartFile:
        try:
            return cls.parse(Path(path).read_bytes())
        except ChartFormatError as exc:
            raise ChartFormatError(f"{path}: {exc}") from None

    def to_bytes(self) -> bytes:
        lines: list[str] = []
        for node in self.nodes:
            lines.extend(node.lines() if isinstance(node, Section) else [node.render()])
        fmt = self.format
        text = fmt.newline.join(lines) + (fmt.newline if fmt.final_newline else "")
        try:
            payload = text.encode(fmt.encoding)
        except UnicodeEncodeError as exc:
            raise ChartFormatError(
                f"chart text cannot be written as {fmt.encoding}: {exc}"
            ) from None
        return fmt.bom_bytes() + payload

    def write(self, path: str | Path) -> None:
        Path(path).write_bytes(self.to_bytes())

    # -- typed view --------------------------------------------------------- #

    @property
    def chart(self) -> Section:
        """The ``<chart>`` section."""
        return next(n for n in self.nodes if isinstance(n, Section) and n.tag == "chart")

    @property
    def symbol(self) -> str | None:
        return self.chart.get("symbol")

    @symbol.setter
    def symbol(self, value: str) -> None:
        self.chart.set("symbol", value)

    @property
    def timeframe(self) -> Timeframe | None:
        """The chart timeframe, or ``None`` when the period keys are absent/invalid."""
        unit = _int_or_none(self.chart.get("period_type"))
        size = _int_or_none(self.chart.get("period_size"))
        if unit is None or size is None:
            return None
        return Timeframe.from_codes(unit, size)

    @timeframe.setter
    def timeframe(self, value: Timeframe) -> None:
        period_type, period_size = value.codes
        self.chart.set("period_type", str(period_type))
        self.chart.set("period_size", str(period_size))

    def _expert_section(self) -> Section | None:
        # Only a <expert> directly under <chart> is the chart's expert advisor;
        # indicators carry their own <inputs> deeper inside <window> blocks.
        return self.chart.section("expert")

    @property
    def expert(self) -> Expert | None:
        """The attached expert advisor, or ``None`` for a chart without one."""
        section = self._expert_section()
        if section is None:
            return None
        inputs = section.section("inputs")
        return Expert(
            name=section.get("name") or "",
            path=section.get("path") or "",
            mode=_int_or_none(section.get("expertmode")),
            inputs=tuple((e.key, e.value) for e in inputs.entries()) if inputs else (),
        )

    def set_expert(self, expert: Expert) -> None:
        """Attach *expert*, replacing the current one.

        An existing ``<expert>`` block is edited in place: ``name``, ``path`` and
        ``expertmode`` are updated (``expertmode`` only when ``expert.mode`` is
        set), the ``<inputs>`` block is replaced, and any other key the terminal
        wrote there survives. A new block is inserted before the first
        ``<window>``, which is where the terminal places it.
        """
        for key, value in expert.inputs:
            _check_key(key)
            _check_line_value(f"input {key!r}", value)
        section = self._expert_section()
        if section is None:
            section = Section("expert")
            children = self.chart.children
            first_window = next(
                (
                    i
                    for i, c in enumerate(children)
                    if isinstance(c, Section) and c.tag == "window"
                ),
                len(children),
            )
            children.insert(first_window, section)
        section.set("name", expert.name)
        section.set("path", expert.path)
        if expert.mode is not None:
            section.set("expertmode", str(expert.mode))
        inputs = section.section("inputs")
        if inputs is None:
            inputs = Section("inputs")
            section.children.append(inputs)
        # Rebuilt directly rather than through set(), which would merge a
        # repeated key into one entry.
        inputs.children = [Entry(key, value) for key, value in expert.inputs]

    def remove_expert(self) -> bool:
        """Detach the expert advisor. Returns whether there was one."""
        section = self._expert_section()
        if section is None:
            return False
        self.chart.children.remove(section)
        return True

    def set_input(self, key: str, value: str) -> None:
        """Change one input of the attached expert, keeping its position."""
        section = self._expert_section()
        inputs = section.section("inputs") if section else None
        if inputs is None or inputs.get(key) is None:
            raise KeyError(f"the chart's expert has no input {key!r}")
        inputs.set(key, value)

    # -- identity ----------------------------------------------------------- #

    def fingerprint(self) -> str:
        """A digest of what the chart *trades*: symbol, timeframe, expert, inputs.

        The terminal keeps saving display state (scroll position, scale,
        window geometry) into chart files, so a byte hash of a running chart
        drifts without any real change. Deploy decisions compare this fingerprint instead, so a
        chart counts as changed only when its symbol, period, expert, expert
        mode or inputs differ.
        """
        chart = self.chart
        expert = self.expert
        identity = {
            "symbol": chart.get("symbol"),
            "period_type": chart.get("period_type"),
            "period_size": chart.get("period_size"),
            "expert": None
            if expert is None
            else {
                "name": expert.name,
                "path": expert.path,
                "mode": expert.mode,
                "inputs": [list(pair) for pair in expert.inputs],
            },
        }
        canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
