"""Tests for ``microphone-cli record`` — hardware-free by construction.

Same posture as ``tests/test_stream.py``: a synthetic device tree, an autouse
booby trap on :func:`subprocess.Popen` / :func:`subprocess.run`, the activation
log redirected into ``tmp_path``, and the ``--apply`` path driven entirely
through this module's ``_spawn`` / ``_sleep`` / ``_monotonic`` seams so the
duration and max-bytes bounds are exercised without a real recorder.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - only ever monkeypatched into a booby trap here

import pytest

from microphone_cli import access, activation, engine
from microphone_cli.cli import _CliArgumentParser, _dispatch
from microphone_cli.cli._commands import record

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
BASELINE = os.path.join(FIXTURES, "host-baseline")
SELECTOR = "Audio"


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def _trap(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"subprocess spawned in a test: {args!r} {kwargs!r}")

    monkeypatch.setattr(subprocess, "Popen", _trap)
    monkeypatch.setattr(subprocess, "run", _trap)


@pytest.fixture(autouse=True)
def _activation_log(monkeypatch: pytest.MonkeyPatch, tmp_path) -> str:
    path = str(tmp_path / "activation.jsonl")
    monkeypatch.setenv(activation.ENV_LOG_PATH, path)
    return path


def run(argv: list[str]) -> int:
    parser = _CliArgumentParser(prog="microphone-cli")
    sub = parser.add_subparsers(dest="command", parser_class=_CliArgumentParser)
    record.register(sub)
    args = parser.parse_args(argv)
    return _dispatch(args)


def payload(capsys: pytest.CaptureFixture[str]) -> dict:
    return json.loads(capsys.readouterr().out)


def available_capability() -> engine.Capability:
    return engine.Capability(
        gst_launch="/usr/bin/gst-launch-1.0",
        gst_inspect="/usr/bin/gst-inspect-1.0",
        plugins=dict.fromkeys(engine.ALL_ELEMENTS, True),
        available=True,
    )


def ok_report(path: str) -> access.AccessReport:
    return access.AccessReport(path=path, kind="audio", state=access.AccessState.OK, remediation="")


class FakeProc:
    """Popen-like: writes ``chunk`` bytes to ``path`` on each poll, then exits.

    ``exit_after`` polls of ``None`` are returned before ``returncode`` is
    reported; ``None`` means "never exits on its own", which is how the
    duration and max-bytes bounds get exercised.
    """

    def __init__(
        self,
        path: str,
        *,
        chunk: int = 16,
        exit_after: int | None = 2,
        rc: int = 0,
    ) -> None:
        self.pid = 4242
        self.path = path
        self.chunk = chunk
        self.exit_after = exit_after
        self.rc = rc
        self.polls = 0
        self.terminated = False
        self.waited = False
        self._returncode: int | None = None

    def _write(self) -> None:
        with open(self.path, "ab") as handle:
            handle.write(b"\0" * self.chunk)

    def poll(self) -> int | None:
        self.polls += 1
        self._write()
        if self.exit_after is not None and self.polls > self.exit_after:
            self._returncode = self.rc
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True
        self._returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        self._returncode = 0 if self._returncode is None else self._returncode
        return self._returncode


def arm_apply(
    monkeypatch: pytest.MonkeyPatch, proc: FakeProc, *, ticks: float = 1.0
) -> list[float]:
    """Wire every seam --apply uses; return the list sleeps were recorded into."""
    slept: list[float] = []
    clock = {"t": 0.0}

    def _monotonic() -> float:
        return clock["t"]

    def _sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["t"] += ticks

    monkeypatch.setattr(engine, "detect", available_capability)
    monkeypatch.setattr(access, "check_access", lambda path, kind: ok_report(path))
    monkeypatch.setattr(record, "_spawn", lambda argv: proc)
    monkeypatch.setattr(record, "_monotonic", _monotonic)
    monkeypatch.setattr(record, "_sleep", _sleep)
    return slept


def base_argv(output: str, *extra: str) -> list[str]:
    return ["record", SELECTOR, output, "--root", BASELINE, *extra]


# ---------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------


def test_dry_run_prints_the_pipeline_and_writes_nothing(
    tmp_path, capsys: pytest.CaptureFixture[str], _activation_log: str
) -> None:
    out_path = str(tmp_path / "clip.mka")
    assert run(base_argv(out_path)) == 0
    out = capsys.readouterr().out
    assert "gst-launch-1.0" in out
    assert not os.path.exists(out_path)
    assert not os.path.exists(_activation_log)


def test_dry_run_json_reports_no_hardware_and_no_engine_check(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_path = str(tmp_path / "clip.mka")
    assert run(base_argv(out_path, "--json")) == 0
    data = payload(capsys)
    assert set(data) == set(record.PAYLOAD_KEYS)
    assert data["mode"] == "dry-run"
    assert data["apply"] is False
    assert data["kind"] == "audio"
    assert data["hardware_touched"] is False
    assert data["engine_checked"] is False
    assert data["output_path"] == out_path
    assert data["would_write"] == [out_path]
    assert data["audio_address"] == "hw:CARD=Audio"
    assert data["capture_node"].endswith("dev/snd/pcmC1D0c")
    assert data["pipeline_preview"][:2] == ["gst-launch-1.0", "-e"]
    assert "queue" in data["pipeline_preview"]
    assert "matroskamux" in data["pipeline_preview"]
    assert data["engine"]["checked"] is False
    assert data["access"]["checked"] is False
    assert data["access"]["state"] == "absent"
    assert data["bound"] == {
        "duration_s": 30.0,
        "max_bytes": record.DEFAULT_MAX_BYTES,
        "unbounded_is_impossible": True,
    }
    assert data["warmup_s"] == 0.0
    assert data["warmup_basis"]
    assert data["timestamps"]["resolved_at"]


def test_wav_extension_selects_the_wav_container(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(base_argv(str(tmp_path / "clip.wav"), "--json")) == 0
    data = payload(capsys)
    assert data["container"] == "wav"
    assert "wavenc" in data["pipeline_preview"]
    assert "matroskamux" not in data["pipeline_preview"]


def test_unknown_extension_is_a_user_error(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(base_argv(str(tmp_path / "clip.mp3"))) == 1
    assert "error:" in capsys.readouterr().err


def test_missing_parent_directory_is_a_user_error(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(base_argv(str(tmp_path / "nope" / "clip.mka"))) == 1
    assert "error:" in capsys.readouterr().err


def test_existing_file_is_refused_unless_overwrite(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_path = tmp_path / "clip.mka"
    out_path.write_bytes(b"old")
    assert run(base_argv(str(out_path))) == 1
    assert "error:" in capsys.readouterr().err
    assert run(base_argv(str(out_path), "--json", "--overwrite")) == 0
    assert payload(capsys)["mode"] == "dry-run"
    # A dry run never touches the existing artifact, --overwrite or not.
    assert out_path.read_bytes() == b"old"


@pytest.mark.parametrize("duration", ["0", "-1", "3601"])
def test_out_of_range_duration_is_a_user_error(
    tmp_path, capsys: pytest.CaptureFixture[str], duration: str
) -> None:
    assert run(base_argv(str(tmp_path / "clip.mka"), "--duration", duration)) == 1
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("max_bytes", ["0", "4294967297"])
def test_out_of_range_max_bytes_is_a_user_error(
    tmp_path, capsys: pytest.CaptureFixture[str], max_bytes: str
) -> None:
    assert run(base_argv(str(tmp_path / "clip.mka"), "--max-bytes", max_bytes)) == 1
    assert "error:" in capsys.readouterr().err


def test_duration_bounds_the_pipeline_itself(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(base_argv(str(tmp_path / "clip.mka"), "--json", "--duration", "5")) == 0
    data = payload(capsys)
    assert data["bound"]["duration_s"] == 5.0
    assert any(token.startswith("num-buffers=") for token in data["pipeline_preview"])


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


def test_probe_checks_the_engine_but_spawns_nothing(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _activation_log: str,
) -> None:
    monkeypatch.setattr(engine, "detect", available_capability)
    out_path = str(tmp_path / "clip.mka")
    assert run(base_argv(out_path, "--json", "--probe")) == 0
    data = payload(capsys)
    assert data["mode"] == "probe"
    assert data["apply"] is False
    assert data["hardware_touched"] is False
    assert data["engine_checked"] is True
    assert data["engine"]["checked"] is True
    assert data["engine"]["available"] is True
    assert data["access"]["checked"] is True
    assert data["access"]["state"] == "absent"
    assert not os.path.exists(out_path)
    assert not os.path.exists(_activation_log)


def test_probe_without_the_engine_exits_two(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        engine,
        "detect",
        lambda: engine.Capability(
            gst_launch=None,
            gst_inspect=None,
            plugins=dict.fromkeys(engine.ALL_ELEMENTS, False),
            available=False,
        ),
    )
    assert run(base_argv(str(tmp_path / "clip.mka"), "--probe")) == 2
    assert "error:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def test_apply_records_until_eos_and_logs_the_activation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _activation_log: str,
) -> None:
    out_path = str(tmp_path / "clip.mka")
    proc = FakeProc(out_path, chunk=8, exit_after=2)
    arm_apply(monkeypatch, proc)

    assert run(base_argv(out_path, "--json", "--apply", "--duration", "5")) == 0
    data = payload(capsys)
    assert data["mode"] == "apply"
    assert data["apply"] is True
    assert data["hardware_touched"] is True
    assert data["engine_checked"] is True
    assert data["stopped_reason"] == "eos"
    assert data["bytes_written"] == os.stat(out_path).st_size
    assert data["bytes_written"] > 0
    assert data["pipeline"][:2] == ["gst-launch-1.0", "-e"]
    assert data["timestamps"]["started_at"] and data["timestamps"]["ended_at"]

    # It wrote to the named path and nowhere else in the directory.
    assert sorted(os.listdir(tmp_path)) == ["activation.jsonl", "clip.mka"]

    lines = open(_activation_log, encoding="utf-8").read().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["verb"] == "record"
    assert entry["device"] == "usb-Pollen_Robotics_Reachy_Mini_Audio_RM0001"
    assert entry["params"]["output_path"] == out_path
    assert entry["params"]["stopped_reason"] == "eos"
    assert entry["params"]["bytes_written"] == data["bytes_written"]
    assert entry["ended_at"]


def test_apply_stops_on_the_duration_bound(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out_path = str(tmp_path / "clip.wav")
    proc = FakeProc(out_path, chunk=4, exit_after=None)
    arm_apply(monkeypatch, proc, ticks=1.0)

    assert run(base_argv(out_path, "--json", "--apply", "--duration", "3")) == 0
    data = payload(capsys)
    assert data["stopped_reason"] == "duration"
    assert proc.terminated is True
    assert proc.waited is True
    assert data["bytes_written"] > 0


def test_apply_stops_on_the_max_bytes_bound(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _activation_log: str,
) -> None:
    out_path = str(tmp_path / "clip.wav")
    proc = FakeProc(out_path, chunk=64, exit_after=None)
    arm_apply(monkeypatch, proc)

    argv = base_argv(out_path, "--json", "--apply", "--duration", "600", "--max-bytes", "100")
    assert run(argv) == 0
    data = payload(capsys)
    assert data["stopped_reason"] == "max_bytes"
    assert proc.terminated is True
    assert data["bytes_written"] >= 100
    entry = json.loads(open(_activation_log, encoding="utf-8").read().strip())
    assert entry["params"]["stopped_reason"] == "max_bytes"


def test_apply_reports_a_failed_pipeline_as_an_environment_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out_path = str(tmp_path / "clip.mka")
    proc = FakeProc(out_path, chunk=4, exit_after=1, rc=1)
    arm_apply(monkeypatch, proc)
    assert run(base_argv(out_path, "--apply", "--duration", "5")) == 2
    assert "error:" in capsys.readouterr().err


def test_apply_on_a_busy_device_exits_three(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out_path = str(tmp_path / "clip.mka")
    monkeypatch.setattr(engine, "detect", available_capability)
    monkeypatch.setattr(
        access,
        "check_access",
        lambda path, kind: access.AccessReport(
            path=path,
            kind=kind,
            state=access.AccessState.BUSY,
            remediation="stop the holder",
            holder=access.Holder(pid=7, command="arecord"),
        ),
    )
    monkeypatch.setattr(record, "_spawn", lambda argv: pytest.fail("spawned despite busy"))
    assert run(base_argv(out_path, "--apply")) == 3
    assert "busy" in capsys.readouterr().err
    assert not os.path.exists(out_path)


def test_apply_without_the_engine_exits_two(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        engine,
        "detect",
        lambda: engine.Capability(
            gst_launch=None,
            gst_inspect=None,
            plugins=dict.fromkeys(engine.ALL_ELEMENTS, False),
            available=False,
        ),
    )
    monkeypatch.setattr(record, "_spawn", lambda argv: pytest.fail("spawned without an engine"))
    assert run(base_argv(str(tmp_path / "clip.mka"), "--apply")) == 2
    assert "error:" in capsys.readouterr().err


def test_apply_requires_the_container_elements(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plugins = dict.fromkeys(engine.ALL_ELEMENTS, True)
    plugins["matroskamux"] = False
    monkeypatch.setattr(
        engine,
        "detect",
        lambda: engine.Capability(
            gst_launch="/usr/bin/gst-launch-1.0",
            gst_inspect="/usr/bin/gst-inspect-1.0",
            plugins=plugins,
            available=True,
        ),
    )
    monkeypatch.setattr(access, "check_access", lambda path, kind: ok_report(path))
    monkeypatch.setattr(record, "_spawn", lambda argv: pytest.fail("spawned without matroskamux"))
    assert run(base_argv(str(tmp_path / "clip.mka"), "--apply")) == 2
    assert "matroskamux" in capsys.readouterr().err


def test_apply_that_produces_no_artifact_is_an_environment_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out_path = str(tmp_path / "clip.mka")
    proc = FakeProc(out_path, chunk=0, exit_after=1)

    def _no_write() -> None:
        return None

    monkeypatch.setattr(proc, "_write", _no_write)
    arm_apply(monkeypatch, proc)
    assert run(base_argv(out_path, "--apply", "--duration", "5")) == 2
    assert "error:" in capsys.readouterr().err
