"""ALSA mixer control via ``amixer`` — no ALSA library bindings.

Wraps ``amixer -c <card> contents`` / ``amixer -c <card> cset numid=<n> <v>``
so gain get/set can read and write the capture-volume control without linking
against ``libasound``. This keeps microphone-cli at zero runtime dependencies
(see CLAUDE.md) — ``amixer`` is a subprocess, not an import.

``-c <card>`` accepts either the ALSA card index or the card id; every caller
here passes the numeric ``card_index`` (not the stable id), matching what
``amixer -c`` and ``arecord -l`` both expect and matching the caller's other
subprocess-based tooling. A card id would also work (``amixer`` resolves both
forms) but the index is what :mod:`microphone_cli.devices` hands back as
``card_index``, so that is what is documented and tested here.

Testing seam: every function that shells out takes ``run`` (defaulting to
:func:`subprocess.run`), so tests inject a fake and no test ever calls the
real ``amixer``.
"""

from __future__ import annotations

import re
import subprocess  # nosec B404 - fixed argv, no shell, used only as a type/default
from dataclasses import dataclass
from typing import Callable, Sequence

from .cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError

__all__ = [
    "MixerControl",
    "RunFunc",
    "list_controls",
    "find_capture_volume",
    "get_gain",
    "set_gain_argv",
    "set_gain",
]

#: Signature every ``run=`` seam here expects: ``subprocess.run``-compatible.
RunFunc = Callable[..., "subprocess.CompletedProcess[str]"]

_AMIXER_MISSING_HINT = (
    "amixer was not found on PATH; install alsa-utils "
    "(e.g. `sudo apt install alsa-utils` / `sudo dnf install alsa-utils`) and retry."
)

# "numid=3,iface=MIXER,name='Mic Capture Volume'"
# A second control with the same name carries a trailing ",index=1" (found on
# hardware: the XVF3800 exposes two 'Headset Capture Volume' controls). Without
# the optional suffix the second block's value lines were attributed to the
# first control, so set_gain's readback reported the wrong control's value.
_NUMID_RE = re.compile(
    r"^numid=(?P<numid>\d+),iface=(?P<iface>[^,]+),name='(?P<name>[^']*)'"
    r"(?:,index=(?P<index>\d+))?$"
)
# "; type=INTEGER,access=rw---R--,values=1,min=0,max=30,step=0"
_ATTR_RE = re.compile(r"^;\s*type=(?P<type>[^,]+),access=(?P<access>[^,]+),(?P<rest>.*)")
# ": values=20" or ": values=20,20"
# No trailing ``$``: ``.*`` already runs to the end of the (single) line, and
# the anchor only gives the engine a reason to backtrack (SonarCloud S8786).
_VALUE_LINE_RE = re.compile(r"^:\s*values=(?P<values>.*)")
# Possessive ``\w++`` (Python >= 3.11): once a word run is consumed it is never
# given back, so a long run without ``=`` cannot make ``findall`` quadratic
# (SonarCloud S8786).
_KV_RE = re.compile(r"(\w++)=(-?\d+)")

_PREFERRED_NAME_HINTS = ("capture volume", "mic")


@dataclass(frozen=True)
class MixerControl:
    """One ``amixer contents`` block, parsed."""

    numid: int
    iface: str
    name: str
    control_type: str
    access: str
    count: int
    min: int | None
    max: int | None
    step: int | None
    values: tuple[int | str, ...]

    @property
    def value(self) -> int | str | None:
        """The first (and usually only) reported value, or ``None`` if empty."""
        return self.values[0] if self.values else None

    def as_dict(self) -> dict[str, object]:
        return {
            "control": self.name,
            "numid": self.numid,
            "iface": self.iface,
            "type": self.control_type,
            "access": self.access,
            "count": self.count,
            "value": self.value,
            "values": list(self.values),
            "min": self.min,
            "max": self.max,
            "step": self.step,
        }


def _run_amixer(argv: Sequence[str], *, run: RunFunc) -> str:
    try:
        result = run(list(argv), capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message="amixer is not installed",
            remediation=_AMIXER_MISSING_HINT,
        ) from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"amixer failed (exit {result.returncode}): {stderr or 'no output'}",
            remediation=(
                "confirm the card index with `microphone device list --json`, "
                "then retry `amixer -c <card> contents` by hand"
            ),
        )
    return result.stdout or ""


def _parse_values(raw: str) -> tuple[int | str, ...]:
    out: list[int | str] = []
    for part in raw.split(","):
        part = part.strip()
        try:
            out.append(int(part))
        except ValueError:
            out.append(part)
    return tuple(out)


