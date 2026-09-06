"""Markdown catalog for ``microphone explain <path>``.

Each entry is verbatim markdown. Keys are command-path tuples. The empty tuple,
``("microphone",)`` and the legacy ``("microphone-cli",)`` all resolve to the
root entry — an agent that guesses the distribution name still lands on the
docs, even though only ``microphone`` is ever typed.

Every path registered in :func:`microphone_cli.cli._build_parser` needs an entry
here; ``tests/test_cli.py`` walks the live parser tree and fails when one is
missing, so this file cannot silently fall behind the surface.

Keep bodies self-contained: an agent reading one entry should get enough
context without chaining reads.
"""

from __future__ import annotations

_ROOT = """\
# microphone

Agent for the USB microphones and microphone arrays attached to this host. It
enumerates what is attached and gives each a stable name, reports honestly what
can be opened right now, exposes the array firmware an ordinary audio API cannot
reach (direction-of-arrival, echo-canceller state, raw XVF3800 parameters), and
hands a consumer either a live stream or a bounded recorded file. Interpreting
what is *in* the audio is a speech model's job, not this tool's.

Three names, one typable: the installed command is `microphone`, the import
package is `microphone_cli`, and the PyPI distribution is `microphone-cli`. Only
`microphone` is ever typed.

## Verbs

- `microphone list` — attached microphones: stable id, ALSA address, access state.
- `microphone inspect <device>` — capture formats, rates, channels, firmware identity.
- `microphone gain get|set <device>` — read or change capture gain.
- `microphone array doa <device>` — direction of arrival, raw firmware radians.
- `microphone array aec get|set <device>` — echo-canceller state and switches.
- `microphone param list|get|set` — raw XVF3800 firmware parameters.
- `microphone stream audio <device>` — serve a live attachment point (unbounded).
- `microphone record <device> <output>` — record a bounded clip to one file.
- `microphone whoami` — identity probe from `culture.yaml`.
- `microphone learn` — structured self-teaching prompt.
- `microphone explain <path>` — markdown docs for any noun/verb.
- `microphone overview` — descriptive snapshot of the agent.
- `microphone doctor` — check the agent-identity invariants.
- `microphone cli overview` — describe the CLI surface.

## What touches the hardware

Every verb sits on one of three levels, readable from the flags alone:

- **default (no flag)** — a dry run. Resolves the device from filesystem reads,
  validates the request, prints the plan it would run. **Nothing is opened, no
  transfer is issued, nothing is spawned and nothing is logged.**
- **`--probe`** — checks the capture engine and the capture node's real access
  state (`stream audio`, `record`). Still spawns nothing.
- **`--apply`** — commits: opens the device and streams/records, or issues the
  ALSA and firmware writes. Written to the activation log.

Read-only vendor control transfers are the case in between: `inspect` (firmware
identity, arrays only), `array doa`, `array aec get`, `param get` and `gain get`
open the USB device and *read* from it with no `--apply`. They change nothing,
but they need permission on the device node.

`list` opens nothing beyond one non-blocking permission probe per capture node.

## Naming a device

Use the stable id printed by `microphone list` (or a unique substring of it). A
bare `hw:N` or card index is refused on purpose: ALSA card numbering is plug
order and re-enumeration order, not identity, so an index is not a reproducible
instruction. `microphone list --json` prints the id to use.

## Exit-code policy

- `0` success
- `1` user-input error (unknown device, unknown parameter, bad value, bad flag)
- `2` environment error (no capture engine, forbidden device node) — not
  retryable without a config/environment fix
- `3` device busy (EBUSY) — retryable; another process holds the device, and
  waiting for it to release (or stopping it) is enough, no fix needed. Kept
  distinct from `2` on purpose: an agent that gets the same code for both
  cannot tell "wait and retry" from "this will never work" without
  string-matching the message.
- `4+` reserved

## Who this is for

`media-cli` is the composing consumer: it orchestrates capture across tools and
calls this CLI for everything microphone-shaped rather than re-deriving it.
Agents and human operators are the other two readers — an agent needs to decide
from the surface alone whether an invocation opens a device, and an operator
needs to debug an array that is misbehaving.

## Consent

Every `--apply` is appended to the activation log — by default
`~/.local/state/microphone-cli/activation.jsonl` (XDG state dir; override with
`$MICROPHONE_ACTIVATION_LOG`) — and a recording writes only to the path you
name, with no hidden buffer and never to stdout.

A hardware activity light **cannot** be promised: that is device firmware,
outside this tool's control. This tool records activations; it does not prevent
covert use, and nothing here should be read as claiming otherwise.

## See also

- `microphone explain list`
- `microphone explain inspect`
- `microphone explain array`
- `microphone explain param`
- `microphone explain stream`
- `microphone explain record`
"""

