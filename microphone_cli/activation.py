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
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

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


def record_activation(activation: Activation, *, path: Path | None = None) -> None:
    """Append exactly one JSON line for ``activation`` to the activation log.

    Writes via a single ``os.open(..., O_APPEND)`` followed by exactly one
    ``os.write()`` of the complete ``line + "\\n"``. Under ``O_APPEND`` the
    kernel serialises writers to the same regular file, so a single syscall
    carrying a complete line cannot interleave with another writer's line.

    Any failure to write — permission denied, missing parent, disk full, a
    parent path component that is not a directory, ... — propagates to the
    caller as the underlying ``OSError``. It is never swallowed here: a
    silently-lost line would break the guarantee that every ``--apply``
    action is logged.
    """
    target = path if path is not None else log_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    line = json.dumps(activation.to_dict())
    payload = (line + "\n").encode("utf-8")

    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def activation_scope(
    verb: str,
    device: str,
    params: dict[str, object] | None = None,
    *,
    path: Path | None = None,
) -> Iterator[Activation]:
    """Context manager around one ``--apply`` action; writes exactly one line on exit.

    The activation is recorded once, on exit — never on entry, so a still
    running action never appears in the log — and exactly once whether the
    body finishes cleanly or raises. On a raise, the exception's type and
    message are folded into ``params["error"]`` (without overwriting an
    "error" key the caller already set) before the single line is written,
    and the original exception is re-raised unchanged. This is deliberate:
    an action that dies mid-flight must still leave a completed record with
    ``ended_at`` set, not vanish silently.
    """
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
        finished = replace(activation, ended_at=_now_iso(), params=crash_params)
        record_activation(finished, path=path)
        raise
    else:
        finished = replace(activation, ended_at=_now_iso())
        record_activation(finished, path=path)
