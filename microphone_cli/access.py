"""Typed device-access errors: absent, forbidden, and busy device nodes.

Cited from webcam-cli ``webcam_cli/access.py`` and adapted for this project's
device kinds: ``audio`` (ALSA capture nodes, e.g. ``/dev/snd/pcmC0D0c``) and
``usb`` (raw USB nodes for array firmware access, e.g. ``/dev/bus/usb/001/004``).

Scope: this module owns exactly one question — *can this device node be
opened right now, and if not, why not and what fixes it*. It does not own
device enumeration or pairing and it does not own sample formats or gain.
Callers pass a plain path string; this module never imports
``microphone_cli`` sibling modules besides ``cli._errors``.

Three failure states matter and must never be conflated:

* **absent** — no node at that path at all (a bad/stale path, or hardware
  unplugged). The agent named a device that is not there: a user error.
* **forbidden** — the node exists but ``open()`` fails with ``EACCES``/
  ``EPERM``. ALSA capture nodes under ``/dev/snd`` are typically gated by
  ``audio``-group membership; raw USB nodes under ``/dev/bus/usb`` are
  typically gated by a udev rule granting access by vendor/product id (no
  group membership fixes a USB node without one). The two subsystems fail
  for different reasons and need differently-worded fixes.
* **busy** — the node exists, is permitted, but is already held open by
  another process (``EBUSY``). :func:`find_holder` makes a best-effort,
  bounded, non-blocking scan of ``/proc/*/fd`` to name the holder; when it
  cannot (permission-limited for another user's process, a race on a
  vanishing pid, a dangling symlink), it degrades to ``holder=None`` rather
  than raising or hanging. A busy report with an unknown holder is a normal,
  expected outcome — not a bug.

:func:`check_access` never raises for an inaccessible device; it is the
reporting path used by ``list``-shaped verbs, which must exit 0 and simply
show a bad device as bad. :func:`require_access` is the enforcing path used
by capture verbs: it raises the typed
:class:`~microphone_cli.cli._errors.CliError` for anything other than
``AccessState.OK``.
"""

from __future__ import annotations

import errno
import os
from dataclasses import dataclass
from enum import Enum

from microphone_cli.cli._errors import EXIT_BUSY_ERROR, EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError

__all__ = [
    "AccessState",
    "Holder",
    "AccessReport",
    "busy_error",
    "check_access",
    "require_access",
    "find_holder",
    "access_error",
]

_KINDS = ("audio", "usb")


class AccessState(str, Enum):
    """The outcome of attempting to open a device node."""

    OK = "ok"
    ABSENT = "absent"
    FORBIDDEN = "forbidden"
    BUSY = "busy"


@dataclass(frozen=True)
class Holder:
    """The process a busy device is currently held open by, best-effort."""

    pid: int
    command: str  # process name, best-effort (from /proc/<pid>/comm)


@dataclass(frozen=True)
class AccessReport:
    """The result of :func:`check_access` — never raised, always returned."""

    path: str
    kind: str  # "audio" | "usb"
    state: AccessState
    remediation: str  # "" when state is OK
    holder: Holder | None = None  # populated only when state is BUSY and determinable


def _validate_kind(kind: str) -> None:
    if kind not in _KINDS:
        raise ValueError(f"kind must be one of {_KINDS!r}, got {kind!r}")


def _open_flags(kind: str) -> int:
    # O_NONBLOCK so a slow/blocking-on-open node can never hang us here; this
    # module only ever asks "can it be opened", it does not read or stream.
    # O_CLOEXEC so a probe never leaks an fd into a child process.
    extra = getattr(os, "O_CLOEXEC", 0)
    if kind == "audio":
        return os.O_RDONLY | os.O_NONBLOCK | extra
    return os.O_RDWR | os.O_NONBLOCK | extra


def _absent_remediation(kind: str, path: str) -> str:
    if kind == "audio":
        return (
            f"{path} does not exist — check the microphone/card is plugged in and "
            "listed by 'arecord -l' or /proc/asound/cards; ALSA card numbers renumber "
            "on replug, so a stale path is the most common cause"
        )
    return (
        f"{path} does not exist — check the microphone array is plugged in and "
        "listed by 'lsusb' or under /dev/bus/usb/; USB bus/device numbers renumber "
        "on replug, so a stale path is the most common cause"
    )


