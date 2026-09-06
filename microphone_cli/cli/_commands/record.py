"""``microphone record`` — capture a bounded audio clip to a file.

Cited (audio half only) from ``webcam-cli/webcam_cli/cli/_commands/record.py``:
the flag set (lines 1408-1529), the dry-run payload keys (1051-1090) — ``mode``,
``apply``, ``device``, ``kind``, ``capture_node``, ``audio_address``,
``audio_format``, ``pipeline_preview``, ``bound``, ``warmup_s``,
``warmup_basis``, ``output_path``, ``would_write``, ``access``, ``engine``,
``timestamps`` — and the apply payload's additions (1281-1306):
``bytes_written``, ``stopped_reason``, ``pipeline``. The video-only keys
(``video_format``, ``warmup_frames``) are dropped rather than emitted as
``null``, and ``kind`` is always ``"audio"`` here.

**There is no flag that means "forever."** ``--duration`` defaults to
:data:`DEFAULT_DURATION_S` and is capped at :data:`MAX_DURATION_S`;
``--max-bytes`` defaults to :data:`DEFAULT_MAX_BYTES` and is capped at
:data:`MAX_BYTES_CEILING`. Both bounds are enforced twice over: the built argv
is self-limiting (``alsasrc num-buffers``, see
:func:`microphone_cli.engine.build_audio_record_argv`) *and* this module polls
the growing artifact and stops the child when either bound is reached. The
JSON says which one won, in ``stopped_reason``. Stopping the child means
stopping it — SIGTERM, then SIGKILL, waiting for each — and if the finished
artifact is nonetheless larger than ``--max-bytes`` (the cap is polled, so a
pipeline can blow it and exit inside one interval) that is a typed exit-2
error naming the size and the cap, not a successful bounded recording.

Hardware contact is the same three-level split as ``stream audio`` — nothing
(default), engine + access check (``--probe``), spawn (``--apply``) — and the
capture-node helper is shared with that module rather than re-derived.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess  # nosec B404 - the spawn seam; fixed argv, never a shell
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from microphone_cli import access, activation, devices, engine
from microphone_cli.cli._commands import JSON_FLAG_HELP
from microphone_cli.cli._commands.stream import (
    DEFAULT_CHANNELS,
    DEFAULT_RATE,
    DEFAULT_SAMPLE_FORMAT,
    advertised_format,
    capture_node_path,
)
from microphone_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from microphone_cli.cli._output import emit_diagnostic, emit_result

DEFAULT_DURATION_S = 30.0
MAX_DURATION_S = 3600.0
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
MAX_BYTES_CEILING = 4 * 1024 * 1024 * 1024  # 4 GiB

#: How often the apply loop checks the child and the artifact's size. Small
#: enough that a max-bytes overshoot stays bounded, large enough not to spin.
POLL_INTERVAL_S = 0.25
#: Slack added to --duration before this module stops the child itself. The
#: argv is already self-limiting, so the wall-clock timer is a backstop for a
#: pipeline that ignores its own bound, not the primary mechanism.
STOP_GRACE_S = 2.0
#: How long to wait for a SIGTERM'd child to actually exit before escalating
#: to SIGKILL. A recording that outlives this verb keeps the capture PCM open,
#: so "warn and return" is not an option — the caller would be told the bound
#: stopped a recording that is in fact still running.
TERMINATE_TIMEOUT_S = 5.0
#: How long to wait for a SIGKILL'd child. Still alive after this and the
#: recording is not stoppable from here: a typed environment error, never a
#: successful bounded recording.
KILL_TIMEOUT_S = 5.0


#: Container per output extension. An unknown extension is a typed user error:
#: guessing a container for a caller would silently produce a file that is not
#: what its name claims.
_CONTAINERS = {".mka": "mka", ".wav": "wav"}

_CONTAINER_ELEMENTS = {
    "mka": ["audioconvert", "audioresample", "opusenc", "matroskamux"],
    "wav": ["wavenc"],
}

_WARMUP_BASIS = (
    "none — an ALSA capture device has no sensor that has to settle (unlike a UVC "
    "camera's auto-exposure), so no lead-in audio is discarded and the whole recorded "
    "window is kept"
)

#: Keys every payload carries (dry-run and probe). ``--apply`` adds
#: :data:`APPLY_PAYLOAD_KEYS` on top.
PAYLOAD_KEYS = (
    "mode",
    "apply",
    "hardware_touched",
    "engine_checked",
    "device",
    "kind",
    "container",
    "capture_node",
    "audio_address",
    "audio_format",
    "pipeline_preview",
    "pipeline_preview_str",
    "bound",
    "warmup_s",
    "warmup_basis",
    "output_path",
    "would_write",
    "access",
    "engine",
    "timestamps",
)

APPLY_PAYLOAD_KEYS = ("bytes_written", "stopped_reason", "pipeline")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _monotonic() -> float:
    """Clock seam — patched in tests so the duration bound is deterministic."""
    return time.monotonic()


def _sleep(seconds: float) -> None:
    """Sleep seam — patched in tests so the poll loop costs no wall-clock time."""
    time.sleep(seconds)


def _spawn(argv: list[str]) -> subprocess.Popen:
    """Spawn ``argv`` — the single seam the ``--apply`` path goes through."""
    return subprocess.Popen(  # nosec B603 - fixed argv built by engine.py, shell=False
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def _user_error(message: str, remediation: str) -> CliError:
    return CliError(code=EXIT_USER_ERROR, message=message, remediation=remediation)


def _validate_duration(duration: float) -> float:
    if not 0 < duration <= MAX_DURATION_S:
        raise _user_error(
            f"invalid --duration {duration:g}: must satisfy 0 < duration <= {MAX_DURATION_S:g}",
            "pass a duration inside the range; there is deliberately no value meaning "
            "'record forever'",
        )
    return float(duration)


def _validate_max_bytes(max_bytes: int) -> int:
    if not 0 < max_bytes <= MAX_BYTES_CEILING:
        raise _user_error(
            f"invalid --max-bytes {max_bytes}: must satisfy 0 < max-bytes <= "
            f"{MAX_BYTES_CEILING}",
            "pass a size cap inside the range; the ceiling is 4 GiB",
        )
    return int(max_bytes)


def _resolve_output(path: str, *, overwrite: bool) -> tuple[str, str]:
    """Validate the output path and derive its container; returns ``(path, container)``.

    Three refusals, all exit 1 and all *before* any hardware decision:
    an unknown extension (the container would have to be guessed), a missing
    parent directory (this verb creates the file, never the directory tree),
    and an existing file without ``--overwrite`` (a recording must not silently
    destroy an earlier one).
    """
    output_path = os.path.abspath(path)
    extension = os.path.splitext(output_path)[1].lower()
    container = _CONTAINERS.get(extension)
    if container is None:
        known = ", ".join(sorted(_CONTAINERS))
        raise _user_error(
            f"cannot infer a container from output extension {extension or '(none)'!r}",
            f"use one of the known extensions ({known}); the container is never guessed",
        )

    parent = os.path.dirname(output_path) or "."
    if not os.path.isdir(parent):
        raise _user_error(
            f"output directory {parent} does not exist",
            "create the directory first — record writes the file, not the tree above it",
        )

    if os.path.exists(output_path) and not overwrite:
        raise _user_error(
            f"output path {output_path} already exists",
            "pass --overwrite to replace it, or choose another path",
        )

    return output_path, container


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------


def _paper_access(node: str) -> dict[str, object]:
    """Stat, never open — see ``stream``'s helper for why this is not a check."""
    return {
        "path": node,
        "kind": "audio",
        "checked": False,
        "state": "absent" if not os.path.exists(node) else "unchecked",
        "holder": None,
        "remediation": "",
        "note": "dry run — the node was stat'ed, never opened; pass --probe to really check",
    }


