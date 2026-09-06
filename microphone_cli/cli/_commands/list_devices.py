"""``microphone list`` — attached microphones, identity, and access status.

Composes the two wave-1 modules that already own this domain rather than
re-deriving anything: :mod:`microphone_cli.devices` for *what is attached and
how to name it* (identity is keyed on the USB descriptors, not the plug-order
ALSA card index — see that module's docstring for why), and
:mod:`microphone_cli.access` for *can the capture node be opened right now*.
This module adds nothing to either: it enumerates, probes access on the one
ALSA capture node each device exposes, and renders.

Cited from ``../webcam-cli/webcam_cli/cli/_commands/list_devices.py`` (the
pattern of composing ``enumerate_devices`` with a per-node ``check_access``
probe, and never hard-failing on a bad device) with the video/audio pairing
dropped — this project has exactly one node per device, the ALSA capture PCM.

``list`` never fails because one device is unhappy. :func:`~microphone_cli.
access.check_access` *reports* rather than raises, and this module never calls
:func:`microphone_cli.access.access_error` — a forbidden, absent, or busy
device is shown as such, with its remediation carried through, while ``list``
itself still exits 0.

No hardware is activated beyond the single non-blocking permission probe
:func:`~microphone_cli.access.check_access` already performs per node (an
``O_NONBLOCK`` ``open()``/``close()`` pair, not a capture).
"""

from __future__ import annotations

import argparse
import os
import re

from microphone_cli.access import AccessReport, check_access
from microphone_cli.cli._output import emit_result
from microphone_cli.devices import MicrophoneDevice, enumerate_devices

# A capture PCM directory under /proc/asound/cardN: "pcm0c" (playback is "pcm0p").
_CAPTURE_PCM_RE = re.compile(r"^pcm(?P<device>\d+)c$")


def _capture_pcm_device(root: str, index: int) -> int:
    """Lowest capture PCM device number of an ALSA card; ``0`` if none is listed.

    Every device :func:`~microphone_cli.devices.enumerate_devices` returns has
    at least one capture PCM (that is how it qualified as a microphone in the
    first place); the ``0`` fallback only matters for a fixture tree that
    doesn't bother recreating the ``pcm0c`` directory.
    """
    card_dir = os.path.join(root or "/", "proc", "asound", f"card{index}")
    try:
        entries = os.listdir(card_dir)
    except OSError:
        return 0
    numbers = [
        int(matched.group("device"))
        for matched in (_CAPTURE_PCM_RE.match(entry) for entry in entries)
        if matched is not None
    ]
    return min(numbers) if numbers else 0


def _audio_node_path(root: str, device: MicrophoneDevice) -> str:
    """The ALSA capture PCM device node for ``device``, e.g. ``/dev/snd/pcmC1D0c``."""
    pcm_device = _capture_pcm_device(root, device.card_index)
    return f"/dev/snd/pcmC{device.card_index}D{pcm_device}c"


def _access_payload(report: AccessReport) -> dict[str, object]:
    return {
        "state": report.state.value,
        "path": report.path,
        "remediation": report.remediation,
    }


def _device_payload(root: str, device: MicrophoneDevice) -> dict[str, object]:
    """``device.as_dict()`` plus one access probe on its ALSA capture node."""
    payload = device.as_dict()
    payload["audio_access"] = _access_payload(check_access(_audio_node_path(root, device), "audio"))
    return payload


def build_report(root: str) -> dict[str, object]:
    """Enumerate every microphone under ``root`` with its access status.

    Pure composition of :func:`microphone_cli.devices.enumerate_devices`
    (identity) and :func:`microphone_cli.access.check_access` (openability).
    Never raises: a device whose node is absent, forbidden, or busy is still
    listed, with that state and its remediation attached.
    """
    devices = enumerate_devices(root=root)
    return {
        "devices": [_device_payload(root, device) for device in devices],
        "count": len(devices),
    }


def _render_text(report: dict[str, object]) -> str:
    devices = report["devices"]
    if not devices:
        return "no microphones found"

    lines: list[str] = [f"{report['count']} microphone(s)"]
    for payload in devices:
        marker = " [array]" if payload["is_array"] else ""
        access = payload["audio_access"]
        lines.append(
            f"{payload['stable_id']} ({payload['label']}) {payload['alsa_address']}{marker}"
        )
        lines.append(f"  audio access: {access['state']}")
        if access["remediation"]:
            lines.append(f"    hint: {access['remediation']}")
    return "\n".join(lines)


def cmd_list(args: argparse.Namespace) -> int:
    root = getattr(args, "root", None) or "/"
    report = build_report(root)
    json_mode = bool(getattr(args, "json", False))
    if json_mode:
        emit_result(report, json_mode=True)
    else:
        emit_result(_render_text(report), json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "list",
        help="List attached microphones: stable id, ALSA address, and access status.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.add_argument(
        "--root",
        default="/",
        metavar="PATH",
        help=(
            "Filesystem root to enumerate devices under (default: /); "
            "mainly for pointing at a synthetic device tree in tests."
        ),
    )
    p.set_defaults(func=cmd_list)
