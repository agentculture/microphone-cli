"""Tests for the ``param`` noun (`microphone_cli.cli._commands.param`).

The noun is not wired into the top-level CLI yet (that is a separate wiring
task), so tests build a standalone parser with ``param.register()`` directly,
reusing the real ``_CliArgumentParser``/``_dispatch`` error plumbing from
:mod:`microphone_cli.cli`. Every device/transfer touchpoint is monkeypatched:
``param.resolve`` returns a fixed fake :class:`MicrophoneDevice`, and
``param._open_array`` returns ``Xvf3800(FakeTransfer())`` — no test here ever
opens ``/dev``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from microphone_cli.cli import _argv_has_json, _CliArgumentParser, _dispatch
from microphone_cli.cli._commands import param
from microphone_cli.devices import MicrophoneDevice, UsbIds
from microphone_cli.xvf3800 import PARAMETERS, Xvf3800

FAKE_DEVICE = MicrophoneDevice(
    stable_id="usb-test-array",
    label="Test Array",
    alsa_address="hw:CARD=Test",
    card_id="Test",
    card_index=1,
    usb_path="1-1",
    usb_ids=UsbIds(vendor="38fb", product="1001"),
    serial="SN1",
    is_array=True,
    channels=6,
    pipewire_visible=None,
)

FAKE_NON_ARRAY_DEVICE = MicrophoneDevice(
    stable_id="usb-test-plain",
    label="Plain Mic",
    alsa_address="hw:CARD=Plain",
    card_id="Plain",
    card_index=2,
    usb_path="1-2",
    usb_ids=UsbIds(vendor="dead", product="beef"),
    serial="SN2",
    is_array=False,
    channels=1,
    pipewire_visible=None,
)


class FakeTransfer:
    """Records every control-transfer call; round-trips OUT writes into IN replies."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self._written: dict[tuple[int, int], bytes] = {}

    def __call__(
        self,
        request_type: int,
        request: int,
        value: int,
        index: int,
        data_or_length: object,
    ) -> bytes:
        self.calls.append((request_type, request, value, index, data_or_length))
        if request_type == 0xC0:  # IN
            length = int(data_or_length)  # type: ignore[arg-type]
            cmdid = value & 0x7F
            body = self._written.get((index, cmdid), b"")
            body = body[: length - 1].ljust(length - 1, b"\x00")
            return bytes([0]) + body
        # OUT
        self._written[(index, value)] = bytes(bytearray(data_or_length))  # type: ignore[arg-type]
        return b""


def _run(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    *,
    device: MicrophoneDevice = FAKE_DEVICE,
    transfer: FakeTransfer | None = None,
    forbid_open: bool = False,
) -> int:
    monkeypatch.setattr(param, "resolve", lambda selector, root="/": device)
    if forbid_open:

        def _no_open(device: MicrophoneDevice, root: str) -> Xvf3800:
            raise AssertionError("_open_array must not be called")

        monkeypatch.setattr(param, "_open_array", _no_open)
    else:
        fake = transfer if transfer is not None else FakeTransfer()
        monkeypatch.setattr(param, "_open_array", lambda device, root: Xvf3800(fake))

    _CliArgumentParser._json_hint = _argv_has_json(argv)
    parser = _CliArgumentParser(prog="microphone-cli")
    sub = parser.add_subparsers(dest="command", parser_class=_CliArgumentParser)
    param.register(sub)
    args = parser.parse_args(argv)
    return _dispatch(args)


# --- overview ---------------------------------------------------------------


