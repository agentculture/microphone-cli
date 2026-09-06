"""``microphone learn`` — the learnability affordance.

Prints a structured self-teaching prompt. Must satisfy the agent-first rubric:
>=200 chars and mention purpose, command map, exit codes, --json, and explain.

Beyond the rubric, this text leads with the one fact an agent needs *before* it
invokes anything here: which invocations open a microphone or write to array
firmware. A tool that can open a microphone is a surveillance surface and a tool
that can write firmware parameters is a bricking surface, so "did that touch the
hardware?" must be answerable from the surface alone — and the answer is stated
first, not buried in a verb's ``--help``.

Ported from ``../webcam-cli/webcam_cli/cli/_commands/learn.py`` (the
purpose/hardware-split/command-map/exit-code section order and the
``_as_json_payload`` key set), with the video half dropped and the
array-firmware half — ``array``, ``param``, the persistent-write tier — added.
"""

from __future__ import annotations

import argparse

from microphone_cli import __version__, activation
from microphone_cli.cli._output import emit_result

_PURPOSE = (
    "Own the USB microphones and microphone arrays attached to this host: enumerate "
    "what is attached and give each a stable name, report honestly what can be opened "
    "right now, expose the array firmware an ordinary audio API cannot reach "
    "(direction-of-arrival, echo-canceller state, raw XVF3800 parameters), and hand a "
    "consumer either a live stream or a bounded recorded file. Interpreting what is "
    "*in* the audio is a speech model's job, not this tool's."
)

_AUDIENCE = [
    "media-cli — the composing consumer: it orchestrates capture across tools and "
    "calls this CLI for everything microphone-shaped rather than re-deriving it",
    "agents — anything that must decide, from the surface alone, whether an "
    "invocation opens a device",
    "operators — a human at a shell debugging an array that is not behaving",
]

