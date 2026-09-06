"""XVF3800 vendor control protocol (read/write firmware parameters).

The XMOS XVF3800 in the Reachy Mini Audio card (and in the older ReSpeaker
XVF3800 boards) exposes its parameters over USB *vendor* control transfers:

* read  — ``bmRequestType 0xC0`` (IN | VENDOR | DEVICE), ``bRequest 0``,
  ``wValue = 0x80 | cmdid``, ``wIndex = resid``, ``wLength = payload + 1``.
  Byte 0 of the reply is a status byte: ``0`` success, ``64`` "servicer busy,
  retry", anything else an error.
* write — ``bmRequestType 0x40`` (OUT | VENDOR | DEVICE), ``bRequest 0``,
  ``wValue = cmdid``, ``wIndex = resid``, little-endian packed payload.

Parameter appendix:
https://www.xmos.com/documentation/XM-014888-PC/html/modules/fwk_xvf/doc/user_guide/AA_control_command_appendix.html

Provenance
----------
:data:`PARAMETERS` is vendored **verbatim** (name -> ``(resid, cmdid, count,
access, type)``) from Pollen Robotics' reachy_mini, file
``src/reachy_mini/media/audio_control_utils.py``, licensed **Apache-2.0**
(see that project's ``LICENSE``). The wire protocol implemented below is a
port of the same file; the transport is rewritten on stdlib ``usbdevfs``
ioctls (:mod:`microphone_cli.usbctl`) instead of pyusb, since microphone-cli
carries no runtime dependencies.

Two deliberate deviations from the upstream port, both bug fixes: upstream
returned the ``uint8`` / ``int32`` / ``uint32`` reply *including* the status
byte and without unpacking the 32-bit types; here the status byte is stripped
and 32-bit types are unpacked little-endian, so a read returns exactly
``count`` values of the declared type.
"""

from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from .cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from .usbctl import DEFAULT_TIMEOUT_MS, control_transfer

__all__ = [
    "PARAMETERS",
    "PERSISTENT",
    "KNOWN_IDS",
    "FIRMWARE_OVERLAYS",
    "SEEED_VENDOR",
    "REACHY_VENDOR",
    "parameters_for",
    "ParamInfo",
    "param_info",
    "Xvf3800",
]

#: Reply status byte values.
CONTROL_SUCCESS = 0
SERVICER_COMMAND_RETRY = 64

#: Retry budget for a busy servicer: 100 attempts, 10 ms apart.
MAX_READ_ATTEMPTS = 100
RETRY_DELAY_S = 0.01

REQUEST_TYPE_IN = 0xC0  # IN | VENDOR | DEVICE
REQUEST_TYPE_OUT = 0x40  # OUT | VENDOR | DEVICE
REQUEST = 0
READ_BIT = 0x80

#: USB ids known to speak this protocol.
KNOWN_IDS: dict[tuple[str, str], str] = {
    ("38fb", "1001"): "Reachy Mini Audio",
    ("2886", "001a"): "ReSpeaker XVF3800 (Seeed USB firmware)",
}

#: USB vendor id of Seeed Studio boards running Seeed's own USB firmware.
SEEED_VENDOR = "2886"
#: USB vendor id of Pollen Robotics' Reachy Mini Audio firmware (the base table).
REACHY_VENDOR = "38fb"

# Testing seam: replaced in tests so the retry loop costs no wall-clock time.
_sleep: Callable[[float], None] = time.sleep

