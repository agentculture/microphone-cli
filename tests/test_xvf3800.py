"""Tests for the XVF3800 parameter protocol layer.

Every transfer is served by a fake ioctl injected into
:mod:`microphone_cli.usbctl`; no real device is ever opened.
"""

from __future__ import annotations

import ctypes
import struct

import pytest

from microphone_cli import usbctl, xvf3800
from microphone_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError


class FakeIoctl:
    """Records calls; serves queued IN replies (status byte first)."""

    def __init__(self, replies: list[bytes] | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.replies = list(replies or [])
        self.default_reply: bytes | None = None

    def __call__(self, fd: int, request: int, arg: object) -> int:
        xfer = arg
        payload = b""
        if xfer.wLength and xfer.data:
            payload = ctypes.string_at(xfer.data, xfer.wLength)
        self.calls.append(
            {
                "bRequestType": xfer.bRequestType,
                "bRequest": xfer.bRequest,
                "wValue": xfer.wValue,
                "wIndex": xfer.wIndex,
                "wLength": xfer.wLength,
                "payload": payload,
            }
        )
        if xfer.bRequestType & 0x80:
            if self.replies:
                reply = self.replies.pop(0)
            elif self.default_reply is not None:
                reply = self.default_reply
            else:  # pragma: no cover - defensive
                raise AssertionError("unexpected IN transfer")
            ctypes.memmove(xfer.data, reply, min(len(reply), xfer.wLength))
        return xfer.wLength


@pytest.fixture()
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(xvf3800, "_sleep", slept.append)
    return slept


def install(
    monkeypatch: pytest.MonkeyPatch, replies: list[bytes] | None = None
) -> tuple[xvf3800.Xvf3800, FakeIoctl]:
    fake = FakeIoctl(replies)
    monkeypatch.setattr(usbctl, "_ioctl", fake)
    return xvf3800.Xvf3800(3), fake


# --- table provenance -------------------------------------------------------


def test_parameters_table_is_vendored_verbatim() -> None:
    assert xvf3800.PARAMETERS["DOA_VALUE_RADIANS"] == (20, 19, 2, "ro", "radians")
    assert xvf3800.PARAMETERS["AUDIO_MGR_MIC_GAIN"] == (35, 0, 1, "rw", "float")
    assert xvf3800.PARAMETERS["VERSION"] == (48, 0, 3, "ro", "uint8")
    assert xvf3800.PARAMETERS["REBOOT"] == (48, 7, 1, "wo", "uint8")
    assert len(xvf3800.PARAMETERS) == 126


def test_module_cites_source_and_license() -> None:
    doc = xvf3800.__doc__ or ""
    assert "reachy_mini/media/audio_control_utils.py" in doc
    assert "Apache-2.0" in doc


# --- read -------------------------------------------------------------------


def test_read_doa_radians_wire_format(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = b"\x00" + struct.pack("<ff", 1.5, -0.25)
    dev, fake = install(monkeypatch, [reply])
    assert dev.read("DOA_VALUE_RADIANS") == pytest.approx([1.5, -0.25])
    call = fake.calls[0]
    assert call["bRequestType"] == 0xC0
    assert call["bRequest"] == 0
    assert call["wValue"] == 0x80 | 19
    assert call["wIndex"] == 20
    assert call["wLength"] == 9


def test_read_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    dev, _ = install(monkeypatch, [b"\x00" + struct.pack("<ff", 0.0, 0.0)])
    assert dev.read("doa_value_radians") == pytest.approx([0.0, 0.0])


def test_read_retries_on_status_64_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    busy = b"\x40" + b"\x00" * 8
    good = b"\x00" + struct.pack("<ff", 2.0, 3.0)
    dev, fake = install(monkeypatch, [busy, busy, good])
    assert dev.read("DOA_VALUE_RADIANS") == pytest.approx([2.0, 3.0])
    assert len(fake.calls) == 3
    assert no_sleep == [0.01, 0.01]


def test_read_busy_forever_raises_env_error(
    monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    dev, fake = install(monkeypatch)
    fake.default_reply = b"\x40" + b"\x00" * 8
    with pytest.raises(CliError) as exc:
        dev.read("DOA_VALUE_RADIANS")
    assert exc.value.code == EXIT_ENV_ERROR
    assert "reachy-mini-daemon" in exc.value.remediation
    assert len(fake.calls) == 100


def test_read_unknown_status_raises_env_error(monkeypatch: pytest.MonkeyPatch) -> None:
    dev, _ = install(monkeypatch, [b"\x07" + b"\x00" * 8])
    with pytest.raises(CliError) as exc:
        dev.read("DOA_VALUE_RADIANS")
    assert exc.value.code == EXIT_ENV_ERROR
    assert "7" in exc.value.message


def test_read_uint8_returns_ints(monkeypatch: pytest.MonkeyPatch) -> None:
    dev, fake = install(monkeypatch, [b"\x00\x01\x02\x03"])
    assert dev.read("VERSION") == [1, 2, 3]
    assert fake.calls[0]["wLength"] == 4


def test_read_char_strips_nulls(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = b"\x00" + b"hello".ljust(50, b"\x00")
    dev, _ = install(monkeypatch, [reply])
    assert dev.read("BLD_MSG") == "hello"


def test_read_int32_and_uint32(monkeypatch: pytest.MonkeyPatch) -> None:
    dev, _ = install(monkeypatch, [b"\x00" + struct.pack("<i", -5)])
    assert dev.read("AEC_NUM_MICS") == [-5]
    dev2, _ = install(monkeypatch, [b"\x00" + struct.pack("<II", 7, 9)])
    assert dev2.read("DOA_VALUE") == [7, 9]


def test_read_uint8_short_reply_raises_env_error() -> None:
    from microphone_cli.xvf3800 import Xvf3800

    def transfer(request_type, request, value, index, data_or_length):
        # VERSION wants 3 uint8 values + status byte; only send status + 1 byte.
        return b"\x00\x01"

    chip = Xvf3800(transfer)
    with pytest.raises(CliError) as exc:
        chip.read("VERSION")
    assert exc.value.code == EXIT_ENV_ERROR
    assert "short reply reading VERSION" in exc.value.message
    assert "1 of 3 bytes" in exc.value.message


def test_read_write_only_is_user_error(monkeypatch: pytest.MonkeyPatch) -> None:
    dev, fake = install(monkeypatch)
    with pytest.raises(CliError) as exc:
        dev.read("REBOOT")
    assert exc.value.code == EXIT_USER_ERROR
    assert fake.calls == []


def test_read_unknown_name_is_user_error(monkeypatch: pytest.MonkeyPatch) -> None:
    dev, fake = install(monkeypatch)
    with pytest.raises(CliError) as exc:
        dev.read("NOT_A_PARAM")
    assert exc.value.code == EXIT_USER_ERROR
    assert fake.calls == []


# --- write ------------------------------------------------------------------


def test_write_float_wire_format(monkeypatch: pytest.MonkeyPatch) -> None:
    dev, fake = install(monkeypatch)
    dev.write("AUDIO_MGR_MIC_GAIN", [0.5])
    call = fake.calls[0]
    assert call["bRequestType"] == 0x40
    assert call["bRequest"] == 0
    assert call["wValue"] == 0
    assert call["wIndex"] == 35
    assert call["payload"] == struct.pack("<f", 0.5)


def test_write_uint8_and_int32(monkeypatch: pytest.MonkeyPatch) -> None:
    dev, fake = install(monkeypatch)
    dev.write("AUDIO_MGR_OP_L", [3, 0])
    assert fake.calls[0]["payload"] == b"\x03\x00"
    dev.write("AEC_FILTER_CMD_ABORT", [1])
    assert fake.calls[1]["payload"] == struct.pack("<i", 1)
    dev.write("AEC_RESET_MIN_IDLE_TIME", [2])
    assert fake.calls[2]["payload"] == struct.pack("<I", 2)


def test_write_uint8_out_of_range_is_user_error_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dev, fake = install(monkeypatch)
    with pytest.raises(CliError) as exc:
        dev.write("LED_BRIGHTNESS", [256])
    assert exc.value.code == EXIT_USER_ERROR
    assert "256" in exc.value.message
    assert fake.calls == []

    dev2, fake2 = install(monkeypatch)
    with pytest.raises(CliError) as exc2:
        dev2.write("LED_BRIGHTNESS", [-1])
    assert exc2.value.code == EXIT_USER_ERROR
    assert "-1" in exc2.value.message
    assert fake2.calls == []


def test_write_read_only_is_user_error_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dev, fake = install(monkeypatch)
    with pytest.raises(CliError) as exc:
        dev.write("AEC_NUM_MICS", [1])
    assert exc.value.code == EXIT_USER_ERROR
    assert "read-only" in exc.value.message
    assert fake.calls == []


def test_write_wrong_count_is_user_error(monkeypatch: pytest.MonkeyPatch) -> None:
    dev, fake = install(monkeypatch)
    with pytest.raises(CliError) as exc:
        dev.write("AUDIO_MGR_MIC_GAIN", [0.1, 0.2])
    assert exc.value.code == EXIT_USER_ERROR
    assert fake.calls == []


def test_write_non_numeric_is_user_error(monkeypatch: pytest.MonkeyPatch) -> None:
    dev, fake = install(monkeypatch)
    with pytest.raises(CliError) as exc:
        dev.write("AUDIO_MGR_MIC_GAIN", ["loud"])
    assert exc.value.code == EXIT_USER_ERROR
    assert fake.calls == []


def test_write_unknown_name_is_user_error(monkeypatch: pytest.MonkeyPatch) -> None:
    dev, fake = install(monkeypatch)
    with pytest.raises(CliError) as exc:
        dev.write("NOT_A_PARAM", [1])
    assert exc.value.code == EXIT_USER_ERROR
    assert fake.calls == []


# --- firmware / metadata ----------------------------------------------------


def test_firmware_info(monkeypatch: pytest.MonkeyPatch) -> None:
    replies = [
        b"\x00\x01\x02\x03",
        b"\x00" + b"build-msg".ljust(50, b"\x00"),
        b"\x00" + b"buildhost".ljust(30, b"\x00"),
        b"\x00" + b"deadbeef".ljust(40, b"\x00"),
    ]
    dev, _ = install(monkeypatch, replies)
    assert dev.firmware_info() == {
        "version": "1.2.3",
        "build": "build-msg",
        "host": "buildhost",
        "repo_hash": "deadbeef",
    }


def test_known_ids() -> None:
    assert xvf3800.KNOWN_IDS[("38fb", "1001")] == "Reachy Mini Audio"
    assert xvf3800.KNOWN_IDS[("2886", "001a")] == "ReSpeaker XVF3800 (Seeed USB firmware)"


def test_persistent_set() -> None:
    for name in (
        "SAVE_CONFIGURATION",
        "CLEAR_CONFIGURATION",
        "REBOOT",
        "TEST_CORE_BURN",
        "TEST_AEC_DISABLE_CONTROL",
        "USB_BIT_DEPTH",
    ):
        assert name in xvf3800.PERSISTENT
    specials = {n for n in xvf3800.PARAMETERS if n.startswith("SPECIAL_CMD_")}
    assert specials and specials <= xvf3800.PERSISTENT
    assert "AUDIO_MGR_MIC_GAIN" not in xvf3800.PERSISTENT
    assert xvf3800.PERSISTENT <= set(xvf3800.PARAMETERS)


def test_param_info() -> None:
    info = xvf3800.param_info("audio_mgr_mic_gain")
    assert info.name == "AUDIO_MGR_MIC_GAIN"
    assert (info.resid, info.cmdid, info.count) == (35, 0, 1)
    assert info.access == "rw"
    assert info.type == "float"
    assert info.persistent is False
    assert xvf3800.param_info("REBOOT").persistent is True
    assert xvf3800.param_info("REBOOT").to_dict()["access"] == "wo"


def test_param_info_unknown_is_user_error() -> None:
    with pytest.raises(CliError) as exc:
        xvf3800.param_info("NOPE")
    assert exc.value.code == EXIT_USER_ERROR


def test_accepts_callable_transfer() -> None:
    calls: list[tuple[object, ...]] = []

    # type: ignore[no-untyped-def]
    def transfer(request_type, request, value, index, data_or_length):
        calls.append((request_type, request, value, index, data_or_length))
        return b"\x00" + struct.pack("<ff", 1.0, 2.0)

    dev = xvf3800.Xvf3800(transfer)
    assert dev.read("DOA_VALUE_RADIANS") == pytest.approx([1.0, 2.0])
    assert calls[0] == (0xC0, 0, 0x80 | 19, 20, 9)


def test_close_closes_owned_fd(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[int] = []
    monkeypatch.setattr(xvf3800.os, "close", closed.append)
    dev = xvf3800.Xvf3800(11)
    dev.close()
    dev.close()
    assert closed == [11]


# ---------------------------------------------------------------------------
# Firmware overlays (found on hardware: Seeed USB firmware v2.1.0, 2026-09-06)
# ---------------------------------------------------------------------------


def test_seeed_overlay_changes_doa_and_drops_radians() -> None:
    from microphone_cli.xvf3800 import SEEED_VENDOR, parameters_for

    seeed = parameters_for(SEEED_VENDOR)
    assert seeed["DOA_VALUE"] == (20, 18, 2, "ro", "uint16")
    assert "DOA_VALUE_RADIANS" not in seeed
    assert seeed["LED_RING_COLOR"] == (20, 19, 12, "rw", "uint32")
    # The base (Reachy) table is untouched.
    assert parameters_for(None)["DOA_VALUE"][4] == "uint32"
    assert "DOA_VALUE_RADIANS" in parameters_for("38fb")


def test_param_info_honours_vendor() -> None:
    from microphone_cli.xvf3800 import SEEED_VENDOR, param_info

    assert param_info("doa_value", SEEED_VENDOR).type == "uint16"
    with pytest.raises(CliError) as exc:
        param_info("DOA_VALUE_RADIANS", SEEED_VENDOR)
    assert exc.value.code == EXIT_USER_ERROR
    assert "2886" in exc.value.message


def test_uint16_read_requests_five_bytes_and_decodes_degrees() -> None:
    import struct

    from microphone_cli.xvf3800 import SEEED_VENDOR, Xvf3800

    calls: list[tuple[int, int, int, int, int | bytes]] = []

    def transfer(request_type, request, value, index, data_or_length):
        calls.append((request_type, request, value, index, data_or_length))
        return b"\x00" + struct.pack("<HH", 133, 1)

    chip = Xvf3800(transfer, vendor=SEEED_VENDOR)
    assert chip.read("DOA_VALUE") == [133, 1]
    assert calls == [(0xC0, 0, 0x80 | 18, 20, 5)]


def test_uint16_write_packs_little_endian() -> None:
    from microphone_cli.xvf3800 import FIRMWARE_OVERLAYS, Xvf3800

    # No writable uint16 exists in either firmware; use a synthetic overlay entry.
    FIRMWARE_OVERLAYS["ffff"] = {"TEST_U16": (99, 1, 2, "rw", "uint16")}
    try:
        sent: list[bytes] = []
        chip = Xvf3800(lambda *a: sent.append(bytes(a[4])) or len(a[4]), vendor="ffff")
        chip.write("TEST_U16", [1, 65535])
        assert sent == [b"\x01\x00\xff\xff"]
    finally:
        del FIRMWARE_OVERLAYS["ffff"]