def _vanished_remediation(kind: str, path: str) -> str:
    subsystem = "microphone/card" if kind == "audio" else "USB device"
    listing = "'arecord -l'" if kind == "audio" else "'lsusb'"
    return (
        f"{path} exists but has no device behind it — the {subsystem} was unplugged "
        f"after the node was enumerated; re-enumerate against {listing} and use the "
        "current path, since node numbers change on replug"
    )


def _forbidden_remediation(kind: str, path: str) -> str:
    if kind == "audio":
        return (
            f"permission denied opening {path} — ALSA capture devices are gated by "
            "'audio'-group membership; add the invoking user to the 'audio' group and "
            "re-login"
        )
    return (
        f"permission denied opening {path} — raw USB device nodes are gated by udev "
        "and are not fixed by group membership alone; add a udev rule granting access "
        "by vendor/product id, e.g. a file under /etc/udev/rules.d/ containing:\n"
        '  SUBSYSTEM=="usb", ATTR{idVendor}=="XXXX", ATTR{idProduct}=="YYYY", MODE="0666"\n'
        "then 'udevadm control --reload-rules && udevadm trigger', with idVendor/"
        "idProduct read from 'lsusb' for this device"
    )


def _busy_remediation(kind: str, holder: Holder | None) -> str:
    subsystem = "ALSA capture device" if kind == "audio" else "USB device"
    if holder is not None:
        who = f"{holder.command} (pid {holder.pid})"
    else:
        who = (
            "another process that could not be identified "
            "(no permission to read its /proc/<pid>/fd)"
        )
    return (
        f"{subsystem} is already open by {who}; only one exclusive capture client is "
        "supported at a time — stop that process (or wait for it to release the device), "
        "then retry"
    )


def check_access(path: str, kind: str) -> AccessReport:
    """Report whether ``path`` (a ``kind`` device node) can be opened right now.

    Never raises for an inaccessible device — this is the reporting path used
    by ``list``-shaped verbs, which must exit 0 while still showing a bad
    device as bad. Only an invalid ``kind`` (a caller bug, not a device-access
    outcome) raises ``ValueError``.

    Makes exactly one non-blocking open attempt (``O_NONBLOCK``) and closes
    the descriptor immediately on success; no retries, no sleeps, no loop —
    so this call is bounded by however long a single ``open(2)`` takes, which
    for a character device is not a wait on remote I/O.
    """
    _validate_kind(kind)
    try:
        fd = os.open(path, _open_flags(kind))
    except FileNotFoundError:
        return AccessReport(
            path=path,
            kind=kind,
            state=AccessState.ABSENT,
            remediation=_absent_remediation(kind, path),
        )
    except PermissionError:
        return AccessReport(
            path=path,
            kind=kind,
            state=AccessState.FORBIDDEN,
            remediation=_forbidden_remediation(kind, path),
        )
    except OSError as exc:
        if exc.errno == errno.EBUSY:
            holder = find_holder(path)
            return AccessReport(
                path=path,
                kind=kind,
                state=AccessState.BUSY,
                remediation=_busy_remediation(kind, holder),
                holder=holder,
            )
        if exc.errno in (errno.ENODEV, errno.ENXIO):
            # The node exists but nothing is behind it: the device was
            # unplugged between enumeration and open. That is an absent
            # device, not a permission problem — reporting FORBIDDEN here
            # would hand back group/udev remediation for a device that is
            # simply gone.
            return AccessReport(
                path=path,
                kind=kind,
                state=AccessState.ABSENT,
                remediation=_vanished_remediation(kind, path),
            )
        # Any other OSError is still an environment problem the caller cannot
        # fix by blindly retrying — report it rather than silently claim OK.
        return AccessReport(
            path=path,
            kind=kind,
            state=AccessState.FORBIDDEN,
            remediation=_forbidden_remediation(kind, path),
        )
    else:
        os.close(fd)
        return AccessReport(path=path, kind=kind, state=AccessState.OK, remediation="")


def require_access(path: str, kind: str) -> None:
    """Raise the typed :class:`CliError` unless ``path`` is openable now.

    Used by capture paths, which need to fail loudly rather than report.
    """
    report = check_access(path, kind)
    if report.state is not AccessState.OK:
        raise access_error(report)


def busy_error(path: str, kind: str) -> CliError:
    """Build the typed BUSY error for ``path``, looking the holder up first.

    For callers that learned the device is busy some way *other* than
    ``open(2)``.
    """
    _validate_kind(kind)
    holder = find_holder(path)
    return access_error(
        AccessReport(
            path=path,
            kind=kind,
            state=AccessState.BUSY,
            remediation=_busy_remediation(kind, holder),
            holder=holder,
        )
    )