# name -> (resid, cmdid, count, access, type)
# Vendored verbatim from reachy_mini/media/audio_control_utils.py (Apache-2.0).
PARAMETERS: dict[str, tuple[int, int, int, str, str]] = {
    # APPLICATION_SERVICER_RESID commands
    "VERSION": (48, 0, 3, "ro", "uint8"),
    "BLD_MSG": (48, 1, 50, "ro", "char"),
    "BLD_HOST": (48, 2, 30, "ro", "char"),
    "BLD_REPO_HASH": (48, 3, 40, "ro", "char"),
    "BLD_MODIFIED": (48, 4, 6, "ro", "char"),
    "BOOT_STATUS": (48, 5, 3, "ro", "char"),
    "TEST_CORE_BURN": (48, 6, 1, "rw", "uint8"),
    "REBOOT": (48, 7, 1, "wo", "uint8"),
    "USB_BIT_DEPTH": (48, 8, 2, "rw", "uint8"),
    "SAVE_CONFIGURATION": (48, 9, 1, "wo", "uint8"),
    "CLEAR_CONFIGURATION": (48, 10, 1, "wo", "uint8"),
    # AEC_RESID commands
    "SHF_BYPASS": (33, 70, 1, "rw", "uint8"),
    "AEC_NUM_MICS": (33, 71, 1, "ro", "int32"),
    "AEC_NUM_FARENDS": (33, 72, 1, "ro", "int32"),
    "AEC_MIC_ARRAY_TYPE": (33, 73, 1, "ro", "int32"),
    "AEC_MIC_ARRAY_GEO": (33, 74, 12, "ro", "float"),
    "AEC_AZIMUTH_VALUES": (33, 75, 4, "ro", "radians"),
    "TEST_AEC_DISABLE_CONTROL": (33, 76, 1, "wo", "uint32"),
    "AEC_CURRENT_IDLE_TIME": (33, 77, 1, "ro", "uint32"),
    "AEC_MIN_IDLE_TIME": (33, 78, 1, "ro", "uint32"),
    "AEC_RESET_MIN_IDLE_TIME": (33, 79, 1, "wo", "uint32"),
    "AEC_SPENERGY_VALUES": (33, 80, 4, "ro", "float"),
    "AEC_FIXEDBEAMSAZIMUTH_VALUES": (33, 81, 2, "rw", "radians"),
    "AEC_FIXEDBEAMSELEVATION_VALUES": (33, 82, 2, "rw", "radians"),
    "AEC_FIXEDBEAMSGATING": (33, 83, 1, "rw", "uint8"),
    "SPECIAL_CMD_AEC_FAR_MIC_INDEX": (33, 90, 2, "wo", "int32"),
    "SPECIAL_CMD_AEC_FILTER_COEFF_START_OFFSET": (33, 91, 1, "rw", "int32"),
    "SPECIAL_CMD_AEC_FILTER_COEFFS": (33, 92, 15, "rw", "float"),
    "SPECIAL_CMD_AEC_FILTER_LENGTH": (33, 93, 1, "ro", "int32"),
    "AEC_FILTER_CMD_ABORT": (33, 94, 1, "wo", "int32"),
    "AEC_AECPATHCHANGE": (33, 0, 1, "ro", "int32"),
    "AEC_HPFONOFF": (33, 1, 1, "rw", "int32"),
    "AEC_AECSILENCELEVEL": (33, 2, 2, "rw", "float"),
    "AEC_AECCONVERGED": (33, 3, 1, "ro", "int32"),
    "AEC_AECEMPHASISONOFF": (33, 4, 1, "rw", "int32"),
    "AEC_FAR_EXTGAIN": (33, 5, 1, "rw", "float"),
    "AEC_PCD_COUPLINGI": (33, 6, 1, "rw", "float"),
    "AEC_PCD_MINTHR": (33, 7, 1, "rw", "float"),
    "AEC_PCD_MAXTHR": (33, 8, 1, "rw", "float"),
    "AEC_RT60": (33, 9, 1, "ro", "float"),
    "AEC_ASROUTONOFF": (33, 35, 1, "rw", "int32"),
    "AEC_ASROUTGAIN": (33, 36, 1, "rw", "float"),
    "AEC_FIXEDBEAMSONOFF": (33, 37, 1, "rw", "int32"),
    "AEC_FIXEDBEAMNOISETHR": (33, 38, 2, "rw", "float"),
    # AUDIO_MGR_RESID commands
    "AUDIO_MGR_MIC_GAIN": (35, 0, 1, "rw", "float"),
    "AUDIO_MGR_REF_GAIN": (35, 1, 1, "rw", "float"),
    "AUDIO_MGR_CURRENT_IDLE_TIME": (35, 2, 1, "ro", "int32"),
    "AUDIO_MGR_MIN_IDLE_TIME": (35, 3, 1, "ro", "int32"),
    "AUDIO_MGR_RESET_MIN_IDLE_TIME": (35, 4, 1, "wo", "int32"),
    "MAX_CONTROL_TIME": (35, 5, 1, "ro", "int32"),
    "RESET_MAX_CONTROL_TIME": (35, 6, 1, "wo", "int32"),
    "I2S_CURRENT_IDLE_TIME": (35, 7, 1, "ro", "int32"),
    "I2S_MIN_IDLE_TIME": (35, 8, 1, "ro", "int32"),
    "I2S_RESET_MIN_IDLE_TIME": (35, 9, 1, "wo", "int32"),
    "I2S_INPUT_PACKED": (35, 10, 1, "rw", "uint8"),
    "AUDIO_MGR_SELECTED_AZIMUTHS": (35, 11, 2, "ro", "radians"),
    "AUDIO_MGR_SELECTED_CHANNELS": (35, 12, 2, "rw", "uint8"),
    "AUDIO_MGR_OP_PACKED": (35, 13, 2, "rw", "uint8"),
    "AUDIO_MGR_OP_UPSAMPLE": (35, 14, 2, "rw", "uint8"),
    "AUDIO_MGR_OP_L": (35, 15, 2, "rw", "uint8"),
    "AUDIO_MGR_OP_L_PK0": (35, 16, 2, "rw", "uint8"),
    "AUDIO_MGR_OP_L_PK1": (35, 17, 2, "rw", "uint8"),
    "AUDIO_MGR_OP_L_PK2": (35, 18, 2, "rw", "uint8"),
    "AUDIO_MGR_OP_R": (35, 19, 2, "rw", "uint8"),
    "AUDIO_MGR_OP_R_PK0": (35, 20, 2, "rw", "uint8"),
    "AUDIO_MGR_OP_R_PK1": (35, 21, 2, "rw", "uint8"),
    "AUDIO_MGR_OP_R_PK2": (35, 22, 2, "rw", "uint8"),
    "AUDIO_MGR_OP_ALL": (35, 23, 12, "rw", "uint8"),
    "I2S_INACTIVE": (35, 24, 1, "ro", "uint8"),
    "AUDIO_MGR_FAR_END_DSP_ENABLE": (35, 25, 1, "rw", "uint8"),
    "AUDIO_MGR_SYS_DELAY": (35, 26, 1, "rw", "int32"),
    "I2S_DAC_DSP_ENABLE": (35, 27, 1, "rw", "uint8"),
    # GPO_SERVICER_RESID commands
    "GPO_READ_VALUES": (20, 0, 5, "ro", "uint8"),
    "GPO_WRITE_VALUE": (20, 1, 2, "wo", "uint8"),
    "GPO_PORT_PIN_INDEX": (20, 2, 2, "rw", "uint32"),
    "GPO_PIN_VAL": (20, 3, 3, "wo", "uint8"),
    "GPO_PIN_ACTIVE_LEVEL": (20, 4, 1, "rw", "uint32"),
    "GPO_PIN_PWM_DUTY": (20, 5, 1, "rw", "uint8"),
    "GPO_PIN_FLASH_MASK": (20, 6, 1, "rw", "uint32"),
    "LED_EFFECT": (20, 12, 1, "rw", "uint8"),
    "LED_BRIGHTNESS": (20, 13, 1, "rw", "uint8"),
    "LED_GAMMIFY": (20, 14, 1, "rw", "uint8"),
    "LED_SPEED": (20, 15, 1, "rw", "uint8"),
    "LED_COLOR": (20, 16, 1, "rw", "uint32"),
    "LED_DOA_COLOR": (20, 17, 2, "rw", "uint32"),
    "DOA_VALUE": (20, 18, 2, "ro", "uint32"),
    "DOA_VALUE_RADIANS": (20, 19, 2, "ro", "radians"),
    # PP_RESID commands
    "PP_CURRENT_IDLE_TIME": (17, 70, 1, "ro", "uint32"),
    "PP_MIN_IDLE_TIME": (17, 71, 1, "ro", "uint32"),
    "PP_RESET_MIN_IDLE_TIME": (17, 72, 1, "wo", "uint32"),
    "SPECIAL_CMD_PP_NLMODEL_NROW_NCOL": (17, 90, 2, "ro", "int32"),
    "SPECIAL_CMD_NLMODEL_START": (17, 91, 1, "wo", "int32"),
    "SPECIAL_CMD_NLMODEL_COEFF_START_OFFSET": (17, 92, 1, "rw", "int32"),
    "SPECIAL_CMD_PP_NLMODEL": (17, 93, 15, "rw", "float"),
    "PP_NL_MODEL_CMD_ABORT": (17, 94, 1, "wo", "int32"),
    "SPECIAL_CMD_PP_NLMODEL_BAND": (17, 95, 1, "rw", "uint8"),
    "SPECIAL_CMD_PP_EQUALIZATION_NUM_BANDS": (17, 96, 1, "ro", "int32"),
    "SPECIAL_CMD_EQUALIZATION_START": (17, 97, 1, "wo", "int32"),
    "SPECIAL_CMD_EQUALIZATION_COEFF_START_OFFSET": (17, 98, 1, "rw", "int32"),
    "SPECIAL_CMD_PP_EQUALIZATION": (17, 99, 15, "rw", "float"),
    "PP_EQUALIZATION_CMD_ABORT": (17, 100, 1, "wo", "int32"),
    "PP_AGCONOFF": (17, 10, 1, "rw", "int32"),
    "PP_AGCMAXGAIN": (17, 11, 1, "rw", "float"),
    "PP_AGCDESIREDLEVEL": (17, 12, 1, "rw", "float"),
    "PP_AGCGAIN": (17, 13, 1, "rw", "float"),
    "PP_AGCTIME": (17, 14, 1, "rw", "float"),
    "PP_AGCFASTTIME": (17, 15, 1, "rw", "float"),
    "PP_AGCALPHAFASTGAIN": (17, 16, 1, "rw", "float"),
    "PP_AGCALPHASLOW": (17, 17, 1, "rw", "float"),
    "PP_AGCALPHAFAST": (17, 18, 1, "rw", "float"),
    "PP_LIMITONOFF": (17, 19, 1, "rw", "int32"),
    "PP_LIMITPLIMIT": (17, 20, 1, "rw", "float"),
    "PP_MIN_NS": (17, 21, 1, "rw", "float"),
    "PP_MIN_NN": (17, 22, 1, "rw", "float"),
    "PP_ECHOONOFF": (17, 23, 1, "rw", "int32"),
    "PP_GAMMA_E": (17, 24, 1, "rw", "float"),
    "PP_GAMMA_ETAIL": (17, 25, 1, "rw", "float"),
    "PP_GAMMA_ENL": (17, 26, 1, "rw", "float"),
    "PP_NLATTENONOFF": (17, 27, 1, "rw", "int32"),
    "PP_NLAEC_MODE": (17, 28, 1, "rw", "int32"),
    "PP_MGSCALE": (17, 29, 3, "rw", "float"),
    "PP_FMIN_SPEINDEX": (17, 30, 1, "rw", "float"),
    "PP_DTSENSITIVE": (17, 31, 1, "rw", "int32"),
    "PP_ATTNS_MODE": (17, 32, 1, "rw", "int32"),
    "PP_ATTNS_NOMINAL": (17, 33, 1, "rw", "float"),
    "PP_ATTNS_SLOPE": (17, 34, 1, "rw", "float"),
}

