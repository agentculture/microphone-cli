"""``microphone overview`` — read-only descriptive snapshot of the agent.

Describes the agent to an agent reader: identity (from culture.yaml), the verb
surface, which invocations energize hardware, the contracts the microphone verbs
obey, and the consent posture — stated with its limits, never overstated. The
shared section/render helpers here are reused by every noun group's own
``overview`` (see :mod:`microphone_cli.cli._commands.cli`, ``.gain``, ``.array``,
``.param`` and ``.stream``).

Descriptive verbs never hard-fail on a missing target path — an optional
positional ``target`` is accepted and ignored (overview describes this agent,
not an external target), so ``overview <bogus-path>`` still exits 0.
"""

from __future__ import annotations

import argparse

from microphone_cli import activation
from microphone_cli.cli._commands.whoami import report
from microphone_cli.cli._output import emit_result

#: The registered surface, one line per verb family. Hand-maintained, and
#: ``tests/test_cli.py`` walks the live parser tree to prove every registered
#: path is represented here (a shorthand line may cover several leaf verbs).
_VERBS = [
    "list — attached microphones: stable id, ALSA address, access state",
    "inspect <device> — capture formats, rates, channels, and array firmware identity",
    "gain get|set <device> — read or change capture gain (ALSA, plus firmware on arrays)",
    "gain overview — describe the gain verb group (bare 'gain' does the same)",
    "array doa <device> — direction of arrival, reported as raw firmware radians",
    "array aec get|set <device> — echo-canceller state and the switches safe to flip",
    "array aec overview — describe the aec verb group (bare 'array aec' does the same)",
    "array overview — describe the array verb group (bare 'array' does the same)",
    "param list — every XVF3800 firmware parameter row, with access and tier",
    "param get|set <device> <NAME> [values] — read or write one raw firmware parameter",
    "param overview — describe the param verb group (bare 'param' does the same)",
    "stream audio <device> — serve a live microphone attachment point (unbounded)",
    "stream overview — describe the stream verb group (bare 'stream' does the same)",
    "record <device> <output> — record a bounded clip to one file (caps always apply)",
    "whoami — identity probe (nick, version, backend, model)",
    "learn — structured self-teaching prompt",
    "explain <path> — markdown docs for a topic",
    "overview — this descriptive snapshot",
    "doctor — check the agent-identity invariants",
    "cli overview — describe the CLI surface itself (bare 'cli' does the same)",
]

#: The single most important thing an agent needs before invoking anything here:
#: whether the invocation will open a microphone or write firmware. Readable from
#: the flags alone.
_HARDWARE = [
    "default (no flag) — dry run: resolves the device from filesystem reads, validates "
    "the request, prints the plan. Opens nothing, issues no transfer, logs nothing.",
    "--probe — checks the capture engine and the capture node's real access state "
    "(stream audio, record). Still spawns nothing.",
    "--apply — commits: opens the device and streams/records, or issues the ALSA and "
    "firmware writes; written to the activation log.",
    "read-only control transfers — inspect (firmware identity), array doa, array aec get, "
    "param get and gain get open the USB device and READ from it with no --apply; they "
    "change nothing but still need permission on the device node.",
    "list — opens nothing beyond one non-blocking permission probe per capture node.",
]

_CONTRACTS = [
    "identity is the stable id printed by `microphone list`, never a bare hw:N or card "
    "index: ALSA card numbering is plug order and re-enumeration order, not identity",
    "access is reported as distinct states — absent, forbidden and busy each name their "
    "own fix — and map onto distinct exit codes (1 user, 2 environment, 3 busy/retryable)",
    "every verb is a dry run by default; nothing opens a device or writes firmware "
    "without --probe or --apply, and every --apply leaves one activation-log line",
    "the persistent tier is gated twice: `param set` needs --allow-persistent as well as "
    "--apply for a parameter that survives a power-cycle, reboots the device, or is "
    "otherwise destructive, and the gate runs before the device is opened",
    "firmware values are reported raw: `array doa` gives azimuth in the firmware's own "
    "frame, in radians, with no degree conversion and no coordinate transform",
    "zero runtime dependencies — the standard library only, so nothing here can drag a "
    "native audio stack into a consumer's environment",
]


def _consent_items() -> list[str]:
    return [
        f"activation log: {activation.log_path()} (override ${activation.ENV_LOG_PATH})",
        "a recording writes only to the path you name — no hidden buffer, never to stdout",
        "a hardware activity light CANNOT be promised: that is device firmware, outside "
        "this tool's control. This tool records activations; it does not prevent covert use.",
    ]


def agent_sections() -> list[dict[str, object]]:
    """Sections describing the agent (used by the global verb)."""
    ident = report()
    return [
        {
            "title": "Identity",
            "items": [
                f"nick: {ident['nick']}",
                f"version: {ident['version']}",
                f"backend: {ident['backend']}",
                f"model: {ident['model']}",
            ],
        },
        {"title": "Verbs", "items": list(_VERBS)},
        {"title": "What touches the hardware", "items": list(_HARDWARE)},
        {"title": "Contracts", "items": list(_CONTRACTS)},
        {"title": "Consent", "items": _consent_items()},
    ]


def cli_sections() -> list[dict[str, object]]:
    """Sections describing the CLI surface itself (used by `cli overview`).

    ``_VERBS`` is the single source of truth for the registered surface (see
    ``test_every_registered_path_appears_in_overview_verbs`` in
    ``tests/test_cli.py``), so this reuses it verbatim rather than
    re-declaring ``cli overview`` here too.
    """
    return [
        {
            "title": "Verbs",
            "items": list(_VERBS),
        },
        {
            "title": "Conventions",
            "items": [
                "every command supports --json",
                "results to stdout, errors/diagnostics to stderr (never mixed)",
                "exit codes: 0 success, 1 user error, 2 environment error, "
                "3 device busy (retryable), 4+ reserved",
                "writes and hardware activation are opt-in: --apply commits, --probe "
                "checks the engine and access state, and the default is a dry run",
            ],
        },
    ]


def render_text(subject: str, sections: list[dict[str, object]]) -> str:
    lines = [f"# {subject}", ""]
    for section in sections:
        lines.append(f"## {section['title']}")
        for item in section["items"]:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines).rstrip()


def emit_overview(subject: str, sections: list[dict[str, object]], *, json_mode: bool) -> None:
    if json_mode:
        emit_result({"subject": subject, "sections": sections}, json_mode=True)
    else:
        emit_result(render_text(subject, sections), json_mode=False)


def cmd_overview(args: argparse.Namespace) -> int:
    # `target` is accepted for rubric compatibility (descriptive verbs must not
    # hard-fail on a missing path) but overview describes this agent itself.
    emit_overview(
        "microphone",
        agent_sections(),
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "overview",
        help="Read-only descriptive snapshot of the agent (identity, verbs, contracts, consent).",
    )
    p.add_argument(
        "target",
        nargs="?",
        help="Ignored — overview always describes this agent itself. Accepted so a "
        "stray path argument never hard-fails.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_overview)
