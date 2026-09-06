#!/usr/bin/env bash
# Blind consumer: attach to a live `microphone stream audio` using nothing but
# its --json payload.
#
# Structurally blind by design. Its ONLY input is the payload file. It never
# runs `microphone`, never reads /dev or /proc/asound, and is never told the
# device's stable id. If it can receive and depayload audio from that file
# alone, the attachment contract holds; if it needs any fact the payload did
# not announce, that is a finding, not something to patch here.
#
# Usage: blind-consumer.sh <payload.json> <workdir> [seconds]
#
# Evidence goes to <workdir> (the caller passes a directory under $TMPDIR and
# deletes it). Nothing is written inside a git repository.

set -euo pipefail

payload="${1:?usage: blind-consumer.sh <payload.json> <workdir> [seconds]}"
workdir="${2:?usage: blind-consumer.sh <payload.json> <workdir> [seconds]}"
seconds="${3:-4}"

[ -r "$payload" ] || { echo "blind-consumer: cannot read $payload" >&2; exit 2; }
mkdir -p "$workdir"

# --- everything below is derived from the payload alone ---------------------

get() { python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."):
    d=d[k]
print(d)' "$payload" "$1"; }

uri=$(get attach.uri)
host=$(get attach.host)
port=$(get attach.port)
encode=$(get attach.encode)
announced=$(get "attach.consumer.$encode")

echo "blind-consumer: payload announces uri=$uri encode=$encode"
echo "blind-consumer: announced consumer command:"
echo "  $announced"

pass=0; fail=0
ok() { pass=$((pass + 1)); echo "  -> PASS: $1"; }
bad() { fail=$((fail + 1)); echo "  -> FAIL: $1"; }

# 1. The announced command, verbatim, for a bounded time. gst-launch treats
#    SIGINT as "send EOS and exit 0", so 0 and timeout's 124 both mean it ran.
set +e
timeout --signal=INT "$seconds" bash -c "$announced" >"$workdir/announced.log" 2>&1
status=$?
set -e
if [ "$status" -eq 0 ] || [ "$status" -eq 124 ]; then
    if grep -qi "error" "$workdir/announced.log"; then
        bad "announced consumer logged an ERROR (see announced.log)"
    else
        ok "announced consumer ran for ${seconds}s without error"
    fi
else
    bad "announced consumer exited $status (see announced.log)"
fi

# 2. Count packets: swap the sink for fakesink, keep the announced caps, and
#    stop after 50 buffers. Proves bytes actually arrive on the announced port.
counting=$(printf '%s' "$announced" | sed -E 's/! *autoaudiosink *$/! fakesink num-buffers=50/; s/udpsrc /udpsrc num-buffers=50 /')
set +e
timeout "$seconds" bash -c "$counting -v" >"$workdir/count.log" 2>&1
status=$?
set -e
if [ "$status" -eq 0 ] && grep -q "Got EOS" "$workdir/count.log"; then
    ok "50 RTP buffers received on $host:$port and depayloaded"
else
    bad "packet count run exited $status without EOS (see count.log)"
fi

echo "blind-consumer: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