#: Names whose write persists across a reboot, reboots the device, or is
#: otherwise destructive — a later `param set` should require confirmation.
PERSISTENT: frozenset[str] = frozenset(
    {
        "SAVE_CONFIGURATION",
        "CLEAR_CONFIGURATION",
        "REBOOT",
        "TEST_CORE_BURN",
        "TEST_AEC_DISABLE_CONTROL",
        "USB_BIT_DEPTH",
    }
    | {name for name in PARAMETERS if name.startswith("SPECIAL_CMD_")}
)

_FLOAT_TYPES = ("float", "radians")
_WIDE_TYPES = ("float", "radians", "int32", "uint32")
_HALF_TYPES = ("uint16",)

# Per-firmware overlays on top of PARAMETERS, keyed by USB vendor id.
# ``None`` removes an entry the firmware does not implement.
#
# Found on hardware (2026-09-06, ReSpeaker XVF3800 USB firmware v2.1.0,
# build ``ua-io16-sqr``): Seeed's firmware has no DOA_VALUE_RADIANS, its
# DOA_VALUE is two uint16 (degrees 0-359, speech flag) rather than two uint32,
# and it adds LED_RING_COLOR and the AIC3104 output levels. Entries taken from
# respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY python_control/xvf_host.py.
FIRMWARE_OVERLAYS: dict[str, dict[str, tuple[int, int, int, str, str] | None]] = {
    SEEED_VENDOR: {
        "DOA_VALUE": (20, 18, 2, "ro", "uint16"),
        "DOA_VALUE_RADIANS": None,
        "LED_RING_COLOR": (20, 19, 12, "rw", "uint32"),
        "AIC3104_HP_LEVEL": (48, 11, 1, "rw", "uint8"),
        "AIC3104_LINEOUT_LEVEL": (48, 12, 1, "rw", "uint8"),
        "GPO_PIN_PWM_DUTY": None,
        "GPO_PIN_FLASH_MASK": None,
        "SPECIAL_CMD_NLMODEL_START": None,
        "SPECIAL_CMD_NLMODEL_COEFF_START_OFFSET": None,
        "SPECIAL_CMD_PP_NLMODEL": None,
        "SPECIAL_CMD_PP_NLMODEL_BAND": None,
        "SPECIAL_CMD_PP_NLMODEL_NROW_NCOL": None,
        "SPECIAL_CMD_PP_EQUALIZATION_NUM_BANDS": None,
        "SPECIAL_CMD_EQUALIZATION_START": None,
        "SPECIAL_CMD_EQUALIZATION_COEFF_START_OFFSET": None,
        "SPECIAL_CMD_PP_EQUALIZATION": None,
    }
}


