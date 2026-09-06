"""Tests for ``microphone list`` (:mod:`microphone_cli.cli._commands.list_devices`).

Everything runs against fixture trees under ``tests/fixtures`` (or an empty
``tmp_path``) — no real ``/dev`` node is ever opened; ``check_access`` still
runs its single non-blocking ``open()``/``close()`` probe, but the probed path
never exists under a fixture root, so the worst it can do is report
``state: "absent"``.
"""

from __future__ import annotations

import argparse
import json
import os

from microphone_cli.cli._commands import list_devices
from microphone_cli.devices import enumerate_devices

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def tree(name: str) -> str:
    return os.path.join(FIXTURES, name)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    list_devices.register(sub)
    return parser


def test_register_exposes_list_with_json_and_root() -> None:
    parser = _parser()
    args = parser.parse_args(["list", "--json", "--root", tree("host-baseline")])
    assert args.func is list_devices.cmd_list
    assert args.json is True
    assert args.root == tree("host-baseline")


def test_list_default_root_is_slash() -> None:
    parser = _parser()
    args = parser.parse_args(["list"])
    assert args.root == "/"
    assert args.json is False


def test_list_json_payload_shape() -> None:
    report = list_devices.build_report(tree("host-baseline"))
    assert set(report) == {"devices", "count"}
    assert report["count"] == 1
    (payload,) = report["devices"]
    assert set(payload["audio_access"]) == {"state", "path", "remediation"}


def test_list_devices_equal_enumerate_devices_as_dict() -> None:
    root = tree("host-baseline")
    report = list_devices.build_report(root)
    expected = [device.as_dict() for device in enumerate_devices(root=root)]
    assert report["count"] == len(expected)
    for got, want in zip(report["devices"], expected):
        got_without_access = {k: v for k, v in got.items() if k != "audio_access"}
        assert got_without_access == want


def test_list_audio_access_absent_when_no_dev_node_in_fixture() -> None:
    report = list_devices.build_report(tree("host-baseline"))
    (payload,) = report["devices"]
    assert payload["audio_access"]["state"] == "absent"
    assert payload["audio_access"]["path"] == "/dev/snd/pcmC1D0c"


def test_list_two_arrays_reports_both_devices() -> None:
    root = tree("two-arrays")
    report = list_devices.build_report(root)
    assert report["count"] == 2
    assert {d["serial"] for d in report["devices"]} == {"RM0001", "RM0002"}


def test_list_json_cli_matches_build_report(capsys) -> None:
    root = tree("host-baseline")
    parser = _parser()
    args = parser.parse_args(["list", "--json", "--root", root])
    rc = args.func(args)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == list_devices.build_report(root)


def test_list_text_mode_prints_stable_id_label_address_and_array_marker(capsys) -> None:
    parser = _parser()
    args = parser.parse_args(["list", "--root", tree("host-baseline")])
    rc = args.func(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "usb-Pollen_Robotics_Reachy_Mini_Audio_RM0001" in out
    assert "Reachy Mini Audio" in out
    assert "hw:CARD=Audio" in out
    assert "[array]" in out


def test_list_empty_root_reports_zero_devices(tmp_path, capsys) -> None:
    parser = _parser()
    args = parser.parse_args(["list", "--json", "--root", str(tmp_path)])
    rc = args.func(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"devices": [], "count": 0}


def test_list_empty_root_text_mode(tmp_path, capsys) -> None:
    parser = _parser()
    args = parser.parse_args(["list", "--root", str(tmp_path)])
    args.func(args)
    assert capsys.readouterr().out.strip() == "no microphones found"
