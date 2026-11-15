"""Builders for the bash scripts mt5ctl runs on a terminal's host.

Every function here is pure: it returns a script and touches nothing, so the
shell logic is unit-tested (and executed by the test suite with a real bash)
without a host. Scripts answer on stdout in a ``|``-separated line protocol;
any free-form field (file names, file contents, log text) is base64-encoded so
it can never break the framing.

Invariant: every value embedded in a script goes through :func:`sh_quote`.
Nothing is interpolated into a double-quoted shell context.

The scripts run on GNU/Linux hosts and, for the test suite and ``local`` hosts,
on macOS; the helpers paper over the GNU/BSD differences in ``stat`` and
``base64``.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Iterable, Mapping

from . import mt5spec
from .bundle import CHARTS_ROOT, EXPERTS_DIR, charts_dir
from .manifest import MANIFEST_RELPATH, STATE_DIR
from .models import ServiceScope

SEP = "|"

BACKUP_DIR = f"{STATE_DIR}/backups"
STAGING_DIR = f"{STATE_DIR}/staging"
# An atomic mkdir is the lock primitive: it survives across separate SSH
# sessions, which a per-process flock cannot.
LOCK_DIR = f"{STATE_DIR}/deploy.lock.d"
BACKUPS_KEPT = 5
CONTROL_ACTIONS = ("start", "stop", "restart")

HELPERS = r"""
b64() { base64 < "$1" | tr -d '\n'; }
b64s() { printf '%s' "$1" | base64 | tr -d '\n'; }
sha() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}';
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}
fsize() { wc -c < "$1" | tr -d ' '; }
fmtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1"; }
fdev() { stat -c %d "$1" 2>/dev/null || stat -f %d "$1"; }
limit() { local t="$1"; shift; if command -v timeout >/dev/null 2>&1; then timeout "$t" "$@"; else "$@"; fi; }
"""


def _is_terminal_function() -> str:
    """``is_terminal <pid>``: whether the process runs the terminal executable.

    Matched on the command line, not on ``comm`` (see
    :data:`mt5spec.TERMINAL_EXECUTABLES`): the executable name preceded by a
    path separator or the start of the line, and followed by a space or the end.
    """
    names = "|".join(re.escape(n) for n in mt5spec.TERMINAL_EXECUTABLES)
    pattern = rf"(^|[\\/ ])({names})( |$)"
    return (
        "is_terminal() { tr '\\000' ' ' < \"/proc/$1/cmdline\" 2>/dev/null "
        f"| grep -qiE {sh_quote(pattern)}; }}"
    )


def sh_quote(value: str) -> str:
    """POSIX single-quote *value* for safe embedding in a bash script."""
    return "'" + value.replace("'", "'\\''") + "'"


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _systemctl(scope: ServiceScope, *, mutate: bool) -> str:
    """The systemctl invocation for a unit scope.

    A system unit needs root to change state; ``sudo -n`` fails fast instead of
    prompting when no password-less rule exists. Reads need no privileges.
    """
    if scope is ServiceScope.USER:
        return "systemctl --user"
    if not mutate:
        return "systemctl"
    return '$( [ "$(id -u)" = 0 ] && echo systemctl || echo "sudo -n systemctl" )'


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


def build_control_script(service: str, scope: ServiceScope, action: str) -> str:
    """Run a systemd action; print ``STATE|<state>|<rc>`` and exit with its code."""
    if action not in CONTROL_ACTIONS:
        raise ValueError(f"action must be one of {CONTROL_ACTIONS}, got {action!r}")
    return f"""\
set +e
SVC={sh_quote(service)}
{_systemctl(scope, mutate=True)} {action} "$SVC"
rc=$?
st=$({_systemctl(scope, mutate=False)} is-active "$SVC" 2>/dev/null)
echo "STATE{SEP}${{st:-unknown}}{SEP}$rc"
exit $rc
"""


def build_is_active_script(service: str, scope: ServiceScope) -> str:
    """Print ``ACTIVE|<state>`` for a unit (``systemctl is-active``)."""
    return f"""\