def _header_fields(line: str) -> dict[str, object] | None:
    """Parse a ``numid=...`` header line into a fresh ``current`` dict, or
    ``None`` if ``line`` is not a header."""
    header = _NUMID_RE.match(line)
    if header is None:
        return None
    return {
        "numid": int(header.group("numid")),
        "iface": header.group("iface"),
        "name": header.group("name"),
    }


def _apply_attr_line(current: dict[str, object], line: str) -> bool:
    """Merge a ``; type=...`` attribute line into ``current``. Returns whether
    ``line`` was an attribute line at all."""
    attr = _ATTR_RE.match(line)
    if attr is None:
        return False
    current["type"] = attr.group("type")
    current["access"] = attr.group("access")
    for key, value in _KV_RE.findall(attr.group("rest")):
        if key == "values":
            current["count"] = int(value)
        elif key in ("min", "max", "step"):
            current[key] = int(value)
    return True


def _apply_value_line(current: dict[str, object], line: str) -> bool:
    """Merge a ``: values=...`` line into ``current``. Returns whether
    ``line`` was a value line at all."""
    value_line = _VALUE_LINE_RE.match(line)
    if value_line is None:
        return False
    current["values"] = _parse_values(value_line.group("values"))
    return True


def _control_from_fields(fields: dict[str, object]) -> MixerControl:
    return MixerControl(
        numid=fields["numid"],  # type: ignore[arg-type]
        iface=fields["iface"],  # type: ignore[arg-type]
        name=fields["name"],  # type: ignore[arg-type]
        control_type=fields.get("type", ""),  # type: ignore[arg-type]
        access=fields.get("access", ""),  # type: ignore[arg-type]
        count=fields.get("count", 0),  # type: ignore[arg-type]
        min=fields.get("min"),  # type: ignore[arg-type]
        max=fields.get("max"),  # type: ignore[arg-type]
        step=fields.get("step"),  # type: ignore[arg-type]
        values=fields.get("values", ()),  # type: ignore[arg-type]
    )


def list_controls(card: str | int, *, run: RunFunc = subprocess.run) -> list[MixerControl]:
    """Parse ``amixer -c <card> contents`` into every :class:`MixerControl`.

    Never raises for a card with zero controls (returns ``[]``); raises
    :class:`CliError` (``code=2``) if ``amixer`` is missing or exits non-zero.
    """
    text = _run_amixer(["amixer", "-c", str(card), "contents"], run=run)

    controls: list[MixerControl] = []
    current: dict[str, object] | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        header = _header_fields(line)
        if header is not None:
            if current is not None:
                controls.append(_control_from_fields(current))
            current = header
            continue
        if current is None:
            continue
        if _apply_attr_line(current, line):
            continue
        _apply_value_line(current, line)

    if current is not None:
        controls.append(_control_from_fields(current))
    return controls


def find_capture_volume(controls: Sequence[MixerControl]) -> MixerControl | None:
    """Pick the capture-gain control: prefer a name containing "Capture Volume",
    then any name containing "Mic"; ``None`` if neither is present."""
    lowered = [(control, control.name.lower()) for control in controls]
    for hint in _PREFERRED_NAME_HINTS:
        for control, name in lowered:
            if hint in name:
                return control
    return None


def get_gain(card: str | int, *, run: RunFunc = subprocess.run) -> MixerControl:
    """Return the card's capture-volume control.

    Raises :class:`CliError` (``code=1``) if the card exposes no capture
    control amixer can name.
    """
    controls = list_controls(card, run=run)
    control = find_capture_volume(controls)
    if control is None:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"card {card} has no capture-volume mixer control",
            remediation=(
                f"run `amixer -c {card} contents` to inspect the controls this card exposes"
            ),
        )
    return control


def set_gain_argv(card: str | int, numid: int, value: int) -> list[str]:
    """The exact ``amixer`` argv :func:`set_gain` would issue — pure, for dry-run display."""
    return ["amixer", "-c", str(card), "cset", f"numid={numid}", str(value)]


def set_gain(
    card: str | int,
    control: MixerControl,
    value: int,
    *,
    run: RunFunc = subprocess.run,
) -> MixerControl:
    """Write ``value`` to ``control`` via ``amixer cset``, then re-read it.

    Returns the control's post-write state (a fresh :func:`get_gain`), so
    callers report what the hardware actually holds rather than the value they
    asked for.
    """
    _run_amixer(set_gain_argv(card, control.numid, value), run=run)
    return get_gain(card, run=run)