_LIST = """\
# microphone list

Every microphone attached to this host, with the access state of its capture
node. Read-only: no capture, no format enumeration, no engine call, no USB
control transfer. The only hardware touch is one non-blocking `open()`/`close()`
per capture node — that pair *is* the permission probe.

`list` never fails because one device is unhappy: a forbidden, absent or busy
device is reported as such, with its remediation carried through, and `list`
itself still exits 0.

## Usage

    microphone list
    microphone list --json
    microphone list --root PATH    # resolve under a synthetic device tree (tests)

## What one entry means

- **`stable_id`** — the device's identity, built from its USB descriptors
  (vendor, product, serial) rather than the ALSA card index. This is the
  selector every other verb accepts.
- **`card_index` / ALSA address** — where the kernel put it *this boot*. Plug
  order and re-enumeration order, not identity; useful to show, never to store.
- **`is_array`** — whether the device is an XVF3800-based microphone array, and
  therefore whether the `array` and `param` nouns have anything to talk to.
- **`access`** — whether the capture node is present, forbidden, busy, or
  openable, each with its own remediation.

## See also

- `microphone explain inspect`
"""

_INSPECT = """\
# microphone inspect <device>

One device's capture capability, as the USB-audio driver itself reports it:
sample formats, sample rates and channel count, read from
`/proc/asound/card<N>/stream0`. No ALSA library is loaded and no device is
opened for capture.

On an XVF3800-based array this additionally looks the device up on the USB bus
and reads its firmware identity over a read-only vendor control transfer. That
control path needs permission on a device node most agents do not have without a
udev rule, so a permission or lookup failure there is reported as
`firmware: {"error": ...}` rather than raised — `inspect` is descriptive and must
not hard-fail on a permissions problem non-array microphones do not even have. A
non-array microphone reports `firmware: null`: there is no XVF3800 control
protocol to speak to it with.

## Usage

    microphone inspect <device>
    microphone inspect <device> --json
    microphone inspect <device> --root PATH

`<device>` is the stable id from `microphone list`, or a unique substring of it.

## See also

- `microphone explain list`
- `microphone explain array`
"""

_GAIN = """\
# microphone gain

Noun group for capture gain. Two independent knobs exist on an XVF3800 array and
only one on a plain USB microphone:

- **ALSA** — the kernel capture-volume mixer control, read and written through
  `amixer`. Every USB capture device has one.
- **Firmware** — `AUDIO_MGR_MIC_GAIN` on the XVF3800 itself, read and written
  over a USB vendor control transfer. Arrays only.

`gain get` reports both where both exist. `gain set` defaults to `--target both`
(silently ALSA-only on a non-array device) and is a **dry run unless `--apply`**
is passed: without it the plan is computed and printed with no `cset` issued and
no USB device opened. With `--apply`, both actions are issued inside one
activation-log line.

Value mapping: the CLI value is a float. For ALSA it is linearly mapped from
`0.0..1.0` onto the control's reported `[min, max]` and rounded with Python's
`round()` (banker's rounding: ties go to the nearest even integer). For firmware
the value is written verbatim as the raw float — there is no range to map onto.

## Usage

    microphone gain overview
    microphone gain get <device>
    microphone gain set <device> <value> [--apply] [--target alsa|firmware|both]

## See also

- `microphone explain param`
"""

