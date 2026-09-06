"""``microphone stream audio`` — serve a live microphone stream over RTP/UDP.

Cited (audio half only) from ``webcam-cli/webcam_cli/cli/_commands/stream.py``:
the noun group + ``_no_verb`` overview shape (lines 1490-1672) and the
``_payload`` key set (lines 1130-1193) — ``verb``, ``medium``, ``mode``,
``applied``, ``probed``, ``hardware_touched``, ``engine_checked``, ``device``,
``source``, ``request``, ``negotiation``, ``attach``, ``warmup``, ``pipeline``,
``pipeline_str``, ``bounded``, ``lifetime``, ``exclusive_access``, ``access``,
``consent``, ``started_at``, ``pid``. The video-only keys of the source
(pixel format, warm-up *frames*) are dropped rather than faked: ALSA capture
has no sensor that has to settle, so ``warmup`` here reports zero and says why.

Three levels of hardware contact, and nothing in between:

* **no flag (default)** — a dry run. Resolves the device from ``/proc``/``/sys``
  (filesystem reads only), builds the exact argv that ``--apply`` would run and
  prints it. No engine detection, no ``open()`` of a device node, no spawn:
  ``hardware_touched`` and ``engine_checked`` are both ``false``. The reported
  access state comes from a plain ``os.path.exists`` on the capture node, which
  is a stat, not an open.
* **``--probe``** — additionally detects the GStreamer engine
  (:func:`microphone_cli.engine.require_engine`, which shells out to
  ``gst-inspect-1.0`` and opens no device) and reports
  :func:`microphone_cli.access.check_access` on the capture node. Still spawns
  nothing: ``engine_checked`` is ``true``, ``hardware_touched`` stays ``false``.
* **``--apply``** — requires the engine and the elements this encode choice
  actually emits, *enforces* access (a busy device is the typed exit-3 error,
  never a silent wait), then spawns ``gst-launch-1.0`` inside an
  :func:`microphone_cli.activation.activation_scope` so the action is written
  to the activation log. Streams are unbounded by design; the pid is returned
  so the caller can stop it.

The spawn goes through the module-level :func:`_spawn` seam so tests can drive
every path without a real subprocess.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess  # nosec B404 - the spawn seam; fixed argv, never a shell
from datetime import datetime, timezone

from microphone_cli import access, activation, devices, engine
from microphone_cli.cli._commands.overview import emit_overview
from microphone_cli.cli._output import emit_result

DEFAULT_PORT = 5000
DEFAULT_HOST = "127.0.0.1"
DEFAULT_RATE = 48000
DEFAULT_CHANNELS = 1
DEFAULT_SAMPLE_FORMAT = "S16LE"

# ALSA spelling (as /proc/asound/cardN/stream0 prints it) -> GStreamer spelling.
_ALSA_TO_GST_FORMAT = {
    "S16_LE": "S16LE",
    "S16_BE": "S16BE",
    "S24_LE": "S24LE",
    "S24_3LE": "S24LE",
    "S32_LE": "S32LE",
    "U8": "U8",
    "FLOAT_LE": "F32LE",
}


def advertised_format(
    root: str,
    device: devices.MicrophoneDevice,
    *,
    rate: int | None,
    channels: int | None,
    sample_format: str | None,
) -> tuple[engine.AudioFormat, dict[str, str]]:
    """Fill unset request fields from what the device advertises in ``stream0``.

    Found on hardware: a fixed 48 kHz mono default cannot open a device that
    only offers 16 kHz stereo, and an exact caps filter never falls back. Each
    field records where its value came from (``explicit`` / ``advertised`` /
    ``default``) so the payload says what was assumed.
    """
    from microphone_cli.cli._commands.inspect import _formats_rates_channels

    formats, rates, adv_channels = _formats_rates_channels(root, device)
    source: dict[str, str] = {}

    if rate is None:
        rate, source["rate"] = (rates[0], "advertised") if rates else (DEFAULT_RATE, "default")
    else:
        source["rate"] = "explicit"
    if channels is None:
        if adv_channels:
            channels, source["channels"] = adv_channels, "advertised"
        else:
            channels, source["channels"] = DEFAULT_CHANNELS, "default"
    else:
        source["channels"] = "explicit"
    if sample_format is None:
        gst = _ALSA_TO_GST_FORMAT.get(formats[0]) if formats else None
        if gst:
            sample_format, source["sample_format"] = gst, "advertised"
        else:
            sample_format, source["sample_format"] = DEFAULT_SAMPLE_FORMAT, "default"
    else:
        source["sample_format"] = "explicit"

    return engine.AudioFormat(rate=rate, channels=channels, sample_format=sample_format), source


_JSON_HELP = "Emit structured JSON."

#: Every key :func:`_payload` emits. Exported so a test (and a reader) can
#: check the contract in one place instead of key-by-key.
PAYLOAD_KEYS = (
    "verb",
    "medium",
    "mode",
    "applied",
    "probed",
    "hardware_touched",
    "engine_checked",
    "device",
    "source",
    "request",
    "negotiation",
    "attach",
    "warmup",
    "pipeline",
    "pipeline_str",
    "bounded",
    "lifetime",
    "exclusive_access",
    "access",
    "consent",
    "started_at",
    "pid",
)

# A capture PCM directory under /proc/asound/cardN ("pcm0c"; playback is
# "pcm0p"). Recomputed here rather than imported from devices' private helper:
# this module only needs the number, and reaching into another module's
# underscore API would couple the two files' internals.
_CAPTURE_PCM_RE = re.compile(r"^pcm(?P<device>\d+)c$")

_WARMUP_NOTE = (
    "none — an ALSA capture device has no sensor that has to settle (unlike a UVC "
    "camera's auto-exposure), so this verb discards no lead-in audio"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _spawn(argv: list[str]) -> subprocess.Popen:
    """Spawn ``argv`` — the single seam every ``--apply`` path goes through.

    Fixed argv, never a shell. Tests monkeypatch this attribute, which is why
    it is a module-level function rather than an inline ``subprocess.Popen``
    call at the call site.
    """
    return subprocess.Popen(  # nosec B603 - fixed argv built by engine.py, shell=False
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def capture_node_path(device: devices.MicrophoneDevice, root: str = "/") -> str:
    """Return the ALSA capture node of ``device`` — ``/dev/snd/pcmC<card>D<n>c``.

    Joined under ``root`` so a fixture tree resolves inside itself: under a
    synthetic root the node is simply absent, which is the honest answer and
    keeps a test from ever naming a real node on the host.

    ``n`` is the card's lowest capture PCM (read from
    ``<root>/proc/asound/card<N>/pcm<n>c``), falling back to ``0`` when the
    card exposes no such directory — the near-universal case for a USB
    microphone, and a name that is at worst absent rather than wrong.
    """
    card_dir = os.path.join(root or "/", "proc", "asound", f"card{device.card_index}")
    try:
        entries = sorted(os.listdir(card_dir))
    except OSError:
        entries = []
    numbers = [
        int(matched.group("device"))
        for matched in (_CAPTURE_PCM_RE.match(entry) for entry in entries)
        if matched is not None
    ]
    pcm = min(numbers) if numbers else 0
    return os.path.join(root or "/", "dev", "snd", f"pcmC{device.card_index}D{pcm}c")


def _paper_access(node: str) -> dict[str, object]:
    """Describe the capture node without opening it (dry-run level).

    ``os.path.exists`` is a stat, not an open: it cannot energize a device, it
    cannot be refused for lack of ``audio``-group membership and it cannot
    report ``busy``. So it answers only "is anything there", and the payload
    says so with ``checked: false``.
    """
    return {
        "path": node,
        "kind": "audio",
        "checked": False,
        "state": "absent" if not os.path.exists(node) else "unchecked",
        "holder": None,
        "remediation": "",
        "note": "dry run — the node was stat'ed, never opened; pass --probe to really check",
    }


def _checked_access(node: str) -> dict[str, object]:
    report = access.check_access(node, "audio")
    holder = (
        {"pid": report.holder.pid, "command": report.holder.command}
        if report.holder is not None
        else None
    )
    return {
        "path": report.path,
        "kind": report.kind,
        "checked": True,
        "state": report.state.value,
        "holder": holder,
        "remediation": report.remediation,
        "note": "one non-blocking open(2), closed immediately; no audio was read",
    }


def _consumer(port: int, host: str) -> dict[str, str]:
    """Ready-to-run receive pipelines, one per wire codec this verb can serve."""
    return {
        "passthrough": (
            f"gst-launch-1.0 udpsrc address={host} port={port} "
            '"caps=application/x-rtp,media=audio,clock-rate=48000,encoding-name=L16" '
            "! rtpL16depay ! audioconvert ! autoaudiosink"
        ),
        "opus": (
            f"gst-launch-1.0 udpsrc address={host} port={port} "
            '"caps=application/x-rtp,media=audio,clock-rate=48000,encoding-name=OPUS" '
            "! rtpopusdepay ! opusdec ! audioconvert ! autoaudiosink"
        ),
    }


def _attach(request: dict[str, object]) -> dict[str, object]:
    host = str(request["host"])
    port = int(request["port"])  # type: ignore[arg-type]
    return {
        "mechanism": "gstreamer-udpsink",
        "transport": "udp",
        "host": host,
        "port": port,
        "uri": f"udp://{host}:{port}",
        "encode": request["encode"],
        "caps": (
            f"audio/x-raw,format={request['sample_format']},"
            f"rate={request['rate']},channels={request['channels']}"
        ),
        "streamable": True,
        "clients": (
            "UDP is unacknowledged and connectionless: anything bound to the port receives "
            "packets from the moment it starts listening — there is no rewind and no "
            "buffered history"
        ),
        "exposure": (
            f"packets go to {host}:{port} only. With the default loopback host no packet "
            "leaves this machine; pass --host to send them elsewhere, which is a routable "
            "address and is not authenticated by this tool"
        ),
        "consumer": _consumer(port, host),
    }


def _payload(
    *,
    device: devices.MicrophoneDevice,
    selector: str,
    node: str,
    request: dict[str, object],
    argv: list[str],
    mode: str,
    engine_checked: bool,
    access_state: dict[str, object],
    started_at: str | None,
    pid: int | None,
) -> dict[str, object]:
    applied = mode == "apply"
    probed = mode in ("probe", "apply")
    device_dict = device.as_dict()
    device_dict["selector"] = selector
    return {
        "verb": "stream audio",
        "medium": "audio",
        "mode": mode,
        "applied": applied,
        "probed": probed,
        "hardware_touched": applied,
        "engine_checked": engine_checked,
        "device": device_dict,
        "source": {
            "alsa_address": device.alsa_address,
            "capture_node": node,
            "card_index": device.card_index,
        },
        "request": request,
        "negotiation": {
            "requested": dict(request),
            "planned": dict(request),
            "probed": False,
            "note": (
                "the requested rate/channels/format are applied as an exact caps filter — "
                "an unsupported combination fails loudly at pipeline start rather than "
                "being silently substituted"
            ),
        },
        "attach": _attach(request),
        "warmup": {"seconds": 0.0, "basis": _WARMUP_NOTE},
        "pipeline": list(argv),
        "pipeline_str": " ".join(shlex.quote(token) for token in argv),
        "bounded": False,
        "lifetime": (
            "unbounded — the stream runs until the spawned gst-launch-1.0 is stopped "
            "(SIGINT/SIGTERM) or exits. There is no duration cap by design; use "
            "`microphone record` for a bounded artifact"
        ),
        "exclusive_access": (
            f"while this stream runs it holds {node} open — an ALSA capture PCM is "
            "single-open, so another client gets the typed busy error (exit 3) naming "
            "this process until the stream stops"
        ),
        "access": access_state,
        "consent": {
            "activation_log": str(activation.log_path()),
            "logged": applied,
            "bytes_written": (
                "none — audio goes to the announced UDP attachment point only; no file, "
                "no hidden buffer, and never to stdout"
            ),
        },
        "started_at": started_at,
        "pid": pid,
    }


def _render_text(data: dict[str, object]) -> str:
    device = data["device"]
    attach = data["attach"]
    request = data["request"]
    lines = [
        f"verb:      stream audio ({data['mode']})",
        f"device:    {device['stable_id']}",  # type: ignore[index]
        f"source:    {data['source']['alsa_address']}  ({data['source']['capture_node']})",
        f"format:    {request['sample_format']} {request['rate']} Hz "  # type: ignore[index]
        f"{request['channels']} ch, encode={request['encode']}",  # type: ignore[index]
        f"attach:    {attach['uri']}",  # type: ignore[index]
        f"access:    {data['access']['state']}",
        f"pipeline:  {data['pipeline_str']}",
    ]
    if data["pid"] is not None:
        lines.append(f"pid:       {data['pid']}")
    else:
        lines.append("hardware:  untouched — pass --apply to actually stream")
    lines.append(f"consumer:  {attach['consumer'][request['encode']]}")  # type: ignore[index]
    return "\n".join(lines)


def _required_elements(encode: str) -> list[str]:
    """The optional elements the built argv will actually name, per encode choice."""
    if encode == "opus":
        return ["audioconvert", "audioresample", "opusenc", "rtpopuspay", "udpsink"]
    return ["audioconvert", "rtpL16pay", "udpsink"]


def cmd_stream_audio(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    root = getattr(args, "root", "/") or "/"

    device = devices.resolve(args.device, root=root)
    node = capture_node_path(device, root=root)

    fmt, fmt_source = advertised_format(
        root, device, rate=args.rate, channels=args.channels, sample_format=args.format
    )
    request: dict[str, object] = {
        "rate": fmt.rate,
        "channels": fmt.channels,
        "sample_format": fmt.sample_format,
        "format_source": fmt_source,
        "encode": args.encode,
        "host": args.host,
        "port": args.port,
    }
    # Built before any hardware decision so the dry run prints exactly the argv
    # --apply would run — and so an invalid rate/port/encode is a typed user
    # error at every level, not only under --apply.
    argv = engine.build_audio_stream_argv(
        device.alsa_address, fmt, args.port, encode=args.encode, host=args.host
    )

    apply_mode = bool(args.apply)
    probe_mode = bool(args.probe) or apply_mode

    if not probe_mode:
        data = _payload(
            device=device,
            selector=args.device,
            node=node,
            request=request,
            argv=argv,
            mode="dry-run",
            engine_checked=False,
            access_state=_paper_access(node),
            started_at=None,
            pid=None,
        )
        _emit(data, json_mode=json_mode)
        return 0

    cap = engine.require_engine()
    engine.require_elements(cap, _required_elements(args.encode))

    if not apply_mode:
        data = _payload(
            device=device,
            selector=args.device,
            node=node,
            request=request,
            argv=argv,
            mode="probe",
            engine_checked=True,
            access_state=_checked_access(node),
            started_at=None,
            pid=None,
        )
        _emit(data, json_mode=json_mode)
        return 0

    # --apply: enforce access before anything is spawned, so a busy or
    # forbidden device is the typed error rather than a gst-launch crash.
    access.require_access(node, "audio")

    params = dict(request)
    params["capture_node"] = node
    started_at = _now_iso()
    with activation.activation_scope("stream audio", device.stable_id, params) as act:
        proc = _spawn(argv)
        act.params["pid"] = proc.pid
        act.params["pipeline"] = list(argv)

    data = _payload(
        device=device,
        selector=args.device,
        node=node,
        request=request,
        argv=argv,
        mode="apply",
        engine_checked=True,
        access_state=_checked_access(node),
        started_at=started_at,
        pid=proc.pid,
    )
    _emit(data, json_mode=json_mode)
    return 0


def _emit(data: dict[str, object], *, json_mode: bool) -> None:
    emit_result(data if json_mode else _render_text(data), json_mode=json_mode)


# --- overview ----------------------------------------------------------------


def stream_sections() -> list[dict[str, object]]:
    return [
        {
            "title": "Verbs",
            "items": [
                "stream audio <device> — serve a live microphone stream over RTP/UDP",
                "stream overview — this description",
            ],
        },
        {
            "title": "Hardware split",
            "items": [
                "default: a dry run — resolves the device from /proc and /sys, prints the "
                "exact gst-launch-1.0 argv, opens nothing, spawns nothing",
                "--probe: also detects the GStreamer engine and checks the capture node's "
                "access state; still spawns nothing",
                "--apply: requires the engine, enforces access (busy is exit 3), spawns the "
                "pipeline and writes one activation-log line",
            ],
        },
        {
            "title": "Attachment",
            "items": [
                f"udpsink to --host (default {DEFAULT_HOST}) on --port (default {DEFAULT_PORT})",
                "--encode passthrough (RTP L16, raw PCM) or opus (needs the opusenc element)",
                "the --json payload announces the uri, caps and a ready-to-run consumer "
                "pipeline per codec — a second process needs nothing else",
            ],
        },
        {
            "title": "Contracts",
            "items": [
                f"defaults: --rate {DEFAULT_RATE}, --channels {DEFAULT_CHANNELS}, "
                f"--format {DEFAULT_SAMPLE_FORMAT}",
                "streams are unbounded: no duration flag; stop the returned pid "
                "(use `microphone record` for a bounded artifact)",
                "no warm-up: an ALSA capture device has no sensor settle time",
                "exit codes: 1 user error, 2 engine/permission, 3 device busy",
            ],
        },
    ]


def cmd_stream_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "microphone stream",
        stream_sections(),
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_stream_overview(args)


# --- registration -------------------------------------------------------------

_HARDWARE_EPILOG = (
    "Hardware: the default dry run touches nothing (it resolves the device and prints the "
    "plan). --probe additionally detects the GStreamer engine and checks the capture node, "
    "still without spawning. --apply opens the device and streams, and is written to the "
    "activation log. Streams are unbounded: stop the returned pid."
)


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid value {raw!r}: must be a whole number") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError(f"invalid value {raw!r}: must be positive")
    return value


def _port_type(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid --port {raw!r}: must be a whole number") from exc
    return value


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "stream",
        help="Serve a live microphone stream over RTP/UDP (dry-run by default).",
        description=(
            "Expose a live audio attachment point another process can consume. "
            "Dry-run by default: nothing is opened until --apply."
        ),
    )
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)

    # parser_class must propagate, or this noun's parse errors bypass the
    # structured error contract and exit 2 instead of 1.
    noun_sub = p.add_subparsers(dest="stream_command", parser_class=type(p))

    ov = noun_sub.add_parser(
        "overview",
        help="Describe the stream verb group (verbs, hardware split, attachment).",
    )
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_stream_overview)

    audio = noun_sub.add_parser(
        "audio",
        help="Serve a live microphone stream via direct ALSA (dry-run by default).",
        epilog=_HARDWARE_EPILOG,
    )
    audio.add_argument(
        "device",
        help="Stable device id, or a unique substring of one (see 'microphone list'). "
        "A bare hw:N is refused: ALSA card numbering is plug-order, not identity.",
    )
    audio.add_argument("--json", action="store_true", help=_JSON_HELP)
    audio.add_argument(
        "--apply",
        action="store_true",
        help="Actually open the device and serve the stream (implies --probe).",
    )
    audio.add_argument(
        "--probe",
        action="store_true",
        help="Check the engine and the capture node's access state instead of describing "
        "them on paper. Spawns nothing.",
    )
    audio.add_argument(
        "--port",
        type=_port_type,
        default=DEFAULT_PORT,
        metavar="N",
        help=f"UDP port of the attachment point (default {DEFAULT_PORT}).",
    )
    audio.add_argument(
        "--host",
        default=DEFAULT_HOST,
        metavar="ADDR",
        help=f"UDP destination address (default {DEFAULT_HOST}, i.e. loopback only).",
    )
    audio.add_argument(
        "--rate",
        type=_positive_int,
        default=None,
        metavar="HZ",
        help=(
            "Sample rate. Default: the first rate the device advertises in "
            f"/proc/asound (else {DEFAULT_RATE}). Applied as an exact caps filter."
        ),
    )
    audio.add_argument(
        "--channels",
        type=_positive_int,
        default=None,
        metavar="N",
        help=f"Channel count. Default: the device's advertised count (else {DEFAULT_CHANNELS}).",
    )
    audio.add_argument(
        "--format",
        default=None,
        metavar="FMT",
        help=(
            "Sample format, GStreamer spelling. Default: the device's advertised format "
            f"(else {DEFAULT_SAMPLE_FORMAT})."
        ),
    )
    audio.add_argument(
        "--encode",
        choices=("passthrough", "opus"),
        default="passthrough",
        help="Wire codec. 'passthrough' (default) pays raw PCM as RTP L16; 'opus' "
        "encodes first (requires the opusenc element).",
    )
    audio.add_argument(
        "--root",
        default="/",
        metavar="PATH",
        help="Filesystem root to resolve the device under (default: /); mainly for "
        "pointing at a synthetic device tree in tests.",
    )
    audio.set_defaults(func=cmd_stream_audio)
