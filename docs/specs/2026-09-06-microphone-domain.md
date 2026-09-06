# microphone domain

> microphone-cli enumerates USB microphones and arrays by stable id, inspects channels and formats, gets and sets gain, and reads direction-of-arrival and AEC state from XVF3800-class array firmware — zero runtime deps, --json everywhere, and a contract media-cli can compose
> instruction: verified when every announced verb runs with --json in a clean venv (uv run microphone `<verb>` --json) and issue #3's on-device checklist is complete

## Audience

- Agents (media-cli, reachy-mini-cli, Culture mesh agents) and operators driving an XVF3800-class USB microphone array from a shell, plus media-cli as an importing/subprocessing consumer
  - instruction: learn.py names both readers; README Scope section names media-cli as the consumer

## Before → After

- Before: microphone-cli is a bare template scaffold (whoami/learn/explain/overview/doctor/cli only, template prose, prog mismatch); the only way to read DoA/AEC or set XVF3800 gain today is `reachy_mini`'s `audio_control_utils.py`, which needs pyusb and the full SDK
  - instruction: git show ed00ba0:`microphone_cli`/cli/`__init__.py` shows no domain verbs; `reachy_mini` `audio_control_utils.py` imports usb.core
- After: 'microphone list/inspect/gain/array doa/array aec' work with --json and typed exit codes 0/1/2/3, stdlib only; a blind consumer can enumerate a mic by stable id, read its formats, read azimuth+speech flag, read AEC state, and set gain under --apply — all without `reachy_mini` or pyusb installed
  - instruction: uv run microphone `<verb>` --json in a venv with only microphone-cli installed; on-device run tracked in issue #3

## Why it matters

- media-cli needs a microphone peer the way it needs webcam-cli, and the XVF3800's DoA/AEC controls are currently locked behind a robot SDK; a zero-dep agent-first CLI makes them a composable contract instead of a copy-pasted parameter table
  - instruction: media-cli CLAUDE.md:23 routes capture to peers; issue #3 records the device to prove it on

## Requirements

- Domain modules land under `microphone_cli`/ as frozen dataclasses with `as_dict`(), mirroring `webcam_cli`/devices.py:105-159 (VideoNode/AudioCard/LogicalDevice) — AudioCard.`alsa_address` 'hw:CARD=...' is the stable handle, card index is ephemeral
  - honesty: list output on the host-baseline fixture tree is byte-identical across a renumber fixture (card index changes, `stable_id` does not)
- Access model reuses `webcam_cli`/access.py's AccessState {ok,absent,forbidden,busy} → exit 1/2/3, with audio-group remediation for FORBIDDEN and /proc/\*/fd holder lookup for BUSY; this repo's `_errors.py` (`EXIT_SUCCESS`/USER/ENV only) gains `EXIT_BUSY_ERROR`=3
  - honesty: `EXIT_BUSY_ERROR`=3 is raised when another process holds the PCM open, with the holder pid/command in the message when /proc is readable
- New verbs: 'list' (--root PATH, --json → {devices,count}), 'inspect `<device>`' (channels, formats, rates from /proc/asound/cardN/stream0 and pcm\*c), 'gain get|set `<device>` \[--apply\]', 'array doa `<device>`', 'array aec get|set'; each registered in `_build_parser`, given a catalog.py entry, a learn.py command-map entry, an overview.`_VERBS` line, and every noun group exposes '`<noun>` overview' with `parser_class`=type(p)
  - honesty: tests/`test_cli.py` asserts `registered_paths` == `known_paths` and every path appears in overview.`_VERBS` and learn commands
- Gain get/set is new ground with no webcam-cli precedent (`webcam_cli` has no control API; only warm-up frame discard, engine.py:110-148). Implement via ALSA mixer: 'amixer -c `<card>` cget/cset' subprocess (present, alsa-utils 1.2.9) for generic USB mics, and `AUDIO_MGR_MIC_GAIN` (resid 35, cmd 0, float) via the XVF3800 vendor control path for arrays
  - honesty: gain get/set on a non-array USB mic works through amixer alone; on an XVF3800 it also reports `AUDIO_MGR_MIC_GAIN`