_GAIN_OVERVIEW = """\
# microphone gain overview

Describes the `gain` verb group: its verbs, the two independent gain knobs
(ALSA mixer and, on arrays, the `AUDIO_MGR_MIC_GAIN` firmware parameter), the
`0.0..1.0` value mapping, and the fact that `gain set` is a dry run unless
`--apply` is passed. A bare `microphone gain` prints the same thing.

## Usage

    microphone gain overview
    microphone gain overview --json
"""

_GAIN_GET = """\
# microphone gain get <device>

Reads the current capture gain. On every device that means the ALSA capture
mixer control; on an XVF3800 array it additionally reads `AUDIO_MGR_MIC_GAIN`
over a **read-only** USB vendor control transfer. Nothing is written and no
`--apply` is involved, but the firmware read does open the USB device node, so
it needs permission on it.

A device with no capture mixer control is a typed user error naming the card,
not a silent zero.

## Usage

    microphone gain get <device>
    microphone gain get <device> --json

## See also

- `microphone explain gain`
"""

_GAIN_SET = """\
# microphone gain set <device> <value>

Sets the capture gain from a float in `0.0..1.0`. **Dry run unless `--apply`.**
Without `--apply` the ALSA control's range is read, the integer that would be
written is computed, and the plan is printed — no `cset`, no USB device opened.
With `--apply`, the ALSA write and (on an array, unless `--target` narrows it)
the firmware write are both issued and wrapped in exactly one activation-log
line.

`--target alsa|firmware|both` picks which knob to move; `both` is the default
and degrades silently to ALSA-only on a non-array device.

## Usage

    microphone gain set <device> 0.75
    microphone gain set <device> 0.75 --apply
    microphone gain set <device> 0.75 --apply --target firmware --json

## See also

- `microphone explain gain`
"""

_ARRAY = """\
# microphone array

Noun group for XVF3800 microphone-array firmware — the state an ordinary audio
API cannot reach. Two verb families hang off it:

- `array doa <device>` — read `DOA_VALUE_RADIANS`, once or continuously.
- `array aec get|set <device>` — the echo-canceller's observable state and the
  switches that are safe to flip.

Reads are read-only vendor control transfers: they change nothing but do open
the USB device node, so they need permission on it. Writes (`array aec set`) are
a dry run unless `--apply`, and `--apply` writes inside an activation scope, so
every hardware-touching run leaves exactly one activation-log line.

A bare `microphone array` prints this noun's own overview.

## Usage

    microphone array overview
    microphone array doa <device> [--watch] [--interval S] [--count N]
    microphone array aec get <device>
    microphone array aec set <device> [--apply]

## See also

- `microphone explain param`
- `microphone explain inspect`
"""

_ARRAY_OVERVIEW = """\
# microphone array overview

Describes the `array` verb group: `doa`, the `aec` sub-noun, which of them read
and which write, and the fact that writes need `--apply`. A bare
`microphone array` prints the same thing.

## Usage

    microphone array overview
    microphone array overview --json
"""

_ARRAY_DOA = """\
# microphone array doa <device>

Reads `DOA_VALUE_RADIANS` from the array firmware: the estimated direction of
arrival of the dominant sound source.

**The value is reported exactly as the firmware reports it** — radians, in the
array's own frame. No degree conversion, no coordinate transform, no re-basing
onto a robot or room frame happens here. A consumer that needs another frame
owns that conversion, and can only do it correctly if it starts from the
untouched firmware value.

This is a read-only vendor control transfer: nothing is written, and no
`--apply` exists for it. It does open the USB device node, so it needs
permission on it.

`--watch` polls continuously, printing one JSON object per line (JSON Lines)
until Ctrl-C or `--count` samples; `--interval` sets the seconds between polls.

## Usage

    microphone array doa <device>
    microphone array doa <device> --json
    microphone array doa <device> --watch --interval 0.25 --count 20

## See also

- `microphone explain array`
"""

_ARRAY_AEC = """\
# microphone array aec

Sub-noun for the XVF3800 echo canceller. `aec get` reports its observable state
— converged, bypass, high-pass filter, echo suppression, microphone count, array
geometry — over read-only control transfers. `aec set` flips the switches that
are safe to flip, and is a **dry run unless `--apply`**; with `--apply` the
writes are issued inside an activation scope, leaving exactly one activation-log
line.

A bare `microphone array aec` prints this sub-noun's own overview.

## Usage

    microphone array aec overview
    microphone array aec get <device>
    microphone array aec set <device> [--apply]

## See also

- `microphone explain array`
"""

