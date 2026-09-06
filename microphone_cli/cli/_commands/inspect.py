"""``microphone inspect <device>`` — capture formats, rates, and firmware.

Resolves a selector through :func:`microphone_cli.devices.resolve` (identity;
never a raw ALSA card number — see that module's docstring), then reads the
USB-audio driver's own report of what the capture interface offers from
``<root>/proc/asound/card<index>/stream0``: ALSA format names, sample rates
and channel count. All reads are taken relative to the device's ``root`` so
tests stay hardware-free — no ALSA library is loaded and no ``/proc`` node
outside a fixture tree is ever touched.

Cited from ``../webcam-cli/webcam_cli/cli/_commands/list_devices.py`` for the
"compose, never hard-fail" shape: a descriptive verb accepts a bad or partial
target and still returns something rather than raising (see
``CLAUDE.md``, "Adding a verb or noun", point 6).

On an XVF3800-based array (``device.is_array``) this also looks the device up
on the USB bus with :func:`microphone_cli.usbctl.find_devices` and reads its
firmware identity with :class:`microphone_cli.xvf3800.Xvf3800`. That control
path needs a device node most agents cannot open without a udev rule (see
``microphone_cli.access``), so a permission or lookup failure there is
reported as ``firmware: {"error": ...}`` rather than raised — ``inspect`` is
descriptive and must not hard-fail on a permissions problem non-array
microphones don't even have. Non-array microphones report ``firmware: null``;
there is no XVF3800 control protocol to speak to them with.
"""

from __future__ import annotations

import argparse
import os
import re

from microphone_cli import usbctl
from microphone_cli.cli._errors import CliError
from microphone_cli.cli._output import emit_result
from microphone_cli.devices import MicrophoneDevice, resolve
from microphone_cli.xvf3800 import Xvf3800

# "Capture:" / "Playback:" section header in /proc/asound/cardN/stream0.
_SECTION_RE = re.compile(r"^(?P<section>Playback|Capture):\s*$")
# "    Format: S32_LE"
_FORMAT_RE = re.compile(r"^\s*Format:\s*(?P<fmt>\S+)\s*$")
# "    Channels: 6"
_CHANNELS_RE = re.compile(r"^\s*Channels:\s*(?P<n>\d+)\s*$")
# "    Rates: 16000, 48000"
_RATES_RE = re.compile(r"^\s*Rates:\s*(?P<rates>.+)$")


def _stream0_path(root: str, device: MicrophoneDevice) -> str:
    return os.path.join(root or "/", "proc", "asound", f"card{device.card_index}", "stream0")


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _parse_capture_block(text: str) -> tuple[list[str], list[int], int | None]:
    """Formats, rates and the widest channel count of the ``Capture:`` block.

    A multi-altset device lists one block per altset; every altset's format
    and rates are collected (order-preserving, de-duplicated), and the widest
    ``Channels:`` value is reported — the same "widest wins" rule
    :mod:`microphone_cli.devices` uses for the same reason: the widest altset
    is the one that carries every microphone of an array.
    """
    formats: list[str] = []
    rates: list[int] = []
    channels: list[int] = []
    section: str | None = None

    for raw_line in text.splitlines():
        matched_section = _SECTION_RE.match(raw_line.strip())
        if matched_section is not None:
            section = matched_section.group("section")
            continue
        if section != "Capture":
            continue

        matched_fmt = _FORMAT_RE.match(raw_line)
        if matched_fmt is not None:
            fmt = matched_fmt.group("fmt")
            if fmt not in formats:
                formats.append(fmt)
            continue

        matched_channels = _CHANNELS_RE.match(raw_line)
        if matched_channels is not None:
            channels.append(int(matched_channels.group("n")))
            continue

        matched_rates = _RATES_RE.match(raw_line)
        if matched_rates is not None:
            for token in matched_rates.group("rates").split(","):
                token = token.strip()
                if token.isdigit():
                    value = int(token)
                    if value not in rates:
                        rates.append(value)

    return formats, sorted(rates), (max(channels) if channels else None)


def _formats_rates_channels(
    root: str, device: MicrophoneDevice
) -> tuple[list[str], list[int], int | None]:
    text = _read_text(_stream0_path(root, device))
    if text is None:
        # No stream0 in this tree: fall back to the device's own channel
        # count (already derived from stream0 or hw_params by devices.py)
        # and report no known formats/rates rather than guessing at them.
        return [], [], device.channels
    formats, rates, channels = _parse_capture_block(text)
    if not formats and not rates and channels is None:
        return [], [], device.channels
    return formats, rates, channels if channels is not None else device.channels


def _firmware_payload(root: str, device: MicrophoneDevice) -> dict[str, object] | None:
    if not device.is_array:
        return None

    matches = usbctl.find_devices(
        root=root,
        vendor=device.usb_ids.vendor,
        product=device.usb_ids.product,
        serial=device.serial,
    )
    if not matches:
        return {
            "error": (
                f"no USB device node found under {root!r} for {device.stable_id} "
                f"({device.usb_ids})"
            )
        }

    node = matches[0]["node"]
    try:
        fd = usbctl.open_device(
            node, vendor=matches[0].get("vendor"), product=matches[0].get("product")
        )
    except CliError as exc:
        return {"error": exc.message}

    try:
        with Xvf3800(fd) as dev:
            return dev.firmware_info()
    except CliError as exc:
        return {"error": exc.message}


def build_report(selector: str, root: str) -> dict[str, object]:
    """Resolve ``selector`` and report its capture formats, rates, and firmware."""
    device = resolve(selector, root=root)
    formats, rates, channels = _formats_rates_channels(root, device)
    return {
        "device": device.as_dict(),
        "formats": formats,
        "rates": rates,
        "channels": channels,
        "firmware": _firmware_payload(root, device),
    }


def _render_text(report: dict[str, object]) -> str:
    device = report["device"]
    lines = [f"{device['stable_id']} ({device['label']}) {device['alsa_address']}"]
    lines.append(f"  channels: {report['channels']}")
    lines.append(f"  formats: {', '.join(report['formats']) or 'unknown'}")
    lines.append(f"  rates: {', '.join(str(r) for r in report['rates']) or 'unknown'}")
    firmware = report["firmware"]
    if firmware is None:
        lines.append("  firmware: n/a (not an XVF3800 array)")
    elif "error" in firmware:
        lines.append(f"  firmware: unavailable ({firmware['error']})")
    else:
        lines.append(
            f"  firmware: version={firmware['version']} build={firmware['build']} "
            f"host={firmware['host']} repo_hash={firmware['repo_hash']}"
        )
    return "\n".join(lines)


def cmd_inspect(args: argparse.Namespace) -> int:
    root = getattr(args, "root", None) or "/"
    report = build_report(args.device, root)
    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result(report, json_mode=True)
    else:
        emit_result(_render_text(report), json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "inspect",
        help="Inspect one microphone's capture formats, rates, channels, and firmware.",
    )
    p.add_argument("device", metavar="<device>", help="Selector: stable id, serial, or label.")
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.add_argument(
        "--root",
        default="/",
        metavar="PATH",
        help=(
            "Filesystem root to resolve the device under (default: /); "
            "mainly for pointing at a synthetic device tree in tests."
        ),
    )
    p.set_defaults(func=cmd_inspect)