_TEXT = """\
microphone — enumerate, inspect, tune, stream, and record the local USB
microphones and microphone arrays.

The installed command is `microphone`. (The project and PyPI distribution are
named microphone-cli and the import package is microphone_cli; neither is ever
typed.)

Purpose
-------
Own the USB microphones and microphone arrays attached to this host: enumerate
what is attached and give each a stable name, report honestly what can be opened
right now, expose the array firmware an ordinary audio API cannot reach
(direction-of-arrival, echo-canceller state, raw XVF3800 parameters), and hand a
consumer either a live stream or a bounded recorded file. Interpreting what is
*in* the audio is a speech model's job, not this tool's.

What touches the hardware — read this before invoking anything
--------------------------------------------------------------
Every verb sits on one of three levels, and which level you are on is readable
from the flags alone:

  (no flag)  Dry run. Resolves the device from filesystem reads, validates the
             request, prints the plan it would run. Opens no device, issues no
             USB transfer, spawns nothing, logs nothing.
  --probe    Checks the capture engine and the capture node's real access state
             (`stream audio`, `record`). Still spawns nothing.
  --apply    Commits: opens the device and streams/records, or issues the ALSA
             and firmware writes. Written to the activation log.

Read-only vendor control transfers are the middle case worth knowing about:
`inspect` (firmware identity, on arrays only), `array doa`, `array aec get`,
`param get` and `gain get` open the USB device and *read* from it with no
--apply. They change nothing, but they are not free: they need permission on the
device node, and a headless agent without the udev rule gets a typed environment
error rather than silence. Writes always need --apply.

`list` opens nothing beyond one non-blocking permission probe per capture node.

Commands
--------
  microphone list                          Attached microphones: id, ALSA address, access.
  microphone inspect <device>              Formats, rates, channels, array firmware identity.
  microphone gain overview                 Describe the gain verb group.
  microphone gain get <device>             Read the ALSA (and firmware) capture gain.
  microphone gain set <device> <value>     Plan or --apply a new gain, 0.0..1.0.
  microphone array overview                Describe the array verb group.
  microphone array doa <device>            Direction of arrival, raw firmware radians.
  microphone array aec overview            Describe the echo-canceller verb group.
  microphone array aec get <device>        Echo-canceller state (read-only).
  microphone array aec set <device>        Flip AEC switches (dry run unless --apply).
  microphone param overview                Describe the param verb group.
  microphone param list                    Every XVF3800 parameter row.
  microphone param get <device> <NAME>     Read one raw firmware parameter.
  microphone param set <device> <NAME> ... Write one raw firmware parameter.
  microphone stream overview               Describe the stream verb group.
  microphone stream audio <device>         Live microphone attachment point (unbounded).
  microphone record <device> <output>      Bounded clip to one file.
  microphone whoami                        Identity from culture.yaml.
  microphone learn                         This self-teaching prompt.
  microphone explain <path>...             Markdown docs for any noun/verb path.
  microphone overview                      Descriptive snapshot of the agent.
  microphone doctor                        Check the agent-identity invariants.
  microphone cli overview                  Describe the CLI surface itself.

Naming a device
---------------
Pass the stable id printed by `microphone list` (or a unique substring of it),
never a bare `hw:N` or a card index: ALSA card numbering is plug order and
re-enumeration order, not identity, so an index is not a reproducible
instruction. `microphone list --json` prints the id to use. Access failures
distinguish absent from present-but-forbidden from busy, and each names its own
fix.

The persistent tier
-------------------
`param set` refuses a persistent or destructive parameter unless
--allow-persistent is passed alongside --apply. An ordinary `rw` write is
volatile and reverts on the next power-cycle; a persistent one survives it,
reboots the device, or is otherwise irreversible. That gate runs before the
device is opened and before any transfer is issued.

Raw firmware values
-------------------
`array doa` reports the azimuth exactly as the firmware reports it: radians, in
the array's own frame. No degree conversion, no coordinate transform, no
re-basing onto a robot frame happens here — a consumer that needs another frame
owns that conversion and can only do it correctly from the untouched value.

Bounds
------
`record` is bounded by construction: a duration cap and a size cap always apply
and no flag combination expresses "forever". `stream audio` is unbounded by
construction: there is no --duration; stop it with SIGINT/SIGTERM or by killing
the returned pid.

Machine-readable output
-----------------------
Every command supports --json. Errors in JSON mode emit
{"code", "message", "remediation"} to stderr. Stdout and stderr never mix.

Exit-code policy
----------------
  0 success
  1 user-input error (bad flag, unknown device, unknown parameter, bad value)
  2 environment error (no capture engine, forbidden device node) — not
    retryable without a config/environment fix
  3 device busy (EBUSY) — retryable; another process holds the device
  4+ reserved

Who this is for
---------------
  media-cli    the composing consumer: it orchestrates capture across tools and
               calls this CLI for everything microphone-shaped
  agents       anything that must decide, from the surface alone, whether an
               invocation opens a device
  operators    a human at a shell debugging an array that is misbehaving

Consent
-------
Every --apply is appended to the activation log (default
~/.local/state/microphone-cli/activation.jsonl; override
$MICROPHONE_ACTIVATION_LOG), and a recording writes only to the path you name —
no hidden buffer, never to stdout. A hardware activity light CANNOT be promised:
that is device firmware, outside this tool's control. This tool records
activations; it does not prevent covert use.

More detail
-----------
  microphone explain microphone
  microphone explain list
  microphone explain array
  microphone explain param
  microphone explain stream
  microphone explain record
"""