- DoA and AEC are read/written through USB vendor control transfers exactly as `reachy_mini`/media/`audio_control_utils.py` does for the XVF3800: bRequest=0, wValue=cmdid (|0x80 for read), wIndex=resid, `CTRL_TYPE_VENDOR`|`RECIPIENT_DEVICE`; `DOA_VALUE_RADIANS`=(resid 20, cmd 19, 2 floats: azimuth radians + speech flag); AEC params on resid 33 (`AEC_AECCONVERGED` cmd 3 ro, `AEC_HPFONOFF` cmd 1 rw, `SHF_BYPASS` cmd 70 rw, `AEC_NUM_MICS` cmd 71, `AEC_MIC_ARRAY_GEO` cmd 74) and `PP_ECHOONOFF` (resid 17, cmd 23); read status byte 0=ok, 64=retry; the XMOS control-command appendix (XM-014888-PC) is the upstream reference
  - honesty: a fake ioctl layer replays the SDK's documented request bytes (bRequest 0, wValue cmd|0x80, wIndex resid) and the parser decodes status byte 0/64 and little-endian floats exactly as `audio_control_utils.py` does
- Video is out of scope. Audio capture verbs ARE in scope: 'stream audio `<device>`' and 'record `<device>` `<output>`' over GStreamer alsasrc (subprocess gst-launch-1.0, same dry-run/--probe/--apply split and typed engine-missing exit 2 as `webcam_cli`/engine.py), so a consumer can attach to or save the array's processed output without webcam-cli
  - honesty: stream audio never opens the device without --apply; dry-run prints the pipeline argv and exits 0 with `hardware_touched`=false
  - honesty: record without --apply writes no file; with --apply the output is bounded by --duration/--max-bytes
- Console-script/prog mismatch is fixed as part of this work: prog becomes 'microphone' to match \[project.scripts\] (webcam-cli did the same, `__init__.py`:9-16, guarded by `test_no_user_facing_string_presents_webcam_cli_as_a_command`); all template prose (learn.py:15-55, catalog.py:15,84, `__init__.py`:74, whoami.py:7, overview.py:4) is rewritten and a `test_no_template_prose_survives` test is ported
  - honesty: grep -r 'microphone-cli ' on --help, learn, overview, catalog finds no runnable-command usage; prog == 'microphone'
- media-cli contract: expose both an importable API (`microphone_cli`.devices.`enumerate_devices`/resolve, `microphone_cli`.array.`read_doa`) and the --json CLI, because media-cli CLAUDE.md:230-245 (Q1) has not decided between import and subprocess; device ids are stable (ALSA card id + USB serial), formats are reported not assumed, and ALSA-visible-but-PipeWire-invisible devices are flagged
  - honesty: from `microphone_cli`.devices import `enumerate_devices` works in a clean venv and returns the same dicts 'microphone list --json' prints
- Docs follow webcam-cli's shape: README gains Status/Scope/'What comes out'/'Why device identity is the hard part' sections and a verb table; docs/specs, docs/plans, docs/deliveries and a docs/acceptance-\*.md land through the devague flow; docs/skill-sources.md is re-synced to guildmaster (webcam-cli's lists 8 skills, this repo's lists 7 and still cites ../devague directly)
  - honesty: README has Status/Scope/CLI-table sections and docs/skill-sources.md matches webcam-cli's guildmaster provenance
- The stdlib control-transfer layer packs struct `usbdevfs_ctrltransfer` exactly as /usr/include/linux/`usbdevice_fs`.h:40-48 (u8 bRequestType, u8 bRequest, u16 wValue, u16 wIndex, u16 wLength, u32 timeout ms, void\* data) and issues `USBDEVFS_CONTROL` = `_IOWR`('U', 0, struct); the node must be opened `O_RDWR`, so on a host without a udev rule /dev/bus/usb/BBB/DDD (default 0664 root:root) yields FORBIDDEN exit 2 whose remediation prints the udev rule line to add
  - honesty: a unit test asserts struct.calcsize of the packed layout equals ctypes.sizeof(the ctypes Structure) and the ioctl number matches the header; the forbidden path's hint contains 'SUBSYSTEM=="usb", ATTR{idVendor}=='
