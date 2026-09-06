"""Tests for ``microphone inspect`` (:mod:`microphone_cli.cli._commands.inspect`).

Firmware reads never touch a real ``/dev/bus/usb`` node: ``usbctl.find_devices``
and ``usbctl.open_device`` are monkeypatched to hand back a fake fd, and
``usbctl._ioctl`` (the same seam :mod:`tests.test_xvf3800` uses) is replaced
with a fake that serves status-0 replies for VERSION/BLD_MSG/BLD_HOST/
BLD_REPO_HASH. No test in this file opens a real device.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil

from microphone_cli import usbctl, xvf3800
from microphone_cli.cli._commands import inspect as inspect_cmd
from microphone_cli.cli._errors import EXIT_ENV_ERROR, CliError
from microphone_cli.devices import enumerate_devices

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def tree(name: str) -> str:
    return os.path.join(FIXTURES, name)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    inspect_cmd.register(sub)
    return parser


class FakeIoctl:
    """Serves queued IN replies in order; records nothing else needed here."""

    def __init__(self, replies: list[bytes]) -> None:
        self.replies = list(replies)
        self.calls: list[int] = []

    def __call__(self, fd: int, request: int, arg: object) -> int:
        xfer = arg
        self.calls.append(fd)
        if xfer.bRequestType & 0x80:
            reply = self.replies.pop(0)
            ctypes.memmove(xfer.data, reply, min(len(reply), xfer.wLength))
        return xfer.wLength


def _firmware_replies() -> list[bytes]:
    return [
        b"\x00\x01\x02\x03",
        b"\x00" + b"build-msg".ljust(50, b"\x00"),
        b"\x00" + b"buildhost".ljust(30, b"\x00"),
        b"\x00" + b"deadbeef".ljust(40, b"\x00"),
    ]


def _patch_firmware_transport(monkeypatch, node: str = "/dev/bus/usb/005/007") -> FakeIoctl:
    """Install a fully fake USB transport: find -> open -> ioctl replies."""

    def fake_find_devices(root, vendor=None, product=None, serial=None):
        return [{"node": node, "vendor": vendor, "product": product, "serial": serial}]

    monkeypatch.setattr(usbctl, "find_devices", fake_find_devices)
    monkeypatch.setattr(usbctl, "open_device", lambda path, **kw: 42)
    monkeypatch.setattr(xvf3800.os, "close", lambda fd: None)
    fake = FakeIoctl(_firmware_replies())
    monkeypatch.setattr(usbctl, "_ioctl", fake)
    return fake


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------


def test_register_exposes_inspect_with_positional_device_json_and_root() -> None:
    parser = _parser()
    args = parser.parse_args(
        ["inspect", "usb-something", "--json", "--root", tree("host-baseline")]
    )
    assert args.func is inspect_cmd.cmd_inspect
    assert args.device == "usb-something"
    assert args.json is True
    assert args.root == tree("host-baseline")


def test_inspect_default_root_is_slash() -> None:
    parser = _parser()
    args = parser.parse_args(["inspect", "usb-something"])
    assert args.root == "/"
    assert args.json is False


# ---------------------------------------------------------------------------
# array fixture: formats/rates/channels + firmware
# ---------------------------------------------------------------------------


def test_inspect_array_reports_formats_rates_channels_and_firmware(monkeypatch, capsys) -> None:
    root = tree("host-baseline")
    (device,) = enumerate_devices(root=root)
    assert device.is_array is True

    _patch_firmware_transport(monkeypatch)

    parser = _parser()
    args = parser.parse_args(["inspect", device.stable_id, "--json", "--root", root])
    rc = args.func(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["formats"] == ["S32_LE"]
    assert payload["rates"] == [48000]
    assert payload["channels"] == 6
    assert payload["firmware"] == {
        "version": "1.2.3",
        "build": "build-msg",
        "host": "buildhost",
        "repo_hash": "deadbeef",
    }
    assert payload["device"]["stable_id"] == device.stable_id


def test_inspect_build_report_matches_cli_json(monkeypatch) -> None:
    root = tree("host-baseline")
    (device,) = enumerate_devices(root=root)
    _patch_firmware_transport(monkeypatch)
    report = inspect_cmd.build_report(device.stable_id, root)
    assert set(report) == {"device", "formats", "rates", "channels", "firmware"}


def test_inspect_text_mode_includes_firmware(monkeypatch, capsys) -> None:
    root = tree("host-baseline")
    (device,) = enumerate_devices(root=root)
    _patch_firmware_transport(monkeypatch)

    parser = _parser()
    args = parser.parse_args(["inspect", device.stable_id, "--root", root])
    rc = args.func(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "channels: 6" in out
    assert "S32_LE" in out
    assert "version=1.2.3" in out


# ---------------------------------------------------------------------------
# non-array: firmware is null
# ---------------------------------------------------------------------------


def test_inspect_non_array_firmware_is_null(tmp_path, capsys) -> None:
    dest = tmp_path / "nonarray"
    shutil.copytree(tree("host-baseline"), dest, symlinks=True)
    sysfs_dev = dest / "sys/devices/platform/NVDA8000:02/usb5/5-1/5-1.1"
    (sysfs_dev / "idVendor").write_text("046d\n")
    (sysfs_dev / "idProduct").write_text("0825\n")

    root = str(dest)
    (device,) = enumerate_devices(root=root)
    assert device.is_array is False

    parser = _parser()
    args = parser.parse_args(["inspect", device.stable_id, "--json", "--root", root])
    rc = args.func(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["firmware"] is None
    # stream0 still describes the same capture interface either way.
    assert payload["channels"] == 6
    assert payload["formats"] == ["S32_LE"]
    assert payload["rates"] == [48000]


# ---------------------------------------------------------------------------
# missing stream0
# ---------------------------------------------------------------------------


def test_inspect_missing_stream0_falls_back_to_device_channels(capsys) -> None:
    root = tree("respeaker")
    (device,) = enumerate_devices(root=root)
    assert device.channels is None  # no stream0 in this fixture (see test_devices.py)

    parser = _parser()
    args = parser.parse_args(["inspect", device.stable_id, "--json", "--root", root])
    rc = args.func(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["formats"] == []
    assert payload["rates"] == []
    assert payload["channels"] is None


# ---------------------------------------------------------------------------
# firmware descriptive failures (never hard-fail)
# ---------------------------------------------------------------------------


def test_inspect_array_no_usb_node_found_reports_error_but_succeeds(monkeypatch, capsys) -> None:
    root = tree("host-baseline")
    (device,) = enumerate_devices(root=root)

    monkeypatch.setattr(
        usbctl, "find_devices", lambda root, vendor=None, product=None, serial=None: []
    )

    parser = _parser()
    args = parser.parse_args(["inspect", device.stable_id, "--json", "--root", root])
    rc = args.func(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "error" in payload["firmware"]
    assert device.stable_id in payload["firmware"]["error"]


def test_inspect_array_open_permission_error_reports_but_succeeds(monkeypatch, capsys) -> None:
    root = tree("host-baseline")
    (device,) = enumerate_devices(root=root)

    monkeypatch.setattr(
        usbctl,
        "find_devices",
        lambda root, vendor=None, product=None, serial=None: [{"node": "/dev/bus/usb/005/007"}],
    )

    def deny(node: str, **_kw: object) -> int:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"permission denied opening {node}",
            remediation="add a udev rule",
        )

    monkeypatch.setattr(usbctl, "open_device", deny)

    parser = _parser()
    args = parser.parse_args(["inspect", device.stable_id, "--json", "--root", root])
    rc = args.func(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["firmware"] == {"error": "permission denied opening /dev/bus/usb/005/007"}