def _checked_access(node: str) -> dict[str, object]:
    report = access.check_access(node, "audio")
    holder = (
        {"pid": report.holder.pid, "command": report.holder.command}
        if report.holder is not None
        else None
    )
    return {
        "path": report.path,
        "kind": report.kind,
        "checked": True,
        "state": report.state.value,
        "holder": holder,
        "remediation": report.remediation,
        "note": "one non-blocking open(2), closed immediately; no audio was read",
    }


def _engine_state(cap: engine.Capability | None) -> dict[str, object]:
    if cap is None:
        return {
            "checked": False,
            "available": None,
            "gst_launch_present": None,
            "note": "dry run — the engine was not detected; pass --probe or --apply to check",
        }
    return {
        "checked": True,
        "available": cap.available,
        "gst_launch_present": cap.gst_launch is not None,
        "note": "detected via gst-inspect-1.0, which opens no device",
    }


@dataclass(frozen=True)
class _Plan:
    """The validated request: what to write, in what shape, under what bounds.

    Built once in :func:`cmd_record` and passed whole, so the payload, the
    activation params and the apply loop all read the same numbers instead of
    threading six arguments each.
    """

    container: str
    fmt: engine.AudioFormat
    fmt_source: dict[str, str]
    duration_s: float
    max_bytes: int
    output_path: str


