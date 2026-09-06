# XVF3800 parameter table — provenance and guide

`microphone_cli.xvf3800.PARAMETERS` (126 entries, keyed by name to
`(resid, cmdid, count, access, type)`) is vendored **verbatim** from Pollen
Robotics' [`reachy_mini`](https://github.com/pollen-robotics/reachy_mini),
file `src/reachy_mini/media/audio_control_utils.py`, licensed **Apache-2.0**.
The wire protocol `microphone_cli/xvf3800.py` implements is a port of the
same file, with the transport rewritten onto a stdlib `usbdevfs` ioctl
(`microphone_cli/usbctl.py`) instead of `pyusb`, since microphone-cli carries
no runtime dependencies. Two deliberate deviations from the upstream port are
called out in the module docstring — both bug fixes to how a multi-value
reply is unpacked, not changes to any parameter's identity.

The upstream reference for the protocol and every parameter's meaning is the
XMOS control-command appendix, document **XM-014888-PC**:
<https://www.xmos.com/documentation/XM-014888-PC/html/modules/fwk_xvf/doc/user_guide/AA_control_command_appendix.html>.
This file is a guide to the table `microphone-cli` carries, not a
restatement of that appendix — consult it for what an individual parameter
actually does.

## The `resid` groups

Every parameter belongs to one of five XMOS "resource id" groups. The table
below is `microphone-cli`'s parameter count per group, not the XMOS spec's:

| resid | Group | Params in this table | Example names |
|-------|-------|----------------------:|----------------|
| 48 | Application | 11 | `VERSION`, `BLD_MSG`, `BLD_HOST`, `BLD_REPO_HASH` |
| 33 | AEC (acoustic echo cancellation) | 33 | `SHF_BYPASS`, `AEC_NUM_MICS`, `AEC_MIC_ARRAY_TYPE`, `AEC_AECCONVERGED` |
| 35 | Audio manager | 28 | `AUDIO_MGR_MIC_GAIN`, `AUDIO_MGR_REF_GAIN`, `AUDIO_MGR_CURRENT_IDLE_TIME` |
| 20 | GPO / LED / DoA | 15 | `GPO_READ_VALUES`, `GPO_WRITE_VALUE`, `GPO_PORT_PIN_INDEX`, `GPO_PIN_VAL` |
| 17 | Post-processing | 39 | `PP_CURRENT_IDLE_TIME`, `PP_MIN_IDLE_TIME`, `PP_RESET_MIN_IDLE_TIME` |

`microphone array doa` reads `DOA_VALUE_RADIANS` — the direction-of-arrival
parameter, in the GPO/LED/DoA group (resid 20) alongside the array's GPIO and
LED controls. `microphone array aec get|set` reads and writes a curated
subset of the AEC group (resid 33): `AEC_AECCONVERGED`, `SHF_BYPASS`,
`AEC_HPFONOFF`, `PP_ECHOONOFF`, `AEC_NUM_MICS`, `AEC_MIC_ARRAY_TYPE`,
`AEC_MIC_ARRAY_GEO`, `AEC_RT60`. `microphone gain set --target firmware`
writes `AUDIO_MGR_MIC_GAIN` in the audio-manager group (resid 35). Every
other row in the table is reachable through `microphone param get|set`, keyed
by name, case-insensitive.

## The persistent tier

`PERSISTENT` names a fixed set of 18 parameters that either persist across a
power-cycle, trigger a reboot, or are otherwise destructive — unlike an
ordinary `rw` write, which is volatile and reverts when the array loses
power:

- `SAVE_CONFIGURATION`, `CLEAR_CONFIGURATION` — write or erase the array's
  persisted configuration.
- `REBOOT` — restarts the array firmware.
- `TEST_CORE_BURN`, `TEST_AEC_DISABLE_CONTROL` — test/diagnostic commands
  with side effects beyond a normal parameter write.
- `USB_BIT_DEPTH` — changes the USB audio interface's bit depth.
- Every `SPECIAL_CMD_*` name — filter-coefficient and equalization
  loading/offset commands (`SPECIAL_CMD_AEC_FILTER_COEFFS`,
  `SPECIAL_CMD_PP_EQUALIZATION`, `SPECIAL_CMD_NLMODEL_START`, and their
  siblings) that write into firmware-resident tables rather than an ordinary
  runtime register.

`microphone param set` on any of these names requires `--allow-persistent`
in addition to `--apply` — the two flags are deliberately separate so that
"I want to write this parameter" and "I understand this survives a
power-cycle or is destructive" are two distinct, explicit opt-ins. Every
other `rw` parameter needs only `--apply`.

## Firmware overlays

The table above is Pollen Robotics' `38fb:1001` map. Seeed's own USB firmware
(`2886:001a`, v2.1.0 at the time of writing) differs, and the CLI patches the
base table per USB vendor id in `xvf3800.FIRMWARE_OVERLAYS`:

| Name | Reachy (`38fb`) | Seeed (`2886`) |
|------|-----------------|----------------|
| `DOA_VALUE` | 20/18, 2 × `uint32` | 20/18, 2 × `uint16` (degrees 0–359, speech flag) |
| `DOA_VALUE_RADIANS` | 20/19, 2 × radians | not implemented |
| `LED_RING_COLOR` | — | 20/19, 12 × `uint32` |
| `AIC3104_HP_LEVEL`, `AIC3104_LINEOUT_LEVEL` | — | 48/11, 48/12, `uint8` |
| `GPO_PIN_PWM_DUTY`, `GPO_PIN_FLASH_MASK`, `SPECIAL_CMD_*` (NL model, equalisation) | present | not implemented |

`microphone param list --vendor 2886` prints the Seeed view. Source:
`python_control/xvf_host.py` in
[respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY).
Verified on hardware on 2026-09-06 (`docs/acceptance-microphone-domain.md`).
