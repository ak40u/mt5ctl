"""The deploy manifest: what mt5ctl placed on a terminal.

Stored as ``<data dir>/.mt5ctl/deployed.json``::

    {"version": 1, "profile": "Default", "files": {"<relpath>": "<digest>", ...}}

The manifest is what makes deploys *managed-subset*: mt5ctl only ever updates or
removes paths listed here, so charts and experts installed by hand or by
another tool (a watchdog, a trade copier) are never touched.

A present but unreadable manifest is an error, never an empty manifest. Treating
it as empty would make every deployed file look foreign and silently orphan the
whole deployment.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from .errors import ManifestError

MANIFEST_VERSION = 1
STATE_DIR = ".mt5ctl"
MANIFEST_RELPATH = f"{STATE_DIR}/deployed.json"


@dataclass(frozen=True, slots=True)
class Manifest:
    profile: str
    files: Mapping[str, str]
    """Managed relative path -> digest recorded when it was placed."""

    def to_json(self) -> str:
        """Canonical serialization (sorted keys), the inverse of :func:`parse_manifest`."""
        return json.dumps(
            {
                "version": MANIFEST_VERSION,
                "profile": self.profile,
                "files": dict(sorted(self.files.items())),
            },
            separators=(",", ":"),
        )


def parse_manifest(raw: str | None) -> Manifest | None:
    """Parse a ``deployed.json`` payload. ``None`` means no manifest (first deploy)."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"deployed.json is corrupt (invalid JSON: {exc})") from None
    if not isinstance(data, dict) or not {"version", "profile", "files"} <= data.keys():
        raise ManifestError("deployed.json is corrupt (missing version, profile or files)")
    if data["version"] != MANIFEST_VERSION:
        raise ManifestError(
            f"deployed.json has version {data['version']!r}, but this mt5ctl reads "
            f"version {MANIFEST_VERSION}; upgrade mt5ctl instead of risking a wrong plan"
        )
    profile, files = data["profile"], data["files"]
    if not isinstance(profile, str) or not isinstance(files, dict):
        raise ManifestError("deployed.json is corrupt (profile or files has the wrong type)")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
        raise ManifestError("deployed.json is corrupt (files is not a path -> digest map)")
    return Manifest(profile=profile, files=files)
