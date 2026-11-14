"""Plan a deploy: diff a bundle against a terminal's current state.

Pure and network-free. The inputs are a :class:`~mt5ctl.bundle.Bundle` and a
:class:`TerminalState` (however it was collected: local disk or SSH), the
output is a :class:`DeployPlan`. Applying a plan is a separate step; computing
one never changes anything, which is what ``mt5ctl plan`` relies on.

Rules (managed-subset reconcile):

* A bundle path recorded in the manifest is *added* when missing on the
  terminal, *unchanged* when its digest matches, *updated* otherwise.
* A bundle path not in the manifest is added when free, and a *conflict* when an
  unmanaged file already sits there; any conflict makes a deploy refuse.
* A managed path absent from the bundle is *removed*: always for charts; for
  experts only when no remaining chart (bundle or foreign) still runs it. One
  foreign chart whose expert cannot be resolved keeps every such expert, since
  removing a file a live chart may need is the worse mistake.
* Live charts that are neither in the bundle nor managed are reported as
  *foreign* and left alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .bundle import Bundle, is_chart
from .errors import ManifestError
from .manifest import Manifest


@dataclass(frozen=True, slots=True)
class TerminalState:
    """What a terminal holds right now, as plain data.

    Attributes:
        manifest: the parsed ``deployed.json``, or ``None`` before the first deploy.
        files: every inspected path that exists on the terminal (all bundle
            paths and all manifest paths) mapped to its digest, or to ``None``
            when the file exists but could not be read or parsed.
        charts: every live chart in the profile mapped to the expert it runs,
            ``None`` when it runs none or the expert cannot be resolved.
        unresolved_charts: live charts that run an expert whose path could not
            be resolved (or that failed to parse) — the fail-safe input for
            expert removal.
    """

    manifest: Manifest | None = None
    files: Mapping[str, str | None] = field(default_factory=dict)
    charts: Mapping[str, str | None] = field(default_factory=dict)
    unresolved_charts: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class DeployPlan:
    """The reconcile result. Every field is a sorted tuple of relative paths."""

    add: tuple[str, ...] = ()
    update: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    keep: tuple[str, ...] = ()
    """Managed experts dropped from the bundle but kept: a remaining chart runs them."""
    foreign: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def has_changes(self) -> bool:
        return bool(self.add or self.update or self.remove)

    @property
    def blocked(self) -> bool:
        return bool(self.conflicts)


def compute_plan(bundle: Bundle, state: TerminalState) -> DeployPlan:
    """Diff *bundle* against *state* (see the module docstring for the rules)."""
    manifest = state.manifest
    if manifest is not None and manifest.profile != bundle.profile:
        raise ManifestError(
            f"the terminal's charts are managed in profile {manifest.profile!r}, but the "
            f"bundle targets {bundle.profile!r}; build the bundle for {manifest.profile!r}"
        )
    managed = dict(manifest.files) if manifest else {}
    notes: list[str] = []
    add: list[str] = []
    update: list[str] = []
    unchanged: list[str] = []
    conflicts: list[str] = []

    for path in sorted(bundle.files):
        want = bundle.files[path]
        present = path in state.files
        actual = state.files.get(path)
        if path in managed:
            if not present:
                add.append(path)
                notes.append(
                    f"{path}: managed but missing on the terminal; will be placed again"
                )
            elif actual == want:
                unchanged.append(path)
            else:
                update.append(path)
                if actual != managed[path]:
                    notes.append(f"{path}: changed on the terminal since the last deploy")
        elif present:
            conflicts.append(path)
            what = "a live chart" if path in state.charts else "a file"
            notes.append(
                f"{path}: {what} mt5ctl does not manage already exists here; "
                "renumber the bundle around it or adopt the terminal first"
            )
        else:
            add.append(path)

    bundle_charts = set(bundle.charts)
    # Experts still needed after the deploy: those the bundle's charts run, plus
    # those run by live charts the deploy leaves in place (foreign charts).
    surviving_refs = set(bundle.chart_experts.values())
    ambiguous = False
    for chart, ref in state.charts.items():
        if chart in bundle_charts or chart in managed:
            continue
        if chart in state.unresolved_charts:
            ambiguous = True
            notes.append(
                f"{chart}: foreign chart whose expert cannot be resolved; "
                "no managed expert will be removed"
            )
        elif ref is not None:
            surviving_refs.add(ref)

    remove: list[str] = []
    keep: list[str] = []
    for path in sorted(p for p in managed if p not in bundle.files):
        if is_chart(path):
            remove.append(path)
        elif path in surviving_refs or ambiguous:
            keep.append(path)
        else:
            remove.append(path)

    foreign = sorted(c for c in state.charts if c not in bundle_charts and c not in managed)
    return DeployPlan(
        add=tuple(add),
        update=tuple(update),
        remove=tuple(remove),
        unchanged=tuple(unchanged),
        keep=tuple(keep),
        foreign=tuple(foreign),
        conflicts=tuple(conflicts),
        notes=tuple(notes),
    )
