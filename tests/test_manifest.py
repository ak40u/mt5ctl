"""deployed.json: strict parsing, canonical writing."""

from __future__ import annotations

import json

import pytest

from mt5ctl.errors import ManifestError
from mt5ctl.manifest import Manifest, parse_manifest


def test_round_trip_is_canonical() -> None:
    manifest = Manifest(profile="Default", files={"b": "sha256:2", "a": "sha256:1"})
    raw = manifest.to_json()
    assert raw == ('{"version":1,"profile":"Default","files":{"a":"sha256:1","b":"sha256:2"}}')
    assert parse_manifest(raw) == manifest


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