def parameters_for(vendor: str | None) -> dict[str, tuple[int, int, int, str, str]]:
    """The parameter table for the firmware behind USB ``vendor`` (base when unknown)."""
    table = dict(PARAMETERS)
    for name, row in FIRMWARE_OVERLAYS.get((vendor or "").lower(), {}).items():
        if row is None:
            table.pop(name, None)
        else:
            table[name] = row
    return table


@dataclass(frozen=True)
class ParamInfo:
    """One row of :data:`PARAMETERS`, resolved and named."""

    name: str
    resid: int
    cmdid: int
    count: int
    access: str
    type: str
    persistent: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "resid": self.resid,
            "cmdid": self.cmdid,
            "count": self.count,
            "access": self.access,
            "type": self.type,
            "persistent": self.persistent,
        }


def param_info(name: str, vendor: str | None = None) -> ParamInfo:
    """Resolve a parameter name (case-insensitive) to its :class:`ParamInfo`.

    ``vendor`` selects the firmware overlay (see :data:`FIRMWARE_OVERLAYS`).
    """
    key = str(name).strip().upper()
    try:
        resid, cmdid, count, access, type_ = parameters_for(vendor)[key]
    except KeyError:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"unknown parameter: {name}"
            + (f" (not implemented by vendor {vendor} firmware)" if vendor else ""),
            remediation=(
                "Run `microphone param list` to see the parameter names this firmware exposes."
            ),
        ) from None
    return ParamInfo(
        name=key,
        resid=resid,
        cmdid=cmdid,
        count=count,
        access=access,
        type=type_,
        persistent=key in PERSISTENT,
    )