def _payload(
    *,
    device: devices.MicrophoneDevice,
    selector: str,
    node: str,
    plan: _Plan,
    argv: list[str],
    mode: str,
    cap: engine.Capability | None,
    access_state: dict[str, object],
    resolved_at: str,
) -> dict[str, object]:
    applied = mode == "apply"
    fmt = plan.fmt
    device_dict = device.as_dict()
    device_dict["selector"] = selector
    return {
        "mode": mode,
        "apply": applied,
        "hardware_touched": applied,
        "engine_checked": cap is not None,
        "device": device_dict,
        "kind": "audio",
        "container": plan.container,
        "capture_node": node,
        "audio_address": device.alsa_address,
        "audio_format": {
            "requested": {
                "rate": fmt.rate,
                "channels": fmt.channels,
                "sample_format": fmt.sample_format,
                "source": plan.fmt_source or {},
            },
            "planned": {
                "rate": fmt.rate,
                "channels": fmt.channels,
                "sample_format": fmt.sample_format,
            },
            "probed": False,
            "note": (
                "applied as an exact caps filter — an unsupported combination fails at "
                "pipeline start rather than being silently substituted"
            ),
        },
        "pipeline_preview": list(argv),
        "pipeline_preview_str": " ".join(shlex.quote(token) for token in argv),
        "bound": {
            "duration_s": plan.duration_s,
            "max_bytes": plan.max_bytes,
            "unbounded_is_impossible": True,
        },
        "warmup_s": 0.0,
        "warmup_basis": _WARMUP_BASIS,
        "output_path": plan.output_path,
        "would_write": [plan.output_path],
        "access": access_state,
        "engine": _engine_state(cap),
        "timestamps": {"resolved_at": resolved_at},
    }


def _render_text(data: dict[str, object]) -> str:
    fmt = data["audio_format"]["planned"]  # type: ignore[index]
    bound = data["bound"]
    lines = [
        f"verb:      record ({data['mode']})",
        f"device:    {data['device']['stable_id']}",  # type: ignore[index]
        f"source:    {data['audio_address']}  ({data['capture_node']})",
        f"format:    {fmt['sample_format']} {fmt['rate']} Hz {fmt['channels']} ch "
        f"-> {data['container']}",
        f"bound:     {bound['duration_s']:g}s / {bound['max_bytes']} bytes",  # type: ignore
        f"output:    {data['output_path']}",
        f"access:    {data['access']['state']}",  # type: ignore[index]
        f"pipeline:  {data['pipeline_preview_str']}",
    ]
    if data["apply"]:
        lines.append(f"written:   {data['bytes_written']} bytes ({data['stopped_reason']})")
    else:
        lines.append("hardware:  untouched — pass --apply to actually record")
    return "\n".join(lines)


