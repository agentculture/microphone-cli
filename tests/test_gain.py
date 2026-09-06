"""Tests for the `gain` noun group: microphone_cli.cli._commands.gain + mixer.py.

Hardware-free throughout: ``subprocess.run`` is always a fake (never a real
``amixer`` invocation) and the USB/firmware seam
(``gain._open_firmware``) is always monkeypatched to a fake
:class:`~microphone_cli.xvf3800.Xvf3800` built on a fake transfer callable —
no ``/dev`` node is ever opened.
"""

from __future__ import annotations

import json
import struct
import subprocess  # nosec B404 - only used to build fake CompletedProcess objects
from pathlib import Path

import pytest

from microphone_cli import mixer, xvf3800
from microphone_cli.cli import _CliArgumentParser
from microphone_cli.cli._commands import gain
from microphone_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from microphone_cli.cli._output import emit_error
from microphone_cli.devices import MicrophoneDevice, UsbIds
from microphone_cli.xvf3800 import Xvf3800

CAPTURE_VOLUME_CONTROL = "Mic Capture Volume"


def run_gain(argv: list[str]) -> int:
    """Parse/dispatch ``argv`` through a standalone parser carrying only the
    ``gain`` noun group.

    The ``gain`` command is not yet wired into
    :func:`microphone_cli.cli.main` (a separate task owns
    ``cli/__init__.py``), so tests build the exact same parser shape
    ``_build_parser()`` would once it registers ``gain`` — same
    ``_CliArgumentParser`` class, same ``parser_class`` propagation — and
    replicate ``_dispatch``'s ``CliError`` -> exit-code translation.
    """
    parser = _CliArgumentParser(prog="microphone-cli")
    sub = parser.add_subparsers(dest="command", parser_class=_CliArgumentParser)
    gain.register(sub)
    args = parser.parse_args(["gain", *argv])
    json_mode = bool(getattr(args, "json", False))
    try:
        rc = args.func(args)
    except CliError as err:
        emit_error(err, json_mode=json_mode)
        return err.code
    return rc if rc is not None else 0


# ---------------------------------------------------------------------------
# fixtures / fakes
# ---------------------------------------------------------------------------


class FakeAmixerRun:
    """Fake ``subprocess.run`` serving ``amixer -c <card> contents/cset``."""

    def __init__(self, *, lo: int = 0, hi: int = 30, initial: int = 20, has_control: bool = True):
        self.calls: list[list[str]] = []
        self.value = initial
        self.lo = lo
        self.hi = hi
        self.has_control = has_control

    def __call__(self, argv, capture_output=True, text=True, check=False):
        self.calls.append(list(argv))
        if "cset" in argv:
            self.value = int(argv[-1])
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if "contents" in argv:
            if not self.has_control:
                stdout = (
                    "numid=1,iface=MIXER,name='PCM Playback Switch'\n"
                    "  ; type=BOOLEAN,access=rw------,values=1\n"
                    "  : values=on\n"
                )
            else:
                stdout = (
                    f"numid=3,iface=MIXER,name='{CAPTURE_VOLUME_CONTROL}'\n"
                    f"  ; type=INTEGER,access=rw---R--,values=1,"
                    f"min={self.lo},max={self.hi},step=0\n"
                    f"  : values={self.value}\n"
                )
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")
        raise AssertionError(f"unexpected amixer invocation: {argv}")  # pragma: no cover


def _missing_amixer_run(argv, capture_output=True, text=True, check=False):
    raise FileNotFoundError("amixer")


class FakeFirmwareTransfer:
    """Fake XVF3800 transfer callable serving AUDIO_MGR_MIC_GAIN (resid=35, cmdid=0)."""

    def __init__(self, gain_value: float = 0.42, *, fail: bool = False):
        self.calls: list[tuple] = []
        self.gain_value = gain_value
        self.fail = fail

    def __call__(self, request_type, request, value, index, data_or_length):
        self.calls.append((request_type, request, value, index, data_or_length))
        if self.fail:
            return bytes([1])  # non-zero, non-retry status -> CliError
        if request_type == xvf3800.REQUEST_TYPE_IN:
            return bytes([0]) + struct.pack("<f", self.gain_value)
        # OUT (write): decode and remember the new value.
        self.gain_value = struct.unpack("<f", bytes(data_or_length))[0]
        return len(data_or_length)