- Two arrays with the same VID:PID must be addressable: the stable id is built from the USB serial (sysfs 'serial' attr) with the sysfs device path as fallback; an ambiguous selector exits 1 listing candidates, as `webcam_cli`/devices.py:470-531 resolve() does
  - honesty: a fixture tree with two 38fb:1001 devices resolves each by serial and rejects the bare product name as ambiguous
- array doa --watch lifecycle: SIGINT ends the loop with exit 0 after flushing; device unplug mid-loop (ENODEV/ENOENT on the node) ends with exit 2 and one error object on stderr, never a traceback; each poll re-resolves nothing (the fd stays open) so USB bus/dev renumbering cannot silently switch devices
  - honesty: a fake ioctl raising OSError(ENODEV) on the third poll produces exactly two stdout lines, one stderr error, exit 2
- param set treats persistence and destructive commands as a separate tier: `SAVE_CONFIGURATION`, `CLEAR_CONFIGURATION`, REBOOT, `TEST_CORE_BURN`, `TEST_AEC_DISABLE_CONTROL` and the `SPECIAL_CMD_`\* filter/model uploads require --apply AND --allow-persistent, and the hint explains that every other rw write is volatile and reverts on power-cycle — that volatility is the rollback path
  - honesty: param set REBOOT --apply without --allow-persistent exits 1 and sends nothing; the same with --allow-persistent sends the transfer and is written to the activation log
- Every --apply action (record, stream audio, gain set, aec set, param set) is appended to an activation log modelled on `webcam_cli`/activation.py: JSONL under `XDG_STATE_HOME` with a `MICROPHONE_ACTIVATION_LOG` override, recording verb, device stable id, parameters written, and timestamps, so a consumer or operator can audit what touched the microphone
  - honesty: after gain set --apply the log's last line parses as JSON with verb, device, and the value written; without --apply nothing is appended

## Honesty conditions

- each verb named in the announcement exists in the live argparse tree and has a catalog entry
- pyproject dependencies stays \[\] and 'import usb' appears nowhere in `microphone_cli`/
- 38fb:1001 and 2886:001a are matched from /sys/bus/usb/devices/\*/idVendor+idProduct; any other id is reported as 'not an XVF3800 array' rather than probed
- doctor --json shape is unchanged: {healthy, checks:\[{id,passed,severity,message,remediation}\]} with only identity checks
- no write path (gain set, aec set, `SAVE_CONFIGURATION`) issues a control transfer or amixer cset unless --apply is present
- the full suite passes with /dev/snd and /dev/bus/usb absent (fixture root only)
- learn.py --json 'audience' names media-cli
- the CHANGELOG entry for the domain release cites the scaffold commit it replaces
- a blind-consumer script (like webcam-cli scripts/acceptance/blind-consumer.sh) drives every verb from --json output alone
- media-cli can consume microphone-cli without a decision on import-vs-subprocess because both surfaces exist
- CI lint job output shows 26/26 passed
- acceptance doc records the two DoA readings side by side with timestamps
- tests.yml diff against main is empty
- a fake ioctl that returns status 64 twice then 0 yields a successful read; a fake that returns 64 forever exits 2 with a 'device busy with another controller' hint
- `microphone_cli`/engine.py's element list and `require_engine`() behaviour match `webcam_cli`/engine.py for the audio subset
- docs/ names the `reachy_mini` source file and license for the table; inspect --json on an array includes firmware.version and firmware.build
- param set on an unknown name, on a ro name, or with the wrong value count each exit 1 with a hint and issue no transfer

## Success signals

- teken cli doctor --strict stays 26/26 green and coverage >= 60% with every new verb having a catalog entry, learn entry and overview line (parity tests ported from webcam-cli tests/`test_cli.py`:190-256)
  - instruction: uv run teken cli doctor . --strict; uv run pytest --cov=`microphone_cli`
