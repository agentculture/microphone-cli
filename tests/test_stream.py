"""Tests for ``microphone-cli stream audio`` — hardware-free by construction.

Every test runs against the synthetic ``tests/fixtures/host-baseline`` tree,
an autouse fixture booby-traps :func:`subprocess.Popen` / :func:`subprocess.run`
so a stray real spawn is an immediate failure, and the activation log is
redirected into ``tmp_path``. No ALSA node is ever opened: the capture node
under a fixture root simply does not exist, which is exactly the ``absent``
state the descriptive payloads report.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # nosec B404 - only ever monkeypatched into a booby trap here

import pytest

from microphone_cli import access, activation, engine
from microphone_cli.cli import _CliArgumentParser, _dispatch
from microphone_cli.cli._commands import stream

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
BASELINE = os.path.join(FIXTURES, "host-baseline")
SELECTOR = "Audio"


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any real subprocess spawn from a test is a bug, not a slow test."""

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
    """Parse and dispatch ``argv`` through this noun's own parser.

    The noun is not wired into ``cli._build_parser()`` yet (that is a separate
    task's file), so the test builds the same parser shape by hand — same
    ``parser_class``, same ``_dispatch``, therefore the same error contract.
    """
    parser = _CliArgumentParser(prog="microphone-cli")
    sub = parser.add_subparsers(dest="command", parser_class=_CliArgumentParser)
    stream.register(sub)
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
    """A Popen-like stand-in: it has a pid and can be stopped, nothing else."""

    def __init__(self) -> None:
        self.pid = 4242
        self.terminated = False
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0
        return 0


def base_argv(*extra: str) -> list[str]:
    return ["stream", "audio", SELECTOR, "--root", BASELINE, *extra]


# ---------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------


def test_dry_run_prints_the_pipeline_and_touches_nothing(
    capsys: pytest.CaptureFixture[str], _activation_log: str
) -> None:
    assert run(base_argv()) == 0
    out = capsys.readouterr().out
    assert "gst-launch-1.0" in out
    assert "udpsink" in out
    assert not os.path.exists(_activation_log)


def test_dry_run_json_reports_no_hardware_and_no_engine_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(base_argv("--json")) == 0
    data = payload(capsys)
    assert data["verb"] == "stream audio"
    assert data["medium"] == "audio"
    assert data["mode"] == "dry-run"
    assert data["applied"] is False
    assert data["probed"] is False
    assert data["hardware_touched"] is False
    assert data["engine_checked"] is False
    assert data["pid"] is None
    assert data["started_at"] is None
    assert data["pipeline"][:2] == ["gst-launch-1.0", "-e"]
    assert "queue" in data["pipeline"]
    assert data["pipeline_str"].startswith("gst-launch-1.0 -e ")
    assert data["bounded"] is False


def test_dry_run_payload_carries_every_documented_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(base_argv("--json")) == 0
    data = payload(capsys)
    assert set(data) == set(stream.PAYLOAD_KEYS)