def _emit(data: dict[str, object], *, json_mode: bool) -> None:
    emit_result(data if json_mode else _render_text(data), json_mode=json_mode)


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def _artifact_size(path: str) -> int:
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


def _stop(proc: subprocess.Popen) -> None:
    """Stop the child for real: SIGTERM, then SIGKILL, and wait for each.

    This function only returns once the child is known to be gone. A child
    that ignores SIGTERM is escalated to SIGKILL (with a diagnostic saying so)
    and waited for again; a child that survives *that* is a
    :class:`CliError` (exit 2), because returning here would let
    :func:`_run_bounded` report a bound as having stopped a recording that is
    in fact still running and still holding the capture device.
    """
    try:
        proc.terminate()
    except OSError as exc:
        # Already reaped / already gone: nothing left to stop.
        emit_diagnostic(f"warning: could not signal the recording pipeline: {exc}")
        return
    try:
        proc.wait(timeout=TERMINATE_TIMEOUT_S)
        return
    except subprocess.TimeoutExpired:
        emit_diagnostic(
            f"warning: recording pipeline (pid {proc.pid}) did not exit within "
            f"{TERMINATE_TIMEOUT_S:g}s of SIGTERM; escalating to SIGKILL"
        )
    except OSError as exc:
        emit_diagnostic(f"warning: could not wait for the recording pipeline: {exc}")
        return

    try:
        proc.kill()
        proc.wait(timeout=KILL_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=(
                f"recording pipeline (pid {proc.pid}) survived SIGTERM and SIGKILL; "
                "the recording is still running and its bound was not enforced"
            ),
            remediation=(
                f"the process is likely stuck in uninterruptible I/O on the capture "
                f"device — inspect it with `ps -o stat= -p {proc.pid}`, then unplug or "
                "reset the device if it stays in D state"
            ),
        ) from exc
    except OSError as exc:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=(
                f"could not kill the recording pipeline (pid {proc.pid}): {exc}; "
                "the recording may still be running"
            ),
            remediation=f"check the process by hand (`ps -p {proc.pid}`) and stop it",
        ) from exc


def _run_bounded(
    proc: subprocess.Popen, output_path: str, duration_s: float, max_bytes: int
) -> str:
    """Poll the child until a bound is hit or it exits; returns ``stopped_reason``.

    ``"eos"`` — the pipeline ended on its own (the argv's ``num-buffers`` bound
    ran out, the normal path); ``"error"`` — it exited non-zero;
    ``"duration"`` / ``"max_bytes"`` — this loop stopped it because the child
    outlived its own bound or the artifact outgrew the size cap.

    Returning is a claim that the child is gone: :func:`_stop` waits for it and
    raises rather than returning while it lives. The reason says *why* the
    recording ended; whether the artifact honoured ``max_bytes`` is a separate
    question, checked against the file itself in :func:`_apply` — the exit
    check below can fire in the same poll interval in which the cap was blown.
    """
    started = _monotonic()
    deadline = duration_s + STOP_GRACE_S
    while True:
        code = proc.poll()
        if code is not None:
            return "eos" if code == 0 else "error"
        if _artifact_size(output_path) >= max_bytes:
            _stop(proc)
            return "max_bytes"
        if _monotonic() - started >= deadline:
            _stop(proc)
            return "duration"
        _sleep(POLL_INTERVAL_S)