# The registered surface, as an agent should read it. Kept in the order
# `_build_parser` registers it; ``tests/test_cli.py`` walks the live parser tree
# and fails when this list and that tree disagree.
_COMMANDS: list[dict[str, object]] = [
    {"path": ["list"], "summary": "Attached microphones: stable id, ALSA address, access state."},
    {
        "path": ["inspect"],
        "summary": "One device's capture formats, rates, channels, and array firmware identity.",
    },
    {"path": ["gain"], "summary": "Capture-gain noun group."},
    {"path": ["gain", "overview"], "summary": "Describe the gain verb group."},
    {"path": ["gain", "get"], "summary": "Read the ALSA (and, on arrays, firmware) capture gain."},
    {"path": ["gain", "set"], "summary": "Plan or --apply a new capture gain, 0.0..1.0."},
    {"path": ["array"], "summary": "Microphone-array firmware noun group."},
    {"path": ["array", "overview"], "summary": "Describe the array verb group."},
    {
        "path": ["array", "doa"],
        "summary": "Read direction-of-arrival as raw firmware radians (--watch for JSON Lines).",
    },
    {"path": ["array", "aec"], "summary": "Echo-canceller noun group."},
    {"path": ["array", "aec", "overview"], "summary": "Describe the echo-canceller verb group."},
    {"path": ["array", "aec", "get"], "summary": "Read echo-canceller state (read-only)."},
    {
        "path": ["array", "aec", "set"],
        "summary": "Flip echo-canceller switches (dry run unless --apply).",
    },
    {"path": ["param"], "summary": "Raw XVF3800 firmware-parameter noun group."},
    {"path": ["param", "overview"], "summary": "Describe the param verb group and its gates."},
    {"path": ["param", "list"], "summary": "Every XVF3800 parameter row, with access and tier."},
    {"path": ["param", "get"], "summary": "Read one raw firmware parameter."},
    {
        "path": ["param", "set"],
        "summary": "Write one raw firmware parameter (--apply, plus --allow-persistent "
        "for the persistent tier).",
    },
    {"path": ["stream"], "summary": "Live attachment-point noun group."},
    {"path": ["stream", "overview"], "summary": "Describe the stream verb group."},
    {"path": ["stream", "audio"], "summary": "Serve a live microphone stream over RTP/UDP."},
    {"path": ["record"], "summary": "Record a bounded clip to one file."},
    {"path": ["whoami"], "summary": "Identity probe from culture.yaml."},
    {"path": ["learn"], "summary": "Self-teaching prompt."},
    {"path": ["explain"], "summary": "Markdown docs by path."},
    {"path": ["overview"], "summary": "Descriptive snapshot of the agent."},
    {"path": ["doctor"], "summary": "Check the agent-identity invariants."},
    {"path": ["cli"], "summary": "CLI-surface introspection (noun group)."},
    {"path": ["cli", "overview"], "summary": "Describe the CLI surface."},
]


def _as_json_payload() -> dict[str, object]:
    return {
        "tool": "microphone-cli",
        "command": "microphone",
        "import_package": "microphone_cli",
        "version": __version__,
        "purpose": _PURPOSE,
        "audience": list(_AUDIENCE),
        "commands": [dict(entry) for entry in _COMMANDS],
        "hardware_activation": {
            "default": "dry run — resolves and validates from filesystem reads, opens no "
            "device, issues no transfer, spawns nothing, logs nothing",
            "--probe": "checks the capture engine and the capture node's real access state "
            "(stream audio, record); still spawns nothing",
            "--apply": "commits — opens the device and streams/records, or issues the ALSA "
            "and firmware writes; written to the activation log",
            "read_only_control_transfers": (
                "inspect (firmware identity), array doa, array aec get, param get and gain "
                "get open the USB device and READ from it with no --apply. They change "
                "nothing, but they need permission on the device node"
            ),
            "list": "opens nothing beyond one non-blocking permission probe per capture node",
        },
        "device_selector": (
            "the stable id printed by `microphone list` (or a unique substring); a bare "
            "hw:N or card index is refused because ALSA card numbering is plug order, "
            "not identity"
        ),
        "bounds": {
            "record": "bounded by construction — a duration cap and a size cap always "
            "apply; no flag means 'forever'",
            "stream audio": "unbounded by construction — there is no --duration; stop with "
            "SIGINT/SIGTERM or by killing the returned pid",
        },
        "persistent_tier": (
            "`param set` refuses a persistent or destructive parameter unless "
            "--allow-persistent accompanies --apply; the gate runs before the device is "
            "opened and before any transfer is issued"
        ),
        "raw_firmware_values": (
            "`array doa` reports the azimuth exactly as the firmware reports it — radians "
            "in the array's own frame, with no conversion or coordinate transform"
        ),
        "exit_codes": {
            "0": "success",
            "1": "user-input error",
            "2": "environment error — not retryable without a config fix",
            "3": "device busy (EBUSY) — retryable",
        },
        "consent": {
            "activation_log": str(activation.log_path()),
            "activation_log_env": activation.ENV_LOG_PATH,
            "bytes_written": "only to the path named on the command line — no hidden buffer",
            "activity_light": (
                "cannot be promised — a hardware activity LED is device firmware, outside "
                "this tool's control; this tool records activations, it does not prevent "
                "covert use"
            ),
        },
        "json_support": True,
        "explain_pointer": "microphone explain <path>",
    }


def cmd_learn(args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        emit_result(_as_json_payload(), json_mode=True)
    else:
        emit_result(_TEXT, json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "learn",
        help="Print a structured self-teaching prompt for agent consumers.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_learn)