_ARRAY_AEC_OVERVIEW = """\
# microphone array aec overview

Describes the `aec` sub-noun: the state fields `get` reads, the switches `set`
can flip, and the `--apply` gate in front of every write. A bare
`microphone array aec` prints the same thing.

## Usage

    microphone array aec overview
    microphone array aec overview --json
"""

_ARRAY_AEC_GET = """\
# microphone array aec get <device>

Reads the echo canceller's observable state: whether it has converged, whether
it is bypassed, the high-pass filter and echo-suppression settings, the
microphone count and the array geometry. Read-only vendor control transfers —
nothing is written, and there is no `--apply` for this verb. It does open the
USB device node, so it needs permission on it.

## Usage

    microphone array aec get <device>
    microphone array aec get <device> --json

## See also

- `microphone explain array`
"""

_ARRAY_AEC_SET = """\
# microphone array aec set <device>

Flips the echo-canceller switches that are safe to flip, each as an explicit
`on`/`off` flag. **Dry run unless `--apply`**: without it the requested writes
are validated and the plan is printed with no transfer issued. With `--apply`
the writes go out inside an activation scope, leaving exactly one activation-log
line.

Only a curated set of switches is exposed here. Anything else on the firmware is
reachable through `microphone param set`, which carries its own persistent-tier
gate.

## Usage

    microphone array aec set <device> --bypass off
    microphone array aec set <device> --bypass off --apply --json

## See also

- `microphone explain array`
- `microphone explain param`
"""

_PARAM = """\
# microphone param

The low-level noun: read and write raw XVF3800 firmware parameters by name
(case-insensitive, always echoed upper-case). Every verb operates directly on
one row of the firmware's parameter table.

Validation — unknown name, wrong access direction, wrong value count or type —
happens **before** any USB transfer is issued, in dry-run and in apply mode
alike, so a rejected command never touches hardware.

## The persistent tier

A second gate sits in front of `--apply`. Some parameters survive a power-cycle
(after `SAVE_CONFIGURATION`), trigger a reboot, or are otherwise destructive.
Writing one of those requires `--allow-persistent` **in addition to** `--apply`.
An ordinary `rw` write is volatile and reverts on the next power-cycle, so that
extra flag is the one place this noun asks for explicit confirmation. The check
runs before the device is opened and before any transfer is issued.

## Usage

    microphone param overview
    microphone param list
    microphone param get <device> <NAME>
    microphone param set <device> <NAME> <values...> [--apply] [--allow-persistent]

## See also

- `microphone explain array`
- `microphone explain gain`
"""

_PARAM_OVERVIEW = """\
# microphone param overview

Describes the `param` noun: its verbs, the pre-transfer validation, and the
persistent/destructive tier that needs `--allow-persistent` on top of `--apply`.
A bare `microphone param` prints the same thing.

## Usage

    microphone param overview
    microphone param overview --json
"""

_PARAM_LIST = """\
# microphone param list

Lists every row of the XVF3800 parameter table this tool knows: name, access
direction, value type and count, and whether the parameter is in the persistent
or destructive tier. Pure data — no device is resolved, no device is opened and
no transfer is issued, so this works with no microphone attached at all.

Use it to find the exact `<NAME>` for `param get` / `param set`.

## Usage

    microphone param list
    microphone param list --json

## See also

- `microphone explain param`
"""

_PARAM_GET = """\
# microphone param get <device> <NAME>

Reads one raw firmware parameter by name (case-insensitive). The name is
validated against the parameter table — unknown name, or a name that is not
readable — before the device is opened, so a rejected command never touches
hardware.

This is a read-only vendor control transfer: nothing is written and there is no
`--apply`. It does open the USB device node, so it needs permission on it.

## Usage

    microphone param get <device> AUDIO_MGR_MIC_GAIN
    microphone param get <device> AUDIO_MGR_MIC_GAIN --json

## See also

- `microphone explain param`
"""

