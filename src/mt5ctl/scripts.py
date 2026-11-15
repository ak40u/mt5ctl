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
LOCK_PATH = f"{STATE_DIR}/lock"
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
    main_pattern = rf"(^|[\\/])({names})$"
    return (
        "is_terminal() { tr '\\000' ' ' < \"/proc/$1/cmdline\" 2>/dev/null "
        f"| grep -qiE {sh_quote(pattern)}; }}\n"
        # The terminal itself, not a launcher that mentions it: its first argument.
        "is_terminal_main() { tr '\\000' '\\n' < \"/proc/$1/cmdline\" 2>/dev/null "
        f"| head -1 | grep -qiE {sh_quote(main_pattern)}; }}"
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


PROCESS_PID = f"{STATE_DIR}/terminal.pid"
PROCESS_OUT = f"{STATE_DIR}/terminal.out"


def build_process_script(
    data_dir: str, action: str, start_command: str, stop_command: str, timeout_s: int
) -> str:
    """Start, stop or restart a terminal without systemd (``process`` mode).

    ``start`` runs *start_command* in the data folder as a background process
    group (``set -m``), its pid in ``.mt5ctl/terminal.pid``, output in
    ``.mt5ctl/terminal.out``. ``stop`` runs *stop_command* with ``$MAINPID``
    set, waits up to *timeout_s* for the group to exit, then sends TERM and,
    after 10 s, KILL. Prints ``WAS|<0|1>``, ``STATE|<active|inactive|failed>|<rc>``
    and, for a stop, ``RESULT|<success|timeout>``.
    """
    if action not in CONTROL_ACTIONS:
        raise ValueError(f"action must be one of {CONTROL_ACTIONS}, got {action!r}")
    return f"""\
set +e
DIR={sh_quote(data_dir)}
PIDF="$DIR/{PROCESS_PID}"
OUT="$DIR/{PROCESS_OUT}"
alive() {{ [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; }}
group_alive() {{ kill -0 -- "-$1" 2>/dev/null; }}
do_stop() {{
  if ! alive; then echo "WAS{SEP}0"; rm -f "$PIDF"; echo "RESULT{SEP}success"; return 0; fi
  echo "WAS{SEP}1"
  pid=$(cat "$PIDF")
  (cd "$DIR" && MAINPID=$pid sh -c {sh_quote(stop_command)}) >>"$OUT" 2>&1
  i=0
  while group_alive "$pid" && [ "$i" -lt {int(timeout_s)} ]; do sleep 1; i=$((i + 1)); done
  r=success
  if group_alive "$pid"; then
    r=timeout
    kill -TERM -- "-$pid" 2>/dev/null
    i=0; while group_alive "$pid" && [ "$i" -lt 10 ]; do sleep 1; i=$((i + 1)); done
    kill -KILL -- "-$pid" 2>/dev/null
  fi
  rm -f "$PIDF"
  echo "RESULT{SEP}$r"
}}
do_start() {{
  if alive; then echo "WAS{SEP}1"; echo "STATE{SEP}active{SEP}0"; return 0; fi
  echo "WAS{SEP}0"
  mkdir -p "$DIR/{STATE_DIR}"
  cd "$DIR" || {{ echo "STATE{SEP}failed{SEP}1"; return 1; }}
  set -m
  nohup sh -c {sh_quote(start_command)} >>"$OUT" 2>&1 </dev/null &
  echo $! > "$PIDF"
  set +m
  sleep 1
  if alive; then echo "STATE{SEP}active{SEP}0"; return 0; fi
  echo "STATE{SEP}failed{SEP}1"; return 1
}}
case {sh_quote(action)} in
  start) do_start; exit $? ;;
  stop) do_stop; echo "STATE{SEP}inactive{SEP}0"; exit 0 ;;
  restart) do_stop >/dev/null; do_start; exit $? ;;
esac
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
    """Take the per-terminal lock: ``LOCK|acquired``, ``LOCK|takeover`` or
    ``LOCK|held|<b64 holder>|<age s>``.

    The lock is a symbolic link whose target is ``<holder> <epoch seconds>``:
    creating it is atomic and carries its owner, so there is never a lock
    without one. The timestamp is read as data and checked to be a plain
    number before any arithmetic (an unreadable one makes the lock stale).
    A lock older than *stale_s* is taken over, one taker at a time (a
    ``.takeover`` mutex), and only if it is still the lock that was judged
    stale; a deploy has no heartbeat, so *stale_s* must exceed the longest run.
    """
    return f"""\
set +e
{HELPERS}
DIR={sh_quote(data_dir)}
H={sh_quote(holder)}
L="$DIR/{LOCK_PATH}"
M="$L.takeover"
mkdir -p "$DIR/{STATE_DIR}"
now=$(date +%s)
if ln -s "$H $now" "$L" 2>/dev/null; then echo "LOCK{SEP}acquired"; exit 0; fi
cur=$(readlink "$L" 2>/dev/null)
ts=${{cur##* }}
case "$ts" in ''|*[!0-9]*) ts=0 ;; esac
[ "${{#ts}}" -le 12 ] || ts=0
age=$((now - ts))
if [ "$age" -gt {int(stale_s)} ]; then
  if [ -d "$M" ]; then
    mt=$(fmtime "$M"); case "$mt" in ''|*[!0-9]*) mt=0 ;; esac
    [ $((now - mt)) -gt 60 ] && rmdir "$M" 2>/dev/null
  fi
  if mkdir "$M" 2>/dev/null; then
    if [ "$(readlink "$L" 2>/dev/null)" = "$cur" ]; then
      rm -f "$L"
      if ln -s "$H $now" "$L" 2>/dev/null; then
        rmdir "$M"; echo "LOCK{SEP}takeover"; exit 0
      fi
    fi
    rmdir "$M"
  fi
fi
echo "LOCK{SEP}held{SEP}$(b64s "${{cur% *}}"){SEP}$age"
exit 1
"""


def lock_guard(holder: str, tag: str) -> str:
    """Script lines that stop with ``<tag>|lost`` unless *holder* still owns the lock.

    Put before any write: a run whose lock was taken over must not write.
    """
    return f"""\
_lk=$(readlink "$DIR/{LOCK_PATH}" 2>/dev/null)
if [ "${{_lk% *}}" != {sh_quote(holder)} ]; then echo "{tag}{SEP}lost"; exit 3; fi"""


def build_lock_release_script(data_dir: str, holder: str) -> str:
    """Release the lock, only if *holder* still owns it."""
    return f"""\
set +e
DIR={sh_quote(data_dir)}
H={sh_quote(holder)}
L="$DIR/{LOCK_PATH}"
cur=$(readlink "$L" 2>/dev/null)
if [ -n "$cur" ] && [ "${{cur% *}}" = "$H" ]; then
  rm -f "$L"; echo "LOCK{SEP}released"
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
    holder: str | None = None,
) -> str:
    """Verify staged files, move them into place, remove dropped ones, write the manifest.

    *place* maps each destination path (also its path inside *staging*) to the
    sha256 of the bundle's bytes. Every staged file is checked **before any
    move**, so a truncated upload aborts with the terminal untouched. The
    manifest is written last: if any step fails (``set -e``), the old manifest
    still describes the old state and the orchestrator restores the backup.
    Prints ``APPLY|ok``, or ``APPLY|hashfail|<path>`` before exiting 1. With
    *holder*, nothing is written unless it still owns the lock (``APPLY|lost``).
    """
    guard = lock_guard(holder, "APPLY") if holder else ""
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
{guard}
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