set +e
st=$({_systemctl(scope, mutate=False)} is-active {sh_quote(service)} 2>/dev/null)
echo "ACTIVE{SEP}${{st:-unknown}}"
"""


def build_drain_script(service: str, scope: ServiceScope, timeout_s: int) -> str:
    """Wait until the unit is down and no terminal process is left in its cgroup.

    The terminal writes chart files while it shuts down, so nothing may be
    written into the profile before the process is really gone. Prints
    ``DRAIN|ok|<unit Result>`` or ``DRAIN|timeout`` (exit 1). The unit's
    ``Result`` tells a clean exit (``success``) from one systemd had to force
    (``timeout``, ``signal``, ...).
    """
    ctl = _systemctl(scope, mutate=False)
    return f"""\
set +e
{_is_terminal_function()}
SVC={sh_quote(service)}
for _ in $(seq 1 {int(timeout_s)}); do
  st=$({ctl} is-active "$SVC" 2>/dev/null)
  busy=0
  case "$st" in active|activating|deactivating|reloading) busy=1 ;; esac
  cg=$({ctl} show -p ControlGroup --value "$SVC" 2>/dev/null)
  if [ "$busy" = 0 ] && [ -n "$cg" ] && [ -r "/sys/fs/cgroup$cg/cgroup.procs" ]; then
    for p in $(cat "/sys/fs/cgroup$cg/cgroup.procs" 2>/dev/null); do
      if is_terminal "$p"; then busy=1; break; fi
    done
  fi
  if [ "$busy" = 0 ]; then
    r=$({ctl} show -p Result --value "$SVC" 2>/dev/null)
    echo "DRAIN{SEP}ok{SEP}${{r:-unknown}}"; exit 0
  fi
  sleep 1
done
echo "DRAIN{SEP}timeout"
exit 1
"""


# --------------------------------------------------------------------------- #
# Deploy
# --------------------------------------------------------------------------- #


def build_lock_acquire_script(data_dir: str, holder: str, stale_s: int) -> str:
    """Take the per-terminal lock: ``LOCK|acquired``, ``LOCK|takeover`` or ``LOCK|held``.

    A lock older than *stale_s* is taken over; a deploy has no heartbeat, so
    *stale_s* must exceed the longest possible deploy.
    """
    return f"""\
set +e
DIR={sh_quote(data_dir)}
H={sh_quote(holder)}
LD="$DIR/{LOCK_DIR}"
mkdir -p "$DIR/{STATE_DIR}"
if mkdir "$LD" 2>/dev/null; then
  printf '%s %s\\n' "$H" "$(date +%s)" > "$LD/owner"; echo "LOCK{SEP}acquired"; exit 0
fi
ts=$(awk '{{print $2}}' "$LD/owner" 2>/dev/null)
now=$(date +%s)
if [ -n "$ts" ] && [ $((now - ts)) -gt {int(stale_s)} ]; then
  rm -rf "$LD"
  if mkdir "$LD" 2>/dev/null; then
    printf '%s %s\\n' "$H" "$now" > "$LD/owner"; echo "LOCK{SEP}takeover"; exit 0
  fi
fi
echo "LOCK{SEP}held{SEP}$(awk '{{print $1}}' "$LD/owner" 2>/dev/null)"
exit 1
"""


def build_lock_release_script(data_dir: str, holder: str) -> str:
    """Release the lock, only if *holder* still owns it."""
    return f"""\
set +e
DIR={sh_quote(data_dir)}
H={sh_quote(holder)}
LD="$DIR/{LOCK_DIR}"
if [ "$(awk '{{print $1}}' "$LD/owner" 2>/dev/null)" = "$H" ]; then
  rm -rf "$LD"; echo "LOCK{SEP}released"
else
  echo "LOCK{SEP}notowner"
