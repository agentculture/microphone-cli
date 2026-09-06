# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

**microphone-cli** — an agent-first CLI for USB microphones and microphone
arrays: enumerate devices, select and inspect channels, control gain and sample
format, and read direction-of-arrival and echo-canceller state from
XVF3800-class array firmware.

**Current state: the domain surface is built.** The repo started as a clone
of the AgentCulture agent template
(`5f9b1bd scaffold microphone-cli from culture-agent-template`), which shipped
only the agent-first introspection surface. The microphone domain has since
landed on top of it: 13 top-level verbs, 276 tests, 92% coverage,
`teken cli doctor . --strict` at 26/26. The converged spec and plan it was
built from live at `docs/specs/2026-09-06-microphone-domain.md` and
`docs/plans/2026-09-06-microphone-domain.md`.

### Module map

| Module | Owns |
|--------|------|
| `microphone_cli/devices.py` | Device identity: `/proc/asound` + sysfs parsing, stable-id synthesis (udev-style, from USB manufacturer/product/serial), `resolve()` by selector. Never opens a device or checks permissions. Cited from `webcam_cli/devices.py` with the video half dropped. |
| `microphone_cli/access.py` | Typed device-access state: `ok` / `absent` / `forbidden` / `busy`, for both `audio` (ALSA capture nodes) and `usb` (raw USB nodes) device kinds, each with its own remediation. Cited from `webcam-cli/webcam_cli/access.py`. |
| `microphone_cli/usbctl.py` | Stdlib `usbdevfs` control transfers (`USBDEVFS_CONTROL` ioctl via `fcntl.ioctl`) — no `pyusb`, no `libusb`. `find_devices()`/`open_device()` plus the `_ioctl` testing seam. |
| `microphone_cli/xvf3800.py` | The XVF3800 vendor control protocol: `PARAMETERS` (vendored verbatim from Pollen Robotics' `reachy_mini`, Apache-2.0), `PERSISTENT`, `Xvf3800` read/write. See `docs/xvf3800-parameters.md`. |
| `microphone_cli/mixer.py` | ALSA mixer control via `amixer` subprocess calls (no `libasound` bindings) — `list_controls`/`get_gain`/`set_gain`, all taking a `run` seam. |
| `microphone_cli/engine.py` | GStreamer boundary: capability detection and pipeline construction, shelling out to `gst-launch-1.0`/`gst-inspect-1.0`. No `gi`/PyGObject import, ever. Cited (audio subset) from `webcam-cli/webcam_cli/engine.py`. |
| `microphone_cli/activation.py` | Append-only activation log: one JSON line per `--apply` action. Path resolution order and shape cited from `webcam-cli/webcam_cli/activation.py`. |

### The three-level hardware split

`array`/`param` (reads open the USB node directly — there is no cheaper probe
stage) and `stream`/`record` (which do have a probe stage) share one rule,
readable from the invocation alone:

| Invocation | What it touches |
|------------|------------------|
| default (no flag) | Nothing. Resolves the device, validates the request, prints the plan. Not logged. |
| `--probe` (`stream`/`record` only) | Detects the GStreamer engine and checks the capture node's access state, still without opening it. Not logged. |
| `--apply` | Opens the device and acts (writes gain, flips AEC state, writes a firmware parameter, streams, or records). Logged — one JSON line appended to the activation log (`$MICROPHONE_ACTIVATION_LOG`, else `$XDG_STATE_HOME/microphone-cli/activation.jsonl`, else `~/.local/state/microphone-cli/activation.jsonl`). |

`param set` on a name in `xvf3800.PERSISTENT` (`SAVE_CONFIGURATION`,
`CLEAR_CONFIGURATION`, `REBOOT`, `TEST_CORE_BURN`,
`TEST_AEC_DISABLE_CONTROL`, `USB_BIT_DEPTH`, every `SPECIAL_CMD_*`) needs
`--allow-persistent` in addition to `--apply` — an ordinary `rw` write is
volatile and reverts on power-cycle, this tier is not.

### Testing seams

No test ever touches real hardware. Every module that would open a device or
spawn a process exposes a callable seam that tests monkeypatch:

- `root=` (`devices.py`, `access.py`, and every command module that resolves
  a device) — points filesystem parsing at a synthetic tree instead of `/`.
- `_open_array` (`cli/_commands/array.py`, `cli/_commands/param.py`) —
  resolves a device and opens an `Xvf3800`; tests replace it.
- `_ioctl` (`usbctl.py`) — module-level callable defaulting to
  `fcntl.ioctl`; tests replace it so no test ever touches `/dev`.
- `_spawn` (`cli/_commands/record.py`, `cli/_commands/stream.py`) — spawns
  the `gst-launch-1.0` subprocess.
- `_sleep` (`cli/_commands/record.py`, and `array.py`'s DoA `--watch` poll
  loop) — the retry/poll delay.
- `run=` (`mixer.py`'s `RunFunc`) — defaults to `subprocess.run`; every
  `amixer`-calling function takes it so tests inject a fake.

Fixture trees live under `tests/fixtures/`: `host-baseline` (one array, one
plain mic), `host-renumbered` (the same devices after a simulated replug —
proves selectors survive card-index churn), `respeaker` (an older ReSpeaker
XVF3800, `2886:001a`), and `two-arrays` (disambiguation when more than one
`38fb:1001`/`2886:001a` device is attached).

### Parity tests

`tests/test_cli.py` enforces that the hand-maintained surfaces stay in sync
with the registered parser: `test_every_catalog_path_resolves` and
`test_every_registered_path_has_a_catalog_entry` (catalog ↔ parser, both
directions), `test_every_registered_path_appears_in_overview_verbs`
(`overview._VERBS` ↔ parser), and
`test_learn_json_command_map_matches_the_registered_surface`
(`learn._TEXT`/`_as_json_payload()` ↔ parser). Adding a verb without updating
all four fails CI, not just the rubric gate.

### Hardware acceptance

No capture device is attached to the dev host (`arecord -l` lists nothing,
`lsusb` shows no `38fb:1001`/`2886:001a`), so every module above is exercised
only against the fixture trees. On-device acceptance is tracked in
[issue #3](https://github.com/agentculture/microphone-cli/issues/3) — connect
a Reachy Mini Lite over USB and run its checklist before treating the array
verbs (`array`, `param`, and `gain`'s firmware target) as validated on real
hardware.

## Commands

```bash
uv sync                                   # install (dev group included)

uv run pytest -n auto                     # full suite, parallel
uv run pytest tests/test_cli.py           # one file
uv run pytest -k whoami                   # one test / pattern
uv run pytest --cov=microphone_cli --cov-report=term   # coverage (fail_under=60)

uv run black --check microphone_cli tests # CI lint set — all four must pass
uv run isort --check-only microphone_cli tests
uv run flake8 microphone_cli tests
uv run bandit -c pyproject.toml -r microphone_cli

uv run teken cli doctor . --strict        # the agent-first rubric gate CI runs

uv run microphone whoami                  # the installed console script
uv run microphone doctor --json
python -m microphone_cli learn
```

**Markdown lint is not a `uv` tool** — `uv sync` does not install it, so it is
not available on a fresh checkout. CI installs it from npm; do the same locally,
pinning the version CI uses so results match:

```bash
npm install -g markdownlint-cli2@0.21.0
markdownlint-cli2 "**/*.md" "#node_modules" "#.local" "#.claude/skills"
```

Config lives in `.markdownlint-cli2.yaml` (MD013 and MD060 off, MD024
siblings-only for the changelog; `.claude/skills/**` ignored).

### Console-script name — RESOLVED

`pyproject.toml` declares `microphone = "microphone_cli.cli:main"` and
argparse's `prog` is also `"microphone"` — the binary and the program name
both agree now. `--help` output and every doc string say `microphone …`;
nothing presents `microphone-cli <verb>` as something to type.
`microphone-cli` still correctly names the *project*, the PyPI *distribution*,
and the mesh *nick* — do not blanket-replace it.

## Architecture

Zero runtime dependencies (`dependencies = []` in `pyproject.toml`) — that is a
deliberate constraint of the agent-first template, and `culture.yaml` is parsed
by hand in `whoami.py` rather than pulling in PyYAML. Keep new runtime deps out
unless the microphone backend genuinely requires one.

### CLI dispatch

`microphone_cli/cli/__init__.py` is the only place that knows the verb set.
`_build_parser()` imports each command module and calls its `register(sub)`;
`main()` parses and hands off to `_dispatch()`, which invokes `args.func(args)`.

Three contracts hold across the whole surface, and the rubric gate checks them:

- **Errors** — `microphone_cli/cli/_errors.py`. Every failure raises `CliError`
  (`{code, message, remediation}`). `_dispatch` wraps any stray exception into
  one, so **no traceback ever reaches stderr**. Exit codes: `0` success,
  `1` user error, `2` environment error, `3+` reserved.
- **Streams** — `microphone_cli/cli/_output.py`. Results to stdout
  (`emit_result`), errors and diagnostics to stderr (`emit_error`,
  `emit_diagnostic`); they never mix. Text errors render `error:` + `hint:`.
- **`--json` everywhere** — every parser adds `--json`; handlers read it via
  `getattr(args, "json", False)`.

Argparse's own errors also honour these contracts: `_CliArgumentParser`
overrides `.error()` to emit a `CliError`, and subparsers are built with
`parser_class=_CliArgumentParser` so nested nouns inherit it. Because
parse-time errors happen before `args.json` exists, `main()` pre-scans raw argv
for `--json` into the class-level `_json_hint`. **When you add a nested
subparser group, pass `parser_class=type(p)` through** (see
`_commands/cli.py`) — forgetting it silently drops that group back to
argparse's default `exit(2)` behaviour.

### Identity

`_commands/whoami.py` walks up from `__file__` (not the CWD) to find the repo's
own `culture.yaml`, so identity is the agent's, not the caller's. In a wheel
install no `culture.yaml` ships and the literal fallbacks apply. `doctor` and
`overview` both build on `whoami`'s `report()` / `read_agent_fields()`.

`doctor` mirrors the two `steward doctor` invariants — **prompt-file-present**
and **backend-consistency** — via the `_PROMPT_FILE` map (`claude` →
`CLAUDE.md`, `colleague` → `AGENTS.colleague.md`, `acp` → `AGENTS.md`,
`gemini` → `GEMINI.md`), plus a skills-present check. It returns
`{healthy, checks: [{id, passed, severity, message, remediation}]}` — the
rubric depends on that exact shape.

**This agent runs `backend: colleague`** (`culture.yaml`), so its resident
prompt file is `AGENTS.colleague.md`; this `CLAUDE.md` is the Claude Code
guidance file. Changing `backend` means adding the matching prompt file or
`doctor` (and CI's rubric gate) goes red.

### Adding a verb or noun

The domain modules (`devices.py`, `access.py`, `usbctl.py`, `xvf3800.py`,
`mixer.py`, `engine.py`, `activation.py`) are the domain logic; a new verb on
an *existing* noun almost never touches them. Scope the change to
`microphone_cli/cli/_commands/*.py` plus the three hand-maintained surfaces
below — that is the whole checklist:

1. New module in `microphone_cli/cli/_commands/` exposing `register(sub)`, with
   `--json` and a `func` default.
2. Register it in `_build_parser()` (there is a marked spot).
3. **Add a catalog entry** in `microphone_cli/explain/catalog.py` keyed by the
   command-path tuple — `tests/test_cli.py::test_every_catalog_path_resolves`
   walks every key, and the rubric requires an `explain` entry per path.
4. Update the command map in `_commands/learn.py` (both `_TEXT` and
   `_as_json_payload()`) and the `_VERBS` list in `_commands/overview.py`.
5. A noun group that gets action-verbs **must** also expose `<noun> overview` —
   the rubric's `overview_cli_noun_exists` check. `_commands/cli.py` is the
   worked example (its bare form prints its own overview).
6. Descriptive verbs must not hard-fail on a bad target path — `overview`
   accepts and ignores a positional `target` for exactly this reason.

## CI and release

Three jobs in `.github/workflows/tests.yml`: `test` (pytest + coverage →
SonarCloud, scan skipped when `SONAR_TOKEN` is empty, so fork PRs stay green),
`lint` (black, isort, flake8, bandit, markdownlint, `teken cli doctor --strict`),
and `version-check`.

**Every PR bumps the version — including docs-, config-, and CI-only PRs.**
Use the `version-bump` skill (or edit `pyproject.toml` + `CHANGELOG.md` by hand,
Keep-a-Changelog format). `__version__` is read from package metadata, so there
is no second version literal to update.

`version-check` only partly enforces that rule: it compares the PR's
`pyproject.toml` version against `origin/main` as **strings** and fails on
equality alone. Any different value passes, a downgrade included — `0.8.1` →
`0.8.0` is green today. Treat the check as a "did you forget entirely?" tripwire,
not a guarantee the version moved forward; the publish job is what actually
breaks later. (The comparison is string-equality in every AgentCulture sibling,
so tightening it belongs upstream, not in this repo alone.)

`publish.yml` publishes to TestPyPI on same-repo PRs (`<version>.devN`) and to
PyPI on push to main, both via Trusted Publishing.

Line length is **100** (black, isort profile=black, flake8 with `E203,W503`
ignored). SonarCloud project key: `agentculture_microphone-cli`; the quality
gate blocks CI when the token is configured.

## Vendored skills

`.claude/skills/` is vendored **cite-don't-import** from `guildmaster` (several
originate in `devague`, one in `colleague`). Do not hand-edit them — they are
excluded from markdownlint and Sonar for that reason. Provenance and the
re-sync procedure live in `docs/skill-sources.md`; per-machine paths go in a
git-ignored `.claude/skills.local.yaml` (copy the `.example`).

Use the `cicd` skill for PR creation and review-comment handling. Online posts
made outside those scripts sign as `- microphone-cli (Claude)`.