def _list_proc_pids() -> list[str]:
    return os.listdir("/proc")


def _list_fds(pid: str) -> list[str]:
    return os.listdir(f"/proc/{pid}/fd")


def _readlink(fd_path: str) -> str:
    return os.readlink(fd_path)


def _read_command(pid: str) -> str:
    with open(f"/proc/{pid}/comm", encoding="utf-8") as handle:
        return handle.read().strip()


def _holder_from_fd(entry: str, fd_name: str, target: str) -> Holder | None:
    """Check one ``/proc/<entry>/fd/<fd_name>`` entry against ``target``.

    Returns the :class:`Holder` if this descriptor is the one holding
    ``target`` open, else ``None`` — including when the symlink cannot be
    read at all (a race on a closing fd, a dangling entry).
    """
    try:
        link = _readlink(f"/proc/{entry}/fd/{fd_name}")
    except OSError:
        return None
    if link != target:
        return None
    try:
        command = _read_command(entry)
    except OSError:
        command = "unknown"
    return Holder(pid=int(entry), command=command)


def _holder_in_pid(entry: str, target: str) -> Holder | None:
    """Scan every open fd of one pid for ``target``, degrading to ``None``.

    A pid's ``fd`` directory can be unreadable (owned by another user, the
    common case) or the pid can exit mid-scan — either way this is not ours
    to read, so it is skipped rather than guessed at.
    """
    try:
        fd_names = _list_fds(entry)
    except OSError:
        return None
    for fd_name in fd_names:
        holder = _holder_from_fd(entry, fd_name, target)
        if holder is not None:
            return holder
    return None


def find_holder(path: str) -> Holder | None:
    """Best-effort, bounded scan of ``/proc/*/fd`` for a process with ``path`` open.

    Degrades gracefully to ``None`` — never raises — on anything short of a
    clean match: ``/proc`` unavailable, a pid's ``fd`` directory unreadable
    (owned by another user, the common case), a pid that exits mid-scan, or a
    dangling symlink. A busy report with ``holder=None`` is correct and
    expected, not a failure of this function.

    The scan is a single linear pass over ``/proc`` with no retries and no
    sleeps, so it is bounded by the number of processes and open descriptors
    on the host at the moment of the call — it cannot hang waiting on a
    device, because it never opens one.
    """
    try:
        target = os.path.realpath(path)
    except OSError:
        return None

    try:
        pids = _list_proc_pids()
    except OSError:
        return None

    for entry in pids:
        if not entry.isdigit():
            continue
        holder = _holder_in_pid(entry, target)
        if holder is not None:
            return holder
    return None


def access_error(report: AccessReport) -> CliError:
    """Map a non-OK :class:`AccessReport` to the typed :class:`CliError`.

    Exit-code policy:

    * ``ABSENT``    -> ``EXIT_USER_ERROR``  (1) — the agent named a device that isn't there.
    * ``FORBIDDEN`` -> ``EXIT_ENV_ERROR``   (2) — the host/session is misconfigured; not
      retryable without a config fix (audio group, udev rule).
    * ``BUSY``      -> ``EXIT_BUSY_ERROR``  (3) — another process holds the device.
      Deliberately distinct from ``FORBIDDEN``: BUSY is retryable (wait for the
      holder to release it, or stop it) while FORBIDDEN is not, and an agent
      cannot tell those apart from a shared exit code without string-matching
      the message — exactly what a typed code exists to prevent.

    Calling this with an ``OK`` report is a programming error, not a device
    outcome, and raises ``ValueError``.
    """
    if report.state is AccessState.OK:
        raise ValueError("access_error() called with an OK report; there is nothing to map")

    if report.state is AccessState.ABSENT:
        return CliError(
            code=EXIT_USER_ERROR,
            message=f"no {report.kind} device at {report.path}",
            remediation=report.remediation,
        )

    if report.state is AccessState.FORBIDDEN:
        return CliError(
            code=EXIT_ENV_ERROR,
            message=f"permission denied opening {report.kind} device {report.path}",
            remediation=report.remediation,
        )

    # BUSY — retryable, so it gets its own code rather than sharing
    # EXIT_ENV_ERROR with FORBIDDEN (which is not retryable).
    holder_desc = ""
    if report.holder is not None:
        holder_desc = f" (held by {report.holder.command}, pid {report.holder.pid})"
    return CliError(
        code=EXIT_BUSY_ERROR,
        message=f"{report.kind} device {report.path} is busy{holder_desc}",
        remediation=report.remediation,
    )
