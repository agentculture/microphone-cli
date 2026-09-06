"""Tests for microphone_cli.engine: capability detection and argv builders.

Audio subset cited from webcam-cli/webcam_cli/engine.py (see module
docstring in microphone_cli/engine.py for exact provenance). Never launches
gst-launch-1.0 against a real device: subprocess.run and shutil.which are
monkeypatched throughout.
"""

from __future__ import annotations

import subprocess

import pytest

from microphone_cli import engine
from microphone_cli.cli._errors import CliError

# --- detect() / require_engine() / require_elements() ------------------------


def test_detect_reports_unavailable_when_gst_launch_missing(monkeypatch):
    monkeypatch.setattr(engine.shutil, "which", lambda _name: None)

    cap = engine.detect()

    assert cap == engine.Capability(
        gst_launch=None,
        gst_inspect=None,
        plugins=dict.fromkeys(engine.ALL_ELEMENTS, False),
        available=False,
    )


def test_detect_never_raises_when_gst_inspect_probe_errors(monkeypatch):
    def fake_which(name):
        return f"/usr/bin/{name}" if name in (engine.GST_LAUNCH, engine.GST_INSPECT) else None

    def fake_run(argv, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(engine.shutil, "which", fake_which)
    monkeypatch.setattr(engine.subprocess, "run", fake_run)

    cap = engine.detect()

    assert cap.available is False
    assert all(present is False for present in cap.plugins.values())


def test_detect_available_when_gst_launch_and_core_elements_present(monkeypatch):
    def fake_which(name):
        return f"/usr/bin/{name}" if name in (engine.GST_LAUNCH, engine.GST_INSPECT) else None

    def fake_run(argv, **kwargs):
        if argv[1:] == ["--help"]:
            return subprocess.CompletedProcess(argv, 0, stdout="--exists  Check if element exists")
        # argv = [gst_inspect, "--exists", element]
        element = argv[2]
        returncode = 0 if element in engine.CORE_ELEMENTS else 1
        return subprocess.CompletedProcess(argv, returncode)

    monkeypatch.setattr(engine.shutil, "which", fake_which)
    monkeypatch.setattr(engine.subprocess, "run", fake_run)

    cap = engine.detect()

    assert cap.available is True
    assert cap.gst_launch == "/usr/bin/gst-launch-1.0"
    for element in engine.CORE_ELEMENTS:
        assert cap.plugins[element] is True


def test_detect_uses_plain_form_when_exists_flag_unsupported(monkeypatch):
    def fake_which(name):
        return f"/usr/bin/{name}" if name in (engine.GST_LAUNCH, engine.GST_INSPECT) else None

    seen_argvs = []

    def fake_run(argv, **kwargs):
        seen_argvs.append(argv)
        if argv[1:] == ["--help"]:
            return subprocess.CompletedProcess(argv, 0, stdout="no exists flag here")
        # plain form: [gst_inspect, element]
        element = argv[1]
        returncode = 0 if element in engine.CORE_ELEMENTS else 1
        return subprocess.CompletedProcess(argv, returncode)

    monkeypatch.setattr(engine.shutil, "which", fake_which)
    monkeypatch.setattr(engine.subprocess, "run", fake_run)

    cap = engine.detect()

    assert cap.available is True
    element_probe_argvs = [argv for argv in seen_argvs if argv[1:] != ["--help"]]
    assert all(len(argv) == 2 for argv in element_probe_argvs)


def test_require_engine_raises_env_error_naming_apt_packages(monkeypatch):
    monkeypatch.setattr(engine.shutil, "which", lambda _name: None)

    with pytest.raises(CliError) as excinfo:
        engine.require_engine()

    err = excinfo.value
    assert err.code == 2
    assert engine.GST_LAUNCH in err.message
    assert "apt install" in err.remediation
    for package in (
        engine.GST_TOOLS_PACKAGE,
        engine.GST_PLUGINS_BASE_PACKAGE,
        engine.GST_PLUGINS_GOOD_PACKAGE,
        engine.GST_ALSA_PACKAGE,
    ):
        assert package in err.remediation


def test_require_engine_names_missing_elements_when_gst_launch_present(monkeypatch):
    def fake_which(name):
        return f"/usr/bin/{name}" if name in (engine.GST_LAUNCH, engine.GST_INSPECT) else None

    def fake_run(argv, **kwargs):
        if argv[1:] == ["--help"]:
            return subprocess.CompletedProcess(argv, 0, stdout="--exists")
        element = argv[2]
        returncode = 0 if element != "queue" else 1
        return subprocess.CompletedProcess(argv, returncode)

    monkeypatch.setattr(engine.shutil, "which", fake_which)
    monkeypatch.setattr(engine.subprocess, "run", fake_run)

    with pytest.raises(CliError) as excinfo:
        engine.require_engine()

    assert excinfo.value.code == 2
    assert "queue" in excinfo.value.message


def test_require_engine_returns_capability_when_available(monkeypatch):
    def fake_which(name):
        return f"/usr/bin/{name}" if name in (engine.GST_LAUNCH, engine.GST_INSPECT) else None

    def fake_run(argv, **kwargs):
        if argv[1:] == ["--help"]:
            return subprocess.CompletedProcess(argv, 0, stdout="--exists")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(engine.shutil, "which", fake_which)
    monkeypatch.setattr(engine.subprocess, "run", fake_run)

    cap = engine.require_engine()

    assert cap.available is True


def test_require_elements_passes_when_all_present():
    cap = engine.Capability(
        gst_launch="/usr/bin/gst-launch-1.0",
        gst_inspect="/usr/bin/gst-inspect-1.0",
        plugins={"opusenc": True, "matroskamux": True},
        available=True,
    )

    engine.require_elements(cap, ["opusenc", "matroskamux"])  # no raise


def test_require_elements_raises_env_error_naming_missing():
    cap = engine.Capability(
        gst_launch="/usr/bin/gst-launch-1.0",
        gst_inspect="/usr/bin/gst-inspect-1.0",
        plugins={"opusenc": False, "matroskamux": True},
        available=True,
    )

    with pytest.raises(CliError) as excinfo:
        engine.require_elements(cap, ["opusenc", "matroskamux"])

    assert excinfo.value.code == 2
    assert "opusenc" in excinfo.value.message
    assert "matroskamux" not in excinfo.value.message.split(":", 1)[1]


# --- build_audio_stream_argv() -------------------------------------------------


def test_build_audio_stream_argv_is_pure(monkeypatch):
    calls = []
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **k: calls.append((a, k)))

    engine.build_audio_stream_argv(
        "hw:CARD=Mic,DEV=0", engine.AudioFormat(rate=48000, channels=2), 5004
    )

    assert calls == []


