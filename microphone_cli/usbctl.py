"""Stdlib USB control transfers over ``usbdevfs`` — no pyusb, no libusb.

The kernel exposes every USB device as a character node at
``/dev/bus/usb/BBB/DDD``. Opening that node ``O_RDWR`` and issuing the
``USBDEVFS_CONTROL`` ioctl performs a control transfer, which is all the
XVF3800 vendor protocol needs. Doing it this way keeps microphone-cli at
zero runtime dependencies (see CLAUDE.md).

Layout mirrored from ``/usr/include/linux/usbdevice_fs.h``::

    struct usbdevfs_ctrltransfer {
        __u8  bRequestType;
        __u8  bRequest;
        __u16 wValue;
        __u16 wIndex;
        __u16 wLength;
        __u32 timeout;   /* in milliseconds */
        void *data;
    };

``USBDEVFS_CONTROL`` is ``_IOWR('U', 0, struct usbdevfs_ctrltransfer)``; the
constant is *derived* from the struct size here rather than hard-coded, so it
stays correct on non-64-bit layouts.

Testing seam: :data:`_ioctl` is a module-level callable defaulting to
``fcntl.ioctl``. Tests replace it, so no test ever touches ``/dev``.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
from typing import Any, Callable

from .cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError

__all__ = [
    "UsbdevfsCtrlTransfer",
    "USBDEVFS_CONTROL",
    "DEFAULT_TIMEOUT_MS",
    "control_transfer",
    "find_devices",
    "open_device",
]

# ``_IOC`` bit layout (asm-generic): dir<<30 | size<<16 | type<<8 | nr.
_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_WRITE = 1
_IOC_READ = 2

#: Control transfers on this device tolerate a long firmware turnaround; the
#: vendored reachy_mini script uses 100 s and so do we.
DEFAULT_TIMEOUT_MS = 100000


def udev_rule(vendor: str | None, product: str | None) -> str:
    """Return the udev rule line that grants non-root access to one USB id.

    Names the ids of the device that was actually refused; falls back to
    ``XXXX``/``YYYY`` placeholders when they are unknown so the hint never
    points at the wrong board.
    """
    vid = vendor.lower() if vendor else "XXXX"
    pid = product.lower() if product else "YYYY"
    return f'SUBSYSTEM=="usb", ATTR{{idVendor}}=="{vid}", ATTR{{idProduct}}=="{pid}", MODE="0666"'


def _ioc(direction: int, type_: int, nr: int, size: int) -> int:
    return (
        (direction << _IOC_DIRSHIFT)
        | (size << _IOC_SIZESHIFT)
        | (type_ << _IOC_TYPESHIFT)
        | (nr << _IOC_NRSHIFT)
    )


def _iowr(type_: int, nr: int, size: int) -> int:
    """``_IOWR(type, nr, size)`` from ``<asm-generic/ioctl.h>``."""
    return _ioc(_IOC_READ | _IOC_WRITE, type_, nr, size)


class UsbdevfsCtrlTransfer(ctypes.Structure):
    """``struct usbdevfs_ctrltransfer`` (natural alignment, as the kernel uses)."""

    _fields_ = [
        ("bRequestType", ctypes.c_uint8),
        ("bRequest", ctypes.c_uint8),
        ("wValue", ctypes.c_uint16),
        ("wIndex", ctypes.c_uint16),
        ("wLength", ctypes.c_uint16),
        ("timeout", ctypes.c_uint32),
        ("data", ctypes.c_void_p),
    ]


#: ``_IOWR('U', 0, struct usbdevfs_ctrltransfer)`` — 0xC0185500 on 64-bit.
USBDEVFS_CONTROL = _iowr(ord("U"), 0, ctypes.sizeof(UsbdevfsCtrlTransfer))

#: Injected ioctl. Tests replace this; production uses ``fcntl.ioctl``.
_ioctl: Callable[..., Any] = fcntl.ioctl


def control_transfer(
    fd: int,
    request_type: int,
    request: int,
    value: int,
    index: int,
    data_or_length: int | bytes | bytearray,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> bytes | int:
    """Issue one USB control transfer on an open usbdevfs ``fd``.

    ``data_or_length`` is an ``int`` for an IN (device-to-host) transfer — the
    number of bytes to request, and the call returns the bytes read — or a
    bytes-like payload for an OUT transfer, where the call returns the number
    of bytes the kernel reports as transferred.
    """
    if isinstance(data_or_length, int):
        length = data_or_length
        buf = ctypes.create_string_buffer(length)
    else:
        payload = bytes(data_or_length)
        length = len(payload)
        buf = (
            ctypes.create_string_buffer(payload, length)
            if length
            else ctypes.create_string_buffer(1)
        )

    xfer = UsbdevfsCtrlTransfer(
        bRequestType=request_type & 0xFF,
        bRequest=request & 0xFF,
        wValue=value & 0xFFFF,
        wIndex=index & 0xFFFF,
        wLength=length & 0xFFFF,
        timeout=timeout_ms,
        data=ctypes.cast(buf, ctypes.c_void_p),
    )

    try:
        ret = _ioctl(fd, USBDEVFS_CONTROL, xfer)
    except OSError as exc:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"USB control transfer failed: {exc}",
            remediation=(
                "Confirm the device is still attached (`microphone` device list) and that no "
                "other process holds it; replug the device if the error persists."
            ),
        ) from exc

    count = ret if isinstance(ret, int) and 0 <= ret <= length else length
    if isinstance(data_or_length, int):
        return bytes(buf.raw[:count])
    return count


def _read_attr(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return None


def _read_device_attrs(devdir: str) -> dict[str, str] | None:
    """Read one sysfs device directory's identity attrs, or ``None`` if it is
    not a device directory (an interface directory such as ``1-3:1.0`` lacks
    ``idVendor``/``idProduct``/``busnum``/``devnum``)."""
    vid = _read_attr(os.path.join(devdir, "idVendor"))
    pid = _read_attr(os.path.join(devdir, "idProduct"))
    busnum = _read_attr(os.path.join(devdir, "busnum"))
    devnum = _read_attr(os.path.join(devdir, "devnum"))
    if not (vid and pid and busnum and devnum):
        return None
    return {
        "vendor": vid.lower(),
        "product": pid.lower(),
        "serial": _read_attr(os.path.join(devdir, "serial")) or "",
        "busnum": busnum,
        "devnum": devnum,
    }


def _matches_filters(
    attrs: dict[str, str],
    vendor: str | None,
    product: str | None,
    serial: str | None,
) -> bool:
    if vendor is not None and attrs["vendor"] != vendor.lower():
        return False
    if product is not None and attrs["product"] != product.lower():
        return False
    if serial is not None and attrs["serial"] != serial:
        return False
    return True


def find_devices(
    root: str = "/",
    vendor: str | None = None,
    product: str | None = None,
    serial: str | None = None,
) -> list[dict[str, str]]:
    """Enumerate USB devices from sysfs, newest-style attrs only.

    Walks ``<root>/sys/bus/usb/devices/*/`` and keeps entries that expose
    ``idVendor``, ``idProduct``, ``busnum`` and ``devnum`` (interface
    directories such as ``1-3:1.0`` do not, and are skipped). Filters compare
    case-insensitively. Returns dicts with ``node`` (the
    ``/dev/bus/usb/BBB/DDD`` path), ``vendor``, ``product``, ``serial``,
    ``busnum``, ``devnum`` and ``sysfs``.
    """
    base = os.path.join(root, "sys", "bus", "usb", "devices")
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []

    out: list[dict[str, str]] = []
    for name in names:
        devdir = os.path.join(base, name)
        attrs = _read_device_attrs(devdir)
        if attrs is None or not _matches_filters(attrs, vendor, product, serial):
            continue
        try:
            node = "/dev/bus/usb/{:03d}/{:03d}".format(int(attrs["busnum"]), int(attrs["devnum"]))
        except ValueError:
            continue
        out.append({**attrs, "node": node, "sysfs": devdir})
    return out


def open_device(node: str, *, vendor: str | None = None, product: str | None = None) -> int:
    """Open a ``/dev/bus/usb/BBB/DDD`` node ``O_RDWR`` and return the fd.

    ``vendor``/``product`` are only used to make the permission-denied
    remediation name the refused device's own USB ids.
    """
    try:
        return os.open(node, os.O_RDWR)
    except PermissionError as exc:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"permission denied opening {node}",
            remediation=(
                "Grant your user access to the device with a udev rule, then replug it:\n"
                f"  echo '{udev_rule(vendor, product)}' | sudo tee "
                "/etc/udev/rules.d/99-microphone-cli.rules\n"
                "  sudo udevadm control --reload-rules && sudo udevadm trigger"
            ),
        ) from exc
    except FileNotFoundError as exc:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"no such USB device node: {node}",
            remediation="Run `microphone list` to see the devices that exist right now.",
        ) from exc
    except OSError as exc:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"cannot open {node}: {exc}",
            remediation="Check that the device is attached and not claimed by another process.",
        ) from exc
