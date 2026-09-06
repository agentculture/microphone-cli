"""``microphone array`` — direction-of-arrival and AEC state for an XVF3800 array.

Two verb families hang off this noun:

* ``array doa <device>`` — read ``DOA_VALUE_RADIANS`` from the firmware, once
  or continuously (``--watch``, JSON Lines). The azimuth is reported **exactly
  as the firmware reports it**: raw radians in the array's own frame. No
  coordinate transform, no degree conversion, no re-basing onto a robot frame
  happens here — a consumer that needs another frame owns that conversion and
  can only do it correctly if it starts from the untouched firmware value.
* ``array aec get|set <device>`` — the echo-canceller's observable state
  (converged, bypass, high-pass filter, echo suppression, mic count, array
  geometry) and the three switches that are safe to flip. ``set`` is a dry run
  unless ``--apply`` is passed; ``--apply`` writes inside an
  :func:`~microphone_cli.activation.activation_scope`, so every hardware-touching
  run leaves exactly one line in the activation log.

The noun group follows the pattern cited from ``webcam-cli``'s
``webcam_cli/cli/_commands/stream.py`` (lines 1603-1672) and the local
:mod:`microphone_cli.cli._commands.cli`: a bare ``array`` prints the noun's own
overview, and **every** nested ``add_subparsers`` call passes
``parser_class=type(p)`` so parse errors keep routing through the structured
error contract instead of argparse's default ``exit(2)``.

Hardware access is funnelled through the module-level :func:`_open_array` and
:data:`_sleep` seams; tests replace both, so no test opens a device node.
"""

from __future__ import annotations

import argparse
import errno
import time
from datetime import datetime, timezone
from typing import Any, Callable

from microphone_cli import devices, usbctl
from microphone_cli.activation import activation_scope
from microphone_cli.cli._commands.overview import emit_overview
from microphone_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from microphone_cli.cli._output import emit_result
from microphone_cli.xvf3800 import Xvf3800

_JSON_HELP = "Emit structured JSON."

#: The one DoA parameter this verb reads. Raw firmware radians, never converted.
DOA_PARAM = "DOA_VALUE_RADIANS"

#: Speech flag threshold: the firmware reports a float alongside the azimuth.
_SPEECH_THRESHOLD = 0.5

#: ``array aec get`` reads these; the payload key is the dict key.
_AEC_READS: tuple[tuple[str, str, str], ...] = (
    ("converged", "AEC_AECCONVERGED", "bool"),
    ("bypass", "SHF_BYPASS", "bool"),
    ("hpf", "AEC_HPFONOFF", "bool"),
    ("echo", "PP_ECHOONOFF", "bool"),
    ("num_mics", "AEC_NUM_MICS", "int"),
    ("geometry_type", "AEC_MIC_ARRAY_TYPE", "int"),
    ("geometry", "AEC_MIC_ARRAY_GEO", "floats"),
    ("rt60", "AEC_RT60", "float"),
)

#: ``array aec set`` switches: flag name -> (parameter, value type).
_AEC_WRITES: tuple[tuple[str, str], ...] = (
    ("echo", "PP_ECHOONOFF"),
    ("bypass", "SHF_BYPASS"),
    ("hpf", "AEC_HPFONOFF"),
)

# Testing seams.
_sleep: Callable[[float], None] = time.sleep


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# device plumbing
# ---------------------------------------------------------------------------


def _resolve_array(selector: str, root: str) -> devices.MicrophoneDevice:
    """Resolve ``selector`` and refuse anything that is not an XVF3800 array."""
    device = devices.resolve(selector, root)
    if not device.is_array:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"{device.stable_id} is not an XVF3800 microphone array",
            remediation=(
                "Only XVF3800 arrays expose DoA and AEC parameters. Run "
                "`microphone list --json` and pick a device whose `is_array` is true."
            ),
        )
    return device


