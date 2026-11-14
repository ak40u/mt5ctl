"""deployed.json: strict parsing, canonical writing."""

from __future__ import annotations

import json

import pytest

from mt5ctl.errors import ManifestError
from mt5ctl.manifest import Manifest, parse_manifest

A = "MQL5/Experts/A.ex5"
B = "MQL5/Profiles/Charts/Default/chart01.chr"


def test_round_trip_is_canonical() -> None:
    manifest = Manifest(profile="Default", files={B: "chart:2", A: "sha256:1"})
    raw = manifest.to_json()
    assert raw == (
        f'{{"version":1,"profile":"Default","files":{{"{A}":"sha256:1","{B}":"chart:2"}}}}'
    )
    assert parse_manifest(raw) == manifest


@pytest.mark.parametrize(
    "path",
    [
        "../victim.txt",
        "MQL5/Experts/../../victim.ex5",
        "/etc/passwd",
        "MQL5/Profiles/Charts/Other/chart01.chr",
        "config/common.ini",
    ],
)
def test_paths_outside_the_layout_are_refused(path: str) -> None:
    raw = json.dumps({"version": 1, "profile": "Default", "files": {path: "sha256:x"}})
    with pytest.raises(ManifestError, match="outside the managed layout"):
        parse_manifest(raw)


def test_invalid_profile_is_refused() -> None:
    raw = json.dumps({"version": 1, "profile": "../x", "files": {}})
    with pytest.raises(ManifestError, match="outside the managed layout"):
        parse_manifest(raw)


def test_absent_manifest_is_none() -> None:
    assert parse_manifest(None) is None


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", "invalid JSON"),
        ("{", "invalid JSON"),
        ("[]", "missing"),
        ('{"version":1,"files":{}}', "missing"),
        ('{"version":2,"profile":"Default","files":{}}', "upgrade mt5ctl"),
        ('{"version":1,"profile":7,"files":{}}', "wrong type"),
        ('{"version":1,"profile":"Default","files":[]}', "wrong type"),
        ('{"version":1,"profile":"Default","files":{"a":1}}', "path -> digest"),
    ],
)
def test_corrupt_manifest_is_an_error_not_empty(raw: str, message: str) -> None:
    with pytest.raises(ManifestError, match=message):
        parse_manifest(raw)


def test_unknown_extra_keys_are_tolerated() -> None:
    raw = json.dumps({"version": 1, "profile": "P", "files": {}, "written_by": "x"})
    assert parse_manifest(raw) == Manifest(profile="P", files={})