def test_build_audio_stream_argv_passthrough_exact():
    fmt = engine.AudioFormat(rate=48000, channels=2, sample_format="S16LE")

    argv = engine.build_audio_stream_argv(
        "hw:CARD=Mic,DEV=0", fmt, 5004, encode="passthrough", host="127.0.0.1"
    )

    assert argv == [
        "gst-launch-1.0",
        "-e",
        "alsasrc",
        "device=hw:CARD=Mic,DEV=0",
        "!",
        "audio/x-raw,format=S16LE,rate=48000,channels=2",
        "!",
        "queue",
        "!",
        "audioconvert",
        "!",
        "audio/x-raw,format=S16BE",
        "!",
        "rtpL16pay",
        "!",
        "udpsink",
        "host=127.0.0.1",
        "port=5004",
    ]


def test_build_audio_stream_argv_opus_exact():
    fmt = engine.AudioFormat(rate=48000, channels=1, sample_format="S16LE")

    argv = engine.build_audio_stream_argv(
        "hw:CARD=Mic,DEV=0", fmt, 6000, encode="opus", host="192.168.1.5"
    )

    assert argv == [
        "gst-launch-1.0",
        "-e",
        "alsasrc",
        "device=hw:CARD=Mic,DEV=0",
        "!",
        "audio/x-raw,format=S16LE,rate=48000,channels=1",
        "!",
        "queue",
        "!",
        "audioconvert",
        "!",
        "audioresample",
        "!",
        "opusenc",
        "!",
        "rtpopuspay",
        "!",
        "udpsink",
        "host=192.168.1.5",
        "port=6000",
    ]


def test_build_audio_stream_argv_has_eos_flag_for_clean_shutdown():
    argv = engine.build_audio_stream_argv(
        "hw:CARD=Mic,DEV=0", engine.AudioFormat(rate=48000, channels=2), 5004
    )

    assert argv[0] == "gst-launch-1.0"
    assert argv[1] == "-e"


def test_build_audio_stream_argv_contains_queue_element():
    argv = engine.build_audio_stream_argv(
        "hw:CARD=Mic,DEV=0", engine.AudioFormat(rate=48000, channels=2), 5004
    )

    assert "queue" in argv


def test_build_audio_stream_argv_rejects_unsupported_encode():
    fmt = engine.AudioFormat(rate=48000, channels=2)
    with pytest.raises(CliError) as excinfo:
        engine.build_audio_stream_argv("hw:CARD=Mic,DEV=0", fmt, 5004, encode="mp3")

    assert excinfo.value.code == 1


def test_build_audio_stream_argv_rejects_invalid_format():
    fmt = engine.AudioFormat(rate=0, channels=2)
    with pytest.raises(CliError) as excinfo:
        engine.build_audio_stream_argv("hw:CARD=Mic,DEV=0", fmt, 5004)

    assert excinfo.value.code == 1


