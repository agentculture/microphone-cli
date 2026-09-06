"""Tests for microphone_cli.access — hardware-free, no real /dev or /proc access.

Every scenario monkeypatches ``os.open`` (never opens a real device node) and,
for the BUSY/holder-lookup path, monkeypatches the module's private
``/proc`` helpers against a fake directory tree built under ``tmp_path``.
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from microphone_cli import access
from microphone_cli.cli._errors import (
    EXIT_BUSY_ERROR,
    EXIT_ENV_ERROR,
    EXIT_USER_ERROR,
    CliError,
)


def _fake_proc_tree(tmp_path: Path, pid: int, command: str, target: str) -> Path:
    """Build a fake /proc/<pid>/{fd/0 -> target, comm} tree under tmp_path."""
    proc_dir = tmp_path / "proc"
    fd_dir = proc_dir / str(pid) / "fd"
    fd_dir.mkdir(parents=True)
    (fd_dir / "3").symlink_to(target)
    (proc_dir / str(pid) / "comm").write_text(f"{command}\n", encoding="utf-8")
    return proc_dir


def _patch_proc(monkeypatch: pytest.MonkeyPatch, proc_dir: Path) -> None:
    def _list_proc_pids() -> list[str]:
        return [p.name for p in proc_dir.iterdir()]

    def _list_fds(pid: str) -> list[str]:
        return [p.name for p in (proc_dir / pid / "fd").iterdir()]

    def _readlink(fd_path: str) -> str:
        # fd_path looks like "/proc/<pid>/fd/<n>"; rebase onto proc_dir.
        rel = Path(fd_path).relative_to("/proc")
        import os as _os

        return _os.readlink(proc_dir / rel)

    def _read_command(pid: str) -> str:
        return (proc_dir / pid / "comm").read_text(encoding="utf-8").strip()

    monkeypatch.setattr(access, "_list_proc_pids", _list_proc_pids)
    monkeypatch.setattr(access, "_list_fds", _list_fds)
    monkeypatch.setattr(access, "_readlink", _readlink)
    monkeypatch.setattr(access, "_read_command", _read_command)


# --------------------------------------------------------------------------- #
# check_access — the four states
# --------------------------------------------------------------------------- #


def test_check_access_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access.os, "open", lambda path, flags: 7)
    monkeypatch.setattr(access.os, "close", lambda fd: None)

    report = access.check_access("/dev/snd/pcmC0D0c", "audio")

    assert report.state is access.AccessState.OK
    assert report.remediation == ""
    assert report.holder is None


def test_check_access_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(path: str, flags: int) -> int:
        raise FileNotFoundError(errno.ENOENT, "No such file or directory")

    monkeypatch.setattr(access.os, "open", _raise)

    report = access.check_access("/dev/snd/pcmC9D9c", "audio")

    assert report.state is access.AccessState.ABSENT
    assert "does not exist" in report.remediation
    assert report.holder is None


def test_check_access_forbidden_audio_names_audio_group(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(path: str, flags: int) -> int:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(access.os, "open", _raise)

    report = access.check_access("/dev/snd/pcmC0D0c", "audio")

    assert report.state is access.AccessState.FORBIDDEN
    assert "'audio' group" in report.remediation


def test_check_access_forbidden_usb_names_udev_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(path: str, flags: int) -> int:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(access.os, "open", _raise)

    report = access.check_access("/dev/bus/usb/001/004", "usb")

    assert report.state is access.AccessState.FORBIDDEN
    assert 'SUBSYSTEM=="usb", ATTR{idVendor}==' in report.remediation


def test_check_access_busy_with_identified_holder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = "/dev/snd/pcmC0D0c"
    proc_dir = _fake_proc_tree(tmp_path, pid=4242, command="arecord", target=target)
    _patch_proc(monkeypatch, proc_dir)
    monkeypatch.setattr(access.os.path, "realpath", lambda path: path)

    def _raise(path: str, flags: int) -> int:
        raise OSError(errno.EBUSY, "Device or resource busy")

    monkeypatch.setattr(access.os, "open", _raise)

    report = access.check_access(target, "audio")

    assert report.state is access.AccessState.BUSY
    assert report.holder == access.Holder(pid=4242, command="arecord")
    assert "arecord" in report.remediation
    assert "4242" in report.remediation


def test_check_access_busy_holder_unknown_degrades_gracefully(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # No fake /proc entries at all -> find_holder degrades to None rather
    # than raising or hanging.
    proc_dir = tmp_path / "proc"
    proc_dir.mkdir()
    _patch_proc(monkeypatch, proc_dir)
    monkeypatch.setattr(access.os.path, "realpath", lambda path: path)

    def _raise(path: str, flags: int) -> int:
        raise OSError(errno.EBUSY, "Device or resource busy")

    monkeypatch.setattr(access.os, "open", _raise)

    report = access.check_access("/dev/bus/usb/001/004", "usb")

    assert report.state is access.AccessState.BUSY
    assert report.holder is None
    assert "could not be identified" in report.remediation


def test_check_access_vanished_device_reports_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(path: str, flags: int) -> int:
        raise OSError(errno.ENODEV, "No such device")

    monkeypatch.setattr(access.os, "open", _raise)

    report = access.check_access("/dev/bus/usb/001/004", "usb")

    assert report.state is access.AccessState.ABSENT
    assert "unplugged" in report.remediation


def test_check_access_invalid_kind_raises() -> None:
    with pytest.raises(ValueError):
        access.check_access("/dev/snd/pcmC0D0c", "video")


# --------------------------------------------------------------------------- #
# require_access / access_error — exit-code mapping
# --------------------------------------------------------------------------- #


def test_require_access_ok_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access.os, "open", lambda path, flags: 7)
    monkeypatch.setattr(access.os, "close", lambda fd: None)

    access.require_access("/dev/snd/pcmC0D0c", "audio")  # no raise


def test_require_access_absent_maps_to_exit_user_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(path: str, flags: int) -> int:
        raise FileNotFoundError(errno.ENOENT, "No such file or directory")

    monkeypatch.setattr(access.os, "open", _raise)

    with pytest.raises(CliError) as exc:
        access.require_access("/dev/snd/pcmC9D9c", "audio")

    assert exc.value.code == EXIT_USER_ERROR == 1


def test_require_access_forbidden_maps_to_exit_env_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(path: str, flags: int) -> int:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(access.os, "open", _raise)

    with pytest.raises(CliError) as exc:
        access.require_access("/dev/snd/pcmC0D0c", "audio")

    assert exc.value.code == EXIT_ENV_ERROR == 2


def test_require_access_busy_maps_to_exit_busy_error_with_holder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = "/dev/bus/usb/001/004"
    proc_dir = _fake_proc_tree(tmp_path, pid=99, command="micctl", target=target)
    _patch_proc(monkeypatch, proc_dir)
    monkeypatch.setattr(access.os.path, "realpath", lambda path: path)

    def _raise(path: str, flags: int) -> int:
        raise OSError(errno.EBUSY, "Device or resource busy")

    monkeypatch.setattr(access.os, "open", _raise)

    with pytest.raises(CliError) as exc:
        access.require_access(target, "usb")

    assert exc.value.code == EXIT_BUSY_ERROR == 3
    assert "micctl" in exc.value.message
    assert "99" in exc.value.message


def test_access_error_ok_report_is_a_programming_error() -> None:
    report = access.AccessReport(
        path="/dev/snd/pcmC0D0c", kind="audio", state=access.AccessState.OK, remediation=""
    )
    with pytest.raises(ValueError):
        access.access_error(report)


def test_busy_error_looks_up_holder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = "/dev/snd/pcmC0D0c"
    proc_dir = _fake_proc_tree(tmp_path, pid=55, command="pulseaudio", target=target)
    _patch_proc(monkeypatch, proc_dir)
    monkeypatch.setattr(access.os.path, "realpath", lambda path: path)

    err = access.busy_error(target, "audio")

    assert err.code == EXIT_BUSY_ERROR
    assert "pulseaudio" in err.message
    assert "55" in err.message


def test_find_holder_returns_none_when_proc_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_listdir() -> list[str]:
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(access, "_list_proc_pids", lambda: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(access.os.path, "realpath", lambda path: path)

    assert access.find_holder("/dev/snd/pcmC0D0c") is None