def _read_length(info: ParamInfo) -> int:
    """Bytes to request for a read: payload plus the leading status byte."""
    if info.type in _WIDE_TYPES:
        return info.count * 4 + 1
    if info.type in _HALF_TYPES:
        return info.count * 2 + 1
    return info.count + 1


class Xvf3800:
    """Parameter read/write against one XVF3800 device.

    Construct with either an open usbdevfs file descriptor (an ``int``, e.g.
    from :func:`microphone_cli.usbctl.open_device`) or a callable with the
    signature ``transfer(request_type, request, value, index, data_or_length)``
    returning ``bytes`` for IN transfers — the latter is what tests inject.
    """

    def __init__(
        self,
        target: int | Callable[..., Any],
        timeout_ms: int | None = None,
        vendor: str | None = None,
    ) -> None:
        #: USB vendor id, selecting the firmware overlay for name resolution.
        self.vendor = (vendor or "").lower() or None
        self._timeout_ms = DEFAULT_TIMEOUT_MS if timeout_ms is None else timeout_ms
        self._fd: int | None = None
        if callable(target):
            self._transfer = target
        else:
            self._fd = int(target)
            self._transfer = self._fd_transfer

    def _fd_transfer(
        self,
        request_type: int,
        request: int,
        value: int,
        index: int,
        data_or_length: int | bytes,
    ) -> Any:
        assert self._fd is not None
        return control_transfer(
            self._fd,
            request_type,
            request,
            value,
            index,
            data_or_length,
            timeout_ms=self._timeout_ms,
        )

    # -- read ---------------------------------------------------------------

    def read(self, name: str) -> Any:
        """Read a parameter, retrying while the servicer reports status 64."""
        info = param_info(name, self.vendor)
        if info.access == "wo":
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"{info.name} is write-only and cannot be read",
                remediation="Use `microphone param set` for write-only parameters.",
            )

        value = READ_BIT | info.cmdid
        length = _read_length(info)

        for attempt in range(MAX_READ_ATTEMPTS):
            response = self._transfer(REQUEST_TYPE_IN, REQUEST, value, info.resid, length)
            data = bytes(response)
            if not data:
                raise CliError(
                    code=EXIT_ENV_ERROR,
                    message=f"empty reply reading {info.name}",
                    remediation=(
                        "Replug the device and retry; the firmware returned no status byte."
                    ),
                )
            status = data[0]
            if status == CONTROL_SUCCESS:
                return _decode(info, data)
            if status != SERVICER_COMMAND_RETRY:
                raise CliError(
                    code=EXIT_ENV_ERROR,
                    message=f"unknown status code {status} reading {info.name}",
                    remediation=(
                        "The firmware rejected the command (status 66 usually means a length "
                        "mismatch). Check the parameter is supported by this firmware: "
                        "`microphone inspect <device> --json` shows its version and build."
                    ),
                )
            if attempt < MAX_READ_ATTEMPTS - 1:
                _sleep(RETRY_DELAY_S)

        raise CliError(
            code=EXIT_ENV_ERROR,
            message=(
                f"device stayed busy reading {info.name} "
                f"after {MAX_READ_ATTEMPTS} attempts (status 64)"
            ),
            remediation=(
                "Another controller may be holding the device — reachy-mini-daemon is the usual "
                "one. Stop it (`systemctl --user stop reachy-mini-daemon`) or wait for it to "
                "release the device, then retry."
            ),
        )

    # -- write --------------------------------------------------------------

    def write(self, name: str, values: Sequence[Any] | str) -> None:
        """Write a parameter. Refuses read-only names and wrong value counts."""
        info = param_info(name, self.vendor)
        if info.access == "ro":
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"{info.name} is read-only and cannot be written",
                remediation="Use `microphone param get` to read it.",
            )
        payload = _encode(info, values)
        self._transfer(REQUEST_TYPE_OUT, REQUEST, info.cmdid, info.resid, payload)

    # -- convenience --------------------------------------------------------

    def firmware_info(self) -> dict[str, str]:
        """Return ``{version, build, host, repo_hash}`` from the app servicer."""
        version = self.read("VERSION")
        return {
            "version": ".".join(str(int(part)) for part in version),
            "build": str(self.read("BLD_MSG")),
            "host": str(self.read("BLD_HOST")),
            "repo_hash": str(self.read("BLD_REPO_HASH")),
        }

    def close(self) -> None:
        """Close the file descriptor, if this object opened one."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "Xvf3800":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _decode(info: ParamInfo, data: bytes) -> Any:
    body = data[1:]
    if info.type == "char":
        return body.rstrip(b"\x00").decode("utf-8", errors="ignore")
    if info.type == "uint8":
        if len(body) < info.count:
            raise CliError(
                code=EXIT_ENV_ERROR,
                message=f"short reply reading {info.name}: {len(body)} of {info.count} bytes",
                remediation="Replug the device and retry.",
            )
        return list(body[: info.count])
    fmt = {"float": "f", "radians": "f", "int32": "i", "uint32": "I", "uint16": "H"}[info.type]
    need = info.count * (2 if info.type in _HALF_TYPES else 4)
    if len(body) < need:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"short reply reading {info.name}: {len(body)} of {need} bytes",
            remediation="Replug the device and retry.",
        )
    return list(struct.unpack("<" + fmt * info.count, body[:need]))


def _encode(info: ParamInfo, values: Sequence[Any] | str) -> bytes:
    if info.type == "char":
        raw = values.encode("utf-8") if isinstance(values, str) else bytes(bytearray(values))
        if len(raw) > info.count:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"{info.name} accepts at most {info.count} characters, got {len(raw)}",
                remediation=f"Shorten the value to {info.count} characters or fewer.",
            )
        return raw

    if isinstance(values, str) or len(values) != info.count:
        given = 1 if isinstance(values, str) else len(values)
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"{info.name} takes {info.count} value(s), got {given}",
            remediation=f"Pass exactly {info.count} value(s) of type {info.type}.",
        )

    try:
        if info.type in _FLOAT_TYPES:
            return struct.pack("<" + "f" * info.count, *(float(v) for v in values))
        if info.type == "uint8":
            ints = [int(v) for v in values]
            _check_int_range(info.name, ints, 0, 255, "uint8")
            return bytes(bytearray(ints))
        if info.type in _HALF_TYPES:
            ints = [int(v) for v in values]
            _check_int_range(info.name, ints, 0, 65535, "uint16")
            return struct.pack("<" + "H" * info.count, *ints)
        fmt = "i" if info.type == "int32" else "I"
        return struct.pack("<" + fmt * info.count, *(int(v) for v in values))
    except (TypeError, ValueError, struct.error) as exc:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"{info.name} value(s) not valid for type {info.type}: {exc}",
            remediation=f"Pass {info.count} value(s) that fit type {info.type}.",
        ) from exc


def _check_int_range(name: str, ints: Sequence[int], lo: int, hi: int, type_name: str) -> None:
    """Raise a user-facing :class:`CliError` naming the offending value and range.

    ``& 0xFF``-style truncation silently turns -1 into 255 and 256 into 0, so this
    check runs *before* any packing/masking — a bad value must be refused, not
    silently reinterpreted and sent to the firmware.
    """
    for value in ints:
        if not lo <= value <= hi:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=(f"{name} value {value} out of range for {type_name}: must be {lo}..{hi}"),
                remediation=f"Pass value(s) in {lo}..{hi}.",
            )