- On the Reachy Mini Lite, 'microphone array doa --json' returns azimuth within 0.01 rad of `reachy_mini`'s `audio_control_utils.py` `DOA_VALUE_RADIANS` read taken within 1 s, and 'array aec get --json' reports >= 5 AEC fields; 'gain set --apply' round-trips to the same value on 'gain get'
  - instruction: issue #3 checklist; evidence recorded in docs/acceptance-microphone-domain.md
- 0 runtime dependencies in pyproject.toml after the domain lands, and every test passes on ubuntu-latest CI with no audio hardware and no apt-get step
  - instruction: grep 'dependencies = \[\]' pyproject.toml; CI tests.yml unchanged apart from names

## Scope / boundaries

- 'doctor' stays an agent-identity check (prompt file + backend consistency + skills); hardware readiness surfaces through 'list' and the typed exit codes, per `webcam_cli`/cli/`_commands`/doctor.py:1-18
- Writes are dry-run by default and need --apply (gain set, aec set, any `SAVE_CONFIGURATION`), following webcam-cli's --probe/--apply three-level split and media-cli's dry-run-by-default convention; reads (list, inspect, doa, aec get, gain get) touch hardware only via read-only control transfers and never mutate firmware state
- CI stays hardware-free: tests replay synthetic /proc/asound, /sys/bus/usb and /dev trees under tests/fixtures/ via the root= param and monkeypatch the ioctl/subprocess boundary; no apt-get or virtual sound card is added to tests.yml
- param get|set accepts only names present in the table (case-insensitive match, echoed upper-case), rejects 'ro' targets on set and 'wo' targets on get, enforces value count and type before packing, and never lets the user supply raw resid/cmdid bytes

## Assumptions

- Array firmware identification targets the XVF3800 class: match USB ids 38fb:1001 (Reachy Mini Audio firmware) and 2886:001a (Seeed ReSpeaker XVF3800 firmware, warn that `reachy_mini` considers it old), per `reachy_mini` `init_respeaker_usb`(); other ReSpeaker products (e.g. 2886:0018, a different XMOS part) stay out until their control map is verified. On-device acceptance runs against a Reachy Mini Lite over USB when it is connected
- A reachy-mini-daemon (running locally today, pid 3180, and on the Reachy Mini at 192.168.1.162) polls `DOA_VALUE_RADIANS` on the same XVF3800 while the CLI runs; the firmware answers concurrent control reads with status 64 (`SERVICER_COMMAND_RETRY`) and the CLI must retry like `audio_control_utils.py`:250-275 (up to 100 attempts, 10 ms apart) rather than fail
- stream audio and record reuse `webcam_cli`/engine.py's approach verbatim (cite-don't-import): alsasrc is a core element (engine.py:71), pulsesrc/pipewire are optional (engine.py:87), gst-launch-1.0 is shelled out and its absence is a typed exit 2; on this host gst-launch-1.0 and the pipewire source are present
- The XVF3800 PARAMETERS table is copied from `reachy_mini` `audio_control_utils.py` (Apache-2.0) with attribution, cite-don't-import; resid/cmdid values are firmware-build-specific, so 'inspect' on an array also reports VERSION, `BLD_MSG` and `BLD_REPO_HASH` (resid 48) so a mismatch is diagnosable

## Scope exploration

- `s1` — `webcam_cli/devices.py (device model, lines 105-159, 286-387)`: Enumeration is pure filesystem parsing of /dev/v4l/by-id, /proc/asound/cards and /proc/asound/cardN/pcm\*c with a root= injection param; mic identity = ALSA card id + USB sysfs parent. microphone-cli reuses the /proc/asound + sysfs walk for capture cards and drops the video half
  - seeds: `c2`