def test_build_audio_stream_argv_rejects_invalid_port():
    fmt = engine.AudioFormat(rate=48000, channels=2)
    with pytest.raises(CliError) as excinfo:
        engine.build_audio_stream_argv("hw:CARD=Mic,DEV=0", fmt, 70000)

    assert excinfo.value.code == 1


# --- build_audio_record_argv() -------------------------------------------------


def test_build_audio_record_argv_is_pure(monkeypatch):
    calls = []
    monkeypatch.setattr(engine.subprocess, "run", lambda *a, **k: calls.append((a, k)))

    engine.build_audio_record_argv(
        "hw:CARD=Mic,DEV=0", engine.AudioFormat(rate=48000, channels=2), "/tmp/out.mka"
    )

    assert calls == []


def test_build_audio_record_argv_mka_exact():
    fmt = engine.AudioFormat(rate=48000, channels=2, sample_format="S16LE")

    argv = engine.build_audio_record_argv("hw:CARD=Mic,DEV=0", fmt, "/tmp/out.mka", container="mka")

    assert argv == [
        "gst-launch-1.0",
        "-e",
        "alsasrc",
        "device=hw:CARD=Mic,DEV=0",
        "!",
        "audio/x-raw,format=S16LE,rate=48000,channels=2",
        "!",
        "queue",
        "!",
        "audioconvert",
        "!",
        "audioresample",
        "!",
        "opusenc",
        "!",
        "matroskamux",
        "!",
        "filesink",
        "location=/tmp/out.mka",
    ]


def test_build_audio_record_argv_wav_exact():
    fmt = engine.AudioFormat(rate=16000, channels=1, sample_format="S16LE")

    argv = engine.build_audio_record_argv("hw:CARD=Mic,DEV=0", fmt, "/tmp/out.wav", container="wav")

    assert argv == [
        "gst-launch-1.0",
        "-e",
        "alsasrc",
        "device=hw:CARD=Mic,DEV=0",
        "!",
        "audio/x-raw,format=S16LE,rate=16000,channels=1",
        "!",
        "queue",
        "!",
        "wavenc",
        "!",
        "filesink",
        "location=/tmp/out.wav",
    ]


def test_build_audio_record_argv_bounded_by_duration_exact():
    fmt = engine.AudioFormat(rate=48000, channels=2, sample_format="S16LE")

    argv = engine.build_audio_record_argv(
        "hw:CARD=Mic,DEV=0", fmt, "/tmp/out.wav", container="wav", duration_s=5.0
    )

    assert argv == [
        "gst-launch-1.0",
        "-e",
        "alsasrc",
        "device=hw:CARD=Mic,DEV=0",
        "num-buffers=500",
        "latency-time=10000",
        "!",
        "audio/x-raw,format=S16LE,rate=48000,channels=2",
        "!",
        "queue",
        "!",
        "wavenc",
        "!",
        "filesink",
        "location=/tmp/out.wav",
    ]


def test_build_audio_record_argv_has_eos_flag_for_clean_finalize():
    argv = engine.build_audio_record_argv(
        "hw:CARD=Mic,DEV=0", engine.AudioFormat(rate=48000, channels=2), "/tmp/out.mka"
    )

    assert argv[0] == "gst-launch-1.0"
    assert argv[1] == "-e"


def test_build_audio_record_argv_contains_queue_element():
    argv = engine.build_audio_record_argv(
        "hw:CARD=Mic,DEV=0", engine.AudioFormat(rate=48000, channels=2), "/tmp/out.mka"
    )

    assert "queue" in argv


def test_build_audio_record_argv_rejects_unsupported_container():
    fmt = engine.AudioFormat(rate=48000, channels=2)
    with pytest.raises(CliError) as excinfo:
        engine.build_audio_record_argv("hw:CARD=Mic,DEV=0", fmt, "/tmp/out.ogg", container="ogg")

    assert excinfo.value.code == 1


def test_build_audio_record_argv_rejects_non_positive_duration():
    fmt = engine.AudioFormat(rate=48000, channels=2)
    with pytest.raises(CliError) as excinfo:
        engine.build_audio_record_argv("hw:CARD=Mic,DEV=0", fmt, "/tmp/out.wav", duration_s=0)

    assert excinfo.value.code == 1


def test_build_audio_record_argv_rejects_invalid_format():
    fmt = engine.AudioFormat(rate=48000, channels=0)
    with pytest.raises(CliError) as excinfo:
        engine.build_audio_record_argv("hw:CARD=Mic,DEV=0", fmt, "/tmp/out.wav")

    assert excinfo.value.code == 1
