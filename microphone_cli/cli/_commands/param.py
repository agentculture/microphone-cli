"""``microphone-cli param`` — read/write raw XVF3800 firmware parameters.

This is the low-level noun: every verb operates directly on one row of
:data:`microphone_cli.xvf3800.PARAMETERS`, addressed by name
(case-insensitive, always echoed upper-case). Validation — unknown name,
wrong access direction, wrong value count/type — happens *before* any USB
transfer is issued, both in dry-run (`param set` without ``--apply``) and in
apply mode, so a rejected command never touches hardware.

A second gate sits in front of ``--apply``: names in
:data:`microphone_cli.xvf3800.PERSISTENT` either survive a power-cycle (after
``SAVE_CONFIGURATION``), trigger a reboot, or are otherwise destructive
(``TEST_CORE_BURN``, the ``SPECIAL_CMD_*`` firmware-update commands, ...).
Writing one of those requires ``--allow-persistent`` in addition to
``--apply`` — an ordinary ``rw`` write is volatile and reverts on the next
power-cycle, so that extra flag is the one place this noun asks for explicit
confirmation. The persistent check runs *before* the device is opened or any
transfer is issued.

:func:`_open_array` is the hardware seam: it resolves a
:class:`~microphone_cli.devices.MicrophoneDevice` to a
:class:`~microphone_cli.xvf3800.Xvf3800` over a real USB node. Tests
monkeypatch it to return ``Xvf3800(fake_transfer)`` instead, so no test here
ever opens ``/dev``.
"""

from __future__ import annotations

import argparse
from typing import Any

from microphone_cli.activation import activation_scope
from microphone_cli.cli._commands.overview import emit_overview
from microphone_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from microphone_cli.cli._output import emit_result
from microphone_cli.devices import MicrophoneDevice, resolve
from microphone_cli.usbctl import find_devices, open_device
from microphone_cli.xvf3800 import PARAMETERS, ParamInfo, Xvf3800, param_info

_NUMERIC_FLOAT_TYPES = ("float", "radians")

_NOT_ARRAY_HINT = (
    "select a microphone array (an XVF3800 device — `microphone list --json` and check "
    "is_array) instead"
)


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------


def _sections() -> list[dict[str, object]]:
    return [
        {
            "title": "Verbs",
            "items": [
                "param list — every row of the XVF3800 parameter table",
                "param get <device> <NAME> — read one parameter",
                "param set <device> <NAME> <values...> [--apply] [--allow-persistent] — "
                "write one parameter",
                "param overview — this description",
            ],
        },
        {
            "title": "Persistent tier (requires --allow-persistent on --apply)",
            "items": [
                "SAVE_CONFIGURATION, CLEAR_CONFIGURATION, REBOOT, TEST_CORE_BURN, "
                "TEST_AEC_DISABLE_CONTROL, USB_BIT_DEPTH and every SPECIAL_CMD_* name persist "
                "across reboot, trigger a reboot, or are otherwise destructive — unlike an "
                "ordinary rw write, which is volatile and reverts on power-cycle.",
            ],
        },
    ]


def cmd_param_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "microphone-cli param",
        _sections(),
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    # `microphone-cli param` with no sub-verb prints the noun's overview.
    return cmd_param_overview(args)


# ---------------------------------------------------------------------------
# hardware seam
# ---------------------------------------------------------------------------


def _resolve_device(selector: str, root: str) -> MicrophoneDevice:
    device = resolve(selector, root=root)
    if not device.is_array:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"{device.stable_id} is not an XVF3800 microphone array",
            remediation=_NOT_ARRAY_HINT,
        )
    return device


def _open_array(device: MicrophoneDevice, root: str) -> Xvf3800:
    """Resolve ``device`` to an open :class:`Xvf3800`. Tests monkeypatch this."""
    matches = find_devices(
        root=root,
        vendor=device.usb_ids.vendor,
        product=device.usb_ids.product,
        serial=device.serial or None,
    )
    if not matches:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"cannot locate a USB node for {device.stable_id}",
            remediation="Replug the device and retry; check `microphone list --json`.",
        )
    fd = open_device(matches[0]["node"])
    return Xvf3800(fd)


# ---------------------------------------------------------------------------
# value parsing
# ---------------------------------------------------------------------------


