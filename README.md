# microphone-cli

Agent-first CLI for USB microphones and microphone arrays: enumerate devices,
select and inspect channels, control gain and sample format, and read
direction-of-arrival and echo-canceller state from XVF3800-class array
firmware.

## Status

**The domain surface has landed.** `list`, `inspect`, `gain get|set`,
`array doa|aec`, `param list|get|set`, `stream audio`, and `record` are
implemented and wired into the CLI, alongside the agent-first baseline
(`whoami`, `learn`, `explain`, `overview`, `doctor`, `cli overview`) — 13
top-level verbs, 285 tests, 92% coverage, `teken cli doctor . --strict` at
26/26.

**On-device acceptance has run** on a Seeed ReSpeaker XVF3800 (`2886:001a`,
USB firmware 2.1.0): every verb was exercised against the real array, the
CLI's direction-of-arrival matched Seeed's reference reader exactly, and seven
defects found only on hardware were fixed. Evidence and findings are in
[docs/acceptance-microphone-domain.md](docs/acceptance-microphone-domain.md).
The Reachy Mini Lite (`38fb:1001`) named in the plan is still open in
[issue #3](https://github.com/agentculture/microphone-cli/issues/3); the
firmware bring-up the ReSpeaker needed is documented in
[issue #4](https://github.com/agentculture/microphone-cli/issues/4).

## Scope

microphone-cli owns local USB **audio capture** devices — microphones and
microphone arrays — and the acts of enumerating them, reading their state,
and getting samples off them. It produces stable ids, honest JSON, and
(for `stream`/`record`) an artifact, and stops there.

It does **not** own:

- **Video** — webcam enumeration and capture is `webcam-cli`'s lane.
- **Reaching a Reachy Mini over the network** — this tool only ever opens a
  USB node on the host it runs on; it has no remote-device concept.
- **Speech-to-text or text-to-speech** — no ASR/TTS model lives here.
- **Speaker or monitor playback** — sound *out* is the `reachy_mini` /
  `reachy_nova` stack's job.
- **Coordinate transforms of direction-of-arrival** — `array doa` reports the
  firmware's own radian value, untouched; re-basing onto a robot or world
  frame is a consumer's job, and it can only be done correctly starting from
  the raw value.

## Quickstart

```bash
uv sync
uv run pytest -n auto                 # run the test suite
uv run microphone learn               # start here: the self-teaching prompt (add --json)
uv run microphone list                # what capture hardware is attached, and can it be opened
uv run microphone whoami              # identity from culture.yaml
uv run teken cli doctor . --strict    # the agent-first rubric gate CI runs
```

The console command is **`microphone`**. The import package is
`microphone_cli` and the PyPI distribution / mesh nick is `microphone-cli`.
Only `microphone` is ever typed — argparse's `prog` also reads `microphone`
now, so nothing in `--help` output presents `microphone-cli <verb>` as
something to run.

## CLI

| Verb | What it does |
|------|--------------|
| `list` | List attached microphones: stable id, ALSA address, and access status. |
| `inspect <device>` | Inspect one microphone's capture formats, rates, channels, and firmware identity. |
| `gain overview` | Describe the `gain` verb group. |
| `gain get <device>` | Read the current ALSA capture gain (and, on an array, the firmware `AUDIO_MGR_MIC_GAIN`). |
| `gain set <device> <value>` | Plan or apply a new capture gain, 0.0–1.0 (`--apply`, `--target alsa\|firmware\|both`). |
| `array overview` | Describe the `array` verb group. |
| `array doa <device>` | Read `DOA_VALUE_RADIANS` once, or continuously as JSON Lines (`--watch`, `--interval`, `--count`). |
| `array aec get <device>` | Echo-canceller state: converged, bypass, HPF, echo on/off, mic count, geometry, RT60. |
| `array aec set <device>` | Flip `--echo`/`--bypass`/`--hpf` on or off (dry run without `--apply`). |
| `param overview` | Describe the `param` noun: verbs and the persistent tier. |
| `param list` | List every row of the XVF3800 parameter table. |
| `param get <device> <NAME>` | Read one raw firmware parameter. |
| `param set <device> <NAME> <values...>` | Write one raw firmware parameter (`--apply`; `--allow-persistent` for the persistent/destructive tier). |
| `stream overview` | Describe the `stream` verb group. |
| `stream audio <device>` | Serve a live microphone stream over RTP/UDP (dry-run by default; `--probe`, `--apply`). |
| `record <device> <path>` | Record a bounded clip (`.mka` Opus or `.wav` PCM) from a resolved microphone. |
| `whoami` | Report this agent's nick, version, backend, and served model. |
| `learn` | Print a structured self-teaching prompt for agent consumers. |
| `explain <path>` | Print markdown docs for a noun/verb path. |
| `overview` | Read-only descriptive snapshot of the agent (identity, verbs, contracts, consent). |
| `doctor` | Check the agent-identity invariants (prompt-file-present, backend-consistency). |
| `cli overview` | Describe the CLI surface itself. |

Every command supports `--json`. Results go to stdout, errors/diagnostics to
stderr (never mixed); text errors render `error:` + `hint:`. Exit codes:
`0` success, `1` user error, `2` environment error, `3` device busy,
`4+` reserved.

Two device classes get different verb sets. Any USB audio capture device
gets `list`/`inspect`/`gain` (ALSA side)/`stream`/`record`. `array`, `param`,
and the firmware side of `gain` need an **XMOS XVF3800** array — Seeed
ReSpeaker XVF3800 boards, or Pollen Robotics' Reachy Mini Audio card (an
XVF3800 derivative) — matched by USB id `38fb:1001` (Reachy Mini Audio) or
`2886:001a` (older ReSpeaker firmware).

## What comes out

`array doa` emits one JSON object per sample:

```json
{"device": "usb-Pollen_Robotics_Reachy_Mini_Audio_RM0001",
 "azimuth_rad": 0.612831, "speech": true,
 "source": "DOA_VALUE_RADIANS", "ts": "2026-09-06T12:00:00+00:00"}
```

`--watch` prints one such object per line (JSON Lines), polling every
`--interval` seconds (default 0.5) until `--count` samples or Ctrl-C, which
exits `0`.

`list --json` reports each device's stable id, ALSA address, card index, USB
path and ids, serial, whether it is an array, channel count, and access
state. `inspect --json` adds capture formats, rates, and — on an array —
firmware identity. `param list --json` reports every row of the XVF3800
table: `name`, `resid`, `cmdid`, `count`, `access` (`ro`/`rw`), `type`, and
`persistent`. `stream audio --json` announces the RTP/UDP `uri`, `caps`, and
a ready-to-run consumer pipeline per codec, so a second process needs
nothing else to attach.

## What touches the hardware

`stream` and `record` share a three-level split, readable from the
invocation alone:

| Invocation | What it touches |
|------------|------------------|
| default (no flag) | Nothing. Resolves the device, validates the request, prints the plan. Not logged. |
| `--probe` | Checks the GStreamer engine and the capture node's access state, still without opening it. Not logged. |
| `--apply` | Opens the device and streams or records. Logged. |

`array` and `param` reads open the USB node directly (a read has no
cheaper probe stage); only `array aec set --apply` and `param set --apply`
write. `gain set --apply` writes the ALSA and/or firmware gain. Every
`--apply` run — on any verb — appends one JSON line to the **activation
log**: `$MICROPHONE_ACTIVATION_LOG` if set, else
`$XDG_STATE_HOME/microphone-cli/activation.jsonl`, else
`~/.local/state/microphone-cli/activation.jsonl`.

Most firmware writes are **volatile** — they revert on power-cycle. A
smaller, explicitly named **persistent tier** — `SAVE_CONFIGURATION`,
`CLEAR_CONFIGURATION`, `REBOOT`, `TEST_CORE_BURN`,
`TEST_AEC_DISABLE_CONTROL`, `USB_BIT_DEPTH`, and every `SPECIAL_CMD_*` name —
persists across reboot, triggers a reboot, or is otherwise destructive;
writing one of these with `--apply` additionally requires
`--allow-persistent`. See [`docs/xvf3800-parameters.md`](docs/xvf3800-parameters.md)
for the full table and its provenance.

`list` opens nothing beyond one non-blocking access probe per node.

## Why device identity is the hard part

ALSA card numbers (`hw:1`) are plug-order, not identity — a replug can hand
the same physical microphone a different index, and the raw USB device path
under `/dev/bus/usb/BBB/DDD` renumbers the same way. Nothing in this CLI is
keyed on either: every selector resolves to a **stable id** synthesized from
USB manufacturer/product/serial descriptors the way udev derives them (e.g.
`usb-Pollen_Robotics_Reachy_Mini_Audio_RM0001`), falling back to a sysfs-path
form when a device has no serial. An ALSA card index is refused outright as
a selector — it is exactly the kind of handle that looks stable in one
session and is wrong in the next.

Raw USB access (needed for `array`/`param`) is gated by a udev rule, not
group membership: non-root access to a `38fb:1001` node needs a rule like
`/etc/udev/rules.d/99-reachy-mini-audio.rules` granting `MODE=0666` by
vendor/product id. When a raw node is forbidden, the CLI's remediation
prints that rule line verbatim so an agent (or a human) can apply the fix
without hunting for the syntax.

## What this repo carries

- **An agent-first CLI** cited from [teken](https://github.com/agentculture/teken)
  (`afi-cli`) — the runtime package has no third-party dependencies
  (`dependencies = []`); DoA/AEC/param go through a stdlib `usbdevfs` ioctl,
  gain through `amixer`, and stream/record shell out to `gst-launch-1.0`.
- **A mesh identity** — `culture.yaml` (`suffix` + `backend`) and the matching
  resident prompt file (`AGENTS.colleague.md`, since this agent runs
  `backend: colleague`).
- **The canonical guildmaster skill kit** under `.claude/skills/`, vendored
  cite-don't-import. See [`docs/skill-sources.md`](docs/skill-sources.md).
- **A build + deploy baseline** — pytest, lint, the agent-first rubric gate,
  and PyPI Trusted Publishing wired into GitHub Actions.
- **The converged spec and plan** this domain was built from:
  [`docs/specs/2026-09-06-microphone-domain.md`](docs/specs/2026-09-06-microphone-domain.md)
  and [`docs/plans/2026-09-06-microphone-domain.md`](docs/plans/2026-09-06-microphone-domain.md).

See [`CLAUDE.md`](CLAUDE.md) for the architecture, the domain constraints,
and the conventions (version-bump-every-PR, the `cicd` PR lane, testing
seams).

## Development

```bash
uv run pytest tests/test_cli.py       # a single file
uv run pytest -k whoami               # a single test
uv run black --check microphone_cli tests
uv run isort --check-only microphone_cli tests
uv run flake8 microphone_cli tests
uv run bandit -c pyproject.toml -r microphone_cli
```

Every PR bumps the version in `pyproject.toml` and adds a `CHANGELOG.md`
entry, even for docs- and CI-only changes. CI's `version-check` job catches
a forgotten bump by failing when the version still matches `main`; it does
not verify the version moved *forward*.

Markdown lint is an npm tool, not a `uv` one — install it separately, pinned
to the version CI uses:

```bash
npm install -g markdownlint-cli2@0.21.0
markdownlint-cli2 "**/*.md" "#node_modules" "#.local" "#.claude/skills"
```

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