_PARAM_SET = """\
# microphone param set <device> <NAME> <values...>

Writes one raw firmware parameter. **Dry run unless `--apply`**: without it the
name, access direction, value count and value types are validated and the plan
is printed, with no device opened and no transfer issued.

A parameter in the persistent or destructive tier additionally requires
`--allow-persistent`. Those parameters survive a power-cycle (after
`SAVE_CONFIGURATION`), trigger a reboot, or are otherwise irreversible, unlike an
ordinary `rw` write which is volatile and reverts on the next power-cycle. The
persistent check runs before the device is opened and before any transfer.

## Usage

    microphone param set <device> AUDIO_MGR_MIC_GAIN 0.5
    microphone param set <device> AUDIO_MGR_MIC_GAIN 0.5 --apply
    microphone param set <device> SAVE_CONFIGURATION 1 --apply --allow-persistent

## See also

- `microphone explain param`
"""

_STREAM = """\
# microphone stream

Noun group for live attachment points: a running microphone another process can
consume, rather than an artifact on disk. Today there is one verb,
`stream audio`. A bare `microphone stream` prints this noun's own overview.

Streams are **unbounded by construction**: there is no `--duration`. Stop one
with SIGINT/SIGTERM, or by killing the pid the command returns. For a bounded
artifact use `microphone record`.

## Usage

    microphone stream overview
    microphone stream audio <device> [--apply]

## See also

- `microphone explain record`
"""

_STREAM_OVERVIEW = """\
# microphone stream overview

Describes the `stream` verb group: its verbs, the three-level hardware split,
the attachment point (host, port, wire codec) and the defaults for rate,
channels and sample format. A bare `microphone stream` prints the same thing.

## Usage

    microphone stream overview
    microphone stream overview --json
"""

_STREAM_AUDIO = """\
# microphone stream audio <device>

Serves a live microphone stream over RTP/UDP — an attachment point, not a file.
Unbounded: there is no `--duration`; stop it with SIGINT/SIGTERM or by killing
the returned pid.

## Three levels of hardware contact

- **default** — a dry run. Resolves the device from `/proc` and `/sys`
  (filesystem reads only), builds the exact argv `--apply` would run and prints
  it. No engine detection, no `open()`, no spawn: `hardware_touched` and
  `engine_checked` are both `false`. The reported access state comes from a
  `stat`, not an open.
- **`--probe`** — additionally detects the GStreamer engine (by shelling out to
  `gst-inspect-1.0`, which opens no device) and reports the capture node's real
  access state. Still spawns nothing.
- **`--apply`** — requires the engine and the elements the chosen encoding needs,
  *enforces* access (a busy device is the typed exit-3 error, never a silent
  wait), then spawns the pipeline inside an activation scope. The pid is
  returned so the caller can stop it.

There is no warm-up: an ALSA capture device has no sensor that has to settle, so
`warmup` reports zero and says why rather than faking a video-shaped field.

## Usage

    microphone stream audio <device>
    microphone stream audio <device> --probe --json
    microphone stream audio <device> --apply --port 5004 --encode opus

## See also

- `microphone explain stream`
- `microphone explain record`
"""

_RECORD = """\
# microphone record <device> <output>

Records a bounded audio clip from a resolved microphone to one file. The
container comes from the extension: `.mka` (Opus in Matroska) or `.wav` (raw
PCM).

**There is no flag that means "forever."** `--duration` and `--max-bytes` both
have defaults and both have ceilings, and both are enforced twice over: the
pipeline argv is self-limiting, *and* the growing artifact is polled and the
child stopped when either bound is reached. The JSON says which bound won, in
`stopped_reason`.

Hardware contact is the same three-level split as `stream audio`: nothing by
default (resolve and validate only), engine plus access check with `--probe`,
and an actual recording with `--apply` — which writes one activation-log line.
An existing output path is refused unless `--overwrite` is passed.

## Usage

    microphone record <device> out.mka
    microphone record <device> out.wav --duration 5 --apply
    microphone record <device> out.mka --probe --json

## See also

- `microphone explain stream`
"""

