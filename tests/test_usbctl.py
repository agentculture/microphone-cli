"""Tests for the stdlib usbdevfs control-transfer layer.

No test in this file may open a real ``/dev/bus/usb`` node or issue a real
USB transfer: the ioctl is injected through ``usbctl._ioctl``.
"""

from __future__ import annotations

import ctypes
import os
import struct

import pytest

from microphone_cli import usbctl
from microphone_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError


class FakeIoctl:
    """Records every ioctl call and optionally fills the IN data buffer."""

    def __init__(self, replies: list[bytes] | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.replies = list(replies or [])

    def __call__(self, fd: int, request: int, arg: object) -> int:
        xfer = arg
        assert isinstance(xfer, usbctl.UsbdevfsCtrlTransfer)
        payload = b""
        if xfer.wLength and xfer.data:
            payload = ctypes.string_at(xfer.data, xfer.wLength)
        self.calls.append(
            {
                "fd": fd,
                "request": request,
                "bRequestType": xfer.bRequestType,
                "bRequest": xfer.bRequest,
                "wValue": xfer.wValue,
                "wIndex": xfer.wIndex,
                "wLength": xfer.wLength,
                "timeout": xfer.timeout,
                "payload": payload,
            }
        )
        if self.replies and xfer.bRequestType & 0x80:
            reply = self.replies.pop(0)
            ctypes.memmove(xfer.data, reply, min(len(reply), xfer.wLength))
            return min(len(reply), xfer.wLength)
        return xfer.wLength


@pytest.fixture()
def fake_ioctl(monkeypatch: pytest.MonkeyPatch) -> FakeIoctl:
    fake = FakeIoctl()
    monkeypatch.setattr(usbctl, "_ioctl", fake)
    return fake


def test_struct_layout_matches_kernel() -> None:
    # struct usbdevfs_ctrltransfer: u8 u8 u16 u16 u16 u32 void*
    assert ctypes.sizeof(usbctl.UsbdevfsCtrlTransfer) == struct.calcsize("BBHHHIP")


def test_usbdevfs_control_constant() -> None:
    if ctypes.sizeof(ctypes.c_void_p) != 8:  # pragma: no cover - 32-bit hosts
        pytest.skip("constant asserted for 64-bit layout only")
    assert usbctl.USBDEVFS_CONTROL == 0xC0185500


def test_iowr_is_derived_not_hardcoded() -> None:
    assert usbctl._iowr(ord("U"), 0, ctypes.sizeof(usbctl.UsbdevfsCtrlTransfer)) == (
        usbctl.USBDEVFS_CONTROL
    )


def test_control_transfer_in_returns_buffer(fake_ioctl: FakeIoctl) -> None:
    fake_ioctl.replies.append(b"\x00\x01\x02\x03")
    got = usbctl.control_transfer(7, 0xC0, 0, 0x93, 20, 4, timeout_ms=1234)
    assert got == b"\x00\x01\x02\x03"
    call = fake_ioctl.calls[0]
    assert call["fd"] == 7
    assert call["request"] == usbctl.USBDEVFS_CONTROL
    assert call["bRequestType"] == 0xC0
    assert call["wValue"] == 0x93
    assert call["wIndex"] == 20
    assert call["wLength"] == 4
    assert call["timeout"] == 1234


def test_control_transfer_out_sends_payload(fake_ioctl: FakeIoctl) -> None:
    n = usbctl.control_transfer(7, 0x40, 0, 0, 35, b"\x00\x00\x00\x3f")
    assert n == 4
    call = fake_ioctl.calls[0]
    assert call["bRequestType"] == 0x40
    assert call["payload"] == b"\x00\x00\x00\x3f"


def test_control_transfer_oserror_becomes_clierror(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object) -> int:
        raise OSError(16, "Device or resource busy")

    monkeypatch.setattr(usbctl, "_ioctl", boom)
    with pytest.raises(CliError) as exc:
        usbctl.control_transfer(7, 0xC0, 0, 0, 20, 4)
    assert exc.value.code == EXIT_ENV_ERROR
    assert exc.value.remediation


def _make_sysfs(tmp_path, name: str, attrs: dict[str, str]) -> None:
    devdir = tmp_path / "sys" / "bus" / "usb" / "devices" / name
    devdir.mkdir(parents=True)
    for key, value in attrs.items():
        (devdir / key).write_text(value + "\n")


def test_find_devices_builds_node_path(tmp_path) -> None:
    _make_sysfs(
        tmp_path,
        "1-3",
        {
            "idVendor": "38fb",
            "idProduct": "1001",
            "serial": "ABC123",
            "busnum": "1",
            "devnum": "7",
        },
    )
    found = usbctl.find_devices(root=str(tmp_path))
    assert len(found) == 1
    assert found[0]["node"] == "/dev/bus/usb/001/007"
    assert found[0]["vendor"] == "38fb"
    assert found[0]["product"] == "1001"
    assert found[0]["serial"] == "ABC123"


def test_find_devices_filters(tmp_path) -> None:
    _make_sysfs(
        tmp_path,
        "1-3",
        {"idVendor": "38fb", "idProduct": "1001", "serial": "A", "busnum": "1", "devnum": "7"},
    )
    _make_sysfs(
        tmp_path,
        "2-1",
        {"idVendor": "2886", "idProduct": "001a", "serial": "B", "busnum": "2", "devnum": "3"},
    )
    assert len(usbctl.find_devices(root=str(tmp_path))) == 2
    assert [d["serial"] for d in usbctl.find_devices(root=str(tmp_path), vendor="2886")] == ["B"]
    assert [d["node"] for d in usbctl.find_devices(root=str(tmp_path), product="1001")] == [
        "/dev/bus/usb/001/007"
    ]
    assert usbctl.find_devices(root=str(tmp_path), serial="nope") == []


def test_find_devices_skips_interfaces_and_missing_attrs(tmp_path) -> None:
    _make_sysfs(tmp_path, "1-3:1.0", {"idVendor": "38fb"})
    _make_sysfs(tmp_path, "usb1", {"busnum": "1", "devnum": "1"})
    assert usbctl.find_devices(root=str(tmp_path)) == []


def test_find_devices_missing_root_is_empty(tmp_path) -> None:
    assert usbctl.find_devices(root=str(tmp_path / "absent")) == []


def test_open_device_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*_args: object, **_kw: object) -> int:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(usbctl.os, "open", denied)
    with pytest.raises(CliError) as exc:
        usbctl.open_device("/dev/bus/usb/001/007", vendor="2886", product="001A")
    assert exc.value.code == EXIT_ENV_ERROR
    assert 'SUBSYSTEM=="usb"' in exc.value.remediation
    # The hint names the ids of the device that was actually refused (found on
    # hardware: a ReSpeaker 2886:001a was told to add a rule for 38fb:1001).
    assert 'ATTR{idVendor}=="2886"' in exc.value.remediation
    assert 'ATTR{idProduct}=="001a"' in exc.value.remediation
    assert 'MODE="0666"' in exc.value.remediation


def test_open_device_permission_error_without_ids_uses_placeholders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(*_args: object, **_kw: object) -> int:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(usbctl.os, "open", denied)
    with pytest.raises(CliError) as exc:
        usbctl.open_device("/dev/bus/usb/001/007")
    assert 'ATTR{idVendor}=="XXXX"' in exc.value.remediation
    assert "38fb" not in exc.value.remediation


def test_open_device_missing_node(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*_args: object, **_kw: object) -> int:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(usbctl.os, "open", missing)
    with pytest.raises(CliError) as exc:
        usbctl.open_device("/dev/bus/usb/001/007")
    assert exc.value.code == EXIT_USER_ERROR


def test_open_device_uses_rdwr(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_open(path: str, flags: int) -> int:
        seen["path"] = path
        seen["flags"] = flags
        return 42

    monkeypatch.setattr(usbctl.os, "open", fake_open)
    assert usbctl.open_device("/dev/bus/usb/001/007") == 42
    assert seen["path"] == "/dev/bus/usb/001/007"
    assert seen["flags"] == os.O_RDWR
