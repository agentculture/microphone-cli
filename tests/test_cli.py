"""Smoke tests for the microphone CLI entry point, plus the surface-parity gates.

The parity block below is ported from
``../webcam-cli/tests/test_cli.py`` (lines 190-327): it walks the *live*
argparse tree and requires the hand-maintained duplicates of the surface —
the explain catalog, ``overview._VERBS`` and ``learn``'s command map — to
agree with it, and it forbids the two prose regressions this repo has
already had: presenting ``microphone-cli`` as a typable command, and the
scaffold's "clonable template" self-description.

Hardware posture: nothing here opens a device. Every capture-surface probe
is a parse-level or dry-run call against a synthetic fixture tree.
"""

from __future__ import annotations

import argparse
import json
import re

import pytest

from microphone_cli import __version__
from microphone_cli.cli import _build_parser, main
from microphone_cli.explain import known_paths
from microphone_cli.explain.catalog import ENTRIES


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_args_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([])
    assert rc == 0
    assert "usage: microphone" in capsys.readouterr().out


def test_unknown_command_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["bogus"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


# --- whoami ---------------------------------------------------------------


def test_whoami_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["whoami"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "nick: microphone-cli" in out
    assert "backend: colleague" in out
    assert "model:" in out


def test_whoami_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["whoami", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["nick"] == "microphone-cli"
    assert payload["version"] == __version__
    assert payload["backend"] == "colleague"


# --- learn ----------------------------------------------------------------


def test_learn_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["learn"])
    assert rc == 0
    out = capsys.readouterr().out
    assert len(out) >= 200
    assert "microphone" in out
    assert "Exit-code policy" in out
    assert "--json" in out
    assert "explain" in out


def test_learn_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["learn", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"] == "microphone-cli"
    assert payload["version"] == __version__
    assert payload["json_support"] is True


# --- explain --------------------------------------------------------------


def test_explain_root(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain"])
    assert rc == 0
    assert "# microphone" in capsys.readouterr().out


def test_explain_self(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "microphone-cli"])
    assert rc == 0
    assert capsys.readouterr().out.startswith("#")


def test_explain_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "whoami", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["path"] == ["whoami"]
    assert "microphone whoami" in payload["markdown"]


def test_explain_unknown_path_errors(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "nonexistent"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error:")
    assert "hint:" in captured.err


def test_every_catalog_path_resolves(capsys: pytest.CaptureFixture[str]) -> None:
    for path in known_paths():
        rc = main(["explain", *path])
        assert rc == 0, f"explain {' '.join(path)} failed"
        capsys.readouterr()


# --- surface wiring -------------------------------------------------------
#
# Ported from ../webcam-cli/tests/test_cli.py:190-327.


def _registered_paths(
    parser: argparse.ArgumentParser | None = None,
    prefix: tuple[str, ...] = (),
) -> list[tuple[str, ...]]:
    """Every command path the *live* parser tree exposes, depth-first."""
    parser = parser if parser is not None else _build_parser()
    paths: list[tuple[str, ...]] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, subparser in action.choices.items():
                path = (*prefix, name)
                paths.append(path)
                paths.extend(_registered_paths(subparser, path))
    return paths


#: The surface this repo intends to expose, written out longhand. Kept as a
#: literal rather than derived from anything so that dropping a `register()`
#: call from `_build_parser` fails here instead of quietly shrinking the CLI.
KNOWN_PATHS: set[tuple[str, ...]] = {
    ("list",),
    ("inspect",),
    ("gain",),
    ("gain", "overview"),
    ("gain", "get"),
    ("gain", "set"),
    ("array",),
    ("array", "overview"),
    ("array", "doa"),
    ("array", "aec"),
    ("array", "aec", "overview"),
    ("array", "aec", "get"),
    ("array", "aec", "set"),
    ("param",),
    ("param", "overview"),
    ("param", "list"),
    ("param", "get"),
    ("param", "set"),
    ("stream",),
    ("stream", "overview"),
    ("stream", "audio"),
    ("record",),
    ("whoami",),
    ("learn",),
    ("explain",),
    ("overview",),
    ("doctor",),
    ("cli",),
    ("cli", "overview"),
}


def test_the_registered_surface_is_exactly_the_known_surface() -> None:
    assert set(_registered_paths()) == KNOWN_PATHS


def test_every_registered_path_has_a_catalog_entry() -> None:
    """The converse of `test_every_catalog_path_resolves`.

    That test proves no catalog entry is dead; this one proves no registered
    verb is undocumented, which is the direction that actually breaks an agent.
    """
    undocumented = sorted(set(_registered_paths()) - set(known_paths()))
    assert not undocumented, f"registered but not in the explain catalog: {undocumented}"


_WORD_RE = re.compile(r"[a-zA-Z]+")


def _word_tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def test_every_registered_path_appears_in_overview_verbs() -> None:
    """`overview._VERBS` is a hand-maintained duplicate of the surface.

    Walk the *live* parser tree (recursively, so noun-group sub-verbs like
    ``array aec get`` are included) and require every path to be represented in
    ``_VERBS``. "Represented" means some single entry's word-tokens are a
    superset of the path's components — so a shorthand line like
    ``gain get|set <device>`` covers two leaf verbs at once. A path with no
    such entry fails loudly instead of drifting silently.
    """
    from microphone_cli.cli._commands.overview import _VERBS

    entry_tokens = [_word_tokens(entry) for entry in _VERBS]
    for path in _registered_paths():
        wanted = set(path)
        assert any(
            wanted <= tokens for tokens in entry_tokens
        ), f"`{' '.join(path)}` is registered but not represented in overview._VERBS"


def test_learn_json_command_map_matches_the_registered_surface() -> None:
    """`learn --json` is the machine-readable command map; it must be complete."""
    from microphone_cli.cli._commands.learn import _as_json_payload

    listed = {tuple(entry["path"]) for entry in _as_json_payload()["commands"]}
    assert listed == set(_registered_paths())


# --- the command an agent is told to type ---------------------------------

# `microphone-cli` presented as something typable: the string followed by a
# flag, a placeholder, or one of the registered top-level verbs. Bare mentions
# of the project, the PyPI distribution, the mesh nick, or the `microphone-cli/`
# state directory are correct and deliberately not matched.
_DEAD_COMMAND_RE = re.compile(
    r"microphone-cli\s+(?:--?\w|<|list\b|inspect\b|gain\b|array\b|param\b|stream\b"
    r"|record\b|whoami\b|learn\b|explain\b|overview\b|doctor\b|cli\b)"
)


def _help_texts(parser: argparse.ArgumentParser | None = None) -> list[str]:
    parser = parser if parser is not None else _build_parser()
    texts = [parser.format_help()]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for subparser in action.choices.values():
                texts.extend(_help_texts(subparser))
    return texts


def _agent_facing_texts(capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    """Everything the CLI tells an agent about itself, keyed by where it came from."""
    texts = {f"--help #{i}": text for i, text in enumerate(_help_texts())}
    texts.update({f"explain {' '.join(path) or '<root>'}": body for path, body in ENTRIES.items()})

    for argv in (
        ["learn"],
        ["learn", "--json"],
        ["overview"],
        ["overview", "--json"],
        ["cli", "overview"],
        ["stream", "overview"],
        ["array", "overview"],
        ["array", "aec", "overview"],
        ["param", "overview"],
        ["gain", "overview"],
        ["whoami"],
        ["doctor"],
    ):
        main(argv)
        captured = capsys.readouterr()
        texts[" ".join(argv)] = captured.out + captured.err
    return texts


def test_no_user_facing_string_presents_microphone_cli_as_a_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`microphone-cli explain …` is not an installed binary; instructing it is a dead end.

    The three-way split is deliberate — command ``microphone``, import package
    ``microphone_cli``, distribution ``microphone-cli`` — so this asserts only
    that the dist name is never presented as something to *type*, not that it
    is absent.
    """
    offenders = {
        source: _DEAD_COMMAND_RE.findall(text)
        for source, text in _agent_facing_texts(capsys).items()
        if _DEAD_COMMAND_RE.search(text)
    }
    assert not offenders, f"`microphone-cli` presented as a typable command in: {offenders}"


def test_no_template_prose_survives(capsys: pytest.CaptureFixture[str]) -> None:
    """The scaffold described this repo as a clonable template. It is a microphone agent."""
    banned = ("clonable", "template", "scaffold", "rename the package", "mint a new agent")
    offenders = {
        source: [phrase for phrase in banned if phrase in text.lower()]
        for source, text in _agent_facing_texts(capsys).items()
    }
    offenders = {source: hits for source, hits in offenders.items() if hits}
    assert not offenders, f"template prose still in the self-description: {offenders}"


# --- the microphone verbs, reached through main() -------------------------
#
# Hardware posture: dry runs against a synthetic fixture tree, or parse-level
# failures. Nothing here opens a device — `--probe`/`--apply` belong to the
# on-host acceptance run.


def test_list_runs_through_main(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["list", "--json", "--root", "tests/fixtures/host-baseline"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)


def test_param_list_runs_through_main(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["param", "list", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)


def test_unknown_device_is_a_structured_error_not_a_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["array", "doa", "no-such-device", "--root", "tests/fixtures/host-baseline"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert captured.err.startswith("error:")
    assert "hint:" in captured.err
    assert captured.out == ""