def _open_array(
    device: devices.MicrophoneDevice, root: str = "/", timeout_ms: int | None = None
) -> Xvf3800:
    """Open the USB node behind ``device`` and return an :class:`Xvf3800` for it."""
    matches = usbctl.find_devices(
        root=root,
        vendor=device.usb_ids.vendor,
        product=device.usb_ids.product,
        serial=device.serial,
    )
    if not matches:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"no USB node found for {device.stable_id}",
            remediation=(
                "The ALSA card exists but its USB device node does not. Replug the array and "
                "retry; `microphone list --json` shows what is attached right now."
            ),
        )
    return Xvf3800(usbctl.open_device(matches[0]["node"]), timeout_ms=timeout_ms)


def _transport_error(exc: OSError, device: devices.MicrophoneDevice) -> CliError:
    """Translate a raw transport ``OSError`` into the structured contract."""
    if exc.errno in (errno.ENODEV, errno.ENOENT, errno.ESHUTDOWN):
        return CliError(
            code=EXIT_ENV_ERROR,
            message=f"device disappeared: {device.stable_id} ({exc})",
            remediation=(
                "The array was unplugged or reset mid-transfer. Replug it and retry; "
                "`microphone list --json` shows what is attached right now."
            ),
        )
    return CliError(
        code=EXIT_ENV_ERROR,
        message=f"USB transfer failed for {device.stable_id}: {exc}",
        remediation="Check that no other process holds the array, then retry.",
    )


# ---------------------------------------------------------------------------
# doa
# ---------------------------------------------------------------------------


def _read_doa(chip: Xvf3800, device: devices.MicrophoneDevice) -> dict[str, object]:
    """One DoA sample. ``azimuth_rad`` is the firmware's own radian value, untouched."""
    values = chip.read(DOA_PARAM)
    azimuth = float(values[0])
    speech = float(values[1]) if len(values) > 1 else 0.0
    return {
        "device": device.stable_id,
        "azimuth_rad": azimuth,
        "speech": speech >= _SPEECH_THRESHOLD,
        "source": DOA_PARAM,
        "ts": _now(),
    }


def _render_doa(payload: dict[str, object]) -> str:
    return (
        f"device: {payload['device']}  azimuth_rad: {payload['azimuth_rad']:.6f}  "
        f"speech: {str(payload['speech']).lower()}  source: {payload['source']}"
    )


def cmd_array_doa(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    device = _resolve_array(args.device, args.root)

    if args.watch and args.interval <= 0:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"--interval must be greater than zero, got {args.interval}",
            remediation="Pass a positive number of seconds, e.g. `--interval 0.5`.",
        )
    if args.count is not None and args.count <= 0:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"--count must be greater than zero, got {args.count}",
            remediation="Pass a positive poll count, e.g. `--count 10`.",
        )

    chip = _open_array(device, args.root)
    try:
        if not args.watch:
            payload = _read_doa(chip, device)
            emit_result(
                payload if json_mode else _render_doa(payload),
                json_mode=json_mode,
            )
            return 0
        return _watch_doa(chip, device, interval=args.interval, count=args.count)
    finally:
        chip.close()


def _watch_doa(
    chip: Xvf3800,
    device: devices.MicrophoneDevice,
    *,
    interval: float,
    count: int | None,
) -> int:
    """Poll DoA, one JSON object per line, until ``count``, SIGINT, or a lost device.

    JSON Lines is the wire format whether or not ``--json`` was passed: a
    stream of samples has no useful non-JSON rendering, and a consumer reading
    the stream should not have to switch parsers on a flag.
    """
    polls = 0
    try:
        while count is None or polls < count:
            try:
                payload = _read_doa(chip, device)
            except OSError as exc:
                raise _transport_error(exc, device) from exc
            emit_result(payload, json_mode=True)
            polls += 1
            if count is not None and polls >= count:
                break
            _sleep(interval)
    except KeyboardInterrupt:
        # Ctrl-C is how a watch is meant to end: already-printed samples stand
        # and the run is a success.
        return 0
    return 0


# ---------------------------------------------------------------------------
# aec
# ---------------------------------------------------------------------------


def _coerce(kind: str, raw: Any) -> object:
    if kind == "bool":
        return bool(int(raw[0]))
    if kind == "int":
        return int(raw[0])
    if kind == "float":
        return float(raw[0])
    return [float(value) for value in raw]