def build_log_tails_script(
    specs: Iterable[tuple[str, str]],
    log_dirs: Iterable[str],
    tail_bytes: int,
    previous_bytes: int = 0,
) -> str:
    """The end of each terminal's newest dated log, several terminals in one trip.

    *log_dirs* are relative to each data folder (the journal, or the experts'
    log). One line per terminal::

        TAIL|<id>|<b64 newest file name>|<size>|<b64 tail>|<b64 tail of the file before>

    The tails are raw bytes for Python to decode. Byte counts are kept even,
    so a UTF-16 tail starts on a character.
    """
    n = int(tail_bytes) + int(tail_bytes) % 2
    m = int(previous_bytes) + int(previous_bytes) % 2
    dirs = " ".join(sh_quote(d) for d in log_dirs)
    calls = "\n".join(f"tailof {sh_quote(tid)} {sh_quote(d)}" for tid, d in specs)
    return f"""\
set +e
{HELPERS}
tailof() {{
  log=""; prev=""
  for sub in {dirs}; do
    for f in "$2/$sub"/{mt5spec.JOURNAL_GLOB}; do
      [ -f "$f" ] || continue
      if [ -z "$log" ] || [ "${{f##*/}}" \\> "${{log##*/}}" ]; then prev=$log; log=$f
      elif [ -z "$prev" ] || [ "${{f##*/}}" \\> "${{prev##*/}}" ]; then prev=$f; fi
    done
  done
  if [ -z "$log" ]; then echo "TAIL{SEP}$1{SEP}{SEP}0{SEP}{SEP}"; return; fi
  p=""
  if [ {m} -gt 0 ] && [ -n "$prev" ]; then p=$(tail -c {m} "$prev" | base64 | tr -d '\\n'); fi
  echo "TAIL{SEP}$1{SEP}$(b64s "${{log##*/}}"){SEP}$(fsize "$log"){SEP}$(tail -c {n} "$log" | base64 | tr -d '\\n'){SEP}$p"
}}
{calls}
"""