def non_array_device(card_index: int = 1) -> MicrophoneDevice:
    return MicrophoneDevice(
        stable_id="usb-Generic_USB_Mic_ABC123",
        label="Generic USB Mic",
        alsa_address="hw:CARD=Mic",
        card_id="Mic",
        card_index=card_index,
        usb_path="3-1",
        usb_ids=UsbIds(vendor="046d", product="0825"),
        serial="ABC123",
        is_array=False,
        channels=1,
        pipewire_visible=None,
    )


def array_device(card_index: int = 2) -> MicrophoneDevice:
    return MicrophoneDevice(
        stable_id="usb-Pollen_Robotics_Reachy_Mini_Audio_RM0001",
        label="Reachy Mini Audio",
        alsa_address="hw:CARD=Audio",
        card_id="Audio",
        card_index=card_index,
        usb_path="5-1.1",
        usb_ids=UsbIds(vendor="38fb", product="1001"),
        serial="RM0001",
        is_array=True,
        channels=6,
        pipewire_visible=True,
    )


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> FakeAmixerRun:
    run = FakeAmixerRun()
    monkeypatch.setattr(subprocess, "run", run)
    return run


def _patch_resolve(monkeypatch: pytest.MonkeyPatch, device: MicrophoneDevice) -> None:
    monkeypatch.setattr(gain, "resolve", lambda selector, root="/": device)


def _patch_firmware(monkeypatch: pytest.MonkeyPatch, transfer: FakeFirmwareTransfer) -> None:
    monkeypatch.setattr(gain, "_open_firmware", lambda device, root: Xvf3800(transfer))


# ---------------------------------------------------------------------------
# mixer.py — parsing
# ---------------------------------------------------------------------------


def test_list_controls_parses_amixer_contents_blocks(fake_run: FakeAmixerRun) -> None:
    controls = mixer.list_controls(1, run=fake_run)
    assert len(controls) == 1
    control = controls[0]
    assert control.numid == 3
    assert control.name == CAPTURE_VOLUME_CONTROL
    assert control.control_type == "INTEGER"
    assert control.min == 0
    assert control.max == 30
    assert control.values == (20,)
    assert control.value == 20
    assert ["amixer", "-c", "1", "contents"] in fake_run.calls


def test_find_capture_volume_prefers_capture_volume_then_mic() -> None:
    volume = mixer.MixerControl(
        1, "MIXER", "Mic Capture Volume", "INTEGER", "rw", 1, 0, 30, 0, (5,)
    )
    switch = mixer.MixerControl(
        2, "MIXER", "Mic Capture Switch", "BOOLEAN", "rw", 1, None, None, None, ("on",)
    )
    unrelated = mixer.MixerControl(
        3, "MIXER", "PCM Playback Switch", "BOOLEAN", "rw", 1, None, None, None, ("on",)
    )

    assert mixer.find_capture_volume([switch, unrelated, volume]) is volume
    assert mixer.find_capture_volume([switch, unrelated]) is switch
    assert mixer.find_capture_volume([unrelated]) is None


def test_get_gain_no_capture_control_is_a_user_error() -> None:
    run = FakeAmixerRun(has_control=False)
    with pytest.raises(CliError) as exc:
        mixer.get_gain(1, run=run)
    assert exc.value.code == EXIT_USER_ERROR


def test_amixer_missing_is_an_env_error_with_apt_hint() -> None:
    with pytest.raises(CliError) as exc:
        mixer.list_controls(1, run=_missing_amixer_run)
    assert exc.value.code == EXIT_ENV_ERROR
    assert "apt" in exc.value.remediation


def test_set_gain_argv_is_pure() -> None:
    assert mixer.set_gain_argv(2, 3, 25) == ["amixer", "-c", "2", "cset", "numid=3", "25"]


def test_set_gain_issues_cset_then_rereads(fake_run: FakeAmixerRun) -> None:
    control = mixer.get_gain(1, run=fake_run)
    updated = mixer.set_gain(1, control, 25, run=fake_run)
    assert updated.value == 25
    assert fake_run.calls[-2] == ["amixer", "-c", "1", "cset", "numid=3", "25"]
    assert fake_run.calls[-1] == ["amixer", "-c", "1", "contents"]


