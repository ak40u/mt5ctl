"""Decode and re-encode the terminal's text files without losing their format.

Chart files, logs and presets come in several encodings depending on who wrote
them. :func:`decode` detects the encoding and remembers it in a
:class:`TextFormat`, so a file can be written back exactly as it was read.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass

from . import mt5spec

_BOMS = (
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)


class TextDecodeError(ValueError):
    """The bytes are not valid text in the detected encoding."""


@dataclass(frozen=True, slots=True)
class TextFormat:
    """How a text file was encoded on disk, so it can be written back alike."""

    encoding: str
    """Python codec name: ``utf-16-le``, ``utf-16-be``, ``utf-8`` or ``cp1252``."""
    bom: bool
    newline: str
    """``\\r\\n`` or ``\\n``."""
    final_newline: bool

    @classmethod
    def terminal_default(cls) -> TextFormat:
        """The format the terminal itself writes (see :mod:`mt5ctl.mt5spec`)."""
        return cls(
            encoding=mt5spec.CHART_ENCODING,
            bom=mt5spec.CHART_BOM,
            newline=mt5spec.CHART_NEWLINE,
            final_newline=mt5spec.CHART_FINAL_NEWLINE,
        )

    def bom_bytes(self) -> bytes:
        if not self.bom:
            return b""
        return next(bom for bom, enc in _BOMS if enc == self.encoding)


def decode(data: bytes) -> tuple[str, TextFormat]:
    """Decode text bytes, detecting the encoding.

    A byte-order mark decides when present. Without one, a NUL in the second
    byte means BOM-less UTF-16LE (the terminal's files start with an ASCII
    character); otherwise UTF-8 is tried, then Windows-1252, the code page older
    tools write.
    """
    encoding: str | None = None
    bom = False
    body = data
    for mark, enc in _BOMS:
        if data.startswith(mark):
            encoding, bom, body = enc, True, data[len(mark) :]
            break
    if encoding is None:
        if len(data) >= 2 and data[0] != 0 and data[1] == 0:
            encoding = "utf-16-le"
        else:
            try:
                data.decode("utf-8")
                encoding = "utf-8"
            except UnicodeDecodeError:
                encoding = "cp1252"
    try:
        text = body.decode(encoding)
    except UnicodeDecodeError as exc:
        raise TextDecodeError(f"cannot decode as {encoding}: {exc}") from None
    newline = "\r\n" if "\r\n" in text else "\n"
    fmt = TextFormat(
        encoding=encoding, bom=bom, newline=newline, final_newline=text.endswith("\n")
    )
    return text, fmt


def decode_lenient(data: bytes) -> str:
    """Decode for reading only (logs): never fails, replaces undecodable bytes.

    A log tail read from a byte offset may start mid-character; UTF-16 is
    detected by NUL density rather than by a BOM the tail does not have.
    """
    for mark, enc in _BOMS:
        if data.startswith(mark):
            return data[len(mark) :].decode(enc, "replace")
    sample = data[:4096]
    if sample and sample.count(0) * 4 >= len(sample):
        odd_nuls = sample[1::2].count(0)
        even_nuls = sample[0::2].count(0)
        enc = "utf-16-le" if odd_nuls >= even_nuls else "utf-16-be"
        usable = len(data) - (len(data) % 2)
        return data[:usable].decode(enc, "replace")
    return data.decode("utf-8", "replace")