# Tools the generated scripts need on every host, and those only some commands
# need (the command is named in the check).
DOCTOR_REQUIRED = ("bash", "tar", "base64", "awk", "sed", "mktemp", "tail", "wc")
DOCTOR_OPTIONAL = {
    "ss": "status attributes broker connections to each terminal",
    "Xvfb": "login runs the terminal on a private display",
    "iconv": "login reads the journal",
    "shred": "login erases its temporary file (without it: zeros, then rm)",
    "import": "screenshot captures the display (ImageMagick)",
}


def build_doctor_script(
    specs: Iterable[tuple[str, str, str, ServiceScope, str, str, str]],
) -> str:
    """Check a host for what mt5ctl needs, read-only.

    Each spec is ``(id, data dir, mode, scope, unit, wine, wine prefix)``, mode
    ``systemd``, ``process`` or ``none``. Output::

        TOOL|<name>|ok|missing
        TERM|<id>|<dir>|<mql5>|<exe>|<unit>|<sudo>|<wine>|<prefix>|<journal>

    each field ``ok``, ``missing``, or ``-`` when it does not apply; ``sudo`` is
    ``root`` when the user is root.
    """
    tools = " ".join([*DOCTOR_REQUIRED, *DOCTOR_OPTIONAL, "systemctl", "sha256sum", "shasum"])
    calls = "\n".join(
        "checkterm "
        + " ".join(sh_quote(str(v)) for v in (tid, d, mode, scope.value, svc, w, p))
        for tid, d, mode, scope, svc, w, p in specs
    )
    return f"""\
set +e
for t in {tools}; do
  if command -v "$t" >/dev/null 2>&1; then echo "TOOL{SEP}$t{SEP}ok"; else echo "TOOL{SEP}$t{SEP}missing"; fi
done
yn() {{ if "$@" >/dev/null 2>&1; then echo ok; else echo missing; fi; }}
checkterm() {{
  id=$1; dir=$2; mode=$3; scope=$4; svc=$5; wine=$6; wp=$7
  d=$(yn test -d "$dir"); m=$(yn test -d "$dir/MQL5"); e=$(yn test -f "$dir/terminal64.exe")
  u=-; s=-; w=-; p=-
  if [ "$mode" = systemd ]; then
    if [ "$scope" = user ]; then
      u=$(yn systemctl --user cat "$svc")
    else
      u=$(yn systemctl cat "$svc")
      if [ "$(id -u)" = 0 ]; then s=root
      elif sudo -n -l systemctl start "$svc" >/dev/null 2>&1 \
        && sudo -n -l systemctl stop "$svc" >/dev/null 2>&1 \
        && sudo -n -l systemctl restart "$svc" >/dev/null 2>&1; then s=ok
      else s=missing; fi
    fi
  fi
  if [ -n "$wine" ]; then
    case "$wine" in */*) w=$(yn test -x "$wine") ;; *) w=$(yn command -v "$wine") ;; esac
  fi
  [ -n "$wp" ] && p=$(yn test -f "$wp/system.reg")
  j=missing
  for f in "$dir"/{_JOURNAL}; do [ -f "$f" ] && j=ok && break; done
  echo "TERM{SEP}$id{SEP}$d{SEP}$m{SEP}$e{SEP}$u{SEP}$s{SEP}$w{SEP}$p{SEP}$j"
}}
{calls}
"""