def _read_aec(chip: Xvf3800, device: devices.MicrophoneDevice) -> dict[str, object]:
    payload: dict[str, object] = {"device": device.stable_id}
    for key, name, kind in _AEC_READS:
        payload[key] = _coerce(kind, chip.read(name))
    payload["ts"] = _now()
    return payload


def _render_aec(payload: dict[str, object]) -> str:
    lines = []
    for key, value in payload.items():
        if isinstance(value, bool):
            rendered = str(value).lower()
        elif isinstance(value, list):
            rendered = ", ".join(f"{item:.4f}" for item in value)
        else:
            rendered = str(value)
        lines.append(f"{key}: {rendered}")
    return "\n".join(lines)


def cmd_array_aec_get(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    device = _resolve_array(args.device, args.root)
    chip = _open_array(device, args.root)
    try:
        payload = _read_aec(chip, device)
    except OSError as exc:
        raise _transport_error(exc, device) from exc
    finally:
        chip.close()
    emit_result(payload if json_mode else _render_aec(payload), json_mode=json_mode)
    return 0


def _plan(args: argparse.Namespace) -> list[dict[str, object]]:
    planned: list[dict[str, object]] = []
    for flag, param in _AEC_WRITES:
        setting = getattr(args, flag, None)
        if setting is None:
            continue
        planned.append(
            {
                "setting": flag,
                "value": setting,
                "param": param,
                "values": [1 if setting == "on" else 0],
            }
        )
    return planned


def cmd_array_aec_set(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    planned = _plan(args)
    if not planned:
        raise CliError(
            code=EXIT_USER_ERROR,
            message="nothing to set: pass at least one of --echo, --bypass, --hpf",
            remediation=(
                "For example `microphone array aec set <device> --echo off --apply`. "
                "Run `microphone array aec get <device>` to see the current state."
            ),
        )

    device = _resolve_array(args.device, args.root)

    if not args.apply:
        # Dry run: the device is never opened, so nothing can touch hardware.
        payload = {
            "device": device.stable_id,
            "mode": "dry-run",
            "applied": False,
            "hardware_touched": False,
            "planned": planned,
        }
        emit_result(
            payload if json_mode else _render_dry_run(device.stable_id, planned),
            json_mode=json_mode,
        )
        return 0

    params = {str(item["setting"]): item["value"] for item in planned}
    chip = _open_array(device, args.root)
    try:
        with activation_scope("array aec set", device.stable_id, params):
            try:
                for item in planned:
                    chip.write(str(item["param"]), list(item["values"]))
                state = _read_aec(chip, device)
            except OSError as exc:
                raise _transport_error(exc, device) from exc
    finally:
        chip.close()

    payload = {
        "device": device.stable_id,
        "mode": "apply",
        "applied": True,
        "hardware_touched": True,
        "planned": planned,
        "state": state,
    }
    emit_result(payload if json_mode else _render_apply(payload), json_mode=json_mode)
    return 0


def _render_dry_run(stable_id: str, planned: list[dict[str, object]]) -> str:
    lines = [f"device: {stable_id}", "mode: dry-run (nothing was written; pass --apply)"]
    for item in planned:
        lines.append(
            f"would set {item['param']} = {item['values']}  ({item['setting']}={item['value']})"
        )
    return "\n".join(lines)


def _render_apply(payload: dict[str, object]) -> str:
    planned = payload["planned"]
    assert isinstance(planned, list)
    lines = [f"device: {payload['device']}", "mode: apply"]
    for item in planned:
        lines.append(
            f"wrote {item['param']} = {item['values']}  ({item['setting']}={item['value']})"
        )
    state = payload["state"]
    assert isinstance(state, dict)
    lines.append(_render_aec(state))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# overviews
# ---------------------------------------------------------------------------


def _array_sections() -> list[dict[str, object]]:
    return [
        {
            "title": "Verbs",
            "items": [
                "array overview — this description",
                "array doa <device> — read direction-of-arrival once",
                "array doa <device> --watch — stream DoA as JSON Lines "
                "(--interval, --count; Ctrl-C exits 0)",
                "array aec get <device> — echo-canceller state",
                "array aec set <device> — flip --echo/--bypass/--hpf (dry run without --apply)",
            ],
        },
        {
            "title": "Frame",
            "items": [
                f"azimuth comes from the firmware parameter {DOA_PARAM}",
                "raw firmware radians — no coordinate transform is applied here",
                "`speech` is the firmware's second DoA value, thresholded at 0.5",
            ],
        },
        {
            "title": "Hardware",
            "items": [
                "reads open the USB node; only `aec set --apply` writes",
                "every --apply run appends one line to the activation log "
                "($MICROPHONE_ACTIVATION_LOG)",
                "exit codes: 0 success, 1 user error, 2 device/environment error",
            ],
        },
    ]


def _aec_sections() -> list[dict[str, object]]:
    return [
        {
            "title": "get",
            "items": [f"{key} <- {name}" for key, name, _kind in _AEC_READS],
        },
        {
            "title": "set",
            "items": [f"--{flag} on|off -> {name}" for flag, name in _AEC_WRITES]
            + ["--apply is required to touch hardware; without it the plan is printed"],
        },
    ]


def cmd_array_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "microphone array",
        _array_sections(),
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def cmd_array_aec_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "microphone array aec",
        _aec_sections(),
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("device", help="Device selector (stable id, card id, serial, substring).")
    parser.add_argument(
        "--root",
        default="/",
        help="Filesystem root to read device state from (tests point this at a fixture tree).",
    )
    parser.add_argument("--json", action="store_true", help=_JSON_HELP)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "array",
        help="Microphone-array firmware: direction-of-arrival and echo-canceller state.",
        description="XVF3800 array verbs. Reads are safe; `aec set` needs --apply.",
    )
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=cmd_array_overview, json=False)

    # parser_class must propagate, or this noun's parse errors bypass the
    # structured error contract and exit 2 instead of 1.
    noun_sub = p.add_subparsers(dest="array_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the array verb group.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_array_overview)

    doa = noun_sub.add_parser(
        "doa",
        help="Read direction-of-arrival (raw firmware radians).",
        description=(
            "Read DOA_VALUE_RADIANS from the array firmware. The azimuth is reported in the "
            "firmware's own frame, in radians, with no coordinate transform applied."
        ),
    )
    _add_common(doa)
    doa.add_argument(
        "--watch",
        action="store_true",
        help="Poll continuously, printing one JSON object per line until Ctrl-C or --count.",
    )
    doa.add_argument(
        "--interval",
        type=float,
        default=0.5,
        metavar="SECONDS",
        help="Seconds between polls while watching (default 0.5).",
    )
    doa.add_argument(
        "--count",
        type=int,
        default=None,
        metavar="N",
        help="Stop after N samples while watching (default: run until interrupted).",
    )
    doa.set_defaults(func=cmd_array_doa)

    aec = noun_sub.add_parser(
        "aec",
        help="Echo-canceller state (see 'microphone array aec overview').",
    )
    aec.add_argument("--json", action="store_true", help=_JSON_HELP)
    aec.set_defaults(func=cmd_array_aec_overview, json=False)
    # Nested one level deeper: propagate parser_class again.
    aec_sub = aec.add_subparsers(dest="aec_command", parser_class=type(p))

    aec_ov = aec_sub.add_parser("overview", help="Describe the aec verb group.")
    aec_ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    aec_ov.set_defaults(func=cmd_array_aec_overview)

    aec_get = aec_sub.add_parser("get", help="Read echo-canceller state (read-only).")
    _add_common(aec_get)
    aec_get.set_defaults(func=cmd_array_aec_get)

    aec_set = aec_sub.add_parser(
        "set",
        help="Flip echo-canceller switches (dry run unless --apply).",
    )
    _add_common(aec_set)
    for flag, param in _AEC_WRITES:
        aec_set.add_argument(
            f"--{flag}",
            choices=("on", "off"),
            default=None,
            help=f"Set {param} on or off.",
        )
    aec_set.add_argument(
        "--apply",
        action="store_true",
        help="Actually write to the device (and append one activation-log line).",
    )
    aec_set.set_defaults(func=cmd_array_aec_set)
