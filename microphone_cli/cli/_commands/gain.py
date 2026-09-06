"""``microphone gain`` — read/set capture gain over ALSA and, on arrays, firmware.

Two independent gain knobs exist on an XVF3800 array and only one on a plain
USB microphone:

* **ALSA** — the kernel-level capture-volume mixer control, read and written
  through ``amixer`` (:mod:`microphone_cli.mixer`). Every USB capture device
  has one (or ``gain get``/``gain set`` fails with a user error naming the
  card).
* **Firmware** — ``AUDIO_MGR_MIC_GAIN`` on the XVF3800 itself
  (:mod:`microphone_cli.xvf3800`), read/written over a USB vendor control
  transfer. Only present on :attr:`~microphone_cli.devices.MicrophoneDevice.is_array`
  devices.

``gain get`` reports both when available. ``gain set`` defaults to
``--target both`` (silently ALSA-only on a non-array device) and is a dry run
unless ``--apply`` is passed: without ``--apply`` it computes and prints the
plan without issuing the ALSA ``cset`` or the firmware write, and without
opening the USB device at all; with ``--apply`` both actions are issued and
wrapped in exactly one :func:`~microphone_cli.activation.activation_scope`
line.

Value mapping: the CLI value is a float. For ALSA it is linearly mapped from
``0.0..1.0`` onto the control's reported ``[min, max]`` and rounded to the
nearest integer with Python's ``round()`` (banker's rounding: ties round to
the nearest even integer). For firmware, the value is written verbatim as the
raw ``AUDIO_MGR_MIC_GAIN`` float — there is no range to map onto.
"""

from __future__ import annotations

import argparse
import subprocess  # nosec B404 - fixed argv, no shell; passed through to mixer.py
from typing import Any

from microphone_cli import mixer
from microphone_cli.activation import activation_scope
from microphone_cli.cli._commands.overview import emit_overview
from microphone_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from microphone_cli.cli._output import emit_result
from microphone_cli.devices import MicrophoneDevice, resolve
from microphone_cli.usbctl import find_devices, open_device
from microphone_cli.xvf3800 import Xvf3800

_FIRMWARE_PARAM = "AUDIO_MGR_MIC_GAIN"
_TARGETS = ("alsa", "firmware", "both")

_VERBS = [
    "gain overview — this description",
    "gain get <device> — read the current ALSA and (on arrays) firmware gain",
    "gain set <device> <value> [--apply] — plan or apply a new gain (0.0..1.0)",
]


def _sections() -> list[dict[str, object]]:
    return [
        {"title": "Verbs", "items": list(_VERBS)},
        {
            "title": "Notes",
            "items": [
                "value is a float; ALSA maps 0.0..1.0 onto the control's min..max",
                "firmware gain (AUDIO_MGR_MIC_GAIN) is written verbatim, array devices only",
                "gain set is a dry run unless --apply is passed",
                "--apply writes exactly one line to the activation log",
            ],
        },
    ]


def cmd_gain_overview(args: argparse.Namespace) -> int:
    emit_overview("microphone gain", _sections(), json_mode=bool(getattr(args, "json", False)))
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_gain_overview(args)


# ---------------------------------------------------------------------------
# firmware access seam
# ---------------------------------------------------------------------------


def _open_firmware(device: MicrophoneDevice, root: str) -> Xvf3800:
    """Open the array's XVF3800 over USB. The seam tests monkeypatch."""
    matches = find_devices(
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
                "reattach the device and retry; `microphone device list` shows what's attached"
            ),
        )
    fd = open_device(
        matches[0]["node"], vendor=matches[0].get("vendor"), product=matches[0].get("product")
    )
    return Xvf3800(fd)


def _read_firmware_gain(device: MicrophoneDevice, root: str) -> dict[str, object]:
    """``{"mic_gain": f}`` on success, ``{"error": message}`` on any failure."""
    try:
        xvf = _open_firmware(device, root)
    except CliError as exc:
        return {"error": exc.message}
    try:
        values = xvf.read(_FIRMWARE_PARAM)
    except CliError as exc:
        return {"error": exc.message}
    finally:
        xvf.close()
    return {"mic_gain": values[0]}


# ---------------------------------------------------------------------------
# gain get
# ---------------------------------------------------------------------------


def _alsa_payload(control: mixer.MixerControl) -> dict[str, object]:
    return {
        "control": control.name,
        "numid": control.numid,
        "value": control.value,
        "min": control.min,
        "max": control.max,
    }


