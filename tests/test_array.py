"""Tests for the ``array`` noun group (DoA + AEC).

Nothing here opens a device node: every transfer is served by
:class:`FakeFirmware`, a callable with the ``transfer(request_type, request,
value, index, data_or_length)`` signature :class:`microphone_cli.xvf3800.Xvf3800`
accepts, and the module-level ``_open_array`` seam is monkeypatched to hand
back an :class:`Xvf3800` bound to it.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import struct
from typing import Any, Callable

import pytest

from microphone_cli import devices as devices_mod
from microphone_cli.activation import ENV_LOG_PATH
from microphone_cli.cli import _CliArgumentParser, _dispatch
from microphone_cli.cli._commands import array as array_cmd
from microphone_cli.devices import MicrophoneDevice, UsbIds
from microphone_cli.xvf3800 import PARAMETERS, Xvf3800

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
BASELINE = os.path.join(FIXTURES, "host-baseline")
SELECTOR = "usb-Pollen_Robotics_Reachy_Mini_Audio_RM0001"

_BY_ID = {(resid, cmdid): name for name, (resid, cmdid, *_rest) in PARAMETERS.items()}


def run(argv: list[str]) -> int:
    """Parse and dispatch ``argv`` through a parser carrying only the array noun."""
    parser = _CliArgumentParser(prog="microphone-cli")
    sub = parser.add_subparsers(dest="command", parser_class=_CliArgumentParser)
    array_cmd.register(sub)
    _CliArgumentParser._json_hint = any(token == "--json" for token in argv)
    args = parser.parse_args(argv)
    return _dispatch(args)


def _pack(name: str, values: Any) -> bytes:
    _resid, _cmdid, count, _access, type_ = PARAMETERS[name]
    if type_ == "char":
        raw = str(values).encode("utf-8")
        return raw.ljust(count, b"\x00")
    if type_ == "uint8":
        return bytes(bytearray(int(v) & 0xFF for v in values))
    fmt = {"float": "f", "radians": "f", "int32": "i", "uint32": "I"}[type_]
    if fmt == "f":
        return struct.pack("<" + fmt * count, *(float(v) for v in values))
    return struct.pack("<" + fmt * count, *(int(v) for v in values))


def _unpack(name: str, payload: bytes) -> list[Any]:
    _resid, _cmdid, count, _access, type_ = PARAMETERS[name]
    if type_ == "uint8":
        return list(payload[:count])
    fmt = {"float": "f", "radians": "f", "int32": "i", "uint32": "I"}[type_]
    return list(struct.unpack("<" + fmt * count, payload[: count * 4]))


class FakeFirmware:
    """An in-memory XVF3800: serves reads from ``values``, records writes."""

    def __init__(self, **overrides: Any) -> None:
        self.values: dict[str, Any] = {
            "DOA_VALUE_RADIANS": [1.25, 1.0],
            "AEC_AECCONVERGED": [1],
            "SHF_BYPASS": [0],
            "AEC_HPFONOFF": [1],
            "PP_ECHOONOFF": [1],
            "AEC_NUM_MICS": [4],
            "AEC_MIC_ARRAY_TYPE": [0],
            "AEC_MIC_ARRAY_GEO": [float(i) / 100.0 for i in range(12)],
            "AEC_RT60": [0.25],
        }
        self.values.update(overrides)
        self.reads: list[str] = []
        self.writes: list[tuple[str, list[Any]]] = []
        #: raise ``OSError(ENODEV)`` on the Nth read of this parameter.
        self.fail_read_after: int | None = None
        self.fail_param = "DOA_VALUE_RADIANS"

    def __call__(
        self,
        request_type: int,
        request: int,
        value: int,
        index: int,
        data_or_length: int | bytes,
    ) -> Any:
        if request_type & 0x80:
            name = _BY_ID[(index, value & 0x7F)]
            self.reads.append(name)
            if (
                self.fail_read_after is not None
                and name == self.fail_param
                and self.reads.count(name) > self.fail_read_after
            ):
                raise OSError(errno.ENODEV, "No such device")
            return b"\x00" + _pack(name, self.values[name])
        name = _BY_ID[(index, value)]
        self.writes.append((name, _unpack(name, bytes(data_or_length))))
        self.values[name] = self.writes[-1][1]
        return len(bytes(data_or_length))


@pytest.fixture()
def firmware(monkeypatch: pytest.MonkeyPatch) -> FakeFirmware:
    fake = FakeFirmware()
    opened: list[Any] = []

    def _open(device: Any, root: str = "/", timeout_ms: int | None = None) -> Xvf3800:
        opened.append(device)
        return Xvf3800(fake)

    monkeypatch.setattr(array_cmd, "_open_array", _open)
    monkeypatch.setattr(array_cmd, "_sleep", lambda _seconds: None)
    fake.opened = opened  # type: ignore[attr-defined]
    return fake


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# noun group shape
# ---------------------------------------------------------------------------


def test_bare_array_prints_the_noun_overview(capsys: pytest.CaptureFixture[str]) -> None:
    rc = run(["array"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "microphone array" in out
    assert "doa" in out
    assert "aec" in out


def test_array_overview_json_lists_sections(capsys: pytest.CaptureFixture[str]) -> None:
    rc = run(["array", "overview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "microphone array"
    assert [section["title"] for section in payload["sections"]]


def test_subparsers_use_the_structured_parser_class(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = _CliArgumentParser(prog="microphone-cli")
    sub = parser.add_subparsers(dest="command", parser_class=_CliArgumentParser)
    array_cmd.register(sub)
    array_parser = sub.choices["array"]
    noun_sub = [
        action for action in array_parser._actions if isinstance(action, argparse._SubParsersAction)
    ][0]
    assert noun_sub._parser_class is _CliArgumentParser
    aec_parser = noun_sub.choices["aec"]
    aec_sub = [
        action for action in aec_parser._actions if isinstance(action, argparse._SubParsersAction)
    ][0]
    assert aec_sub._parser_class is _CliArgumentParser

    with pytest.raises(SystemExit) as exc:
        run(["array", "bogus", "--json"])
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == 1


# ---------------------------------------------------------------------------
# doa
# ---------------------------------------------------------------------------


def test_doa_json_single_shot(firmware: FakeFirmware, capsys: pytest.CaptureFixture[str]) -> None:
    rc = run(["array", "doa", SELECTOR, "--root", BASELINE, "--json"])
    assert rc == 0
    out = _lines(capsys.readouterr().out)
    assert len(out) == 1
    payload = json.loads(out[0])
    assert payload["azimuth_rad"] == pytest.approx(1.25)
    assert payload["speech"] is True
    assert payload["source"] == "DOA_VALUE_RADIANS"
    assert payload["device"] == SELECTOR
    assert payload["ts"]
    assert firmware.reads == ["DOA_VALUE_RADIANS"]


def test_doa_text_single_shot(firmware: FakeFirmware, capsys: pytest.CaptureFixture[str]) -> None:
    rc = run(["array", "doa", SELECTOR, "--root", BASELINE])
    assert rc == 0
    out = _lines(capsys.readouterr().out)
    assert len(out) == 1
    assert "azimuth_rad" in out[0]
    assert "1.25" in out[0]


def test_doa_speech_flag_is_false_below_the_threshold(
    firmware: FakeFirmware, capsys: pytest.CaptureFixture[str]
) -> None:
    firmware.values["DOA_VALUE_RADIANS"] = [-0.5, 0.0]
    rc = run(["array", "doa", SELECTOR, "--root", BASELINE, "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["speech"] is False
    assert payload["azimuth_rad"] == pytest.approx(-0.5)


def test_doa_watch_count_prints_exactly_n_json_lines(
    firmware: FakeFirmware, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(array_cmd, "_sleep", slept.append)
    rc = run(
        [
            "array",
            "doa",
            SELECTOR,
            "--root",
            BASELINE,
            "--watch",
            "--count",
            "3",
            "--interval",
            "0.25",
        ]
    )
    assert rc == 0
    out = _lines(capsys.readouterr().out)
    assert len(out) == 3
    for line in out:
        payload = json.loads(line)
        assert payload["source"] == "DOA_VALUE_RADIANS"
    assert slept == [0.25, 0.25]


def test_doa_watch_emits_json_lines_without_the_json_flag(
    firmware: FakeFirmware, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = run(["array", "doa", SELECTOR, "--root", BASELINE, "--watch", "--count", "2"])
    assert rc == 0
    out = _lines(capsys.readouterr().out)
    assert len(out) == 2
    assert all(json.loads(line)["device"] == SELECTOR for line in out)


def test_doa_watch_device_disappears_on_the_third_poll(
    firmware: FakeFirmware, capsys: pytest.CaptureFixture[str]
) -> None:
    firmware.fail_read_after = 2
    rc = run(["array", "doa", SELECTOR, "--root", BASELINE, "--watch", "--count", "5", "--json"])
    assert rc == 2
    captured = capsys.readouterr()
    assert len(_lines(captured.out)) == 2
    err_lines = _lines(captured.err)
    assert len(err_lines) == 1
    payload = json.loads(err_lines[0])
    assert payload["code"] == 2
    assert "disappear" in payload["message"]
    assert payload["remediation"]


def test_doa_watch_sigint_exits_zero_after_flushing(
    firmware: FakeFirmware, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(array_cmd, "_sleep", _interrupt)
    rc = run(["array", "doa", SELECTOR, "--root", BASELINE, "--watch", "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    assert len(_lines(captured.out)) == 1
    assert captured.err == ""


def test_doa_rejects_a_non_positive_interval(
    firmware: FakeFirmware, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = run(["array", "doa", SELECTOR, "--root", BASELINE, "--watch", "--interval", "0", "--json"])
    assert rc == 1
    assert json.loads(capsys.readouterr().err)["code"] == 1


def test_doa_refuses_a_device_that_is_not_an_array(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plain = MicrophoneDevice(
        stable_id="usb-Generic_Mic_0001",
        label="Generic Mic",
        alsa_address="hw:CARD=Mic",
        card_id="Mic",
        card_index=1,
        usb_path="1-1",
        usb_ids=UsbIds(vendor="046d", product="0825"),
        serial="0001",
        is_array=False,
        channels=1,
        pipewire_visible=True,
    )
    monkeypatch.setattr(devices_mod, "resolve", lambda selector, root="/": plain)
    rc = run(["array", "doa", "whatever", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().err)
    assert "array" in payload["message"]
    assert payload["remediation"]


def test_doa_unknown_selector_is_a_user_error(capsys: pytest.CaptureFixture[str]) -> None:
    rc = run(["array", "doa", "no-such-microphone", "--root", BASELINE, "--json"])
    assert rc == 1
    assert json.loads(capsys.readouterr().err)["code"] == 1


# ---------------------------------------------------------------------------
# aec get
# ---------------------------------------------------------------------------


def test_aec_get_json_reports_the_documented_keys(
    firmware: FakeFirmware, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = run(["array", "aec", "get", SELECTOR, "--root", BASELINE, "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    for key in ("converged", "bypass", "hpf", "echo", "num_mics", "geometry"):
        assert key in payload
    assert payload["converged"] is True
    assert payload["bypass"] is False
    assert payload["hpf"] is True
    assert payload["echo"] is True
    assert payload["num_mics"] == 4
    assert len(payload["geometry"]) == 12
    assert payload["device"] == SELECTOR


def test_aec_get_text(firmware: FakeFirmware, capsys: pytest.CaptureFixture[str]) -> None:
    rc = run(["array", "aec", "get", SELECTOR, "--root", BASELINE])
    assert rc == 0
    out = capsys.readouterr().out
    assert "converged: true" in out
    assert "num_mics: 4" in out


def test_aec_overview(capsys: pytest.CaptureFixture[str]) -> None:
    rc = run(["array", "aec", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "microphone array aec"


# ---------------------------------------------------------------------------
# aec set
# ---------------------------------------------------------------------------


def test_aec_set_without_apply_touches_no_hardware(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Any
) -> None:
    log = tmp_path / "activation.jsonl"
    monkeypatch.setenv(ENV_LOG_PATH, str(log))

    def _boom(*_args: Any, **_kwargs: Any) -> Xvf3800:
        raise AssertionError("dry run must not open the device")

    monkeypatch.setattr(array_cmd, "_open_array", _boom)
    rc = run(["array", "aec", "set", SELECTOR, "--root", BASELINE, "--echo", "off", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry-run"
    assert payload["applied"] is False
    assert payload["hardware_touched"] is False
    assert payload["planned"] == [
        {"setting": "echo", "value": "off", "param": "PP_ECHOONOFF", "values": [0]}
    ]
    assert not log.exists()


def test_aec_set_with_apply_writes_and_logs(
    firmware: FakeFirmware,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    log = tmp_path / "activation.jsonl"
    monkeypatch.setenv(ENV_LOG_PATH, str(log))
    rc = run(
        [
            "array",
            "aec",
            "set",
            SELECTOR,
            "--root",
            BASELINE,
            "--echo",
            "off",
            "--apply",
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "apply"
    assert payload["applied"] is True
    assert payload["hardware_touched"] is True
    assert firmware.writes == [("PP_ECHOONOFF", [0])]
    assert payload["state"]["echo"] is False

    entries = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert len(entries) == 1
    assert entries[0]["verb"] == "array aec set"
    assert entries[0]["device"] == SELECTOR
    assert entries[0]["params"]["echo"] == "off"
    assert entries[0]["ended_at"]


def test_aec_set_applies_every_flag(
    firmware: FakeFirmware, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv(ENV_LOG_PATH, str(tmp_path / "activation.jsonl"))
    rc = run(
        [
            "array",
            "aec",
            "set",
            SELECTOR,
            "--root",
            BASELINE,
            "--echo",
            "on",
            "--bypass",
            "on",
            "--hpf",
            "off",
            "--apply",
            "--json",
        ]
    )
    assert rc == 0
    assert firmware.writes == [
        ("PP_ECHOONOFF", [1]),
        ("SHF_BYPASS", [1]),
        ("AEC_HPFONOFF", [0]),
    ]


def test_aec_set_without_any_flag_is_a_user_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = run(["array", "aec", "set", SELECTOR, "--root", BASELINE, "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == 1
    assert payload["remediation"]


def test_aec_set_records_a_line_even_when_the_write_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
) -> None:
    log = tmp_path / "activation.jsonl"
    monkeypatch.setenv(ENV_LOG_PATH, str(log))
    fake = FakeFirmware()

    def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError(errno.ENODEV, "No such device")

    def _open(*_args: Any, **_kwargs: Any) -> Xvf3800:
        return Xvf3800(_explode)

    monkeypatch.setattr(array_cmd, "_open_array", _open)
    rc = run(
        ["array", "aec", "set", SELECTOR, "--root", BASELINE, "--echo", "off", "--apply", "--json"]
    )
    assert rc == 2
    assert json.loads(capsys.readouterr().err)["code"] == 2
    entries = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert len(entries) == 1
    assert "error" in entries[0]["params"]
    assert fake.writes == []


# ---------------------------------------------------------------------------
# the open seam itself
# ---------------------------------------------------------------------------


def test_open_array_reports_a_missing_usb_node(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The fixture tree has no /sys/bus/usb/devices, so find_devices() is empty.
    rc = run(["array", "doa", SELECTOR, "--root", BASELINE, "--json"])
    assert rc == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == 2
    assert payload["remediation"]


def test_open_array_opens_the_matching_node(monkeypatch: pytest.MonkeyPatch) -> None:
    device = devices_mod.resolve(SELECTOR, BASELINE)
    seen: dict[str, Any] = {}

    def _find(**kwargs: Any) -> list[dict[str, str]]:
        seen.update(kwargs)
        return [{"node": "/dev/bus/usb/001/007"}]

    opened: list[str] = []

    def _open(node: str, **_kw: object) -> int:
        opened.append(node)
        return 4242

    monkeypatch.setattr(array_cmd.usbctl, "find_devices", _find)
    monkeypatch.setattr(array_cmd.usbctl, "open_device", _open)
    chip = array_cmd._open_array(device, root=BASELINE)
    assert isinstance(chip, Xvf3800)
    assert opened == ["/dev/bus/usb/001/007"]
    assert seen["vendor"] == "38fb"
    assert seen["product"] == "1001"
    assert seen["serial"] == "RM0001"
    chip._fd = None  # do not close the fake descriptor


def test_read_doa_helper_is_pure(firmware: FakeFirmware) -> None:
    device = devices_mod.resolve(SELECTOR, BASELINE)
    transfer: Callable[..., Any] = firmware
    payload = array_cmd._read_doa(Xvf3800(transfer), device)
    assert set(payload) == {"device", "azimuth_rad", "azimuth_deg", "speech", "source", "ts"}


# ---------------------------------------------------------------------------
# Seeed firmware: DoA comes from DOA_VALUE (degrees, speech) — found on hardware
# ---------------------------------------------------------------------------


def test_doa_on_seeed_firmware_reads_degrees(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import math
    import struct

    from microphone_cli.xvf3800 import SEEED_VENDOR

    reads: list[tuple[int, int]] = []

    def transfer(request_type, request, value, index, data_or_length):
        assert request_type & 0x80, "doa is read-only"
        reads.append((index, value & 0x7F))
        assert data_or_length == 5, "two uint16 plus the status byte"
        return b"\x00" + struct.pack("<HH", 133, 1)

    monkeypatch.setattr(
        array_cmd,
        "_open_array",
        lambda device, root="/", timeout_ms=None: Xvf3800(transfer, vendor=SEEED_VENDOR),
    )
    rc = run(["array", "doa", SELECTOR, "--root", BASELINE, "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert reads == [(20, 18)]
    assert payload["source"] == "DOA_VALUE"
    assert payload["azimuth_deg"] == 133.0
    assert payload["azimuth_rad"] == pytest.approx(math.radians(133))
    assert payload["speech"] is True


def test_doa_on_reachy_firmware_reports_degrees_too(
    firmware: FakeFirmware, capsys: pytest.CaptureFixture[str]
) -> None:
    import math

    rc = run(["array", "doa", SELECTOR, "--root", BASELINE, "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "DOA_VALUE_RADIANS"
    assert payload["azimuth_rad"] == 1.25
    assert payload["azimuth_deg"] == pytest.approx(math.degrees(1.25))