_WHOAMI = """\
# microphone whoami

Reports the agent's identity from `culture.yaml`: nick (`suffix`), backend,
served model, and the package version. Read-only, and the `culture.yaml` it
reads is the agent's own — found by walking up from the installed module, not
from the caller's working directory.

## Usage

    microphone whoami
    microphone whoami --json
"""

_LEARN = """\
# microphone learn

Prints a structured self-teaching prompt: purpose, the three-level hardware
split (and which read-only verbs still open the USB device), the command map,
the device-selector rule, the exit-code policy, `--json` support, the consent
posture, and the `explain` pointer.

Read this before invoking anything that could open a microphone.

## Usage

    microphone learn
    microphone learn --json
"""

_EXPLAIN = """\
# microphone explain <path>

Prints markdown documentation for any noun/verb path. Unlike `--help` (terse,
positional), `explain` is global and addressable by path, so an agent can read
about `array aec set` without first constructing that command.

## Usage

    microphone explain microphone
    microphone explain list
    microphone explain array aec set
    microphone explain --json <path>
"""

_OVERVIEW = """\
# microphone overview

Read-only descriptive snapshot of the agent: identity (from `culture.yaml`), the
verb surface, which invocations energize hardware, the contracts the microphone
verbs obey, and the consent posture. Accepts an ignored `target` so a stray path
never hard-fails.

Each noun group has its own overview too — `microphone stream overview`,
`microphone array overview`, `microphone param overview`,
`microphone gain overview` — and `microphone cli overview` describes the CLI
surface itself.

## Usage

    microphone overview
    microphone overview --json
"""

_DOCTOR = """\
# microphone doctor

Checks the agent-identity invariants `steward doctor` verifies:
prompt-file-present and backend-consistency (`colleague` → `AGENTS.colleague.md`),
plus a skills-present check. Exits 1 when unhealthy.

This is an *identity* check, not a capture-readiness check: it says nothing about
whether a microphone is attached, whether a capture engine is installed, or
whether this process can open a device node. Use `microphone list` for that.

## Usage

    microphone doctor
    microphone doctor --json
"""

_CLI = """\
# microphone cli

Noun group for CLI-surface introspection. `cli overview` describes the CLI
itself — its verbs and the conventions every one of them obeys — as distinct
from the global `overview`, which describes the agent.

## Usage

    microphone cli overview
    microphone cli overview --json
"""


ENTRIES: dict[tuple[str, ...], str] = {
    (): _ROOT,
    ("microphone",): _ROOT,
    # Legacy alias: the distribution name, kept resolvable so a guess still
    # lands on the docs. Never advertised as something to type.
    ("microphone-cli",): _ROOT,
    ("list",): _LIST,
    ("inspect",): _INSPECT,
    ("gain",): _GAIN,
    ("gain", "overview"): _GAIN_OVERVIEW,
    ("gain", "get"): _GAIN_GET,
    ("gain", "set"): _GAIN_SET,
    ("array",): _ARRAY,
    ("array", "overview"): _ARRAY_OVERVIEW,
    ("array", "doa"): _ARRAY_DOA,
    ("array", "aec"): _ARRAY_AEC,
    ("array", "aec", "overview"): _ARRAY_AEC_OVERVIEW,
    ("array", "aec", "get"): _ARRAY_AEC_GET,
    ("array", "aec", "set"): _ARRAY_AEC_SET,
    ("param",): _PARAM,
    ("param", "overview"): _PARAM_OVERVIEW,
    ("param", "list"): _PARAM_LIST,
    ("param", "get"): _PARAM_GET,
    ("param", "set"): _PARAM_SET,
    ("stream",): _STREAM,
    ("stream", "overview"): _STREAM_OVERVIEW,
    ("stream", "audio"): _STREAM_AUDIO,
    ("record",): _RECORD,
    ("whoami",): _WHOAMI,
    ("learn",): _LEARN,
    ("explain",): _EXPLAIN,
    ("overview",): _OVERVIEW,
    ("doctor",): _DOCTOR,
    ("cli",): _CLI,
    ("cli", "overview"): _CLI,
}