- `s2` — `webcam_cli/access.py:64-89,354-397 and microphone_cli/cli/_errors.py:21-23`: webcam-cli distinguishes absent/forbidden/busy with exit 1/2/3 and per-subsystem remediation; microphone-cli's scaffold only defines exit 0/1/2, so the busy category (3) must be added — media-cli CLAUDE.md:199-201 wants forbidden vs busy kept distinct
  - seeds: `c3`
- `s3` — `microphone_cli/cli/__init__.py:64-119 + _commands/cli.py:38-40 + explain/catalog.py + CLAUDE.md 'Adding a verb or noun'`: The registration spot is `__init__.py`:91-93; the rubric (teken cli doctor, 26 checks, currently 26/26 green in both repos) requires explain-per-path, `overview_cli_noun_exists`, --json everywhere, no traceback; webcam-cli's tests/`test_cli.py`:190-256 walks the live argparse tree to enforce catalog/learn/overview parity and should be ported
  - seeds: `c4`
- `s4` — `webcam_cli/engine.py:110-148 (warm-up) + host amixer 1.2.9`: webcam-cli never reads or writes a hardware control, so gain has no sibling convention; ALSA amixer exists on the host and shells out cleanly (zero-dep), and the XVF3800 exposes a separate firmware-level mic gain
  - seeds: `c5`
