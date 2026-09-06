"""Tests for :mod:`microphone_cli.devices`, driven entirely by fixture trees.

Every test points ``root=`` at a synthetic ``tests/fixtures/<tree>`` directory
containing only text files and relative symlinks — no real capture hardware is
touched and no device node is opened.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 - fixed argv, no shell, used for an import-isolation check
import sys

import pytest

from microphone_cli.cli._errors import EXIT_USER_ERROR, CliError
from microphone_cli.devices import (
    MicrophoneDevice,
    enumerate_devices,
    is_array_ids,
    resolve,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# Every key the acceptance criteria require of ``as_dict()``.
REQUIRED_KEYS = {
    "stable_id",
    "label",
    "alsa_address",
    "card_id",
    "usb_path",
    "usb_ids",
    "serial",
    "is_array",
    "channels",
    "pipewire_visible",
}


def tree(name: str) -> str:
    return os.path.join(FIXTURES, name)


# ---------------------------------------------------------------------------
# enumeration
# ---------------------------------------------------------------------------


def test_baseline_enumerates_the_single_usb_capture_card() -> None:
    devices = enumerate_devices(root=tree("host-baseline"))
    assert len(devices) == 1
    device = devices[0]
    assert isinstance(device, MicrophoneDevice)
    assert device.stable_id == "usb-Pollen_Robotics_Reachy_Mini_Audio_RM0001"
    assert device.label == "Reachy Mini Audio"
    assert device.card_id == "Audio"
    assert device.card_index == 1
    assert device.alsa_address == "hw:CARD=Audio"
    assert device.usb_path == "5-1.1"
    assert device.usb_ids.vendor == "38fb"
    assert device.usb_ids.product == "1001"
    assert device.serial == "RM0001"
    assert device.is_array is True
    assert device.channels == 6
    assert device.pipewire_visible is True


def test_playback_only_and_non_usb_cards_are_skipped() -> None:
    devices = enumerate_devices(root=tree("host-baseline"))
    # card 0 in the fixture is a playback-only, non-USB HDA controller.
    assert [device.card_id for device in devices] == ["Audio"]


def test_stable_id_survives_renumbering_while_the_card_index_moves() -> None:
    baseline = enumerate_devices(root=tree("host-baseline"))
    renumbered = enumerate_devices(root=tree("host-renumbered"))

    assert [device.stable_id for device in baseline] == [device.stable_id for device in renumbered]
    assert [device.card_index for device in baseline] != [
        device.card_index for device in renumbered
    ]
    # The ALSA address is keyed on the card id, so it survives too.
    assert baseline[0].alsa_address == renumbered[0].alsa_address
    # ...but the USB port genuinely moved, proving identity is not topological.
    assert baseline[0].usb_path != renumbered[0].usb_path


def test_two_arrays_are_distinct_devices() -> None:
    devices = enumerate_devices(root=tree("two-arrays"))
    assert len(devices) == 2
    assert {device.serial for device in devices} == {"RM0001", "RM0002"}
    assert len({device.stable_id for device in devices}) == 2
    assert all(device.is_array for device in devices)


def test_respeaker_falls_back_to_the_sysfs_path_when_no_serial_is_present() -> None:
    (device,) = enumerate_devices(root=tree("respeaker"))
    assert device.serial is None
    assert device.stable_id == "usb-path-3-1"
    assert device.is_array is True  # 2886:001a
    assert device.channels is None  # no stream0 in the fixture
    assert device.pipewire_visible is None  # no PipeWire runtime socket


def test_is_array_ids_covers_both_xvf3800_pairs() -> None:
    assert is_array_ids("38fb", "1001") is True
    assert is_array_ids("2886", "001a") is True
    assert is_array_ids("046d", "0825") is False


# ---------------------------------------------------------------------------
# as_dict shape
# ---------------------------------------------------------------------------


def test_as_dict_carries_every_required_key() -> None:
    (device,) = enumerate_devices(root=tree("host-baseline"))
    payload = device.as_dict()
    assert REQUIRED_KEYS <= set(payload)
    assert payload["usb_ids"] == {"vendor": "38fb", "product": "1001"}
    assert payload["is_array"] is True
    assert isinstance(payload["pipewire_visible"], bool)


def test_as_dict_is_json_serialisable() -> None:
    import json

    for name in ("host-baseline", "two-arrays", "respeaker"):
        for device in enumerate_devices(root=tree(name)):
            json.loads(json.dumps(device.as_dict()))


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "selector, serial",
    [
        ("usb-Pollen_Robotics_Reachy_Mini_Audio_RM0001", "RM0001"),
        ("usb-Pollen_Robotics_Reachy_Mini_Audio_RM0002", "RM0002"),
        ("RM0001", "RM0001"),
        ("RM0002", "RM0002"),
        ("Audio_1", "RM0002"),
    ],
)
def test_resolve_selects_by_stable_id_serial_and_card_id(selector: str, serial: str) -> None:
    device = resolve(selector, root=tree("two-arrays"))
    assert device.serial == serial


def test_resolve_by_label_is_unambiguous_on_a_single_device_host() -> None:
    device = resolve("Reachy Mini Audio", root=tree("host-baseline"))
    assert device.serial == "RM0001"


def test_resolve_ambiguous_label_lists_every_candidate() -> None:
    root = tree("two-arrays")
    with pytest.raises(CliError) as excinfo:
        resolve("Reachy Mini Audio", root=root)
    error = excinfo.value
    assert error.code == EXIT_USER_ERROR
    assert "usb-Pollen_Robotics_Reachy_Mini_Audio_RM0001" in error.message
    assert "usb-Pollen_Robotics_Reachy_Mini_Audio_RM0002" in error.message
    assert error.remediation


def test_resolve_unknown_selector_is_a_user_error() -> None:
    root = tree("two-arrays")
    with pytest.raises(CliError) as excinfo:
        resolve("no-such-microphone", root=root)
    assert excinfo.value.code == EXIT_USER_ERROR


def test_resolve_empty_selector_is_a_user_error() -> None:
    root = tree("two-arrays")
    with pytest.raises(CliError) as excinfo:
        resolve("   ", root=root)
    assert excinfo.value.code == EXIT_USER_ERROR


@pytest.mark.parametrize("selector", ["hw:1", "hw:0,0", "1", "0", "plughw:1"])
def test_resolve_refuses_raw_card_number_selectors(selector: str) -> None:
    root = tree("two-arrays")
    with pytest.raises(CliError) as excinfo:
        resolve(selector, root=root)
    error = excinfo.value
    assert error.code == EXIT_USER_ERROR
    assert "stable" in error.message.lower() or "stable" in error.remediation.lower()
    assert "usb-" in error.remediation


def test_refusal_names_the_owning_device_when_the_card_number_exists() -> None:
    root = tree("two-arrays")
    with pytest.raises(CliError) as excinfo:
        resolve("hw:1", root=root)
    assert "usb-Pollen_Robotics_Reachy_Mini_Audio_RM0002" in excinfo.value.remediation


# ---------------------------------------------------------------------------
# import surface
# ---------------------------------------------------------------------------


def test_module_imports_with_no_other_imports() -> None:
    """``from microphone_cli.devices import enumerate_devices, resolve`` stands alone."""
    completed = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-c", "from microphone_cli.devices import enumerate_devices, resolve"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