def build_start_file_script(specs: Iterable[tuple[str, str]]) -> str:
    """The ``[Experts] Enabled`` value of each terminal's start file, and nothing else.

    Each spec is ``(terminal id, host path of the start file)``. Output::

        START|<id>|missing
        START|<id>|ok|<value, empty when the key is absent>

    The file may hold a password: only that one value is printed. NUL, CR and
    byte-order-mark bytes are dropped first, which turns a UTF-16 file with
    ASCII keys into plain text.
    """
    calls = "\n".join(f"startfile {sh_quote(tid)} {sh_quote(path)}" for tid, path in specs)
    sect = mt5spec.START_FILE_EXPERTS_SECTION
    key = mt5spec.START_FILE_ALGO_KEY
    return f"""\
set +e
export LC_ALL=C
startfile() {{
  if [ ! -f "$2" ]; then echo "START{SEP}$1{SEP}missing"; return; fi
  v=$(tr -d '\\000\\r\\376\\377\\357\\273\\277' < "$2" | awk '
    /^[ \\t]*\\[/ {{ s = tolower($0); gsub(/[][ \\t]/, "", s); next }}
    s == "{sect}" {{
      k = $0; sub(/=.*/, "", k); gsub(/[ \\t]/, "", k)
      if (tolower(k) == "{key}") {{ v = $0; sub(/^[^=]*=/, "", v); gsub(/[ \\t]/, "", v) }}
    }}
    END {{ print v }}')
  case "$v" in *[!0-9]*) v="?" ;; esac
  echo "START{SEP}$1{SEP}ok{SEP}$v"
}}
{calls}
"""