- `s5` — `../reachy_mini/src/reachy_mini/media/audio_control_utils.py:1-330 + docs media_advanced_controls.md:56-108`: The target is the XMOS XVF3800 audio processor class (Seeed ReSpeaker XVF3800 boards and Pollen's Reachy Mini Audio card, which is an XVF3800 derivative, per `reachy_mini` docs hardware.md:46). `reachy_mini`'s `audio_control_utils.py` PARAMETERS table (name → resid, cmdid, count, rw, type) is the XVF3800 control-command map and reads DoA via `DOA_VALUE_RADIANS`; the XMOS control-command appendix is the upstream reference. A Reachy Mini Lite (USB) is available to connect for acceptance testing later
  - seeds: `c6`
- `s6` — `pyproject.toml dependencies=[] (both repos) + reachy_mini audio_control_utils.py imports usb.core/libusb_package + host python3 3.12 ctypes/fcntl ok, pyusb not installed`: Both siblings hold dependencies=\[\] deliberately (`webcam_cli`/engine.py:10-14 refuses even PyGObject); pyusb is not on the host, so DoA/AEC must go through usbdevfs ioctl in stdlib or break the constraint
  - seeds: `c7`
- `s7` — `../reachy_mini audio_control_utils.py:353-411 (init_respeaker_usb) + /etc/udev/rules.d/{60-respeaker,99-reachy-mini-audio}.rules`: The SDK tries 38fb:1001 then 2886:001a; host udev rules already grant MODE=0666 to 38fb:1001 and 2886:0018, so non-root control transfers work for the Reachy device without new setup
  - seeds: `c8`
- `s8` — `webcam_cli/cli/_commands/stream.py (stream audio) + media-cli CLAUDE.md:23`: webcam-cli already streams audio via GStreamer and media-cli routes capture to it; duplicating capture here would create two owners for the same lane
  - seeds: `c9`
- `s9` — `webcam_cli/cli/_commands/doctor.py:1-18 and catalog _DOCTOR`: webcam-cli explicitly kept doctor free of device checks and points readiness questions at list; the rubric depends on doctor's exact {healthy,checks} shape
  - seeds: `c10`
- `s10` — `webcam-cli learn.py:27-142 hardware-touch split + media-cli CLAUDE.md:387-392`: Both siblings treat physically observable side effects as opt-in via --apply; DoA read is a read-only vendor request so it is safe without --apply
  - seeds: `c11`
- `s11` — `.github/workflows/tests.yml (identical between repos, no apt-get) + webcam-cli tests/fixtures/{host-baseline,host-renumbered,camera-only,degraded}`: webcam-cli's CI installs nothing domain-specific and every task before on-host acceptance was built hardware-free; ubuntu-latest has no capture device
  - seeds: `c12`
- `s12` — `microphone_cli template-prose grep + pyproject [project.scripts] + webcam_cli/cli/__init__.py:9-16`: Nine template strings remain in `microphone_cli`/ and prog='microphone-cli' contradicts the installed 'microphone' binary; webcam-cli resolved both with a regression test
  - seeds: `c13`
- `s13` — `../media-cli (grep: zero code hits for microphone/webcam; CLAUDE.md:23,41-42,117-158,230-267)`: media-cli is itself a scaffold with no device code and an unresolved import-vs-subprocess question; its only settled asks are stable identity, format reporting, forbidden-vs-busy distinction and dry-run-by-default
  - seeds: `c14`
- `s14` — `README.md headings (both repos) + docs/ listings + docs/skill-sources.md diff`: microphone-cli's README is still the template shape and its skill-sources ledger lags webcam-cli's; webcam-cli carries spec/plan/delivery/acceptance docs for its feature
  - seeds: `c15`
- `s15` — `challenge pass / adjacent-systems lens: reachy_mini daemon routers/state.py:68,122 + audio_control_utils.py:250-275 + pgrep reachy-mini-daemon`: the daemon reads DoA concurrently through the same vendor-control path; the firmware's retry status is the only arbitration, so the CLI must honour it
  - seeds: `c27`
- `s16` — `challenge pass / adjacent-systems lens: webcam_cli/engine.py:65-87,1052-1122 + which gst-launch-1.0`: webcam-cli already solved GStreamer detection and alsasrc pipeline building for audio; reusing it avoids a second engine
  - seeds: `c28`
- `s17` — `challenge pass / unstated-assumptions lens: reachy_mini LICENSE (Apache-2.0) + PARAMETERS table provenance`: the table's provenance and firmware-specificity were nowhere in the frame
  - seeds: `c29`
- `s18` — `challenge pass / unstated-assumptions lens: /usr/include/linux/usbdevice_fs.h:40-48,187 + ls -l /dev/bus/usb`: the ioctl struct and number are verifiable from the header without hardware; permissions on a fresh host are the likely first failure
  - seeds: `c30`
- `s19` — `challenge pass / overlooked-actors lens: webcam_cli/devices.py:470-531 resolve() + /sys/bus/usb/devices/*/serial`: multi-array hosts were not considered; serial is the only stable discriminator
  - seeds: `c31`
- `s20` — `challenge pass / lifecycle lens: --watch mode (decision c24) + reachy_mini examples/debug/sound_doa.py loop`: the watch decision named the output format but not termination or unplug behaviour
  - seeds: `c32`
- `s21` — `challenge pass / reversibility lens: PARAMETERS table 'wo' entries resid 48 (REBOOT/SAVE/CLEAR) and SPECIAL_CMD_* filter coefficient uploads`: an unguarded generic escape hatch can persist or brick the array; volatile writes are self-reverting, persistent ones are not
  - seeds: `c33`
- `s22` — `challenge pass / security lens: audio_control_utils.py write() validation (ro check, count check)`: the SDK validates only ro and count; type validation and raw-byte refusal are additions
  - seeds: `c34`
- `s23` — `challenge pass / observability-and-consent lens: webcam_cli/activation.py:28-129 + media-cli CLAUDE.md consent posture`: the frame carried dry-run-by-default but no audit trail; microphones are privacy-sensitive and webcam-cli already has the pattern
  - seeds: `c35`
- `s24` — `challenge pass / concurrency lens: PipeWire/WirePlumber (pgrep) + usbdevfs vs snd-usb-audio binding`: clean beyond the retry assumption above and parked items v2/v3: device-recipient vendor control transfers do not need the audio interface claimed, so snd-usb-audio/PipeWire holding the PCM does not block DoA/AEC reads (the SDK never detaches the kernel driver); residual risk is only the ALSA mixer vs WirePlumber saved-state interplay already parked
- `s25` — `challenge pass / cheap-probes lens: usbdevice_fs.h, which gst-launch-1.0, gst-inspect-1.0 pipewire, ls -l /dev/bus/usb, pgrep reachy-mini-daemon, reachy_mini LICENSE`: all probes were read-only; an end-to-end control-transfer probe is impossible today (no writable USB node) and is deferred to issue #3
- `s26` — `challenge pass / cheap-probes lens: live device after user connected it (lsusb, /proc/asound/cards, journalctl -k)`: the user reports the Reachy is connected over USB but nothing enumerates on this host (no 38fb/2886 id, no capture card, no kernel USB event), so the read-only DoA probe could not run; the ioctl path stays parked (v5) for issue #3

## Decisions

- Zero runtime deps is kept: the vendor control transfer is issued with stdlib only — ctypes/fcntl `USBDEVFS_CONTROL` ioctl on /dev/bus/usb/BBB/DDD (device located by matching idVendor/idProduct in /sys/bus/usb/devices/\*/) — instead of pyusb + `libusb_package` that `reachy_mini` imports
- AEC surface in v1 is read AND write: 'array aec get' reports converged/bypass/high-pass/echo/mic count/geometry and 'array aec set' toggles `PP_ECHOONOFF`, `SHF_BYPASS`, `AEC_HPFONOFF` under --apply
  - instruction: array aec set --apply round-trips on the Reachy Mini Lite (issue #3)
- 'array doa' supports --watch: one JSON object per poll on stdout as JSON Lines (jsonl), --interval seconds, runs until SIGINT or --count; single-shot without --watch
  - instruction: array doa --watch --count 3 --json prints exactly 3 lines each parseable by json.loads
- A generic 'param get `<NAME>`' / 'param set `<NAME>` `<values...>` --apply' escape hatch covers the full XVF3800 PARAMETERS table (name → resid, cmdid, count, rw, type); the array backend is an abstraction so more devices and array technologies can be added later
  - instruction: param get VERSION returns firmware version; param set on an 'ro' entry exits 1 with a hint; a second backend can register without touching the CLI verbs
- A 'record `<device>` `<output>`' verb is in scope alongside 'stream audio': both via GStreamer alsasrc subprocess, both dry-run by default, both need --apply because they are physically observable; record is bounded by --duration and --max-bytes like webcam-cli's
  - instruction: record without --apply prints the pipeline and writes nothing; with --apply writes a Matroska/Opus or WAV file of bounded size
- v1 targets a locally attached USB XVF3800 only. Non-goals: reaching a Reachy Mini over the network through its daemon REST, speech-to-text, text-to-speech, and speaker/monitor playback — those belong to the `reachy_mini`/`reachy_nova` stack; microphone-cli must coexist with that project holding the device (busy → exit 3, DoA/AEC reads still work via the firmware retry path)
  - instruction: no network code in `microphone_cli`/; README Scope section lists STT/TTS/playback/remote as non-goals

## Open parks

- [unknown_nonblocking] Whether `AUDIO_MGR_MIC_GAIN` (firmware float) and the ALSA capture mixer control on the XVF3800's UAC interface are the same knob or two independent stages — needs the device present and 'amixer -c N contents' to compare
- [unknown_nonblocking] PipeWire holds the ALSA device open while a session runs; whether 'inspect' can read /proc/asound/cardN/stream0 formats without EBUSY, and whether gain set via amixer is overridden by WirePlumber's saved state
- [unknown_nonblocking] Whether the PARAMETERS resid/cmdid map is identical on the older 2886:001a ReSpeaker XVF3800 firmware that `reachy_mini` warns about; only the 38fb:1001 build is known to match
- [unknown_nonblocking] The usbdevfs path is unverified against real hardware: no writable USB node exists on this host today (no device plugged, other nodes 0664 root:root), so the first end-to-end control transfer happens in issue #3

## Resolved vagueness

- [unknown_blocking] No capture device is attached to the host right now (arecord -l empty, lsusb shows no 38fb/2886 device, WirePlumber default source names the Reachy Mini Audio but it is unplugged), so DoA/AEC and gain cannot be acceptance-tested until the device is connected — resolved: build against fixture trees; on-device acceptance tracked in issue #3 with the Reachy Mini Lite