def cmd_gain_get(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    root = getattr(args, "root", "/") or "/"
    device = resolve(args.device, root=root)

    control = mixer.get_gain(device.card_index, run=subprocess.run)
    alsa = _alsa_payload(control)

    firmware: dict[str, object] | None = None
    if device.is_array:
        firmware = _read_firmware_gain(device, root)

    payload = {"device": device.stable_id, "alsa": alsa, "firmware": firmware}
    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        lines = [
            f"device: {device.stable_id}",
            f"alsa: {alsa['control']} (numid={alsa['numid']}) "
            f"= {alsa['value']} [{alsa['min']}..{alsa['max']}]",
        ]
        if firmware is None:
            lines.append("firmware: n/a (not an array)")
        elif "error" in firmware:
            lines.append(f"firmware: error: {firmware['error']}")
        else:
            lines.append(f"firmware: mic_gain = {firmware['mic_gain']}")
        emit_result("\n".join(lines), json_mode=False)
    return 0


# ---------------------------------------------------------------------------
# gain set
# ---------------------------------------------------------------------------


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _map_to_alsa(value: float, control: mixer.MixerControl) -> int:
    lo = control.min if control.min is not None else 0
    hi = control.max if control.max is not None else 100
    return round(lo + _clamp01(value) * (hi - lo))


def cmd_gain_set(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    root = getattr(args, "root", "/") or "/"
    target = getattr(args, "target", "both") or "both"
    value = float(args.value)
    apply = bool(getattr(args, "apply", False))

    device = resolve(args.device, root=root)

    do_alsa = target in ("alsa", "both")
    do_firmware = target in ("firmware", "both")
    if target == "firmware" and not device.is_array:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"{device.stable_id} is not a microphone array; it has no firmware gain",
            remediation="use --target alsa (or drop --target, the default is 'both')",
        )
    # "both" silently drops firmware on a non-array device: nothing to target.
    if target == "both" and not device.is_array:
        do_firmware = False

    if not apply:
        payload = _plan(device, value, do_alsa, do_firmware)
    else:
        payload = _apply(device, value, do_alsa, do_firmware, root, target)

    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        emit_result(_render_set_text(payload), json_mode=False)
    return 0


def _plan(
    device: MicrophoneDevice, value: float, do_alsa: bool, do_firmware: bool
) -> dict[str, Any]:
    planned: dict[str, object] = {}
    if do_alsa:
        control = mixer.get_gain(device.card_index, run=subprocess.run)
        raw = _map_to_alsa(value, control)
        planned["alsa"] = {"argv": mixer.set_gain_argv(device.card_index, control.numid, raw)}
    if do_firmware:
        planned["firmware"] = {"param": _FIRMWARE_PARAM, "values": [value]}
    return {
        "mode": "dry-run",
        "applied": False,
        "hardware_touched": False,
        "device": device.stable_id,
        "planned": planned,
    }


def _apply(
    device: MicrophoneDevice,
    value: float,
    do_alsa: bool,
    do_firmware: bool,
    root: str,
    target: str,
) -> dict[str, Any]:
    params: dict[str, object] = {"value": value, "target": target}
    result: dict[str, Any] = {
        "mode": "apply",
        "applied": True,
        "hardware_touched": True,
        "device": device.stable_id,
    }
    with activation_scope("gain set", device.stable_id, params):
        if do_alsa:
            control = mixer.get_gain(device.card_index, run=subprocess.run)
            raw = _map_to_alsa(value, control)
            updated = mixer.set_gain(device.card_index, control, raw, run=subprocess.run)
            result["alsa"] = _alsa_payload(updated)
            params["alsa_raw"] = raw
        if do_firmware:
            xvf = _open_firmware(device, root)
            try:
                xvf.write(_FIRMWARE_PARAM, [value])
                after = xvf.read(_FIRMWARE_PARAM)
            finally:
                xvf.close()
            result["firmware"] = {"mic_gain": after[0]}
            params["firmware_value"] = value
    return result


def _render_set_text(payload: dict[str, Any]) -> str:
    lines = [f"device: {payload['device']}", f"mode: {payload['mode']}"]
    if payload["mode"] == "dry-run":
        planned = payload["planned"]
        if "alsa" in planned:
            lines.append(f"planned alsa argv: {' '.join(planned['alsa']['argv'])}")
        if "firmware" in planned:
            fw = planned["firmware"]
            lines.append(f"planned firmware: {fw['param']} = {fw['values']}")
        if not planned:
            lines.append("planned: nothing to do")
    else:
        if "alsa" in payload:
            alsa = payload["alsa"]
            lines.append(f"alsa: {alsa['control']} (numid={alsa['numid']}) = {alsa['value']}")
        if "firmware" in payload:
            lines.append(f"firmware: mic_gain = {payload['firmware']['mic_gain']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "gain",
        help="Read/set microphone capture gain (see 'microphone gain overview').",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=_no_verb, json=False)
    # Propagate parser_class so nested verbs route parse-time errors through the
    # structured CliError contract instead of argparse's default exit(2).
    noun_sub = p.add_subparsers(dest="gain_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the gain verb group.")
    ov.add_argument("--json", action="store_true", help="Emit structured JSON.")
    ov.set_defaults(func=cmd_gain_overview)

    get = noun_sub.add_parser("get", help="Read the current ALSA and firmware gain.")
    get.add_argument("device", help="A microphone selector (stable id, serial, card id, ...).")
    get.add_argument("--root", default="/", help="Root to resolve devices under (testing).")
    get.add_argument("--json", action="store_true", help="Emit structured JSON.")
    get.set_defaults(func=cmd_gain_get)

    set_ = noun_sub.add_parser("set", help="Plan or apply a new capture gain.")
    set_.add_argument("device", help="A microphone selector (stable id, serial, card id, ...).")
    set_.add_argument("value", type=float, help="Gain, 0.0..1.0.")
    set_.add_argument(
        "--apply",
        action="store_true",
        help="Actually issue the ALSA/firmware writes (default: dry run).",
    )
    set_.add_argument(
        "--target",
        choices=_TARGETS,
        default="both",
        help="Which gain(s) to set (default: both, where available).",
    )
    set_.add_argument("--root", default="/", help="Root to resolve devices under (testing).")
    set_.add_argument("--json", action="store_true", help="Emit structured JSON.")
    set_.set_defaults(func=cmd_gain_set)