fi
"""


def build_preflight_script(
    data_dir: str, profile: str, service: str | None, scope: ServiceScope
) -> str:
    """Deploy prerequisites: ``tar``, a sha256 tool, one filesystem, the unit's user.

    Staged files are moved into place with ``mv``, which is atomic only within
    one filesystem, so the state folder must share a device with both managed
    trees. Prints ``PRE|<check>|ok`` or ``PRE|<check>|<problem>`` per check and
    ``USER|<unit user>`` (the unit's ``User=``, else the user running the script).
    """
    user_probe = (
        f"u=$({_systemctl(scope, mutate=False)} show -p User --value {sh_quote(service)} "
        "2>/dev/null)"
        if service
        else 'u=""'
    )
    return f"""\
set +e
{HELPERS}
DIR={sh_quote(data_dir)}
mkdir -p "$DIR/{STATE_DIR}"
command -v tar >/dev/null 2>&1 && echo "PRE{SEP}tar{SEP}ok" || echo "PRE{SEP}tar{SEP}missing"
if command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1; then
  echo "PRE{SEP}sha256{SEP}ok"
else
  echo "PRE{SEP}sha256{SEP}missing"
fi
d0=$(fdev "$DIR/{STATE_DIR}")
for sub in {sh_quote(charts_dir(profile))} {sh_quote(CHARTS_ROOT)} {sh_quote(EXPERTS_DIR)}; do
  ref="$DIR/$sub"; [ -d "$ref" ] || continue
  d=$(fdev "$ref")
  [ "$d" = "$d0" ] || echo "PRE{SEP}filesystem{SEP}$sub is on another filesystem than {STATE_DIR}"
done
echo "PRE{SEP}filesystem{SEP}ok"
{user_probe}
echo "USER{SEP}${{u:-$(id -un)}}"
"""


def build_backup_script(data_dir: str, paths: Iterable[str], ts: str) -> str:
    """Tar the present *paths* plus the manifest into ``backups/<ts>.tar``.

    Keeps the newest :data:`BACKUPS_KEPT`. Prints ``BACKUP|<path>`` or
    ``BACKUP|none`` when there is nothing to back up (first deploy).
    """
    adds = "\n".join(
        f'[ -f "$DIR/"{sh_quote(p)} ] && printf "%s\\n" {sh_quote(p)} >> "$LIST"'
        for p in paths
    )
    return f"""\
set -e
DIR={sh_quote(data_dir)}
BK="$DIR/{BACKUP_DIR}"
mkdir -p "$BK"
LIST=$(mktemp "$DIR/{STATE_DIR}/backup-list.XXXXXX")
{adds}
[ -f "$DIR/{MANIFEST_RELPATH}" ] && printf '%s\\n' {sh_quote(MANIFEST_RELPATH)} >> "$LIST"
if [ -s "$LIST" ]; then
  tar -C "$DIR" -cf "$BK/"{sh_quote(ts)}.tar -T "$LIST"
  echo "BACKUP{SEP}$BK/"{sh_quote(ts)}.tar
else
  echo "BACKUP{SEP}none"
fi
rm -f "$LIST"
ls -1t "$BK"/*.tar 2>/dev/null | tail -n +{BACKUPS_KEPT + 1} | while IFS= read -r old; do
  rm -f "$old"
done
"""


def _chown_block(user: str, targets: Iterable[str]) -> str:
    """Give written files to the terminal's user, so it can rewrite them later.

    Only meaningful when the deploy runs as root for a unit with another
    ``User=``; otherwise the files already belong to the right user.
    """
    quoted = " ".join(sh_quote(t) for t in targets)
    return f"""\
U={sh_quote(user)}
if [ "$(id -u)" = 0 ] && [ "$U" != root ]; then
  for t in {quoted}; do [ -e "$DIR/$t" ] && chown -R "$U" "$DIR/$t"; done
fi
"""


def build_apply_script(
    data_dir: str,
    staging: str,
    place: Mapping[str, str],
    remove: Iterable[str],
    manifest_json: str,
    unit_user: str,
) -> str:
    """Verify staged files, move them into place, remove dropped ones, write the manifest.

    *place* maps each destination path (also its path inside *staging*) to the
    sha256 of the bundle's bytes. Every staged file is checked **before any
    move**, so a truncated upload aborts with the terminal untouched. The
    manifest is written last: if any step fails (``set -e``), the old manifest
    still describes the old state and the orchestrator restores the backup.
    Prints ``APPLY|ok``, or ``APPLY|hashfail|<path>`` before exiting 1.
    """
    verify = "\n".join(
        f'f={sh_quote(rel)}; [ "$(sha "$STG/$f")" = {sh_quote(sha)} ] '
        f'|| {{ echo "APPLY{SEP}hashfail{SEP}$f"; exit 1; }}'
        for rel, sha in place.items()
    )
    moves = "\n".join(
        f'f={sh_quote(rel)}; mkdir -p "$(dirname "$DIR/$f")"; mv -f "$STG/$f" "$DIR/$f"'
        for rel in place
    )
    removes = "\n".join(f'rm -f "$DIR/"{sh_quote(rel)}' for rel in remove)
    return f"""\
set -e
{HELPERS}
DIR={sh_quote(data_dir)}
STG="$DIR/"{sh_quote(staging)}
{verify}
{moves}
{removes}
TMP=$(mktemp "$DIR/{STATE_DIR}/manifest.XXXXXX")
echo {_b64(manifest_json)} | base64 -d > "$TMP"
mv -f "$TMP" "$DIR/{MANIFEST_RELPATH}"
rm -rf "$STG"
{_chown_block(unit_user, [*place, STATE_DIR])}
echo "APPLY{SEP}ok"
"""


def build_restore_script(
    data_dir: str, backup_tar: str | None, touched: Iterable[str], unit_user: str
) -> str:
    """Undo a failed apply: remove every touched path, then unpack the backup.

    Removing first makes files new in this deploy disappear (they are not in
    the backup). Best effort throughout: every step is attempted.
    """
    purge = "\n".join(f'rm -f "$DIR/"{sh_quote(p)}' for p in touched)
    extract = (
        f'tar -C "$DIR" -xf {sh_quote(backup_tar)} --no-same-owner'
        if backup_tar
        else f'rm -f "$DIR/{MANIFEST_RELPATH}"'
    )
    return f"""\
set +e
DIR={sh_quote(data_dir)}
{purge}
{extract}
rm -rf "$DIR/{STAGING_DIR}"
{_chown_block(unit_user, [*touched, STATE_DIR])}
echo "RESTORE{SEP}done"
"""


def build_manifest_put_script(data_dir: str, manifest_json: str, unit_user: str) -> str:
    """Write a manifest as-is (adopt). Prints ``MANIFEST|ok``."""
    return f"""\
set -e
DIR={sh_quote(data_dir)}
mkdir -p "$DIR/{STATE_DIR}"
TMP=$(mktemp "$DIR/{STATE_DIR}/manifest.XXXXXX")
echo {_b64(manifest_json)} | base64 -d > "$TMP"
mv -f "$TMP" "$DIR/{MANIFEST_RELPATH}"
{_chown_block(unit_user, [STATE_DIR])}
echo "MANIFEST{SEP}ok"
"""


# --------------------------------------------------------------------------- #
# Journal and status
# --------------------------------------------------------------------------- #

_JOURNAL = f"{mt5spec.JOURNAL_DIR}/{mt5spec.JOURNAL_GLOB}"


def build_log_cursor_script(data_dir: str) -> str:
    """Remember where the journal ends now: ``CURSOR|<b64 file name>|<size>``.

    Taken before a start, so verify reads only what the start produced.
    """
    return f"""\
set +e
{HELPERS}
DIR={sh_quote(data_dir)}
log=$(ls -1 "$DIR"/{_JOURNAL} 2>/dev/null | sort | tail -1)
if [ -n "$log" ]; then
  echo "CURSOR{SEP}$(b64s "${{log##*/}}"){SEP}$(fsize "$log")"
