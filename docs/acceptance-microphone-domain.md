# On-device acceptance: microphone domain

Plan task t13 of `docs/plans/2026-09-06-microphone-domain.md`, run on
2026-09-06 against real hardware. Tracked in
[issue #3](https://github.com/agentculture/microphone-cli/issues/3); the
firmware bring-up it required is documented in
[issue #4](https://github.com/agentculture/microphone-cli/issues/4).

## Device

| | |
|---|---|
| Board | Seeed ReSpeaker XVF3800 4-Mic Array, XIAO ESP32-S3 variant |
| USB id | `2886:001a`, serial `114993702263100642` |
| Firmware | Seeed USB firmware v2.1.0, build `ua-io16-sqr`, repo hash `a4e685cf…` (read from the chip through `inspect`) |
| ALSA | card 1 `Array`, `hw:CARD=Array`, capture S16_LE, 2 ch, 16 kHz |
| Host | `spark` (Ubuntu, kernel 6.17, PipeWire + WirePlumber running, `alsa-utils` 1.2.9, `gst-launch-1.0` present, `dfu-util` 0.11) |
| Access | `/etc/udev/rules.d/99-microphone-cli.rules` granting `2886:001a` `MODE="0666"`, exactly the line the CLI's permission hint prints |

**Deviation from the plan.** The plan named a Reachy Mini Lite (`38fb:1001`).
The robot on hand turned out to be a Reachy Mini whose array is wired to its
own Pi, unreachable from this host, so acceptance ran on a standalone
ReSpeaker board instead (plan deviation d1). That board shipped with I2S
firmware, silent on USB, and was reflashed to USB firmware via safe mode and
`dfu-util` (issue #4). The Reachy Mini Lite check remains open in issue #3.

## Privacy posture of this run

Every recording and stream artifact was written under a `mktemp -d`
directory and deleted at the end of the step that made it. Nothing captured
from the microphone is in this repository or in the logs quoted below; only
byte counts, formats, parameter values, and timestamps survive.

## Read-only pass

All reads ran before any write and left the activation log empty.

| Verb | Evidence |
|------|----------|
| `list --json` | one device, `stable_id` `usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array_114993702263100642`, `is_array: true`, `audio_access.state: ok` on `/dev/snd/pcmC1D0c` |
| `inspect --json` | `formats: ["S16_LE"]`, `rates: [16000]`, `channels: 2`, `firmware: {version: "2.1.0", build: "ua-io16-sqr", host: "NA", repo_hash: "a4e685cf35c4c7e9c9ef7877a494a78d127c9051"}` |
| `gain get --json` | `alsa: {control: "Headset Capture Volume", numid: 10, value: 60, min: 0, max: 60}`, `firmware: {mic_gain: 90.0}` |
| `array aec get --json` | `converged: false`, `bypass: false`, `hpf: true`, `echo: true`, `num_mics: 4`, `geometry_type: 2`, 12-float geometry (±0.0333 m square), `rt60` |
| `param get` | `VERSION [2, 1, 0]`, `BLD_MSG`, `BLD_HOST`, `BLD_REPO_HASH`, `AEC_NUM_MICS [4]`, `AEC_MIC_ARRAY_TYPE [2]`, `AEC_AZIMUTH_VALUES` (4 radians), `AEC_SPENERGY_VALUES`, `LED_EFFECT [4]`, `GPO_READ_VALUES` all decode correctly |
| `param list --vendor 2886 --json` | 117 entries (the base Reachy table has 126) |

This is the first end-to-end proof of the stdlib `usbdevfs` control-transfer
path (`microphone_cli/usbctl.py`): every value above came through
`fcntl.ioctl(USBDEVFS_CONTROL)` with no `pyusb` installed. Parked unknown v5
on the spec frame is answered.

## Direction of arrival: CLI versus the vendor's reference reader

Seeed's own `python_control/respeaker_get_doa.py` (pyusb, run in a throwaway
environment) and `microphone array doa --json` were read back to back three
times, each pair within one second:

| Time (UTC) | CLI `azimuth_deg` / `speech` | Seeed reader `DOA_VALUE` / `SPEECH_DETECTED` |
|------------|------------------------------|-----------------------------------------------|
| 20:13:20.985 | 182.0 / false | 182 / 0 |
| 20:13:27.268 | 182.0 / false | 182 / 0 |
| 20:13:33.554 | 182.0 / false | 182 / 0 |

Delta 0.0 rad in every pair (success signal c21 asks for ≤ 0.01 rad).
`array doa --watch --count 3 --interval 0.3` emitted exactly three JSON
Lines with monotonic timestamps.

## Volatile writes (approved: no `--allow-persistent` at any point)

| Step | Dry run | `--apply` |
|------|---------|-----------|
| `gain set 0.5 --target alsa` | printed `amixer -c 1 cset numid=10 30`, nothing sent | readback `value: 30`; raw `amixer cget` agreed (`values=30,30`); restored to 60 |
| `param set AUDIO_MGR_MIC_GAIN 80` | printed the plan, nothing sent | `readback: [80.0]`; restored to `[90.0]` |
| `array aec set --echo off` | printed `PP_ECHOONOFF [0]`, nothing sent | `state.echo: false`; `--echo on` restored `state.echo: true` |
| `param set SAVE_CONFIGURATION 1 --apply` | — | refused, exit 1, hint names `--allow-persistent` and explains volatile versus persistent |
| `record clip.wav --duration 3` (explicit 16 kHz / 2 ch / S16LE) | printed the pipeline, wrote nothing | `bytes_written: 192044`, `stopped_reason: eos`; `file` reports `WAVE audio, Microsoft PCM, 16 bit, stereo 16000 Hz` |
| `record auto.wav --duration 2` (no format flags) | — | `bytes_written: 128044`, format `source: advertised` for all three fields |
| `stream audio --port 5004` (no format flags) | printed the pipeline, spawned nothing | child alive after 3 s; a second-process `udpsrc port=5004 ! fakesink` received 20 RTP packets; SIGINT stopped it cleanly |
| `scripts/acceptance/run.sh --writes --media` (final run) | — | 10 of 10 steps passed; the blind consumer ran the announced command for 4 s without error and depayloaded 50 RTP buffers |

The activation log gained exactly one line per applied action (16 lines for
the 16 applies above), and nothing for any dry run or read.

## Findings

Each of these was found only because the hardware was real. All are fixed in
this branch except where noted.

1. **The udev hint named the wrong device.** The permission remediation
   hardcoded `38fb:1001`; the board that was refused was `2886:001a`. The
   hint now prints the refused device's own ids.
2. **`list --root <fixture>` probed the host's real `/dev/snd` node.** The
   fixture's card 1 collided with the live card 1, so a hardware-free test
   changed answer when a device was plugged in. The node path is root-joined.
3. **The parameter map is firmware-specific.** Seeed's USB firmware has no
   `DOA_VALUE_RADIANS`; its `DOA_VALUE` is two `uint16` (degrees, speech flag),
   and it adds `LED_RING_COLOR` and the AIC3104 output levels. Reads with the
   Reachy table failed with firmware status 66 (length mismatch). Added a
   `uint16` codec and per-vendor overlays (`xvf3800.FIRMWARE_OVERLAYS`);
   `array doa` reads whichever command the firmware has and reports both
   `azimuth_deg` and `azimuth_rad`. Parked unknown v4 is answered: the map is
   not identical across `2886:001a` and `38fb:1001`. Plan deviation d2.
4. **Gain readback reported the wrong control.** The XVF3800 exposes two
   `Headset Capture Volume` controls, the second tagged `,index=1`. The
   `amixer contents` parser did not accept that suffix, so the second block's
   value overwrote the first. Fixed; the write itself had always worked.
5. **Passthrough streaming could not start.** `rtpL16pay` accepts only
   `S16BE`; the pipeline handed it `S16LE` (`could not link queue0 to
   rtpl16pay0`). An `audioconvert ! audio/x-raw,format=S16BE` stage now
   precedes the payloader.
6. **Fixed 48 kHz mono defaults could never open this device.** The exact
   caps filter (by design never falls back) failed on a 16 kHz stereo array.
   `stream audio` and `record` now default each unset field to what the
   device advertises in `/proc/asound/cardN/stream0` and record the source of
   each value (`explicit` / `advertised` / `default`).
7. **The announced RTP L16 consumer said `clock-rate=48000` for a 16 kHz
   stream.** A blind consumer following the announcement decoded at the wrong
   speed. The passthrough consumer caps now carry the negotiated
   `clock-rate` and `encoding-params`/`channels`; Opus stays at 48 kHz on the
   wire as the RTP spec requires.
8. **Bring-up needs CLI support (not fixed here).** Two USB-C ports, I2S
   firmware that is silent on USB, safe mode via the Mute button, `dfu-util`
   flashing, and a missing udev rule cost an hour of manual diagnosis.
   Tracked in issue #4. Voice-activity exposure is tracked in issue #5.

## Parked unknowns, answered on this hardware

| Park | Answer |
|------|--------|
| v2 firmware `AUDIO_MGR_MIC_GAIN` vs ALSA capture volume | Two independent stages: ALSA `Headset Capture Volume` (0–60) moved without changing the firmware value (90.0), and vice versa. |
| v3 PipeWire holding the PCM / WirePlumber reverting gain | Nobody held `pcmC1D0c` during the run; `amixer` writes persisted for the run's duration with WirePlumber running. Not observed on this host; still possible with an active PipeWire client. |
| v4 parameter map identical on `2886:001a`? | No. See finding 3. |
| v5 `usbdevfs` path unverified | Verified end to end; see the read-only pass. |

## Not covered

- The Reachy Mini Lite (`38fb:1001`) named in the plan. Its firmware's
  `DOA_VALUE_RADIANS` path is exercised only by the fake in
  `tests/test_array.py`.
- Multiple arrays attached at once (fixture `two-arrays` only).
- Opus streaming was started and ran without error for 5 s in a manual
  check, but no consumer decoded it in this run.

## How to re-run

```bash
scripts/acceptance/run.sh --device usb-Seeed_Studio_reSpeaker_XVF3800_4-Mic_Array_114993702263100642
```

Read-only by default; pass `--writes` to include the volatile gain and AEC
round trips and `--media` to include record and stream. It never passes
`--allow-persistent`.