def _parse_values(info: ParamInfo, tokens: list[str]) -> Any:
    """Convert CLI string tokens to typed values for ``info``, or raise.

    ``char`` parameters take exactly one string token, used verbatim.
    Every other type takes exactly ``info.count`` tokens, converted to
    ``float`` (``float``/``radians``) or ``int`` (``uint8``/``int32``/
    ``uint32``). Raises :class:`CliError` (exit 1) on a wrong token count or
    a token that does not convert — always before any USB transfer.
    """
    if info.type == "char":
        if len(tokens) != 1:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"{info.name} takes a single string value, got {len(tokens)}",
                remediation=f"Pass exactly one string value (up to {info.count} characters).",
            )
        return tokens[0]

    if len(tokens) != info.count:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"{info.name} takes {info.count} value(s), got {len(tokens)}",
            remediation=f"Pass exactly {info.count} value(s) of type {info.type}.",
        )

    converter = float if info.type in _NUMERIC_FLOAT_TYPES else int
    try:
        return [converter(token) for token in tokens]
    except ValueError as exc:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"{info.name} value(s) not valid for type {info.type}: {exc}",
            remediation=f"Pass {info.count} value(s) that fit type {info.type}.",
        ) from exc


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def cmd_param_list(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    infos = sorted((param_info(name) for name in PARAMETERS), key=lambda info: info.name)
    payload = {"params": [info.to_dict() for info in infos], "count": len(infos)}
    if json_mode:
        emit_result(payload, json_mode=True)
        return 0
    lines = [
        f"{info.name}  resid={info.resid} cmdid={info.cmdid} count={info.count} "
        f"access={info.access} type={info.type} persistent={info.persistent}"
        for info in infos
    ]
    lines.append(f"({len(infos)} parameters)")
    emit_result("\n".join(lines), json_mode=False)
    return 0


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


def cmd_param_get(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    root = getattr(args, "root", "/") or "/"

    info = param_info(args.name)
    if info.access == "wo":
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"{info.name} is write-only and cannot be read",
            remediation="Use `microphone param set` for write-only parameters.",
        )

    device = _resolve_device(args.device, root)
    with _open_array(device, root) as xvf:
        values = xvf.read(info.name)

    payload = {"device": device.stable_id, "param": info.to_dict(), "values": values}
    if json_mode:
        emit_result(payload, json_mode=True)
        return 0
    emit_result(f"{info.name} = {values}", json_mode=False)
    return 0


# ---------------------------------------------------------------------------
# set
# ---------------------------------------------------------------------------


def cmd_param_set(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    root = getattr(args, "root", "/") or "/"
    apply = bool(getattr(args, "apply", False))
    allow_persistent = bool(getattr(args, "allow_persistent", False))

    info = param_info(args.name)
    if info.access == "ro":
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"{info.name} is read-only and cannot be written",
            remediation="Use `microphone param get` to read it.",
        )
    values = _parse_values(info, list(args.values))

    device = _resolve_device(args.device, root)

    if not apply:
        payload = {
            "mode": "dry-run",
            "applied": False,
            "hardware_touched": False,
            "device": device.stable_id,
            "param": info.to_dict(),
            "values": values,
        }
        if json_mode:
            emit_result(payload, json_mode=True)
        else:
            emit_result(
                f"dry-run: would write {info.name} = {values} on {device.stable_id} "
                "(pass --apply to send it)",
                json_mode=False,
            )
        return 0

    if info.persistent and not allow_persistent:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"{info.name} is a persistent/destructive parameter",
            remediation=(
                "Pass --allow-persistent to confirm. Most rw parameter writes are volatile "
                "and revert on the next power-cycle; this one either survives a power-cycle "
                "(e.g. after SAVE_CONFIGURATION), triggers a reboot, or is otherwise "
                "destructive, so it needs an explicit opt-in."
            ),
        )

    with _open_array(device, root) as xvf:
        with activation_scope(
            "param set",
            device.stable_id,
            {"param": info.name, "values": values, "persistent": info.persistent},
        ):
            xvf.write(info.name, values)
            payload = {
                "mode": "apply",
                "applied": True,
                "hardware_touched": True,
                "device": device.stable_id,
                "param": info.to_dict(),
                "values": values,
            }
            if info.access == "rw":
                payload["readback"] = xvf.read(info.name)

    if json_mode:
        emit_result(payload, json_mode=True)
    else:
        suffix = f" readback={payload['readback']}" if "readback" in payload else ""
        emit_result(
            f"applied: {info.name} = {values} on {device.stable_id}{suffix}",
            json_mode=False,
        )
    return 0


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "param",
        help="Read/write raw XVF3800 firmware parameters (see 'microphone param overview').",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=_no_verb, json=False)
    noun_sub = p.add_subparsers(dest="param_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the param noun (verbs, persistent tier).")
    ov.add_argument("--json", action="store_true", help="Emit structured JSON.")
    ov.set_defaults(func=cmd_param_overview)

    lst = noun_sub.add_parser("list", help="List every XVF3800 parameter table row.")
    lst.add_argument("--json", action="store_true", help="Emit structured JSON.")
    lst.set_defaults(func=cmd_param_list)

    get = noun_sub.add_parser("get", help="Read one parameter from a microphone array.")
    get.add_argument("device", help="Stable id (or other selector) of the microphone array.")
    get.add_argument("name", help="Parameter name (case-insensitive).")
    get.add_argument(
        "--root",
        default="/",
        help="Root filesystem to resolve the device under (tests point this at a fixture tree).",
    )
    get.add_argument("--json", action="store_true", help="Emit structured JSON.")
    get.set_defaults(func=cmd_param_get)

    st = noun_sub.add_parser("set", help="Write one parameter on a microphone array.")
    st.add_argument("device", help="Stable id (or other selector) of the microphone array.")
    st.add_argument("name", help="Parameter name (case-insensitive).")
    st.add_argument("values", nargs="+", help="Value(s) to write, parsed per the parameter type.")
    st.add_argument(
        "--apply",
        action="store_true",
        help="Actually send the write (default is a dry-run: validate only, no transfer).",
    )
    st.add_argument(
        "--allow-persistent",
        action="store_true",
        dest="allow_persistent",
        help="Confirm a write to a persistent/destructive parameter (see 'param overview').",
    )
    st.add_argument(
        "--root",
        default="/",
        help="Root filesystem to resolve the device under (tests point this at a fixture tree).",
    )
    st.add_argument("--json", action="store_true", help="Emit structured JSON.")
    st.set_defaults(func=cmd_param_set)
