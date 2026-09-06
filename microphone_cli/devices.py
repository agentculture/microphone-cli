"""Device identity core — *which capture microphones exist, and how to name one*.

This module answers exactly one question and stops: which USB audio capture
devices are attached, and what is the stable name of each. It never opens a
device, never checks permissions, never sets a gain and never reads DOA — those
are separate concerns owned elsewhere. Everything here is filesystem parsing of
``/proc/asound`` and ``/sys``, so it is safe to call from any context,
including a dry run.

Cited from ``webcam_cli/devices.py`` (lines 105-159, 286-387 and 470-531 of the
reference implementation in the sibling ``webcam-cli`` repo) with the video
half dropped: the ``/dev/v4l/by-id`` scan, the video/audio pairing and the
``LogicalDevice`` grouping have no counterpart here, while the sysfs USB-parent
walk, the ``/proc/asound/cards`` parse, the udev-style stable-id synthesis and
the selector-refusal policy carry over almost verbatim.

Four facts about Linux USB audio shape the design:

1. **ALSA card numbers are plug-order, not identity.** ``hw:1`` is a different
   microphone after a replug. Nothing here is keyed on the index:
   :attr:`MicrophoneDevice.card_index` is reported because callers see it in
   ``arecord -l``, but it is explicitly ephemeral. Persist
   :attr:`MicrophoneDevice.stable_id`; address ALSA through
   :attr:`MicrophoneDevice.alsa_address` (``hw:CARD=<id>``), which is keyed on
   the card's name and survives renumbering.

2. **Identity lives in the USB descriptors.** Each ALSA card's sysfs node hangs
   off a USB *interface* directory (``5-1.1:1.0``) below a USB *device*
   directory (``5-1.1``); the device directory carries ``idVendor``,
   ``idProduct``, ``manufacturer``, ``product`` and ``serial``. The stable id
   is rebuilt from those with udev's own escaping rules, so it matches what
   ``/dev/snd/by-id`` would publish.

3. **Two identical arrays are still two devices.** The reference host can carry
   more than one ``38fb:1001``; they differ only by serial. Selection therefore
   refuses an ambiguous selector rather than silently picking the first match.

4. **USB only.** A capture card with no USB parent in sysfs (an analog HDA
   line-in, a PCIe capture card) is skipped: it has no USB descriptors to
   derive a stable identity from. "Not listed" is not the same as "not
   present".

All reads are taken relative to ``root``, which exists so tests can point at a
synthetic tree instead of the host. ALSA addresses in the returned values are
always reported as ALSA names them and are never prefixed with ``root``.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass

from microphone_cli.cli._errors import EXIT_USER_ERROR, CliError

_SYS_DIR = "sys"
_SOUND_CLASS_DIR = "sys/class/sound"
_ASOUND_DIR = "proc/asound"
_RUNTIME_GLOB = "run/user/*/pipewire-0"

_LIST_HINT = "run `microphone list --json` to see the stable id of every attached microphone"

# XVF3800-based microphone arrays, keyed by (idVendor, idProduct).
_ARRAY_USB_IDS = frozenset({("38fb", "1001"), ("2886", "001a")})

# A sysfs USB *device* directory: "3-1", "5-1.3". Interfaces ("3-1:1.0"),
# root hubs ("usb3") and platform nodes ("NVDA8000:01") deliberately do not match.
_USB_DEVICE_RE = re.compile(r"^\d+-\d+(?:\.\d+)*$")
# " 1 [Audio          ]: USB-Audio - Reachy Mini Audio"
# Every quantifier is possessive so the match is single-pass with no
# backtracking budget to exhaust; ALSA card ids never contain whitespace.
_CARDS_LINE_RE = re.compile(
    r"^\s*+(?P<index>\d++)\s*+\[(?P<id>[^\]\s]*+)\s*+\]\s*+:\s*+(?P<rest>.*+)$"
)
# A capture PCM directory under /proc/asound/cardN: "pcm0c" (playback is "pcm0p").
_CAPTURE_PCM_RE = re.compile(r"^pcm(?P<device>\d+)c$")
# udev's device-name allowlist; everything else becomes "_".
_UDEV_UNSAFE_RE = re.compile(r"[^A-Za-z0-9#+\-.:=@_]")
# A raw ALSA card selector, which this module refuses on purpose:
# "1", "hw:1", "hw:1,0", "plughw:1".
_RAW_CARD_RE = re.compile(r"^(?:(?:plug)?hw:)?(?P<index>\d+)(?:,\d+)?$")
# The stable form of an ALSA address, which is accepted: "hw:CARD=Audio,DEV=0".
_CARD_NAME_RE = re.compile(r"^(?:plug)?hw:CARD=(?P<id>[^,]+)(?:,.*)?$", re.IGNORECASE)
# "    Channels: 6" inside the "Capture:" section of /proc/asound/cardN/stream0.
_CHANNELS_RE = re.compile(r"^\s*Channels:\s*(?P<channels>\d+)\s*$")
_SECTION_RE = re.compile(r"^(?P<section>Playback|Capture):\s*$")
# "channels: 6" in an open PCM's hw_params (only present while a client holds it).
_HW_PARAMS_CHANNELS_RE = re.compile(r"^channels:\s*(?P<channels>\d+)\s*$")

_USB_ATTRS = ("idVendor", "idProduct", "serial", "manufacturer", "product")


def is_array_ids(vendor: str | None, product: str | None) -> bool:
    """Whether a ``(idVendor, idProduct)`` pair identifies an XVF3800 array."""
    if not vendor or not product:
        return False
    return (vendor.lower(), product.lower()) in _ARRAY_USB_IDS


@dataclass(frozen=True)
class UsbIds:
    """The USB vendor and product ids of a device, as sysfs spells them."""

    vendor: str | None
    product: str | None

    def as_dict(self) -> dict[str, object]:
        return {"vendor": self.vendor, "product": self.product}

    def __str__(self) -> str:
        return f"{self.vendor or '????'}:{self.product or '????'}"


@dataclass(frozen=True)
class MicrophoneDevice:
    """One USB audio capture device.

    ``card_index`` is the current ALSA card number. It is **ephemeral** — it
    changes when devices are replugged and must never be persisted as identity.
    Persist ``stable_id``; address ALSA through ``alsa_address``.
    """

    stable_id: str
    label: str
    alsa_address: str
    card_id: str
    card_index: int
    usb_path: str
    usb_ids: UsbIds
    serial: str | None
    is_array: bool
    channels: int | None
    pipewire_visible: bool | None

    def as_dict(self) -> dict[str, object]:
        return {
            "stable_id": self.stable_id,
            "label": self.label,
            "alsa_address": self.alsa_address,
            "card_id": self.card_id,
            "card_index": self.card_index,
            "usb_path": self.usb_path,
            "usb_ids": self.usb_ids.as_dict(),
            "serial": self.serial,
            "is_array": self.is_array,
            "channels": self.channels,
            "pipewire_visible": self.pipewire_visible,
        }


# ---------------------------------------------------------------------------
# filesystem helpers (all root-relative, all failure-tolerant)
# ---------------------------------------------------------------------------


def _under(root: str, *parts: str) -> str:
    return os.path.join(root or "/", *parts)


def _listdir(path: str) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _read_attr(path: str) -> str | None:
    text = _read_text(path)
    return text.strip() if text is not None else None


def _follow(link: str) -> str | None:
    """Resolve one sysfs symlink *lexically*, relative to its own directory.

    Lexical rather than ``realpath`` so a fixture tree resolves inside itself
    instead of escaping to the host's real ``/sys``.
    """
    try:
        target = os.readlink(link)
    except OSError:
        return None
    if os.path.isabs(target):
        return os.path.normpath(target)
    return os.path.normpath(os.path.join(os.path.dirname(link), target))


def _usb_device_dir(sysfs_path: str, sys_root: str) -> str | None:
    """Walk up a resolved sysfs path to the nearest USB *device* directory.

    ``.../usb5/5-1/5-1.1/5-1.1:1.0/sound/card1`` lands on ``.../usb5/5-1/5-1.1``
    — the directory holding the USB descriptors identity is built from. Only
    components below ``sys_root`` are considered, so a fixture living at a path
    that happens to look like a USB address cannot confuse the walk.
    """
    relative = os.path.relpath(sysfs_path, sys_root)
    if relative.startswith(os.pardir):
        return None
    parts = relative.split(os.sep)
    for cut in range(len(parts), 0, -1):
        if _USB_DEVICE_RE.match(parts[cut - 1]):
            return os.path.join(sys_root, *parts[:cut])
    return None


def _usb_attrs(usb_dir: str | None) -> dict[str, str]:
    if usb_dir is None:
        return {}
    attrs: dict[str, str] = {}
    for key in _USB_ATTRS:
        value = _read_attr(os.path.join(usb_dir, key))
        if value:
            attrs[key] = value
    return attrs


def _udev_safe(text: str) -> str:
    return _UDEV_UNSAFE_RE.sub("_", text)


def _synthesise_stable_id(attrs: dict[str, str], usb_path: str) -> str:
    """Rebuild udev's ``usb-<vendor>_<model>_<serial>`` id from USB descriptors.

    Follows udev's own rules: vendor string, else vendor id; product string,
    else product id; then the serial, with unsafe characters replaced by ``_``.

    A device whose firmware ships no serial descriptor cannot be told apart
    from its twin by descriptors alone, so the id falls back to its sysfs USB
    device path (``usb-path-3-1``). That id is *stable while the device stays
    in that port* and changes if it is moved — which is the honest thing to
    report, since there is nothing better to key on.
    """
    serial = attrs.get("serial")
    if not serial:
        return f"usb-path-{usb_path}" if usb_path else "usb-path-unknown"
    vendor = attrs.get("manufacturer") or attrs.get("idVendor")
    model = attrs.get("product") or attrs.get("idProduct")
    parts = [part for part in (vendor, model, serial) if part]
    return "usb-" + "_".join(_udev_safe(part) for part in parts)


# ---------------------------------------------------------------------------
# channel count
# ---------------------------------------------------------------------------


def _channels_from_stream(root: str, index: int) -> int | None:
    """Capture channel count from ``/proc/asound/cardN/stream0``.

    The USB-audio driver publishes one block per direction; only the
    ``Capture:`` block matters. The largest ``Channels:`` value inside it is
    used, because a device with several altsets lists one line per altset and
    the widest is the one that carries every microphone of an array.
    """
    text = _read_text(_under(root, _ASOUND_DIR, f"card{index}", "stream0"))
    if text is None:
        return None
    section: str | None = None
    found: list[int] = []
    for line in text.splitlines():
        matched_section = _SECTION_RE.match(line.strip())
        if matched_section is not None:
            section = matched_section.group("section")
            continue
        matched = _CHANNELS_RE.match(line)
        if matched is not None and section == "Capture":
            found.append(int(matched.group("channels")))
    return max(found) if found else None


def _channels_from_hw_params(root: str, index: int, device: int) -> int | None:
    """Capture channel count from an open PCM's ``hw_params``, if one is open.

    ``hw_params`` reads ``closed`` while nothing holds the device, so this is a
    fallback that usually yields ``None`` — it only helps on a card whose
    driver publishes no ``stream0`` and that some other client happens to have
    open.
    """
    card_dir = _under(root, _ASOUND_DIR, f"card{index}", f"pcm{device}c")
    for sub in _listdir(card_dir):
        if not sub.startswith("sub"):
            continue
        text = _read_text(os.path.join(card_dir, sub, "hw_params"))
        for line in (text or "").splitlines():
            matched = _HW_PARAMS_CHANNELS_RE.match(line.strip())
            if matched is not None:
                return int(matched.group("channels"))
    return None


# ---------------------------------------------------------------------------
# PipeWire visibility heuristic
# ---------------------------------------------------------------------------


def _pipewire_evidence(root: str) -> bool:
    """Whether a PipeWire runtime socket exists anywhere under ``root``.

    Checking for the socket rather than talking to it keeps this module free of
    runtime dependencies and safe in a dry run: the socket is never connected
    to, only stat'ed.
    """
    return bool(glob.glob(_under(root, _RUNTIME_GLOB)))


def _pipewire_visible(has_pipewire: bool, capture_device: int | None) -> bool | None:
    """Heuristic answer to "would PipeWire enumerate this card as a source?".

    * ``None`` — no PipeWire runtime socket was found under ``root``, so the
      question is undeterminable from the filesystem alone. This is the honest
      answer on a host running bare ALSA or JACK, and the one a fixture tree
      without a socket gets.
    * ``True`` — a PipeWire socket exists and the card exposes a capture PCM,
      which is the condition PipeWire's ALSA monitor uses to publish a source.
    * ``False`` — a PipeWire socket exists but the card exposes no capture PCM.

    **Known limitation**: PipeWire also skips cards udev has tagged
    ``ACP_IGNORE``, and that tag lives in the udev database rather than in
    sysfs, so it cannot be read here. Because :func:`enumerate_devices` already
    drops cards with no capture PCM, ``False`` is unreachable through
    enumeration today — in practice this field is ``True`` or ``None``. Treat
    ``True`` as "nothing in procfs says otherwise", not as a confirmed graph
    node; only PipeWire itself can confirm that.
    """
    if not has_pipewire:
        return None
    return capture_device is not None


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------


def _capture_pcm_device(root: str, index: int) -> int | None:
    """Lowest capture PCM device number of an ALSA card, or ``None`` if it has none.

    A USB card with only ``pcmNp`` entries is a speaker, not a microphone, and
    has no business in a microphone listing.
    """
    devices = [
        int(matched.group("device"))
        for matched in (
            _CAPTURE_PCM_RE.match(entry)
            for entry in _listdir(_under(root, _ASOUND_DIR, f"card{index}"))
        )
        if matched is not None
    ]
    return min(devices) if devices else None


def _card_name(rest: str) -> str:
    """The human name from a ``/proc/asound/cards`` line's tail.

    ``USB-Audio - Reachy Mini Audio`` becomes ``Reachy Mini Audio``; a line with
    no driver prefix is returned whole.
    """
    _, separator, tail = rest.partition(" - ")
    return (tail if separator else rest).strip()


def _build(root: str, index: int, card_id: str, name: str) -> MicrophoneDevice | None:
    """Assemble one device, or ``None`` if the card is not a USB microphone."""
    capture_device = _capture_pcm_device(root, index)
    if capture_device is None:
        return None  # playback-only card

    sys_root = _under(root, _SYS_DIR)
    card_dir = _follow(_under(root, _SOUND_CLASS_DIR, f"card{index}"))
    usb_dir = _usb_device_dir(card_dir, sys_root) if card_dir else None
    if usb_dir is None:
        return None  # not USB: no descriptors, so no stable identity

    attrs = _usb_attrs(usb_dir)
    usb_path = os.path.basename(usb_dir)
    stable_id = _synthesise_stable_id(attrs, usb_path)
    channels = _channels_from_stream(root, index) or _channels_from_hw_params(
        root, index, capture_device
    )
    return MicrophoneDevice(
        stable_id=stable_id,
        label=attrs.get("product") or name or stable_id,
        # Keyed on the card *name*, not the index: the index is plug-order.
        alsa_address=f"hw:CARD={card_id}",
        card_id=card_id,
        card_index=index,
        usb_path=usb_path,
        usb_ids=UsbIds(vendor=attrs.get("idVendor"), product=attrs.get("idProduct")),
        serial=attrs.get("serial"),
        is_array=is_array_ids(attrs.get("idVendor"), attrs.get("idProduct")),
        channels=channels,
        pipewire_visible=_pipewire_visible(_pipewire_evidence(root), capture_device),
    )


def enumerate_devices(root: str = "/") -> tuple[MicrophoneDevice, ...]:
    """Return every USB audio capture device attached under ``root``.

    Playback-only cards and non-USB cards are skipped (see the module
    docstring). The result is sorted by ``stable_id`` and is a pure function of
    the filesystem: no device is opened and no ALSA library is loaded.
    """
    text = _read_text(_under(root, _ASOUND_DIR, "cards"))
    if text is None:
        return ()

    devices: list[MicrophoneDevice] = []
    for line in text.splitlines():
        matched = _CARDS_LINE_RE.match(line)
        if matched is None:
            continue
        device = _build(
            root,
            int(matched.group("index")),
            matched.group("id"),
            _card_name(matched.group("rest")),
        )
        if device is not None:
            devices.append(device)
    devices.sort(key=lambda device: (device.stable_id, device.card_index))
    return tuple(devices)


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def _user_error(message: str, remediation: str) -> CliError:
    return CliError(code=EXIT_USER_ERROR, message=message, remediation=remediation)


def _raw_card_error(selector: str, index: int, devices: tuple[MicrophoneDevice, ...]) -> CliError:
    """Refuse ``hw:N`` / ``N``, but name the stable id that should be used instead."""
    owner = next((device for device in devices if device.card_index == index), None)
    if owner is None:
        remediation = (
            f"no attached microphone is ALSA card {index}; {_LIST_HINT} "
            "and pass a stable id (they start with `usb-`)"
        )
    else:
        remediation = (
            f"ALSA card {index} is plug-order, not identity, and moves between replugs — "
            f"select {owner.stable_id!r} (or {owner.alsa_address!r}) instead; {_LIST_HINT}"
        )
    return _user_error(
        f"selector {selector!r} is an ALSA card number, not a stable microphone id",
        remediation,
    )


def _normalise(selector: str) -> str:
    """Reduce an ALSA address to the bare card id; leave anything else alone."""
    matched = _CARD_NAME_RE.match(selector)
    return matched.group("id") if matched else selector


def _exact_matches(candidate: str, devices: tuple[MicrophoneDevice, ...]) -> list[MicrophoneDevice]:
    """Devices whose stable id, card id or serial equals ``candidate`` exactly."""
    return [
        device
        for device in devices
        if candidate
        in (
            device.stable_id.casefold(),
            device.card_id.casefold(),
            (device.serial or "").casefold(),
        )
    ]


def _select(selector: str, devices: tuple[MicrophoneDevice, ...]) -> MicrophoneDevice:
    raw = selector.strip()
    if not raw:
        raise _user_error("empty microphone selector", f"pass a stable id; {_LIST_HINT}")

    raw_card = _RAW_CARD_RE.match(raw)
    if raw_card is not None:
        raise _raw_card_error(raw, int(raw_card.group("index")), devices)

    candidate = _normalise(raw).casefold()
    exact = _exact_matches(candidate, devices)
    if len(exact) == 1:
        return exact[0]

    matches = exact or [
        device
        for device in devices
        if candidate in device.stable_id.casefold() or candidate in device.label.casefold()
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise _user_error(
            f"no attached microphone matches selector {selector!r}",
            f"{_LIST_HINT}, then pass one of them or a unique substring of one",
        )
    named = ", ".join(device.stable_id for device in matches)
    raise _user_error(
        f"selector {selector!r} is ambiguous — it matches {len(matches)} microphones: {named}",
        f"pass a full stable id or a longer, unique substring; {_LIST_HINT}",
    )


def resolve(selector: str, root: str = "/") -> MicrophoneDevice:
    """Resolve a selector to exactly one microphone.

    ``selector`` may be a full ``stable_id``, an ALSA ``card_id``, a USB
    ``serial``, an ``hw:CARD=<id>`` address, or a unique case-insensitive
    substring of a stable id or a label.

    A raw ``hw:N`` / ``plughw:N`` / bare integer is refused on purpose: card
    numbering is plug-order, so accepting it would hand callers a selector that
    silently means a different microphone after a replug. The refusal names the
    stable id to use instead.

    Raises :class:`~microphone_cli.cli._errors.CliError` with
    ``code=EXIT_USER_ERROR`` (exit status 1) when nothing matches, when the
    selector is a raw card number, or when more than one microphone matches —
    in which case the message lists every candidate's stable id.
    """
    return _select(selector, enumerate_devices(root=root))