# ---------------------------------------------------------------------------
# gain get
# ---------------------------------------------------------------------------


def test_gain_get_json_non_array_reports_alsa_only(
    monkeypatch: pytest.MonkeyPatch, fake_run: FakeAmixerRun, capsys: pytest.CaptureFixture[str]
) -> None:
    device = non_array_device()
    _patch_resolve(monkeypatch, device)

    rc = run_gain(["get", "usb-Generic_USB_Mic_ABC123", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["device"] == device.stable_id
    assert payload["alsa"] == {
        "control": CAPTURE_VOLUME_CONTROL,
        "numid": 3,
        "value": 20,
        "min": 0,
        "max": 30,
    }
    assert payload["firmware"] is None


def test_gain_get_json_array_adds_firmware(
    monkeypatch: pytest.MonkeyPatch, fake_run: FakeAmixerRun, capsys: pytest.CaptureFixture[str]
) -> None:
    device = array_device()
    _patch_resolve(monkeypatch, device)
    _patch_firmware(monkeypatch, FakeFirmwareTransfer(gain_value=0.75))

    rc = run_gain(["get", device.stable_id, "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["firmware"]["mic_gain"] == pytest.approx(0.75, rel=1e-5)


def test_gain_get_firmware_failure_still_returns_alsa_half(
    monkeypatch: pytest.MonkeyPatch, fake_run: FakeAmixerRun, capsys: pytest.CaptureFixture[str]
) -> None:
    device = array_device()
    _patch_resolve(monkeypatch, device)
    _patch_firmware(monkeypatch, FakeFirmwareTransfer(fail=True))

    rc = run_gain(["get", device.stable_id, "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["alsa"]["control"] == CAPTURE_VOLUME_CONTROL
    assert "error" in payload["firmware"]


def test_gain_get_text_mode_smoke(
    monkeypatch: pytest.MonkeyPatch, fake_run: FakeAmixerRun, capsys: pytest.CaptureFixture[str]
) -> None:
    device = non_array_device()
    _patch_resolve(monkeypatch, device)

    rc = run_gain(["get", device.stable_id])

    assert rc == 0
    out = capsys.readouterr().out
    assert "device: " + device.stable_id in out
    assert "firmware: n/a" in out


# ---------------------------------------------------------------------------
# gain set — dry run
# ---------------------------------------------------------------------------


def test_gain_set_dry_run_plans_without_touching_hardware(
    monkeypatch: pytest.MonkeyPatch, fake_run: FakeAmixerRun, capsys: pytest.CaptureFixture[str]
) -> None:
    device = array_device()
    _patch_resolve(monkeypatch, device)
    transfer = FakeFirmwareTransfer()
    _patch_firmware(monkeypatch, transfer)

    rc = run_gain(["set", device.stable_id, "0.5", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry-run"
    assert payload["applied"] is False
    assert payload["hardware_touched"] is False
    assert payload["device"] == device.stable_id
    # ALSA plan: 0.5 mapped onto [0, 30] -> 15, via the real control's numid.
    assert payload["planned"]["alsa"]["argv"] == [
        "amixer",
        "-c",
        str(device.card_index),
        "cset",
        "numid=3",
        "15",
    ]
    assert payload["planned"]["firmware"] == {"param": "AUDIO_MGR_MIC_GAIN", "values": [0.5]}
    # A read of the current control is fine (needed to know numid/min/max);
    # no cset was ever issued.
    assert all("cset" not in call for call in fake_run.calls)
    # Dry run never opens the firmware device at all.
    assert transfer.calls == []


@pytest.mark.parametrize("bad_value", ["1.5", "-0.1", "nan", "inf"])
def test_gain_set_rejects_out_of_range_or_non_finite_values(
    monkeypatch: pytest.MonkeyPatch,
    fake_run: FakeAmixerRun,
    bad_value: str,
) -> None:
    device = array_device()
    _patch_resolve(monkeypatch, device)
    transfer = FakeFirmwareTransfer()
    _patch_firmware(monkeypatch, transfer)

    rc = run_gain(["set", device.stable_id, bad_value, "--apply"])

    assert rc == EXIT_USER_ERROR
    assert all("cset" not in call for call in fake_run.calls)
    assert transfer.calls == []


def test_gain_set_dry_run_target_firmware_on_non_array_is_user_error(
    monkeypatch: pytest.MonkeyPatch, fake_run: FakeAmixerRun
) -> None:
    device = non_array_device()
    _patch_resolve(monkeypatch, device)

    rc = run_gain(["set", device.stable_id, "0.5", "--target", "firmware"])

    assert rc == EXIT_USER_ERROR


def test_gain_set_dry_run_both_on_non_array_only_plans_alsa(
    monkeypatch: pytest.MonkeyPatch, fake_run: FakeAmixerRun, capsys: pytest.CaptureFixture[str]
) -> None:
    device = non_array_device()
    _patch_resolve(monkeypatch, device)

    rc = run_gain(["set", device.stable_id, "1.0", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "firmware" not in payload["planned"]
    assert payload["planned"]["alsa"]["argv"][-1] == "30"  # 1.0 -> max


# ---------------------------------------------------------------------------
# gain set — apply
# ---------------------------------------------------------------------------


def test_gain_set_apply_issues_both_writes_and_logs_one_activation_line(
    monkeypatch: pytest.MonkeyPatch,
    fake_run: FakeAmixerRun,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "activation.jsonl"
    monkeypatch.setenv("MICROPHONE_ACTIVATION_LOG", str(log_path))

    device = array_device()
    _patch_resolve(monkeypatch, device)
    transfer = FakeFirmwareTransfer(gain_value=0.1)
    _patch_firmware(monkeypatch, transfer)

    rc = run_gain(["set", device.stable_id, "0.5", "--apply", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "apply"
    assert payload["applied"] is True
    assert payload["hardware_touched"] is True
    assert payload["alsa"]["value"] == 15
    assert payload["firmware"]["mic_gain"] == pytest.approx(0.5, rel=1e-5)

    # amixer: one read + one cset (+ the re-read inside mixer.set_gain).
    assert any("cset" in call for call in fake_run.calls)
    # firmware: one read (initial not needed) + one write + one read-back.
    assert transfer.calls, "firmware transfer was never invoked"

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["verb"] == "gain set"
    assert record["device"] == device.stable_id
    assert record["ended_at"] is not None


def test_gain_set_apply_alsa_only_on_non_array(
    monkeypatch: pytest.MonkeyPatch,
    fake_run: FakeAmixerRun,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MICROPHONE_ACTIVATION_LOG", str(tmp_path / "activation.jsonl"))
    device = non_array_device()
    _patch_resolve(monkeypatch, device)

    rc = run_gain(["set", device.stable_id, "0.0", "--apply", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["alsa"]["value"] == 0
    assert "firmware" not in payload


# ---------------------------------------------------------------------------
# overview / bare noun
# ---------------------------------------------------------------------------


def test_gain_overview_json() -> None:
    rc = run_gain(["overview", "--json"])
    assert rc == 0


def test_bare_gain_prints_overview(capsys: pytest.CaptureFixture[str]) -> None:
    rc = run_gain([])
    assert rc == 0
    assert "microphone gain" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Two same-named controls (found on hardware: XVF3800 'Headset Capture Volume'
# appears twice, the second with ",index=1"). The parser must keep them apart.
# ---------------------------------------------------------------------------

_TWO_CONTROLS = """numid=10,iface=MIXER,name='Headset Capture Volume'
  ; type=INTEGER,access=rw---R--,values=2,min=0,max=60,step=0
  : values=30,30
  | dBminmax-min=-60.00dB,max=0.00dB
numid=11,iface=MIXER,name='Headset Capture Volume',index=1
  ; type=INTEGER,access=rw---R--,values=1,min=0,max=60,step=0
  : values=60
"""


def test_list_controls_keeps_indexed_duplicate_apart() -> None:
    from microphone_cli import mixer

    def run(argv, **_kw):  # noqa: ANN001
        class R:
            returncode = 0
            stdout = _TWO_CONTROLS
            stderr = ""

        return R()

    controls = mixer.list_controls(1, run=run)
    by_numid = {c.numid: c for c in controls}
    assert set(by_numid) == {10, 11}
    assert by_numid[10].values == (30, 30)
    assert by_numid[11].values == (60,)
    assert mixer.find_capture_volume(controls).numid == 10
