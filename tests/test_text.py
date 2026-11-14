"""Lenient decoding for log tails."""

from __future__ import annotations

import pytest

from mt5ctl.text import TextDecodeError, decode, decode_lenient


def test_decode_raises_text_error() -> None:
    with pytest.raises(TextDecodeError):
        decode(b"\xff\xfe<\x00c")


@pytest.mark.parametrize(
    "data",
    [
        "﻿2026.11.15 authorized on Demo\r\n".encode("utf-16-le"),
        "2026.11.15 authorized on Demo\r\n".encode("utf-16-le"),
        b"2026.11.15 authorized on Demo\n",
        "2026.11.15 authorized on Demo\n".encode("utf-16-be"),
    ],
)
def test_decode_lenient_finds_the_text(data: bytes) -> None:
    assert "authorized on Demo" in decode_lenient(data)


def test_decode_lenient_never_raises() -> None:
    assert decode_lenient(b"\xff\x00\xfe") is not None
    assert decode_lenient(b"") == ""
