"""Activation log — one JSON line per ``--apply`` action.

Cited from ``webcam-cli/webcam_cli/activation.py:26-129`` (module docstring,
``log_path()``, ``Activation``, ``record_activation()``, ``activation_scope()``)
and adapted to microphone-cli's field set: every activation records a verb, a
device (its stable id), the parameters that were applied, and when the action
started and ended — nothing about the audio itself.

Resolution order for the log path mirrors the source module:

1. ``$MICROPHONE_ACTIVATION_LOG`` — an explicit override, used verbatim.
2. ``$XDG_STATE_HOME/microphone-cli/activation.jsonl`` — XDG state dir.
3. ``~/.local/state/microphone-cli/activation.jsonl`` — XDG fallback, used
   when ``$XDG_STATE_HOME`` is unset.

Zero runtime dependencies: standard library only.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from microphone_cli.cli._errors import EXIT_ENV_ERROR, CliError

# Env var that overrides the default activation-log location.
ENV_LOG_PATH = "MICROPHONE_ACTIVATION_LOG"

_ENV_XDG_STATE_HOME = "XDG_STATE_HOME"
_STATE_SUBPATH = Path("microphone-cli") / "activation.jsonl"


def log_path() -> Path:
    """Return the documented default activation-log location.

    Resolution order:

    1. ``$MICROPHONE_ACTIVATION_LOG`` — an explicit override, used verbatim.
    2. ``$XDG_STATE_HOME/microphone-cli/activation.jsonl`` — XDG state dir.
    3. ``~/.local/state/microphone-cli/activation.jsonl`` — XDG fallback,
       used when ``$XDG_STATE_HOME`` is unset.

    This function never touches the filesystem — it only computes a path.
    Callers that write (:func:`record_activation`) create parent
    directories on demand.
    """
    override = os.environ.get(ENV_LOG_PATH)
    if override:
        return Path(override)

    xdg_state_home = os.environ.get(_ENV_XDG_STATE_HOME)
    base = Path(xdg_state_home) if xdg_state_home else Path.home() / ".local" / "state"
    return base / _STATE_SUBPATH


@dataclass(frozen=True)
class Activation:
    """One ``--apply`` action against a device.

    ``ended_at`` is ``None`` while the action is still running; once it is
    populated the activation is complete — including when it ended by
    crashing (see :func:`activation_scope`), so a failed action never
    simply vanishes from the record.
    """

    verb: str
    device: str
    params: dict[str, object]
    started_at: str
    ended_at: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "verb": self.verb,
            "device": self.device,
            "params": self.params,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


def _write_all(fd: int, payload: bytes) -> None:
    """Write every byte of ``payload`` to ``fd``, looping over short writes.

    ``os.write`` is not guaranteed to consume the whole buffer in a single
    call — under ``O_APPEND`` the kernel still serialises writers to the same
    regular file, but a *single* write it accepts may still be partial (e.g.
    interrupted by a signal, or a pipe/special file with a small buffer).
    Looping here keeps single-open, single-line-atomic-under-``PIPE_BUF``
    semantics while guaranteeing the whole line lands. A ``0`` return (or a
    raised ``OSError``) is treated as a hard failure and propagated — it is
    never swallowed, since a silently-lost line would break the guarantee
    that every ``--apply`` action is logged.
    """
    view = memoryview(payload)
    total = len(view)
    written = 0
    while written < total:
        n = os.write(fd, view[written:])
        if n <= 0:
            raise OSError(f"os.write() returned {n} writing to fd {fd} (expected > 0)")
        written += n


def record_activation(activation: Activation, *, path: Path | None = None) -> None:
    """Append exactly one JSON line for ``activation`` to the activation log.

    Writes via a single ``os.open(..., O_APPEND)`` followed by one or more
    ``os.write()`` calls (see :func:`_write_all`) until the complete
    ``line + "\\n"`` has been written. Under ``O_APPEND`` the kernel
    serialises writers to the same regular file, so a line under
    ``PIPE_BUF`` still cannot interleave with another writer's line even
    though it may take more than one syscall to land.

    Any failure to write — permission denied, missing parent, disk full, a
    parent path component that is not a directory, a short write that never
    completes, ... — propagates to the caller as the underlying ``OSError``.
    It is never swallowed here: a silently-lost line would break the
    guarantee that every ``--apply`` action is logged.
    """
    target = path if path is not None else log_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    line = json.dumps(activation.to_dict())
    payload = (line + "\n").encode("utf-8")

    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_log_writable(target: Path) -> None:
    """Fail fast if ``target`` cannot be appended to, before anything else runs.

    Creates the parent directory and opens (then immediately closes) the log
    file in append mode. This is the same open call :func:`record_activation`
    will make later; doing it up front means a permission problem, a full
    disk, or a bad parent path component is surfaced as a :class:`CliError`
    *before* the caller's hardware-touching action runs, instead of after —
    so a command never applies a change it then fails to report.
    """
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)
    except OSError as exc:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"activation log at {target} is not writable: {exc}",
            remediation=(
                f"Fix permissions or free space for {target}, or point "
                f"${ENV_LOG_PATH} at a writable location, then retry."
            ),
        ) from exc


def _finished(activation: Activation, params: dict[str, object]) -> Activation:
    """The closed-out twin of ``activation``: same identity, ``ended_at`` stamped now.

    Built explicitly rather than via ``dataclasses.replace`` so static analysis
    sees an :class:`Activation` flowing into the audit write (SonarCloud S5655).
    """
    return Activation(
        verb=activation.verb,
        device=activation.device,
        params=dict(params),
        started_at=activation.started_at,
        ended_at=_now_iso(),
    )


def _record_or_report_applied(
    finished: Activation, *, verb: str, device: str, target: Path, path: Path | None
) -> None:
    """Write ``finished``; if that write itself fails, say so without hiding the action.

    ``_ensure_log_writable`` already ran before the caller's action, so this
    should only fail for a problem that appeared *during* the action (disk
    filled up, log file/directory removed underneath us, ...). In that case
    the caller's action already happened — raising the raw ``OSError`` (or
    silently discarding it) would read like the action itself failed and
    invite an unsafe retry of something non-idempotent. Instead this raises a
    :class:`CliError` whose message says plainly that the action was applied
    and only the audit-log write failed.
    """
    try:
        record_activation(finished, path=path)
    except OSError as exc:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=(
                f"{verb} on {device} was applied, but writing the activation log to "
                f"{target} failed: {exc}. Do not retry the action — it already ran."
            ),
            remediation=(
                f"Fix permissions or free space for {target}, or point "
                f"${ENV_LOG_PATH} at a writable location, then record this activation "
                "manually if an audit trail is required."
            ),
        ) from exc


@contextmanager
def activation_scope(
    verb: str,
    device: str,
    params: dict[str, object] | None = None,
    *,
    path: Path | None = None,
) -> Iterator[Activation]:
    """Context manager around one ``--apply`` action; writes exactly one line on exit.

    Audit availability is established *before* the caller's action runs:
    entering this context manager resolves the log path, creates its parent
    directories, and opens (then closes) the log file once, so a permission
    or disk-space problem raises :class:`CliError` up front instead of after
    the (non-idempotent) hardware action has already happened.

    The activation is recorded once, on exit — never on entry, so a still
    running action never appears in the log — and exactly once whether the
    body finishes cleanly or raises. On a raise, the exception's type and
    message are folded into ``params["error"]`` (without overwriting an
    "error" key the caller already set) before the single line is written,
    and the original exception is re-raised unchanged. This is deliberate:
    an action that dies mid-flight must still leave a completed record with
    ``ended_at`` set, not vanish silently.

    If the final write fails anyway (e.g. the disk filled up *during* the
    action), the caller's action has already run: this raises
    :class:`CliError` saying so explicitly rather than a bare ``OSError``,
    so nothing downstream mistakes it for "the action failed" and retries a
    change that already took effect.
    """
    target: Path = path if path is not None else log_path()
    _ensure_log_writable(target)

    activation = Activation(
        verb=verb,
        device=device,
        params=dict(params) if params is not None else {},
        started_at=_now_iso(),
        ended_at=None,
    )
    try:
        yield activation
    except BaseException as exc:
        crash_params = dict(activation.params)
        crash_params.setdefault("error", f"{type(exc).__name__}: {exc}")
        finished = _finished(activation, crash_params)
        _record_or_report_applied(finished, verb=verb, device=device, target=target, path=path)
        raise
    else:
        finished = _finished(activation, activation.params)
        _record_or_report_applied(finished, verb=verb, device=device, target=target, path=path)
