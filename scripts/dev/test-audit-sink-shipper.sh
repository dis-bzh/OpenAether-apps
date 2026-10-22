#!/usr/bin/env bash
# The audit-sink shipper (apps/base/observability-audit-sink/scripts/ship.sh) run for real: busybox sh and restic 0.19.1 of the pinned
# restic image, a read-only audit directory and a local restic repository standing for the object store. What it proves (infra #318):
#   - whole lines only, ranges contiguous (`to` of one snapshot = `from` of the next), nothing repeated, nothing missing;
#   - a restart starts from what the SINK holds (every docker run is a new container with an empty work dir);
#   - a rotation: the rest of the old file with `eof`, then the new file from 0;
#   - a sink outage ends the cycle with a failure and ships nothing; afterwards nothing is lost and nothing is duplicated;
#   - a file that vanished before it was shipped is an explicit gap snapshot, not silence;
#   - restic runs without locks (no lock object is ever written, so the key pair never needs a delete right);
#   - the source directory is mounted read-only, and no audit content reaches the logs.
# Needs docker (the restic image) and restic on the PATH to read the result back. Rung: real restic/busybox, local backend, no S3, no Talos.
#   Usage: scripts/dev/test-audit-sink-shipper.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="docker.io/restic/restic:0.19.1"   # the image of apps/base/observability-audit-sink/shipper.yaml
SCRIPT="$ROOT/apps/base/observability-audit-sink/scripts"
command -v docker >/dev/null && command -v restic >/dev/null && command -v jq >/dev/null || { echo "docker, restic and jq are required" >&2; exit 2; }
W="$(mktemp -d)"; PASS=0; FAIL=0
trap 'chmod -R u+rwX "$W" 2>/dev/null; docker run --rm -v "$W:/w" --entrypoint /bin/sh "$IMAGE" -c "rm -rf /w/* /w/.[!.]*" >/dev/null 2>&1; rm -rf "$W"' EXIT
ok()  { echo "  ok   $*"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL $*"; FAIL=$((FAIL + 1)); }
is()  { [ "$2" = "$3" ] && ok "$1" || bad "$1 (expected '$2', got '$3')"; }
SRC="$W/src"; REPO="$W/repo"; mkdir -p "$SRC" "$REPO"
export RESTIC_PASSWORD=test-password-not-a-secret
NODE=cp-1; DAY=2026-10-22; LIVE="$SRC/kube-apiserver.log"
REPOD="$REPO/$NODE/$DAY"
line() { printf '{"kind":"Event","level":"Metadata","verb":"%s","user":{"username":"SECRETMARK-%s"},"stageTimestamp":"2026-10-22T10:00:%02d.000001Z"}\n' "${2:-get}" "$1" "$1"; }

ship() { # [docker args...] -- runs one cycle; the work dir is new each time (a restart: the sink is the only memory)
  docker run --rm --user "$(id -u):$(id -g)" -e ONCE=1 -e NODE_NAME="$NODE" -e DAY="$DAY" -e RESTIC_PASSWORD -e RESTIC_REPO_BASE=/repo -e SHIP_INTERVAL=1 \
    -e SHIP_MAX_BYTES="${MAXB:-16777216}" -e RESTIC_TIMEOUT="${RESTIC_TIMEOUT:-60}" -e WORK=/work \
    -v "$SRC:/var/log/audit/kube:ro" -v "$SCRIPT:/scripts:ro" -v "${REPOMOUNT:-$REPO}:/repo${REPOMODE:+:$REPOMODE}" --tmpfs /work:size=128m,mode=1777 --read-only \
    --entrypoint /bin/sh "$IMAGE" /scripts/ship.sh "$@" 2>&1
}
snaps() { restic -r "$REPOD" snapshots --json 2>/dev/null | jq -r 'sort_by(.time)[] | (.tags | map(select(startswith("oa-"))) | join(" "))'; }
posl() { snaps | sed -n 's/^oa-pos=//p'; }
inode() { stat -c %i "$1"; }
# The bytes of one inode as the sink holds them: the snapshots in order, each dumped from its own path.
sink_bytes() { # <inode>
  # (a marker, from = to, holds a word and no audit byte: skipped)
  restic -r "$REPOD" snapshots --json 2>/dev/null | jq -r --arg i "$1" 'sort_by(.time)[] | select(.tags | any(startswith("oa-pos=" + $i + ":") and (split(":") | .[1] != .[2]))) | .id' | while read -r id; do
    restic -r "$REPOD" dump "$id" "$(restic -r "$REPOD" ls "$id" --json 2>/dev/null | jq -rs '[.[] | select(.type == "file") | .path][0]')" 2>/dev/null
  done
}
# every chunk of inode $1 starts where the previous one ended, from 0, and no chunk follows an `eof`
contiguous() { posl | awk -F: -v i="$1" 'BEGIN { next_ = 0 } $1 == i { if ($2 != next_) { print "break at " $0 " (expected from " next_ ")"; bad_ = 1 } if (closed) { print "chunk after eof: " $0; bad_ = 1 } next_ = $3; if ($4 == "eof") closed = 1 } END { exit bad_ }'; }

echo "== first start: whole lines only, a partial last line waits =="
{ line 1; line 2; line 3; } > "$LIVE"; printf '{"kind":"Event","partial' >> "$LIVE"
INO="$(inode "$LIVE")"; L3="$(head -n 3 "$LIVE" | wc -c)"
OUT="$(ship)"; RC=$?
is "the cycle succeeds" 0 "$RC"
is "one snapshot, from 0 to the end of the third line" "$INO:0:$L3" "$(posl | paste -sd' ')"
is "the sink holds exactly those lines" "$(head -c "$L3" "$LIVE" | sha256sum | cut -d' ' -f1)" "$(sink_bytes "$INO" | sha256sum | cut -d' ' -f1)"
is "no lock object was written (restic runs with --no-lock)" 0 "$(find "$REPOD/locks" -type f 2>/dev/null | wc -l | tr -d ' ')"
grep -q SECRETMARK <<<"$OUT" && bad "audit content reached the log" || ok "the log names positions, never audit content"

echo "== a restart (new container, empty work dir): resumes from the sink, repeats nothing =="
OUT="$(ship)"; RC=$?
is "idle cycle succeeds" 0 "$RC"
is "no new snapshot while nothing was appended" 1 "$(snaps | grep -c oa-pos)"
grep -q 'resuming after inode' <<<"$OUT" && ok "it says it read the position back from the sink" || bad "no resume line: $OUT"
printf '%s\n' "ial line completed" >> "$LIVE"; { line 5; line 6; } >> "$LIVE"
OUT="$(ship)"; RC=$?
is "next cycle succeeds" 0 "$RC"
is "two snapshots, contiguous" 2 "$(snaps | grep -c oa-pos)"
contiguous "$INO" && ok "ranges are contiguous from 0 (no gap, no repeat)" || bad "$(contiguous "$INO")"
is "the sink holds the whole file now" "$(sha256sum <"$LIVE" | cut -d' ' -f1)" "$(sink_bytes "$INO" | sha256sum | cut -d' ' -f1)"

echo "== chunks are cut at a newline, at most SHIP_MAX_BYTES =="
SZ0="$(stat -c %s "$LIVE")"
for n in $(seq 10 20); do line "$n"; done >> "$LIVE"
MAXB=300 ship >/dev/null; RC=$?
is "succeeds" 0 "$RC"
is "more than one new chunk" 1 "$([ "$(snaps | grep -c oa-pos)" -gt 3 ] && echo 1 || echo 0)"
contiguous "$INO" && ok "still contiguous" || bad "$(contiguous "$INO")"
is "the sink equals the file" "$(sha256sum <"$LIVE" | cut -d' ' -f1)" "$(sink_bytes "$INO" | sha256sum | cut -d' ' -f1)"
posl | awk -F: -v z="$SZ0" '$2 >= z {print $3-$2}' | sort -n | tail -1 | { read -r m; [ "$m" -le 300 ] && ok "no chunk above 300 bytes (largest $m)" || bad "a chunk of $m bytes"; }

echo "== rotation: the rest of the old file with eof, then the new file from 0 =="
{ line 30; line 31; } >> "$LIVE"
mv "$LIVE" "$SRC/kube-apiserver-2026-10-22T11-00-00.000.log"; touch -d '2026-10-22 08:00:00' "$SRC/kube-apiserver-2026-10-22T11-00-00.000.log"
{ line 40; line 41; } > "$LIVE"; INO2="$(inode "$LIVE")"
OUT="$(ship)"; RC=$?
is "succeeds" 0 "$RC"
contiguous "$INO" && ok "the old file is contiguous and ends with eof" || bad "$(contiguous "$INO")"
posl | grep -q "^$INO:[0-9]*:[0-9]*:eof$" && ok "its last chunk carries eof" || bad "no eof: $(posl | tail -3)"
contiguous "$INO2" && ok "the new file starts at 0" || bad "$(contiguous "$INO2")"
is "the old file is whole in the sink" "$(sha256sum <"$SRC/kube-apiserver-2026-10-22T11-00-00.000.log" | cut -d' ' -f1)" "$(sink_bytes "$INO" | sha256sum | cut -d' ' -f1)"
is "the new file is whole in the sink" "$(sha256sum <"$LIVE" | cut -d' ' -f1)" "$(sink_bytes "$INO2" | sha256sum | cut -d' ' -f1)"
is "one snapshot of the old inode ends exactly at its size" "$(stat -c %s "$SRC/kube-apiserver-2026-10-22T11-00-00.000.log")" "$(posl | awk -F: -v i="$INO" '$1 == i {t = $3} END {print t}')"

echo "== the sink is down: the cycle fails, ships nothing, and nothing is lost or repeated afterwards =="
BEFORE="$(snaps | grep -c oa-pos)"
{ line 50; line 51; line 52; } >> "$LIVE"
OUT="$(REPOMODE=ro RESTIC_TIMEOUT=15 ship)"; RC=$?
is "the cycle FAILS (a failure the next interval retries)" 1 "$RC"
is "no snapshot was added" "$BEFORE" "$(snaps | grep -c oa-pos)"
grep -q 'cycle incomplete' <<<"$OUT" && ok "it says it will retry and that nothing is dropped" || bad "no message: $OUT"
{ line 53; } >> "$LIVE"
OUT="$(ship)"; RC=$?
is "the sink is back: succeeds" 0 "$RC"
contiguous "$INO2" && ok "contiguous across the outage" || bad "$(contiguous "$INO2")"
is "everything written during the outage is in the sink, once" "$(sha256sum <"$LIVE" | cut -d' ' -f1)" "$(sink_bytes "$INO2" | sha256sum | cut -d' ' -f1)"

echo "== a file that vanished before it was shipped: an explicit gap, then the rest =="
{ line 60; } >> "$LIVE"; ship >/dev/null
mv "$LIVE" "$SRC/kube-apiserver-2026-10-22T12-00-00.000.log"; touch -d '2026-10-22 08:30:00' "$SRC/kube-apiserver-2026-10-22T12-00-00.000.log"
{ line 70; line 71; } > "$LIVE"; INO3="$(inode "$LIVE")"
rm -f "$SRC/kube-apiserver-2026-10-22T12-00-00.000.log" "$SRC/kube-apiserver-2026-10-22T11-00-00.000.log"   # lost with the node's disk rotation
OUT="$(ship)"; RC=$?
is "succeeds" 0 "$RC"
snaps | grep -q "^oa-gap=$INO2:[0-9]*:file-gone" && ok "the loss is recorded as a gap snapshot naming the inode and offset" || bad "no gap: $(snaps | tail -3)"
grep -q 'GAP recorded' <<<"$OUT" && ok "and logged" || bad "not logged"
contiguous "$INO3" && ok "the surviving file is shipped from 0" || bad "$(contiguous "$INO3")"

echo "== a live file that keeps growing: one cycle ends, it does not chase the file (lab 23) =="
# An API server writes all the time. A cycle that looped until it had caught up would never end (each put takes a moment, the file has
# grown by then) and would make a snapshot every second or two. The cycle ships what the file held when it began; the rest waits.
BEFORE="$(snaps | grep -c oa-pos)"
( for i in $(seq 1000 3000); do line "$i" >>"$LIVE"; sleep 0.005; done ) & GROW=$!
T0=$SECONDS; OUT="$(ship)"; RC=$?; T1=$((SECONDS - T0))
kill "$GROW" 2>/dev/null; wait "$GROW" 2>/dev/null
is "the cycle ends while the file is still growing" 0 "$RC"
ADDED=$(( $(snaps | grep -c oa-pos) - BEFORE ))
[ "$ADDED" -ge 1 ] && [ "$ADDED" -le 2 ] && ok "at most two snapshots for the whole cycle ($ADDED in ${T1}s)" || bad "$ADDED snapshots in one cycle of ${T1}s: the shipper chased the file"
contiguous "$INO3" && ok "still contiguous" || bad "$(contiguous "$INO3")"
SHIPPED="$(posl | awk -F: -v i="$INO3" '$1 == i {t = $3} END {print t}')"
is "what is in the sink is a whole-line prefix of the file" "$(head -c "$SHIPPED" "$LIVE" | sha256sum | cut -d' ' -f1)" "$(sink_bytes "$INO3" | sha256sum | cut -d' ' -f1)"
OUT="$(ship)"; RC=$?
is "the next cycle takes the rest, still contiguous" 0 "$RC"
contiguous "$INO3" && ok "contiguous after the second cycle" || bad "$(contiguous "$INO3")"

echo "== the source is never written =="
is "the audit directory is mounted read-only (a write is refused)" "refused" "$(docker run --rm -v "$SRC:/s:ro" --entrypoint /bin/sh "$IMAGE" -c 'touch /s/x 2>/dev/null && echo written || echo refused')"
[ ! -e "$SRC/x" ] && ok "nothing appeared in the audit directory" || bad "a file appeared"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