def _apply(
    *,
    device: devices.MicrophoneDevice,
    node: str,
    argv: list[str],
    plan: _Plan,
    params: dict[str, object],
) -> tuple[str, int, str, str]:
    """Enforce access, spawn, bound, and record the activation.

    Returns ``(stopped_reason, bytes_written, started_at, ended_at)``.
    """
    # Enforced before anything is spawned, so a busy (exit 3) or forbidden
    # (exit 2) device is a typed error rather than a gst-launch crash.
    access.require_access(node, "audio")

    output_path = plan.output_path
    max_bytes = plan.max_bytes

    started_at = _now_iso()
    with activation.activation_scope("record", device.stable_id, params) as act:
        proc = _spawn(argv)
        act.params["pid"] = proc.pid
        stopped_reason = _run_bounded(proc, output_path, plan.duration_s, max_bytes)
        size = _artifact_size(output_path)
        act.params["stopped_reason"] = stopped_reason
        act.params["bytes_written"] = size

        if stopped_reason == "error":
            raise CliError(
                code=EXIT_ENV_ERROR,
                message=f"recording pipeline exited non-zero; wrote {size} bytes",
                remediation="run the printed pipeline by hand to see gst-launch-1.0's own "
                "diagnostics, or re-run with --probe to check the engine and device first",
            )
        if size > max_bytes and stopped_reason != "max_bytes":
            # The cap is polled. When *this loop* stopped the child at the cap,
            # an overshoot of one poll interval is the bound doing its job and
            # is reported as bytes_written. But a pipeline that blows the cap
            # and exits on its own inside a single poll interval was never
            # stopped by this module — the file on disk is over the cap the
            # caller asked for, and saying "eos, all good" about it would be a
            # lie. The artifact is deliberately kept:
            # deleting a recording the caller may still want is not this verb's
            # decision to make.
            raise CliError(
                code=EXIT_ENV_ERROR,
                message=(
                    f"recording exceeded its size cap: wrote {size} bytes, "
                    f"--max-bytes is {max_bytes}"
                ),
                remediation=(
                    f"the file was kept at {output_path} so you can decide what to do with "
                    "it (inspect it, truncate it, delete it); re-run with a larger "
                    "--max-bytes, a shorter --duration, or a lower rate/channel count if "
                    "you want it to fit"
                ),
            )
        if size == 0:
            raise CliError(
                code=EXIT_ENV_ERROR,
                message=f"recording produced no bytes at {output_path}",
                remediation="check that the requested rate/channels/format are supported by "
                "this device (run the printed pipeline by hand to see why it produced nothing)",
            )

    return stopped_reason, size, started_at, _now_iso()


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------


def cmd_record(args: argparse.Namespace) -> None:
    json_mode = bool(getattr(args, "json", False))
    root = getattr(args, "root", "/") or "/"
    resolved_at = _now_iso()

    duration_s = _validate_duration(args.duration)
    max_bytes = _validate_max_bytes(args.max_bytes)
    output_path, container = _resolve_output(args.output, overwrite=bool(args.overwrite))

    device = devices.resolve(args.device, root=root)
    node = capture_node_path(device, root=root)
    fmt, fmt_source = advertised_format(
        root, device, rate=args.rate, channels=args.channels, sample_format=args.format
    )
    argv = engine.build_audio_record_argv(
        device.alsa_address, fmt, output_path, container=container, duration_s=duration_s
    )

    plan = _Plan(
        container=container,
        fmt=fmt,
        fmt_source=fmt_source,
        duration_s=duration_s,
        max_bytes=max_bytes,
        output_path=output_path,
    )

    apply_mode = bool(args.apply)
    probe_mode = bool(args.probe) or apply_mode

    cap: engine.Capability | None = None
    if probe_mode:
        cap = engine.require_engine()
        engine.require_elements(cap, _CONTAINER_ELEMENTS[container])

    access_state = _checked_access(node) if probe_mode else _paper_access(node)
    if apply_mode:
        mode = "apply"
    elif probe_mode:
        mode = "probe"
    else:
        mode = "dry-run"

    data = _payload(
        device=device,
        selector=args.device,
        node=node,
        plan=plan,
        argv=argv,
        mode=mode,
        cap=cap,
        access_state=access_state,
        resolved_at=resolved_at,
    )

    if not apply_mode:
        _emit(data, json_mode=json_mode)
        return

    params: dict[str, object] = {
        "output_path": output_path,
        "container": container,
        "rate": fmt.rate,
        "channels": fmt.channels,
        "sample_format": fmt.sample_format,
        "duration_s": duration_s,
        "max_bytes": max_bytes,
        "capture_node": node,
    }
    stopped_reason, size, started_at, ended_at = _apply(
        device=device,
        node=node,
        argv=argv,
        plan=plan,
        params=params,
    )

    data["access"] = _checked_access(node)
    data["bytes_written"] = size
    data["stopped_reason"] = stopped_reason
    data["pipeline"] = list(argv)
    data["timestamps"] = {
        "resolved_at": resolved_at,
        "started_at": started_at,
        "ended_at": ended_at,
    }
    _emit(data, json_mode=json_mode)


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def _float_type(raw: str) -> float:
    try:
        return float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid value {raw!r}: must be a number") from exc


