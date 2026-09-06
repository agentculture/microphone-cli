"""Tests for microphone_cli.activation — the append-only activation log.

Adapted from webcam-cli/tests/test_activation.py (cited in the module under
test) for microphone-cli's field set: verb, device (stable id), params,
started_at, ended_at. Every test uses tmp_path and an explicit path or
monkeypatched env vars — never the real state dir.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest

from microphone_cli.activation import (
    ENV_LOG_PATH,
    Activation,
    activation_scope,
    log_path,
    record_activation,
)

# --- Activation dataclass ---------------------------------------------------


def test_activation_to_dict_shape() -> None:
    act = Activation(
        verb="gain",
        device="usb-046d_C920_MIC_ARRAY_200901010001",
        params={"db": 6},
        started_at="2026-07-24T12:00:00+00:00",
        ended_at=None,
    )
    assert act.to_dict() == {
        "verb": "gain",
        "device": "usb-046d_C920_MIC_ARRAY_200901010001",
        "params": {"db": 6},
        "started_at": "2026-07-24T12:00:00+00:00",
        "ended_at": None,
    }


def test_activation_is_frozen() -> None:
    act = Activation(verb="gain", device="d", params={}, started_at="s", ended_at=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        act.device = "other"  # type: ignore[misc]


# --- log_path() --------------------------------------------------------------


def test_log_path_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    override = tmp_path / "custom" / "activation.jsonl"
    monkeypatch.setenv(ENV_LOG_PATH, str(override))
    assert log_path() == override


def test_log_path_xdg_state_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(ENV_LOG_PATH, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert log_path() == tmp_path / "microphone-cli" / "activation.jsonl"


def test_log_path_falls_back_to_local_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(ENV_LOG_PATH, raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert log_path() == tmp_path / ".local" / "state" / "microphone-cli" / "activation.jsonl"


def test_log_path_does_not_touch_filesystem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(ENV_LOG_PATH, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "nowhere"))
    log_path()
    assert list(tmp_path.rglob("*")) == []


# --- record_activation: exactly one line ------------------------------------


def test_record_activation_appends_exactly_one_line(tmp_path: Path) -> None:
    log = tmp_path / "activation.jsonl"
    act = Activation(
        verb="gain",
        device="usb-046d_C920_MIC_ARRAY_200901010001",
        params={"db": 6},
        started_at="2026-07-24T12:00:00+00:00",
        ended_at="2026-07-24T12:00:05+00:00",
    )
    record_activation(act, path=log)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed == act.to_dict()
    assert set(parsed) == {"verb", "device", "params", "started_at", "ended_at"}


def test_record_activation_creates_parent_dirs(tmp_path: Path) -> None:
    log = tmp_path / "a" / "b" / "c" / "activation.jsonl"
    assert not log.parent.exists()
    act = Activation(verb="gain", device="d", params={}, started_at="s", ended_at="e")
    record_activation(act, path=log)
    assert log.exists()
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


def test_record_activation_appends_never_overwrites(tmp_path: Path) -> None:
    log = tmp_path / "activation.jsonl"
    for i in range(3):
        act = Activation(
            verb="gain",
            device=f"dev-{i}",
            params={},
            started_at="s",
            ended_at="e",
        )
        record_activation(act, path=log)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["device"] for line in lines] == ["dev-0", "dev-1", "dev-2"]


def test_record_activation_second_call_appends_not_overwrites(tmp_path: Path) -> None:
    log = tmp_path / "activation.jsonl"
    first = Activation(verb="gain", device="d1", params={}, started_at="s1", ended_at="e1")
    second = Activation(verb="format", device="d2", params={}, started_at="s2", ended_at="e2")

    record_activation(first, path=log)
    record_activation(second, path=log)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == first.to_dict()
    assert json.loads(lines[1]) == second.to_dict()


def test_record_activation_propagates_write_failures(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    bad_path = blocker / "activation.jsonl"  # blocker is a file, not a dir
    act = Activation(verb="gain", device="d", params={}, started_at="s", ended_at="e")
    with pytest.raises(OSError):
        record_activation(act, path=bad_path)


# --- activation_scope: exactly one line, including on crash -----------------


def test_activation_scope_writes_nothing_until_exit(tmp_path: Path) -> None:
    log = tmp_path / "activation.jsonl"
    cm = activation_scope("gain", "dev", {"db": 3}, path=log)
    act = cm.__enter__()
    try:
        assert act.ended_at is None
        assert not log.exists()
    finally:
        cm.__exit__(None, None, None)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["ended_at"] is not None


def test_activation_scope_writes_one_line_on_success(tmp_path: Path) -> None:
    log = tmp_path / "activation.jsonl"
    with activation_scope(
        "gain",
        "usb-046d_C920_MIC_ARRAY_200901010001",
        {"db": 6},
        path=log,
    ) as act:
        assert act.device == "usb-046d_C920_MIC_ARRAY_200901010001"
        assert act.verb == "gain"
        assert act.ended_at is None

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["verb"] == "gain"
    assert record["params"] == {"db": 6}
    assert record["ended_at"] is not None


def test_activation_scope_records_on_raise(tmp_path: Path) -> None:
    log = tmp_path / "activation.jsonl"
    with pytest.raises(RuntimeError):
        with activation_scope("format", "dev", {}, path=log):
            raise RuntimeError("boom")

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["ended_at"] is not None
    assert record["params"]["error"] == "RuntimeError: boom"


def test_activation_scope_does_not_overwrite_existing_error_key(tmp_path: Path) -> None:
    log = tmp_path / "activation.jsonl"
    with pytest.raises(RuntimeError):
        with activation_scope("format", "dev", {"error": "pre-existing"}, path=log):
            raise RuntimeError("boom")

    record = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert record["params"]["error"] == "pre-existing"


def test_activation_scope_uses_default_log_path_when_no_path_kwarg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "activation.jsonl"
    monkeypatch.setenv(ENV_LOG_PATH, str(override))

    with activation_scope("gain", "dev", {}):
        pass

    assert override.exists()
    assert len(override.read_text(encoding="utf-8").splitlines()) == 1


def test_record_activation_uses_restrictive_file_mode(tmp_path: Path) -> None:
    log = tmp_path / "activation.jsonl"
    act = Activation(verb="gain", device="d", params={}, started_at="s", ended_at="e")
    record_activation(act, path=log)
    mode = os.stat(log).st_mode & 0o777
    assert mode == 0o600
