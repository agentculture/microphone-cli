"""GStreamer audio engine adapter: capability detection and pipeline construction.

This module owns *what the capture stack can do and how to invoke it* for
microphone-cli's audio surface. It does not own device enumeration/pairing or
CLI verbs — those are sibling concerns built on top of this one.

Zero runtime dependencies: everything shells out to the ``gst-launch-1.0`` /
``gst-inspect-1.0`` binaries via :mod:`subprocess`. No PyGObject/``gi``
import, ever — that would end the zero-runtime-dependency posture
``pyproject.toml``'s ``dependencies = []`` depends on.

Cited (audio subset only) from ``webcam-cli/webcam_cli/engine.py``:
``Capability``/detection scaffolding (module docstring + element lists,
lines 65-87), the audio pipeline builder shape (``build_audio_pipeline``,
lines 1044-1082), and ``require_engine`` (lines 351-369). Adapted for
microphone-cli's own element set (RTP/UDP streaming and Matroska/WAV
recording instead of webcam-cli's video/Matroska-only shapes) and its own
core-element list (``alsasrc``, ``audioconvert``, ``audioresample``,
``queue`` — no ``matroskamux``/``v4l2src``, which are webcam-cli concerns).

The pipeline builders (:func:`build_audio_stream_argv`,
:func:`build_audio_record_argv`) are pure string/list construction — no
subprocess call — so they are trivially unit-testable and never touch a
device. They return an argv list, never a shell string, because this project
runs subprocesses without a shell. Capability detection (:func:`detect`,
:func:`require_engine`, :func:`require_elements`) is the only part of this
module that shells out.
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 - shelling out to gst-* is the documented engine posture
from dataclasses import dataclass
from typing import Sequence

from microphone_cli.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError

GST_LAUNCH = "gst-launch-1.0"
GST_INSPECT = "gst-inspect-1.0"

# Core elements gate Capability.available: gst-launch-1.0 itself plus the
# elements every audio shape this module builds needs regardless of encode
# choice or container. ``alsasrc`` is the capture source, ``audioconvert``/
# ``audioresample`` bridge whatever format ALSA hands back to whatever an
# encoder or sink wants, and ``queue`` is present in every builder output
# (see the pipeline builders below) so a host missing it cannot build any
# pipeline this module knows how to emit.
CORE_ELEMENTS: tuple[str, ...] = ("alsasrc", "audioconvert", "audioresample", "queue")

# Optional elements: surfaced in Capability.plugins so a caller can branch on
# them (e.g. a host without opusenc can still do WAV-in-passthrough
# recording, and one without matroskamux can still stream). Missing optional
# elements never make Capability.available False; they are required
# conditionally, at the moment a builder decides to emit them.
OPTIONAL_ELEMENTS: tuple[str, ...] = (
    "opusenc",
    "matroskamux",
    "wavenc",
    "rtpopuspay",
    "rtpL16pay",
    "udpsink",
)

ALL_ELEMENTS: tuple[str, ...] = CORE_ELEMENTS + OPTIONAL_ELEMENTS

# Timeout (seconds) applied to every gst-* subprocess call in this module.
_PROBE_TIMEOUT_S = 10

# Debian/Ubuntu packages that cover this module's element set. Named
# explicitly in the install hint so a "gst-launch-1.0 not found" or
# "missing element" error tells an agent exactly what to apt install rather
# than pointing at generic GStreamer documentation.
GST_TOOLS_PACKAGE = "gstreamer1.0-tools"
GST_PLUGINS_BASE_PACKAGE = "gstreamer1.0-plugins-base"
GST_PLUGINS_GOOD_PACKAGE = "gstreamer1.0-plugins-good"
GST_ALSA_PACKAGE = "gstreamer1.0-alsa"

_INSTALL_HINT = (
    "install GStreamer with the tools plus base/good plugin sets and ALSA support, e.g. "
    f"'sudo apt install {GST_TOOLS_PACKAGE} {GST_PLUGINS_BASE_PACKAGE} "
    f"{GST_PLUGINS_GOOD_PACKAGE} {GST_ALSA_PACKAGE}'"
)


@dataclass(frozen=True)
class Capability:
    """What the GStreamer engine can do on this host."""

    gst_launch: str | None
    gst_inspect: str | None
    plugins: dict[str, bool]
    available: bool


@dataclass(frozen=True)
class AudioFormat:
    """A negotiable/negotiated audio format."""

    rate: int
    channels: int
    sample_format: str = "S16LE"


# --- capability detection ----------------------------------------------------


def _gst_inspect_supports_exists(gst_inspect: str) -> bool:
    """Detect, once per :func:`detect` call, whether ``--exists`` is available.

    Ported from webcam-cli's identical helper: ``--exists`` probes an
    element's presence without instantiating it, so it never opens a device
    node as a side effect (webcam-cli strace-verified this for ``v4l2src``;
    the same caution applies here for ``alsasrc`` and ALSA device nodes).
    Any failure to even run ``--help``, or a non-zero exit, is treated as
    "unsupported" so callers take the safe (if side-effecting) fallback path
    rather than assume a flag that might not exist.
    """
    try:
        result = subprocess.run(
            [gst_inspect, "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT_S,
            text=True,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    return "--exists" in (result.stdout or "")


def _element_present(gst_inspect: str, element: str, use_exists: bool) -> bool:
    """Probe whether one GStreamer element/plugin is present.

    Two forms, selected by ``use_exists`` (see
    :func:`_gst_inspect_supports_exists`): ``gst-inspect-1.0 --exists
    <element>`` (preferred, side-effect-free) or the plain
    ``gst-inspect-1.0 <element>`` form for an older ``gst-inspect-1.0``
    that predates ``--exists``. Any failure to even run the probe is treated
    as absent, never a crash.
    """
    argv = [gst_inspect, "--exists", element] if use_exists else [gst_inspect, element]
    try:
        result = subprocess.run(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def detect() -> Capability:
    """Detect the GStreamer engine and report its capability set.

    Never raises: an absent engine is reported as
    ``Capability(available=False, ...)``, not an exception. Use
    :func:`require_engine` when absence should be a typed error.
    """
    gst_launch = shutil.which(GST_LAUNCH)
    gst_inspect = shutil.which(GST_INSPECT)

    if gst_inspect is not None:
        use_exists = _gst_inspect_supports_exists(gst_inspect)
        plugins = {
            element: _element_present(gst_inspect, element, use_exists) for element in ALL_ELEMENTS
        }
    else:
        plugins = dict.fromkeys(ALL_ELEMENTS, False)

    core_present = all(plugins[element] for element in CORE_ELEMENTS)
    available = gst_launch is not None and core_present

    return Capability(
        gst_launch=gst_launch,
        gst_inspect=gst_inspect,
        plugins=plugins,
        available=available,
    )


def require_engine() -> Capability:
    """Return the detected :class:`Capability`, or raise a typed exit-2 error.

    Raises:
        CliError: ``code=EXIT_ENV_ERROR`` when ``gst-launch-1.0`` or any core
            element (``alsasrc``, ``audioconvert``, ``audioresample``,
            ``queue``) is missing. Carries an install hint naming the actual
            apt packages.
    """
    cap = detect()
    if cap.available:
        return cap

    if cap.gst_launch is None:
        message = f"{GST_LAUNCH} is not installed"
    else:
        missing = [element for element in CORE_ELEMENTS if not cap.plugins.get(element, False)]
        message = f"required GStreamer element(s) missing: {', '.join(missing)}"

    raise CliError(EXIT_ENV_ERROR, message, remediation=_INSTALL_HINT)


def require_elements(cap: Capability, names: Sequence[str]) -> None:
    """Raise a typed exit-2 error if ``cap`` says this host lacks any of ``names``.

    Used by callers that are about to emit an *optional* element (e.g.
    ``opusenc`` for an opus-encoded stream, ``matroskamux`` for a Matroska
    recording) beyond the always-required :data:`CORE_ELEMENTS`. Never
    degrades to a different codec/container — a missing element is a typed
    error naming the element and the package that carries it, not a silent
    substitution.
    """
    missing = [name for name in names if not cap.plugins.get(name, False)]
    if not missing:
        return
    raise CliError(
        EXIT_ENV_ERROR,
        f"required GStreamer element(s) missing: {', '.join(missing)}",
        remediation=_INSTALL_HINT,
    )


# --- pipeline construction ----------------------------------------------------


def _validate_audio_format(fmt: AudioFormat) -> None:
    if fmt.rate <= 0 or fmt.channels <= 0:
        raise CliError(
            EXIT_USER_ERROR,
            f"invalid audio format rate={fmt.rate} channels={fmt.channels}: "
            "both must be positive",
            remediation="pass a rate/channels pair the device actually supports",
        )
    if not fmt.sample_format:
        raise CliError(
            EXIT_USER_ERROR,
            "invalid audio format: sample_format must not be empty",
            remediation="pass a sample format the device supports, e.g. 'S16LE'",
        )


def _validate_port(port: int) -> None:
    if not 0 < port < 65536:
        raise CliError(
            EXIT_USER_ERROR,
            f"invalid UDP port {port}: must be between 1 and 65535",
            remediation="pass a valid UDP port number",
        )


def _audio_caps_string(fmt: AudioFormat) -> str:
    return f"audio/x-raw,format={fmt.sample_format},rate={fmt.rate},channels={fmt.channels}"


#: Buffer period (microseconds) alsasrc is explicitly told to use via its
#: ``latency-time`` property when a caller wants a duration-bounded record.
#: Fixing this rather than relying on alsasrc's own default is what makes
#: the ``num-buffers`` bound below deterministic: alsasrc emits
#: (approximately) one buffer per ``latency-time`` period, so pinning the
#: period to a known value lets us compute exactly how many buffers cover
#: ``duration_s`` instead of guessing at alsasrc's undocumented default.
_BOUNDED_LATENCY_TIME_US = 10_000


def _bounded_source_props(duration_s: float) -> tuple[int, int]:
    """Return ``(num_buffers, latency_time)`` alsasrc properties bounding a record.

    **Why ``num-buffers`` + an explicit ``latency-time`` rather than some
    other bound:** ``gst-launch-1.0`` has no built-in "run for N seconds and
    stop" flag for a live source; the two real options are (a) send SIGINT/
    EOS from the calling process after a wall-clock sleep, or (b) tell the
    source itself to stop after a fixed number of buffers. This module picks
    (b) because it keeps :func:`build_audio_record_argv` pure — the argv
    alone fully describes a bounded record, with no external timer the
    caller must also get right. ``alsasrc`` emits one buffer per
    ``latency-time`` period, so pinning ``latency-time`` to
    :data:`_BOUNDED_LATENCY_TIME_US` (10 ms) makes ``num-buffers =
    duration_s / 0.01`` an exact, reproducible bound rather than one that
    depends on alsasrc's platform-default period size.
    """
    if duration_s <= 0:
        raise CliError(
            EXIT_USER_ERROR,
            f"invalid duration {duration_s}s: must be positive",
            remediation="pass a positive duration_s, or omit it for an unbounded record",
        )
    num_buffers = max(1, round(duration_s * 1_000_000 / _BOUNDED_LATENCY_TIME_US))
    return num_buffers, _BOUNDED_LATENCY_TIME_US


def build_audio_stream_argv(
    alsa_address: str,
    fmt: AudioFormat,
    port: int,
    *,
    encode: str = "passthrough",
    host: str = "127.0.0.1",
) -> list[str]:
    """Build a ``gst-launch-1.0`` argv streaming ``alsa_address`` over RTP/UDP.

    ``alsa_address`` is a direct ALSA ``hw:`` address (e.g.
    ``hw:CARD=Mic,DEV=0`` — see ``arecord -l``); this function does not
    validate its shape, only ``fmt``/``port``/``encode``, since address
    parsing is a device concern owned elsewhere.

    ``-e`` is passed to ``gst-launch-1.0`` so that a SIGINT/shutdown sends
    EOS through the pipeline (draining ``queue`` and any encoder) instead of
    killing it mid-buffer — the difference between a stream a downstream RTP
    depayloader can close out cleanly and one it can't.

    Args:
        encode: ``"passthrough"`` pays raw PCM directly as RTP (``rtpL16pay``
            — no ``audioconvert``/``audioresample``/encoder needed beyond
            what feeds the payloader, since RTP L16 carries linear PCM as-is).
            ``"opus"`` re-encodes through ``audioconvert ! audioresample !
            opusenc`` before ``rtpopuspay``, trading CPU for bandwidth.
            Any other value is a typed user error.
    """
    _validate_audio_format(fmt)
    _validate_port(port)

    argv: list[str] = [
        GST_LAUNCH,
        "-e",
        "alsasrc",
        f"device={alsa_address}",
        "!",
        _audio_caps_string(fmt),
        "!",
        "queue",
        "!",
    ]

    if encode == "passthrough":
        argv += ["rtpL16pay", "!"]
    elif encode == "opus":
        argv += [
            "audioconvert",
            "!",
            "audioresample",
            "!",
            "opusenc",
            "!",
            "rtpopuspay",
            "!",
        ]
    else:
        raise CliError(
            EXIT_USER_ERROR,
            f"unsupported encode {encode!r}",
            remediation="use encode='passthrough' or encode='opus'",
        )

    argv += ["udpsink", f"host={host}", f"port={port}"]
    return argv


def build_audio_record_argv(
    alsa_address: str,
    fmt: AudioFormat,
    output_path: str,
    *,
    container: str = "mka",
    duration_s: float | None = None,
) -> list[str]:
    """Build a ``gst-launch-1.0`` argv recording ``alsa_address`` to ``output_path``.

    ``-e`` is passed so shutdown (SIGINT, or the ``num-buffers`` bound below
    running out) sends EOS through the pipeline, letting ``matroskamux`` /
    ``wavenc`` finalize the file's header/index rather than leaving a
    truncated one behind.

    Args:
        container: ``"mka"`` encodes ``audioconvert ! audioresample !
            opusenc ! matroskamux`` (Opus-in-Matroska); ``"wav"`` uses
            ``wavenc`` directly over the raw PCM caps (no re-encode). Any
            other value is a typed user error.
        duration_s: when given, bounds the record via ``alsasrc``'s
            ``num-buffers`` property with an explicit ``latency-time`` — see
            :func:`_bounded_source_props` for why that combination, rather
            than an external timer, is what makes this argv self-contained.
            ``None`` (the default) builds an unbounded record; the caller is
            responsible for stopping it (e.g. sending SIGINT for the ``-e``
            EOS to take effect).
    """
    _validate_audio_format(fmt)

    argv: list[str] = [GST_LAUNCH, "-e", "alsasrc", f"device={alsa_address}"]

    if duration_s is not None:
        num_buffers, latency_time = _bounded_source_props(duration_s)
        argv += [f"num-buffers={num_buffers}", f"latency-time={latency_time}"]

    argv += ["!", _audio_caps_string(fmt), "!", "queue", "!"]

    if container == "mka":
        argv += [
            "audioconvert",
            "!",
            "audioresample",
            "!",
            "opusenc",
            "!",
            "matroskamux",
            "!",
        ]
    elif container == "wav":
        argv += ["wavenc", "!"]
    else:
        raise CliError(
            EXIT_USER_ERROR,
            f"unsupported container {container!r}",
            remediation="use container='mka' or container='wav'",
        )

    argv += ["filesink", f"location={output_path}"]
    return argv
