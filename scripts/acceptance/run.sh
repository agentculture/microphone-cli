#!/usr/bin/env bash
# On-device acceptance for the microphone-domain build plan (task t13).
#
# Drives every verb from a stable device id and --json alone against real
# hardware, then hands `stream audio`'s payload to a blind consumer that is
# never told the device. Headless, non-interactive, re-runnable.
#
#   scripts/acceptance/run.sh --device STABLE_ID [--writes] [--media]
#                             [--port N] [--seconds N] [--skip-suite]
#
# Read-only by default. --writes adds the volatile gain and AEC round trips
# (restored afterwards; never --allow-persistent). --media adds record and
# stream, which switch the microphone on.
#
# PRIVACY. With --media this records audio. Every artifact goes under a run
# directory in $TMPDIR — never inside the repository — and is deleted on exit,
# including on failure. Only byte counts, formats and parameter values remain.

set -euo pipefail

DEVICE="${MICROPHONE_ACCEPTANCE_DEVICE:-}"
PORT=5004; SECONDS_PER_STEP=4; WRITES=0; MEDIA=0; RUN_SUITE=1
while [ $# -gt 0 ]; do
    case "$1" in
        --device) DEVICE="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --seconds) SECONDS_PER_STEP="$2"; shift 2 ;;
        --writes) WRITES=1; shift ;;
        --media) MEDIA=1; shift ;;
        --skip-suite) RUN_SUITE=0; shift ;;
        -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
[ -n "$DEVICE" ] || { echo "run.sh: --device STABLE_ID is required (see 'microphone list --json')" >&2; exit 2; }

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(git -C "$HERE" rev-parse --show-toplevel)
RUN=$(mktemp -d -t microphone-acceptance.XXXXXX)
export MICROPHONE_ACTIVATION_LOG="$RUN/activation.jsonl"
cleanup() { rm -rf "$RUN"; }
trap cleanup EXIT

M() { (cd "$REPO" && uv run microphone "$@"); }
J() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {"d": d}))' "$1"; }
pass=0; fail=0
ok() { pass=$((pass + 1)); echo "PASS: $1"; }
bad() { fail=$((fail + 1)); echo "FAIL: $1"; }
step() { echo; echo "== $1"; }

step "0. hardware-free suite"
if [ "$RUN_SUITE" -eq 1 ]; then
    (cd "$REPO" && uv run pytest -n auto -q 2>&1 | tail -1)
fi

step "1. list: the device is present by stable id"
if M list --json | J "[x['stable_id'] for x in d['devices']]" | grep -q "$DEVICE"; then ok "list names $DEVICE"; else bad "list does not name $DEVICE"; fi

step "2. inspect: formats, rates, channels, firmware"
INSPECT=$(M inspect "$DEVICE" --json)
echo "$INSPECT" | J "(d['formats'], d['rates'], d['channels'], d['firmware'])"
if echo "$INSPECT" | J "bool(d['rates']) and bool(d['formats'])" | grep -q True; then ok "inspect advertises formats and rates"; else bad "inspect has no advertised format"; fi
IS_ARRAY=$(echo "$INSPECT" | J "d['device']['is_array']")

step "3. gain get"
M gain get "$DEVICE" --json | J "(d['alsa'], d['firmware'])" && ok "gain get"

if [ "$IS_ARRAY" = "True" ]; then
    step "4. array doa: one sample and a 3-line watch"
    DOA=$(M array doa "$DEVICE" --json); echo "$DOA" | J "(d['azimuth_deg'], d['azimuth_rad'], d['speech'], d['source'])"
    LINES=$(M array doa "$DEVICE" --watch --count 3 --interval 0.3 | wc -l)
    [ "$LINES" -eq 3 ] && ok "doa --watch --count 3 emitted 3 JSON lines" || bad "doa --watch emitted $LINES lines"

    step "5. array aec get"
    M array aec get "$DEVICE" --json | J "{k: d[k] for k in ('converged','bypass','hpf','echo','num_mics')}" && ok "aec get reports 5+ fields"

    step "6. param: version and a persistent-tier refusal"
    M param get "$DEVICE" VERSION --json | J "d['values']"
    if M param set "$DEVICE" SAVE_CONFIGURATION 1 --apply --json >/dev/null 2>&1; then bad "SAVE_CONFIGURATION was NOT refused"; else ok "SAVE_CONFIGURATION refused without --allow-persistent"; fi
fi

if [ "$WRITES" -eq 1 ]; then
    step "7. volatile writes, restored"
    BEFORE=$(M gain get "$DEVICE" --json | J "d['alsa']['value']")
    MAX=$(M gain get "$DEVICE" --json | J "d['alsa']['max']")
    AFTER=$(M gain set "$DEVICE" 0.5 --target alsa --apply --json | J "d['alsa']['value']")
    [ "$AFTER" != "$BEFORE" ] && ok "alsa gain moved $BEFORE -> $AFTER" || bad "alsa gain did not move"
    M gain set "$DEVICE" "$(python3 -c "print($BEFORE/$MAX)")" --target alsa --apply --json >/dev/null && echo "restored alsa gain to $BEFORE"
    if [ "$IS_ARRAY" = "True" ]; then
        ECHO=$(M array aec get "$DEVICE" --json | J "d['echo']")
        TOGGLE=$([ "$ECHO" = "True" ] && echo off || echo on)
        NEW=$(M array aec set "$DEVICE" --echo "$TOGGLE" --apply --json | J "d['state']['echo']")
        [ "$NEW" != "$ECHO" ] && ok "aec echo toggled $ECHO -> $NEW" || bad "aec echo did not toggle"
        M array aec set "$DEVICE" --echo "$([ "$ECHO" = "True" ] && echo on || echo off)" --apply --json >/dev/null && echo "restored aec echo to $ECHO"
    fi
fi

if [ "$MEDIA" -eq 1 ]; then
    step "8. record 2 s with advertised format, under \$TMPDIR"
    REC=$(M record "$DEVICE" "$RUN/clip.wav" --duration 2 --apply --json)
    echo "$REC" | J "(d['bytes_written'], d['stopped_reason'], d['audio_format']['requested'])"
    [ "$(echo "$REC" | J "d['bytes_written'] > 44")" = True ] && ok "record wrote audio" || bad "record wrote nothing"

    step "9. stream + blind consumer (never told the device)"
    M stream audio "$DEVICE" --port "$PORT" --apply --json >"$RUN/stream.json"
    PID=$(J "d['pid']" <"$RUN/stream.json")
    sleep 1
    if bash "$HERE/blind-consumer.sh" "$RUN/stream.json" "$RUN/consumer" "$SECONDS_PER_STEP"; then ok "blind consumer attached from the payload alone"; else bad "blind consumer could not attach"; fi
    kill -INT "$PID" 2>/dev/null || true; sleep 1
fi

step "10. activation log"
APPLIES=$( [ -f "$MICROPHONE_ACTIVATION_LOG" ] && wc -l <"$MICROPHONE_ACTIVATION_LOG" || echo 0 )
echo "activation lines: $APPLIES (one per --apply; reads and dry runs add none)"

echo; echo "== $pass passed, $fail failed (media under $RUN deleted on exit)"
[ "$fail" -eq 0 ]