def test_param_bare_prints_overview(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _run(monkeypatch, ["param"])
    assert rc == 0
    assert "# microphone-cli param" in capsys.readouterr().out


def test_param_overview_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _run(monkeypatch, ["param", "overview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "microphone-cli param"


# --- list ---------------------------------------------------------------


def test_param_list_json_every_entry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _run(monkeypatch, ["param", "list", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == len(PARAMETERS) == len(payload["params"])
    by_name = {row["name"]: row for row in payload["params"]}
    assert set(by_name) == set(PARAMETERS)
    for name, (resid, cmdid, count, access, type_) in PARAMETERS.items():
        row = by_name[name]
        assert row == {
            "name": name,
            "resid": resid,
            "cmdid": cmdid,
            "count": count,
            "access": access,
            "type": type_,
            "persistent": row["persistent"],
        }
        assert isinstance(row["persistent"], bool)
    assert by_name["REBOOT"]["persistent"] is True
    assert by_name["AUDIO_MGR_MIC_GAIN"]["persistent"] is False
    # sorted by name
    names = [row["name"] for row in payload["params"]]
    assert names == sorted(names)


def test_param_list_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _run(monkeypatch, ["param", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "REBOOT" in out
    assert "persistent=True" in out


# --- get ------------------------------------------------------------------


def test_param_get_unknown_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _run(monkeypatch, ["param", "get", "usb-test-array", "NOT_A_PARAM"], forbid_open=True)
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


def test_param_get_wo_name_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert PARAMETERS["REBOOT"][3] == "wo"
    rc = _run(monkeypatch, ["param", "get", "usb-test-array", "reboot"], forbid_open=True)
    assert rc == 1
    err = capsys.readouterr().err
    assert "write-only" in err


def test_param_get_reads_and_echoes_upper(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeTransfer()
    rc = _run(
        monkeypatch,
        ["param", "get", "usb-test-array", "audio_mgr_mic_gain", "--json"],
        transfer=fake,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["device"] == "usb-test-array"
    assert payload["param"]["name"] == "AUDIO_MGR_MIC_GAIN"
    assert payload["values"] == [0.0]


def test_param_get_non_array_device_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _run(
        monkeypatch,
        ["param", "get", "usb-test-plain", "VERSION"],
        device=FAKE_NON_ARRAY_DEVICE,
        forbid_open=True,
    )
    assert rc == 1
    assert "not an XVF3800" in capsys.readouterr().err


# --- set: validation (no transfer) ------------------------------------------


def test_param_set_unknown_name_no_transfer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _run(
        monkeypatch,
        ["param", "set", "usb-test-array", "NOT_A_PARAM", "1"],
        forbid_open=True,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


def test_param_set_ro_name_no_transfer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert PARAMETERS["VERSION"][3] == "ro"
    rc = _run(
        monkeypatch,
        ["param", "set", "usb-test-array", "VERSION", "1", "2", "3"],
        forbid_open=True,
    )
    assert rc == 1
    assert "read-only" in capsys.readouterr().err


def test_param_set_wrong_count_no_transfer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert PARAMETERS["AUDIO_MGR_MIC_GAIN"][2] == 1
    rc = _run(
        monkeypatch,
        ["param", "set", "usb-test-array", "AUDIO_MGR_MIC_GAIN", "1.0", "2.0"],
        forbid_open=True,
    )
    assert rc == 1
    assert "takes 1 value" in capsys.readouterr().err


def test_param_set_wrong_count_no_transfer_even_with_apply(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _run(
        monkeypatch,
        ["param", "set", "usb-test-array", "AUDIO_MGR_MIC_GAIN", "1.0", "2.0", "--apply"],
        forbid_open=True,
    )
    assert rc == 1


# --- set: dry-run -----------------------------------------------------------


def test_param_set_dry_run_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _run(
        monkeypatch,
        ["param", "set", "usb-test-array", "audio_mgr_mic_gain", "0.5", "--json"],
        forbid_open=True,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "mode": "dry-run",
        "applied": False,
        "hardware_touched": False,
        "device": "usb-test-array",
        "param": {
            "name": "AUDIO_MGR_MIC_GAIN",
            "resid": 35,
            "cmdid": 0,
            "count": 1,
            "access": "rw",
            "type": "float",
            "persistent": False,
        },
        "values": [0.5],
    }


# --- set: persistent gate ----------------------------------------------------


def test_param_set_reboot_apply_without_allow_persistent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _run(
        monkeypatch,
        ["param", "set", "usb-test-array", "REBOOT", "1", "--apply"],
        forbid_open=True,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "--allow-persistent" in err
    assert "volatile" in err.lower()


@pytest.mark.parametrize(
    "name,values",
    [
        ("REBOOT", ["1"]),
        ("SAVE_CONFIGURATION", ["1"]),
        ("CLEAR_CONFIGURATION", ["1"]),
        ("TEST_CORE_BURN", ["1"]),
        ("TEST_AEC_DISABLE_CONTROL", ["1"]),
        ("SPECIAL_CMD_AEC_FAR_MIC_INDEX", ["1", "2"]),
    ],
)
def test_persistent_tier_names_require_allow_persistent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    values: list[str],
) -> None:
    rc = _run(
        monkeypatch,
        ["param", "set", "usb-test-array", name, *values, "--apply"],
        forbid_open=True,
    )
    assert rc == 1
    assert "--allow-persistent" in capsys.readouterr().err


def test_param_set_reboot_apply_with_allow_persistent_sends_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    log_file = tmp_path / "activation.jsonl"
    monkeypatch.setenv("MICROPHONE_ACTIVATION_LOG", str(log_file))
    fake = FakeTransfer()
    rc = _run(
        monkeypatch,
        [
            "param",
            "set",
            "usb-test-array",
            "REBOOT",
            "1",
            "--apply",
            "--allow-persistent",
            "--json",
        ],
        transfer=fake,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is True
    assert payload["hardware_touched"] is True
    assert payload["mode"] == "apply"
    assert payload["param"]["name"] == "REBOOT"
    assert "readback" not in payload  # REBOOT is write-only

    # exactly one OUT transfer, and it happened.
    out_calls = [c for c in fake.calls if c[0] == 0x40]
    assert len(out_calls) == 1

    lines = log_file.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["verb"] == "param set"
    assert record["device"] == "usb-test-array"
    assert record["params"]["param"] == "REBOOT"
    assert record["params"]["persistent"] is True
    assert record["ended_at"] is not None


# --- set: apply on an ordinary rw param, with readback -----------------------


def test_param_set_rw_apply_includes_readback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MICROPHONE_ACTIVATION_LOG", str(tmp_path / "activation.jsonl"))
    fake = FakeTransfer()
    rc = _run(
        monkeypatch,
        ["param", "set", "usb-test-array", "AUDIO_MGR_MIC_GAIN", "0.75", "--apply", "--json"],
        transfer=fake,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is True
    assert payload["readback"] == pytest.approx([0.75])


def test_param_set_case_insensitive_echoes_upper(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MICROPHONE_ACTIVATION_LOG", str(tmp_path / "activation.jsonl"))
    fake = FakeTransfer()
    rc = _run(
        monkeypatch,
        ["param", "set", "usb-test-array", "shf_bypass", "1", "--apply", "--json"],
        transfer=fake,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["param"]["name"] == "SHF_BYPASS"