def _int_type(raw: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid value {raw!r}: must be a whole number") from exc


def _positive_int(raw: str) -> int:
    value = _int_type(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"invalid value {raw!r}: must be positive")
    return value


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "record",
        help="Record a bounded audio clip from a microphone to a file.",
        description=(
            "Record a bounded clip from a resolved microphone. Dry-run by default: "
            "resolves the device and validates the request without opening anything. "
            "Pass --apply to actually record. A duration and a size cap are always "
            "enforced — there is no flag that means 'forever'."
        ),
        epilog=(
            "Hardware: the default dry run touches nothing. --probe additionally detects "
            "the GStreamer engine and checks the capture node, still without spawning. "
            "--apply opens the device, records, and writes one activation-log line."
        ),
    )
    p.add_argument(
        "device",
        metavar="DEVICE",
        help="Stable device id, or a unique substring of one (see 'microphone list').",
    )
    p.add_argument(
        "output",
        metavar="OUTPUT_PATH",
        help="File path to write the recording to. The container comes from the extension: "
        ".mka (Opus in Matroska) or .wav (raw PCM).",
    )
    p.add_argument(
        "--duration",
        type=_float_type,
        default=DEFAULT_DURATION_S,
        metavar="SECONDS",
        help=f"Recording cap in seconds, 0 < duration <= {MAX_DURATION_S:g} "
        f"(default {DEFAULT_DURATION_S:g}).",
    )
    p.add_argument(
        "--max-bytes",
        type=_int_type,
        default=DEFAULT_MAX_BYTES,
        metavar="BYTES",
        help=f"Output-size cap in bytes, on top of --duration, 0 < max-bytes <= "
        f"{MAX_BYTES_CEILING} (default {DEFAULT_MAX_BYTES}).",
    )
    p.add_argument(
        "--rate",
        type=_positive_int,
        default=None,
        metavar="HZ",
        help=(
            "Sample rate. Default: the first rate the device advertises in "
            f"/proc/asound (else {DEFAULT_RATE}). Applied as an exact caps filter."
        ),
    )
    p.add_argument(
        "--channels",
        type=_positive_int,
        default=None,
        metavar="N",
        help=f"Channel count. Default: the device's advertised count (else {DEFAULT_CHANNELS}).",
    )
    p.add_argument(
        "--format",
        default=None,
        metavar="FMT",
        help=(
            "Sample format, GStreamer spelling. Default: the device's advertised format "
            f"(else {DEFAULT_SAMPLE_FORMAT})."
        ),
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace OUTPUT_PATH if it already exists (refused by default).",
    )
    p.add_argument(
        "--probe",
        action="store_true",
        help="Check the engine and the capture node's access state instead of describing "
        "them on paper. Spawns nothing and writes no file.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually record (implies --probe). Without it, record only resolves and "
        "validates the request.",
    )
    p.add_argument(
        "--root",
        default="/",
        metavar="PATH",
        help="Filesystem root to resolve the device under (default: /); mainly for "
        "pointing at a synthetic device tree in tests.",
    )
    p.add_argument("--json", action="store_true", help=JSON_FLAG_HELP)
    p.set_defaults(func=cmd_record)