else
  echo "CURSOR{SEP}{SEP}0"
fi
"""


def build_log_since_script(data_dir: str, name: str, size: int, limit: int) -> str:
    """Ship the journal written after a cursor: ``LOG|<b64 file name>|<b64 bytes>``.

    The cursor's file is read past *size* (whole, if it shrank); journal files
    named after it are read whole (the terminal starts a new dated file per
    day, so name order is time order). Each file contributes at most its first
    *limit* bytes: what verify looks for is logged right after a start. An empty *name* reads every journal file.
    """
    return f"""\
set +e
{HELPERS}
DIR={sh_quote(data_dir)}
CN={sh_quote(name)}
CS={int(size)}
LIM={int(limit)}
for p in "$DIR"/{_JOURNAL}; do
  [ -f "$p" ] || continue
  f=${{p##*/}}
  if [ -n "$CN" ] && [ "$f" = "$CN" ]; then
    sz=$(fsize "$p"); start=$((CS + 1)); [ "$sz" -lt "$CS" ] && start=1
  elif [ -z "$CN" ] || [ "$f" \\> "$CN" ]; then
    start=1
  else
    continue
  fi
  data=$(tail -c +"$start" "$p" | head -c "$LIM" | base64 | tr -d '\\n')
  echo "LOG{SEP}$(b64s "$f"){SEP}$data"
done
"""


def build_status_script(
    specs: Iterable[tuple[str, str, ServiceScope, str]], tail_bytes: int
) -> str:
    """Report several terminals on one host in one round trip.

    Each spec is ``(terminal id, unit, scope, data dir)``. One line per terminal::

        TERM|<id>|<state>|<active s|-1>|<journal age s|-1>|<estab|-1>|<b64 journal tail>

    ``active`` is the unit's uptime. ``estab`` counts established connections
    to :data:`mt5spec.BROKER_PORT` owned by the unit's terminal processes; ``-1``
    means it could not be attributed (no ``ss``, no permission, no process), not
    "disconnected". The journal tail is decoded and searched in Python.
    """
    calls = "\n".join(
        f"emit {sh_quote(tid)} {sh_quote(unit)} {sh_quote(_systemctl(scope, mutate=False))} "
        f"{sh_quote(scope.value)} {sh_quote(data_dir)}"
        for tid, unit, scope, data_dir in specs
    )
    return f"""\
set +e
{HELPERS}
{_is_terminal_function()}
NOW=$(date +%s)
UP=$(awk '{{print $1}}' /proc/uptime 2>/dev/null)
CURUID=$(id -u)
CURUSER=$(id -un)
HAVE_SS=0; command -v ss >/dev/null 2>&1 && HAVE_SS=1
SS=$(ss -tnpH state established 2>/dev/null)
emit() {{
  id="$1"; svc="$2"; ctl="$3"; scope="$4"; dir="$5"
  st=$($ctl is-active "$svc" 2>/dev/null); st=${{st:-unknown}}
  aem=$($ctl show -p ActiveEnterTimestampMonotonic --value "$svc" 2>/dev/null)
  active=-1
  if [ -n "$UP" ] && [ -n "$aem" ] && [ "$aem" -gt 0 ] 2>/dev/null; then
    active=$(awk -v u="$UP" -v a="$aem" 'BEGIN {{ d = u - a / 1000000; if (d < 0) d = 0; printf "%d", d }}')
  fi
  log=$(ls -1 "$dir"/{_JOURNAL} 2>/dev/null | sort | tail -1)
  age=-1; tail=""
  if [ -n "$log" ]; then
    age=$((NOW - $(fmtime "$log"))); tail=$(tail -c {int(tail_bytes)} "$log" | base64 | tr -d '\\n')
  fi
  # ss shows the owning process of a socket only to root and to the socket's
  # own user; anyone else would see "no connections" and report a false down.
  unituser=$($ctl show -p User --value "$svc" 2>/dev/null)
  attrib=0
  if [ "$CURUID" = 0 ] || [ "$scope" = user ]; then attrib=1
  elif [ -n "$unituser" ] && [ "$unituser" = "$CURUSER" ]; then attrib=1
  fi
  estab=-1
  cg=$($ctl show -p ControlGroup --value "$svc" 2>/dev/null)
  if [ "$HAVE_SS" = 1 ] && [ "$attrib" = 1 ] && [ -n "$cg" ] && [ -r "/sys/fs/cgroup$cg/cgroup.procs" ]; then
    pids=""
    for p in $(cat "/sys/fs/cgroup$cg/cgroup.procs"); do
      if is_terminal "$p"; then pids="$pids $p"; fi
    done
    if [ -n "$pids" ]; then
      estab=0
      for p in $pids; do
        n=$(printf '%s\\n' "$SS" | awk '$4 ~ /:{mt5spec.BROKER_PORT}$/' | grep -c "pid=$p,")
        estab=$((estab + n))
      done
    fi
  fi
  echo "TERM{SEP}$id{SEP}$st{SEP}$active{SEP}$age{SEP}$estab{SEP}$tail"
}}
{calls}
"""


# --------------------------------------------------------------------------- #
# Compile
# --------------------------------------------------------------------------- #


def build_compile_script(
    data_dir: str,
    work: str,
    source_dirs: Iterable[str],
    sources: Iterable[str],
    *,
    wine: str,
    wineprefix: str | None,
    display: str | None,
    timeout_s: int,
) -> str:
    """Compile uploaded sources with the terminal's MetaEditor under Wine.

    *work* is the upload folder relative to *data_dir*; *source_dirs* are the
    folders inside it that hold ``.mq5`` files (MetaEditor compiles a folder
    non-recursively, and a folder path avoids its known trouble with spaces in
    file names), each with its own ``/log:<file>``. Then reports, for each source in *sources*,
    ``EX5|<b64 source>|<1|0>`` (whether its ``.ex5`` now exists), and ships
    every log as ``LOG|<b64 name>|<b64 bytes>``.
    """
    env = []
    if wineprefix:
        env.append(f"export WINEPREFIX={sh_quote(wineprefix)}")
    if display:
        env.append(f"export DISPLAY={sh_quote(display)}")
    compiles = "\n".join(
        f'd="$WORK/"{sh_quote(d)}; log="$WORK/.build-{i}.log"; '
        f'limit {int(timeout_s)} "$WINE" "$ME" /portable "/compile:$(winpath "$d")" '
        f'"/include:$INC" "/log:$(winpath "$log")" >/dev/null 2>&1; rc=$?; '
        f'echo "RC{SEP}$(b64s {sh_quote(d)}){SEP}$rc"'
        for i, d in enumerate(source_dirs)
    )
    checks = "\n".join(
        f'f={sh_quote(s)}; [ -f "$WORK/${{f%.*}}{mt5spec.EX5_SUFFIX}" ] '
        f'&& echo "EX5{SEP}$(b64s "$f"){SEP}1" || echo "EX5{SEP}$(b64s "$f"){SEP}0"'
        for s in sources
    )
    return f"""\
set +e
{HELPERS}
{chr(10).join(env)}
export WINEDEBUG=-all
DIR={sh_quote(data_dir)}
WORK="$DIR/"{sh_quote(work)}
ME=$(find "$DIR" -maxdepth 1 -type f -iname {sh_quote(mt5spec.METAEDITOR_EXE)} | head -1)
[ -n "$ME" ] || {{ echo "ERROR{SEP}$(b64s "no {mt5spec.METAEDITOR_EXE} in the data folder")"; exit 1; }}
WINE={sh_quote(wine)}
command -v "$WINE" >/dev/null 2>&1 || {{ echo "ERROR{SEP}$(b64s "wine not found: $WINE")"; exit 1; }}
winpath() {{ "$WINE" winepath -w "$1" 2>/dev/null | tr -d '\r'; }}
INC=$(winpath "$DIR/MQL5")
{compiles}
{checks}
find "$WORK" -name '*.log' -type f | while IFS= read -r f; do
  echo "LOG{SEP}$(b64s "${{f#$WORK/}}"){SEP}$(b64 "$f")"
done
"""


def build_fetch_script(data_dir: str, work: str, members: Iterable[str]) -> str:
    """Ship files from *work* as one base64 tar on stdout: ``TAR|<b64 tar>``."""
    listed = " ".join(sh_quote(m) for m in members)
    return f"""\
set -e
DIR={sh_quote(data_dir)}
WORK="$DIR/"{sh_quote(work)}
echo "TAR{SEP}$(tar -C "$WORK" -cf - {listed} | base64 | tr -d '\\n')"
"""


def build_remove_dir_script(data_dir: str, relpath: str) -> str:
    """Remove a folder mt5ctl created under the data folder (compile workspace)."""
    if not relpath or relpath.startswith("/") or ".." in relpath.split("/"):
        raise ValueError(f"refusing to remove {relpath!r}")
    return f"""\
set +e
DIR={sh_quote(data_dir)}
rm -rf "$DIR/"{sh_quote(relpath)}
echo "REMOVED{SEP}ok"
"""