def test_dry_run_request_and_defaults(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(base_argv("--json")) == 0
    request = payload(capsys)["request"]
    # No --rate/--channels/--format given: the request is filled from what the
    # fixture device advertises in stream0 (found on hardware: a fixed 48 kHz
    # mono default could not open a 16 kHz stereo array).
    assert request == {
        "rate": 48000,
        "channels": 6,
        "sample_format": "S32LE",
        "format_source": {
            "rate": "advertised",
            "channels": "advertised",
            "sample_format": "advertised",
        },
        "encode": "passthrough",
        "host": "127.0.0.1",
        "port": 5000,
    }


def test_explicit_format_flags_override_the_advertised_ones(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(base_argv("--json", "--rate", "16000", "--channels", "1")) == 0
    request = payload(capsys)["request"]
    assert (request["rate"], request["channels"], request["sample_format"]) == (
        16000,
        1,
        "S32LE",
    )
    assert request["format_source"] == {
        "rate": "explicit",
        "channels": "explicit",
        "sample_format": "advertised",
    }


def test_dry_run_honours_the_audio_flags(capsys: pytest.CaptureFixture[str]) -> None:
    argv = base_argv(
        "--json", "--rate", "16000", "--channels", "6", "--format", "S32LE", "--port", "6001"
    )
    assert run(argv) == 0
    data = payload(capsys)
    assert data["request"]["rate"] == 16000
    assert data["request"]["channels"] == 6
    assert "audio/x-raw,format=S32LE,rate=16000,channels=6" in data["pipeline"]
    assert "port=6001" in data["pipeline"]


def test_opus_encode_changes_the_payloader(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(base_argv("--json", "--encode", "opus")) == 0
    data = payload(capsys)
    assert "rtpopuspay" in data["pipeline"]
    assert "opusenc" in data["pipeline"]
    assert data["attach"]["encode"] == "opus"


def test_attach_announces_both_receive_pipelines(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(base_argv("--json")) == 0
    attach = payload(capsys)["attach"]
    assert attach["transport"] == "udp"
    assert attach["port"] == 5000
    consumer = attach["consumer"]
    assert "udpsrc" in consumer["passthrough"]
    assert "rtpL16depay" in consumer["passthrough"]
    assert "rtpopusdepay" in consumer["opus"]


def test_dry_run_access_is_reported_not_enforced(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(base_argv("--json")) == 0
    data = payload(capsys)
    assert data["access"]["checked"] is False
    assert data["access"]["state"] == "absent"
    assert data["source"]["capture_node"].endswith("dev/snd/pcmC1D0c")


def test_unknown_device_is_a_user_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["stream", "audio", "nope-not-here", "--root", BASELINE]) == 1
    assert "error:" in capsys.readouterr().err


def test_bad_port_is_a_user_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(base_argv("--port", "0")) == 1
    assert "error:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


def test_probe_checks_the_engine_but_spawns_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _activation_log: str
) -> None:
    monkeypatch.setattr(engine, "detect", available_capability)
    assert run(base_argv("--json", "--probe")) == 0
    data = payload(capsys)
    assert data["mode"] == "probe"
    assert data["probed"] is True
    assert data["applied"] is False
    assert data["hardware_touched"] is False
    assert data["engine_checked"] is True
    assert data["pid"] is None
    assert data["access"]["checked"] is True
    assert data["access"]["state"] == "absent"
    assert not os.path.exists(_activation_log)


def test_probe_without_the_engine_is_an_environment_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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
    assert run(base_argv("--probe")) == 2
    assert "error:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def _arm_apply(monkeypatch: pytest.MonkeyPatch) -> FakeProc:
    proc = FakeProc()
    monkeypatch.setattr(engine, "detect", available_capability)
    monkeypatch.setattr(access, "check_access", lambda path, kind: ok_report(path))
    monkeypatch.setattr(stream, "_spawn", lambda argv: proc)
    return proc


def test_apply_spawns_and_reports_the_pid(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], _activation_log: str
) -> None:
    proc = _arm_apply(monkeypatch)
    assert run(base_argv("--json", "--apply")) == 0
    data = payload(capsys)
    assert data["mode"] == "apply"
    assert data["applied"] is True
    assert data["hardware_touched"] is True
    assert data["engine_checked"] is True
    assert data["pid"] == proc.pid
    assert data["started_at"]
    assert data["access"]["state"] == "ok"

    lines = open(_activation_log, encoding="utf-8").read().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["verb"] == "stream audio"
    assert record["device"] == "usb-Pollen_Robotics_Reachy_Mini_Audio_RM0001"
    assert record["params"]["port"] == 5000
    assert record["params"]["pid"] == proc.pid
    assert record["ended_at"]


def test_apply_passes_the_built_argv_to_the_spawn_seam(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[list[str]] = []
    proc = FakeProc()
    monkeypatch.setattr(engine, "detect", available_capability)
    monkeypatch.setattr(access, "check_access", lambda path, kind: ok_report(path))
    monkeypatch.setattr(stream, "_spawn", lambda argv: seen.append(list(argv)) or proc)
    assert run(base_argv("--json", "--apply")) == 0
    assert seen and seen[0][:2] == ["gst-launch-1.0", "-e"]
    assert seen[0] == payload(capsys)["pipeline"]


def test_apply_on_a_busy_device_exits_three(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(engine, "detect", available_capability)
    monkeypatch.setattr(
        access,
        "check_access",
        lambda path, kind: access.AccessReport(
            path=path,
            kind=kind,
            state=access.AccessState.BUSY,
            remediation="stop the holder",
            holder=access.Holder(pid=11, command="arecord"),
        ),
    )
    monkeypatch.setattr(stream, "_spawn", lambda argv: pytest.fail("spawned despite busy"))
    assert run(base_argv("--apply")) == 3
    assert "busy" in capsys.readouterr().err


def test_apply_without_the_engine_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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
    monkeypatch.setattr(stream, "_spawn", lambda argv: pytest.fail("spawned without an engine"))
    assert run(base_argv("--apply")) == 2
    assert "error:" in capsys.readouterr().err


def test_apply_with_opus_requires_the_optional_elements(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plugins = dict.fromkeys(engine.ALL_ELEMENTS, True)
    plugins["opusenc"] = False
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
    monkeypatch.setattr(stream, "_spawn", lambda argv: pytest.fail("spawned without opusenc"))
    assert run(base_argv("--apply", "--encode", "opus")) == 2
    assert "opusenc" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------


def test_bare_noun_prints_its_overview(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["stream"]) == 0
    assert "stream" in capsys.readouterr().out


def test_overview_json_has_sections(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["stream", "overview", "--json"]) == 0
    data = payload(capsys)
    assert data["subject"] == "microphone stream"
    assert data["sections"]


def test_register_is_argparse_shaped() -> None:
    parser = _CliArgumentParser(prog="microphone-cli")
    sub = parser.add_subparsers(dest="command", parser_class=_CliArgumentParser)
    stream.register(sub)
    assert isinstance(sub, argparse._SubParsersAction)


def test_passthrough_consumer_caps_follow_the_negotiated_format(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Found on hardware: the announced L16 consumer said clock-rate=48000 for a
    # 16 kHz stereo stream and decoded at the wrong speed.
    assert run(base_argv("--json", "--rate", "16000", "--channels", "2")) == 0
    consumer = payload(capsys)["attach"]["consumer"]
    assert "clock-rate=16000" in consumer["passthrough"]
    assert "encoding-params=2" in consumer["passthrough"]
    assert "clock-rate=48000" in consumer["opus"]