def build_screenshot_script(
    display: str | None,
    xauthority: str | None,
    service: str | None,
    scope: ServiceScope,
) -> str:
    """Capture the terminal's whole X display as PNG, sent back on stdout.

    The display and authority file come from the registry, else from the
    unit's ``Environment``. Output: ``SHOT|ok|<b64 png>`` or
    ``SHOT|error|<b64 reason>``. Nothing is written on the host.
    """
    from_unit = (
        f"""for kv in $({_systemctl(scope, mutate=False)} show -p Environment --value {sh_quote(service)} 2>/dev/null); do
  case "$kv" in
    DISPLAY=*) [ -n "$DISP" ] || DISP=${{kv#DISPLAY=}} ;;
    XAUTHORITY=*) [ -n "$XA" ] || XA=${{kv#XAUTHORITY=}} ;;
  esac
done"""
        if service
        else ""
    )
    return f"""\
set +e
{HELPERS}
DISP={sh_quote(display or "")}
XA={sh_quote(xauthority or "")}
{from_unit}
if [ -z "$DISP" ]; then
  echo "SHOT{SEP}error{SEP}$(b64s "no display: set display (and xauthority) for this terminal")"; exit 0
fi
export DISPLAY="$DISP"
[ -n "$XA" ] && export XAUTHORITY="$XA"
if ! command -v import >/dev/null 2>&1; then
  echo "SHOT{SEP}error{SEP}$(b64s "ImageMagick's import is not installed on the host")"; exit 0
fi
PNG=$(limit 30 import -window root png:- 2>/dev/null | base64 | tr -d '\\n')
if [ -n "$PNG" ]; then echo "SHOT{SEP}ok{SEP}$PNG"; exit 0; fi
# Failed: run it again for the reason (only then, so a capture is taken once).
ERR=$(limit 30 import -window root png:- 2>&1 >/dev/null | head -c 400)
echo "SHOT{SEP}error{SEP}$(b64s "${{ERR:-no image}}")"
"""


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
    specs: Iterable[tuple[str, str, ServiceScope, str, str]], tail_bytes: int
) -> str:
    """Report several terminals on one host in one round trip.

    Each spec is ``(terminal id, unit, scope, data dir, mode)``, mode being
    ``systemd`` or ``process``. One line per terminal::

        TERM|<id>|<state>|<active s|-1>|<journal age s|-1>|<estab|-1>|<b64 journal tail>|<procs|-1>

    ``active`` is the unit's uptime. ``estab`` counts established connections
    to :data:`mt5spec.BROKER_PORT` owned by the unit's terminal processes; ``-1``
    means it could not be attributed (no ``ss``, no permission, no process), not
    "disconnected". ``procs`` counts the terminal's own processes in the unit
    (``-1``: the unit's cgroup could not be read), so an active unit whose
    terminal never started shows as such. The journal tail is decoded and
    searched in Python.
    """
    calls = "\n".join(
        f"emit {sh_quote(tid)} {sh_quote(unit)} {sh_quote(_systemctl(scope, mutate=False))} "
        f"{sh_quote(scope.value)} {sh_quote(data_dir)} {sh_quote(mode)}"
        for tid, unit, scope, data_dir, mode in specs
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
  id="$1"; svc="$2"; ctl="$3"; scope="$4"; dir="$5"; mode="$6"
  active=-1; attrib=0; members=""; members_known=0
  if [ "$mode" = process ]; then
    # No systemd: the pid file mt5ctl wrote when it started the process group.
    pid=$(cat "$dir/{PROCESS_PID}" 2>/dev/null)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then st=active; else st=inactive; fi
    if [ "$st" = active ]; then
      a=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ')
      [ -n "$a" ] && active=$a
      members=$(pgrep -g "$pid" 2>/dev/null); members_known=1
    fi
    attrib=1   # started by this user
  else
    st=$($ctl is-active "$svc" 2>/dev/null); st=${{st:-unknown}}
    aem=$($ctl show -p ActiveEnterTimestampMonotonic --value "$svc" 2>/dev/null)
    if [ -n "$UP" ] && [ -n "$aem" ] && [ "$aem" -gt 0 ] 2>/dev/null; then
      active=$(awk -v u="$UP" -v a="$aem" 'BEGIN {{ d = u - a / 1000000; if (d < 0) d = 0; printf "%d", d }}')
    fi
    # ss shows the owning process of a socket only to root and to the socket's
    # own user; anyone else would see "no connections" and report a false down.
    unituser=$($ctl show -p User --value "$svc" 2>/dev/null)
    if [ "$CURUID" = 0 ] || [ "$scope" = user ]; then attrib=1
    elif [ -n "$unituser" ] && [ "$unituser" = "$CURUSER" ]; then attrib=1
    fi
    cg=$($ctl show -p ControlGroup --value "$svc" 2>/dev/null)
    if [ -n "$cg" ] && [ -r "/sys/fs/cgroup$cg/cgroup.procs" ]; then
      members=$(cat "/sys/fs/cgroup$cg/cgroup.procs"); members_known=1
    fi
  fi
  log=$(ls -1 "$dir"/{_JOURNAL} 2>/dev/null | sort | tail -1)
  age=-1; tail=""
  if [ -n "$log" ]; then
    age=$((NOW - $(fmtime "$log"))); tail=$(tail -c {int(tail_bytes)} "$log" | base64 | tr -d '\\n')
  fi
  estab=-1
  procs=-1
  pids=""
  if [ "$members_known" = 1 ] && [ -d /proc/self ]; then
    procs=0
    for p in $members; do
      if is_terminal_main "$p"; then pids="$pids $p"; procs=$((procs + 1)); fi
    done
  fi
  if [ "$HAVE_SS" = 1 ] && [ "$attrib" = 1 ] && [ "$procs" != -1 ]; then
    if [ -n "$pids" ]; then
      estab=0
      for p in $pids; do
        n=$(printf '%s\\n' "$SS" | awk '$4 ~ /:{mt5spec.BROKER_PORT}$/' | grep -c "pid=$p,")
        estab=$((estab + n))
      done
    fi
  fi
  echo "TERM{SEP}$id{SEP}$st{SEP}$active{SEP}$age{SEP}$estab{SEP}$tail{SEP}$procs"
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


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #

_LOGIN_ACCOUNT_RE = re.compile(r"[0-9]{1,20}")
_LOGIN_SERVER_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")


_LOGIN_EOF = "MT5CTL_LOGIN_CONFIG"


def build_login_script(
    data_dir: str,
    *,
    account: str,
    server: str,
    password: str,
    wine: str,
    wineprefix: str | None,
    service: str | None,
    scope: ServiceScope,
    wait_s: int,
) -> str:
    """One headless login: the terminal saves the password and logs in by itself after.

    Writes a temporary start config (mode 600) with the login, password and
    server, and ``KeepPrivate=1`` so the terminal keeps the password; starts the
    terminal once with it on a private Xvfb display; waits until the journal
    shows the authorization (or its failure); closes the terminal the way it
    closes normally (``taskkill`` on its display); and, from an ``EXIT`` trap
    that runs whatever happens, shreds the config. Prints ``LOGIN|ok``,
    ``LOGIN|failed|<b64 journal line>`` or ``LOGIN|timeout``.

    The password is in the script body only, which reaches the host over the
    SSH connection's stdin: it is never in a command line, a log or the output.
    """
    if not _LOGIN_ACCOUNT_RE.fullmatch(account):
        raise ValueError(f"account must be a login number, got {account!r}")
    if not _LOGIN_SERVER_RE.fullmatch(server):
        raise ValueError(f"invalid server name {server!r}")
    if not password or any(c in password for c in "\r\n\0") or password == _LOGIN_EOF:
        raise ValueError("the password must be one non-empty line")
    prefix = (
        f"WP={sh_quote(wineprefix)}"
        if wineprefix
        else (
            f"WP=$({_systemctl(scope, mutate=False)} show -p Environment --value "
            f"{sh_quote(service)} 2>/dev/null | tr ' ' '\\n' | sed -n 's/^WINEPREFIX=//p' | head -1)"
            if service
            else 'WP=""'
        )
    )
    return f"""\\
set +e
{HELPERS}
DIR={sh_quote(data_dir)}
WINE={sh_quote(wine)}
{prefix}
[ -n "$WP" ] && export WINEPREFIX="$WP"
export WINEDEBUG=-all
STATE="$DIR/{STATE_DIR}"
mkdir -p "$STATE"
INI=""; TPID=""; XPID=""; DISP="${{DISPLAY:-}}"
shred_file() {{
  [ -f "$1" ] || return 0
  if command -v shred >/dev/null 2>&1; then shred -u "$1" 2>/dev/null && return 0; fi
  dd if=/dev/zero of="$1" bs=1 count="$(fsize "$1")" conv=notrunc 2>/dev/null
  rm -f "$1"
}}
cleanup() {{
  if [ -n "$TPID" ] && kill -0 "$TPID" 2>/dev/null; then
    (cd "$DIR" && DISPLAY="$DISP" "$WINE" taskkill /IM terminal64.exe) >/dev/null 2>&1
    i=0; while kill -0 "$TPID" 2>/dev/null && [ "$i" -lt 30 ]; do sleep 1; i=$((i + 1)); done
    kill -TERM -- "-$TPID" 2>/dev/null; sleep 1; kill -KILL -- "-$TPID" 2>/dev/null
  fi
  case "$WINE" in */*) WS="${{WINE%/*}}/wineserver" ;; *) WS=wineserver ;; esac
  command -v "$WS" >/dev/null 2>&1 && "$WS" -k 2>/dev/null
  [ -n "$XPID" ] && kill "$XPID" 2>/dev/null
  [ -n "$INI" ] && shred_file "$INI"
}}
trap cleanup EXIT HUP INT TERM
umask 077
T=$(mktemp "$STATE/login.XXXXXX") || {{ echo "LOGIN{SEP}error{SEP}$(b64s "cannot create the login file")"; exit 1; }}
INI="$T.ini"
mv -f "$T" "$INI" || {{ rm -f "$T"; echo "LOGIN{SEP}error{SEP}$(b64s "cannot create the login file")"; exit 1; }}
awk '{{ printf "%s\\r\\n", $0 }}' > "$INI" <<'{_LOGIN_EOF}'
[Common]
Login={account}
Password={password}
Server={server}
KeepPrivate=1
NewsEnable=0
{_LOGIN_EOF}
J0=$(ls -1 "$DIR"/{_JOURNAL} 2>/dev/null | sort | tail -1)
S0=0; [ -n "$J0" ] && S0=$(fsize "$J0")
since() {{
  {{
    [ -n "$J0" ] && [ -f "$J0" ] && tail -c +$((S0 + 1)) "$J0"
    for f in "$DIR"/{_JOURNAL}; do
      [ -f "$f" ] || continue
      if [ -z "$J0" ] || [ "${{f##*/}}" \\> "${{J0##*/}}" ]; then cat "$f"; fi
    done
  }} | iconv -f UTF-16LE -t UTF-8 2>/dev/null | tr -d '\\r'
}}
if command -v Xvfb >/dev/null 2>&1; then
  for n in $(seq 150 199); do
    if [ ! -e "/tmp/.X$n-lock" ] && [ ! -e "/tmp/.X11-unix/X$n" ]; then
      Xvfb ":$n" -screen 0 1280x1024x24 -nolisten tcp -ac >/dev/null 2>&1 &
      XPID=$!; DISP=":$n"; sleep 2; break
    fi
  done
fi
cd "$DIR" || {{ echo "LOGIN{SEP}error{SEP}$(b64s "no data folder")"; exit 1; }}
set -m
DISPLAY="$DISP" "$WINE" terminal64.exe /portable /skipupdate "/config:{STATE_DIR}\\\\${{INI##*/}}" >/dev/null 2>&1 &
TPID=$!
set +m
for _ in $(seq 1 {int(wait_s)}); do
  text=$(since)
  if printf '%s\\n' "$text" | grep -qiE {sh_quote(mt5spec.LOG_AUTHORIZED_RE.pattern)}; then
    echo "LOGIN{SEP}ok"; exit 0
  fi
  bad=$(printf '%s\\n' "$text" | grep -iE {sh_quote(mt5spec.LOG_AUTH_FAILED_RE.pattern)} | tail -1)
  if [ -n "$bad" ]; then echo "LOGIN{SEP}failed{SEP}$(b64s "$bad")"; exit 0; fi
  kill -0 "$TPID" 2>/dev/null || {{ echo "LOGIN{SEP}error{SEP}$(b64s "the terminal exited")"; exit 0; }}
  sleep 1
done
echo "LOGIN{SEP}timeout"
"""
