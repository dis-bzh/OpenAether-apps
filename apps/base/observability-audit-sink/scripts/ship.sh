#!/bin/sh
# audit-sink-shipper: copies the Kubernetes API audit log of THIS control plane to a restic repository in an object store OUTSIDE the
# cluster (infra #318, docs/audit-sink.md in the infra repository). POSIX sh (busybox ash of the restic image) + restic, nothing else.
#
# What it guarantees, and how:
#   - It only READS the audit files (read-only hostPath); the apiserver's write path is never involved and never waits for it.
#   - A byte range is "done" only when `restic backup` returned 0, i.e. the snapshot object is stored. Position = (inode, offset).
#   - The SINK is the cursor: at start the last acknowledged position is read back from the sink (no local state outlives the pod),
#     so a restart neither skips a range nor, but for one lost acknowledgement, repeats one.
#   - Whole lines only: a chunk ends at a newline unless the file was rotated (then the last chunk ends at EOF and carries `eof`).
#   - Nothing is buffered here: the buffer is the apiserver's own rotated files. A file that disappears before it was shipped is
#     recorded as an explicit gap snapshot (oa-gap), never silently skipped.
#   - restic runs with --no-lock: it never deletes a lock object, so the key pair needs no delete right at all.
# One repository per node and UTC day: $RESTIC_REPO_BASE/<node>/<YYYY-MM-DD>. Tags on every snapshot: node=<node> and
# oa-pos=<inode>:<from>:<to>[:eof]  (a range of bytes of one file; `to` of one chunk is `from` of the next). A snapshot with from = to
# is a marker (the end of a file already shipped): its content is a word, not audit bytes.
#
# Environment: RESTIC_REPO_BASE (e.g. s3:https://host/bucket/audit), RESTIC_PASSWORD, AWS_* (S3), NODE_NAME,
#   SRC_DIR (/var/log/audit/kube), LIVE (kube-apiserver.log), SHIP_INTERVAL (300), SHIP_MAX_BYTES (16777216), RECOVER_DAYS (8),
#   WORK (/work), ONCE=1 (one cycle, exit status 0 when everything pending was shipped: tests), DAY (override of the UTC day: tests).
set -u
SRC_DIR="${SRC_DIR:-/var/log/audit/kube}"; LIVE="${LIVE:-kube-apiserver.log}"; NODE="${NODE_NAME:?NODE_NAME is required}"
BASE="${RESTIC_REPO_BASE:?RESTIC_REPO_BASE is required}"; WORK="${WORK:-/work}"
INTERVAL="${SHIP_INTERVAL:-300}"; MAXB="${SHIP_MAX_BYTES:-16777216}"; RECOVER_DAYS="${RECOVER_DAYS:-8}"
export RESTIC_CACHE_DIR="$WORK/cache" HOME="$WORK" TMPDIR="$WORK/tmp"
mkdir -p "$WORK/cache" "$WORK/tmp" || exit 1
CHUNK="$WORK/tmp/chunk"; CUR_INO=""; CUR_OFF=0; CLOSED_AT_EOF=0; LOADED=0; DAYNOW=""

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
repo_of() { printf '%s/%s/%s' "$BASE" "$NODE" "$1"; }
# --no-lock: see above. timeout: restic retries a failing store for up to 15 minutes per call; a call that does not finish is killed, which
# leaves no snapshot (so no acknowledgement) and the cycle is retried at the next interval.
R() { timeout "${RESTIC_TIMEOUT:-900}" restic --no-lock --quiet "$@"; }
lasterr() { tail -n 1 "$WORK/tmp/err" 2>/dev/null | cut -c1-200; }

# Does the repository of day $1 exist? 0 yes, 1 no (restic says so), 2 cannot tell (the sink is unreachable or refuses).
repo_state() {
  R -r "$(repo_of "$1")" cat config >/dev/null 2>"$WORK/tmp/err" && return 0
  # Only a clean "not there" is "no repository"; anything else (network, credentials, a refusal) is "cannot tell", never a reason to init.
  if grep -q 'Is there a repository' "$WORK/tmp/err" && grep -qi 'not found\|does not exist\|no such file\|NoSuchKey' "$WORK/tmp/err"; then return 1; fi
  log "sink: $(lasterr)"; return 2
}

ensure_repo() { # <day>
  repo_state "$1"; s=$?
  [ "$s" = 0 ] && return 0
  [ "$s" = 1 ] || return 1
  R -r "$(repo_of "$1")" init >/dev/null 2>"$WORK/tmp/err" && { log "repository of $1 created"; return 0; }
  # A restarted twin of this pod made it between the two calls: fine.
  repo_state "$1" && return 0
  log "cannot create the repository of $1: $(lasterr)"; return 1
}

# The last acknowledged position, read back from the sink. 0 = loaded (CUR_INO empty when the sink holds nothing for this node).
load_state() {
  i=0; found=""
  while [ "$i" -lt "$RECOVER_DAYS" ]; do
    t0="$(date -u +%s)"; [ -z "${DAY:-}" ] || t0="$(date -u -d "$DAY" +%s)"
    d="$(date -u -d "@$((t0 - i * 86400))" +%F)"
    repo_state "$d"; s=$?
    [ "$s" = 2 ] && return 1
    if [ "$s" = 0 ]; then
      out="$(R -r "$(repo_of "$d")" snapshots --json 2>"$WORK/tmp/err")" || { log "sink: snapshots of $d: $(lasterr)"; return 1; }
      # oa-pos tags in the order restic lists the snapshots (by time); the last one is the newest acknowledged range.
      last="$(printf '%s' "$out" | tr ',' '\n' | grep -o '"oa-pos=[0-9]*:[0-9]*:[0-9]*\(:eof\)\?"' | tail -n 1 | tr -d '"')"
      if [ -n "$last" ]; then found="${last#oa-pos=}"; break; fi
    fi
    i=$((i + 1))
  done
  if [ -n "$found" ]; then
    CUR_INO="${found%%:*}"; rest="${found#*:}"; rest="${rest#*:}"; CUR_OFF="${rest%%:*}"
    case "$rest" in *:eof) CLOSED_AT_EOF=1 ;; *) CLOSED_AT_EOF=0 ;; esac
    log "resuming after inode $CUR_INO offset $CUR_OFF (read back from the sink)"
  else
    CUR_INO=""; CUR_OFF=0; CLOSED_AT_EOF=0; log "the sink holds nothing for $NODE in the last $RECOVER_DAYS days: starting from the oldest file on the node"
  fi
  LOADED=1; return 0
}

# One snapshot. $1=inode $2=from $3=to $4=eof|"" ; the content is the file $5 (may be empty). 0 only when stored.
put() {
  tag="oa-pos=$1:$2:$3${4:+:eof}"
  R -r "$(repo_of "$DAYNOW")" backup --stdin --stdin-filename "audit/$NODE/$DAYNOW/$1-$2-$3.log" --host "$NODE" \
    --tag "$tag" --tag "node=$NODE" <"$5" >/dev/null 2>"$WORK/tmp/err" && { log "shipped $tag ($(wc -c <"$5") bytes)"; return 0; }
  log "NOT shipped $tag: $(lasterr)"; return 1
}

gap() { # <what is missing>
  printf 'gap\n' >"$WORK/tmp/empty"   # restic refuses an empty stdin (exit 3): a marker carries a word
  R -r "$(repo_of "$DAYNOW")" backup --stdin --stdin-filename "audit/$NODE/$DAYNOW/gap" --host "$NODE" --tag "oa-gap=$1" --tag "node=$NODE" \
    <"$WORK/tmp/empty" >/dev/null 2>"$WORK/tmp/err" && { log "GAP recorded: $1"; return 0; }
  log "cannot record the gap $1: $(lasterr)"; return 1
}

# Ship file $1 (inode $2, closed=0|1) from CUR_OFF to its end. 0 = caught up; 1 = a failure (try again next cycle).
ship_file() {
  f="$1"; ino="$2"; closed="$3"
  while :; do
    size="$(stat -c %s "$f" 2>/dev/null)" || return 1                     # gone while we were here: list again next cycle
    [ "$(stat -c %i "$f" 2>/dev/null)" = "$ino" ] || return 1             # renamed under us: list again
    if [ "$size" -lt "$CUR_OFF" ]; then gap "$ino:$CUR_OFF:truncated-to-$size" || return 1; CUR_OFF=0; continue; fi
    if [ "$size" -eq "$CUR_OFF" ]; then
      if [ "$closed" = 1 ] && [ "$CLOSED_AT_EOF" != 1 ]; then printf 'eof-marker\n' >"$CHUNK"; put "$ino" "$CUR_OFF" "$CUR_OFF" eof "$CHUNK" || return 1; CLOSED_AT_EOF=1; fi
      return 0
    fi
    tail -c "+$((CUR_OFF + 1))" "$f" 2>/dev/null | head -c "$MAXB" >"$CHUNK"
    [ "$(stat -c %i "$f" 2>/dev/null)" = "$ino" ] || return 1             # the file was rotated while we read it: discard, list again
    n="$(wc -c <"$CHUNK")"; [ "$n" -gt 0 ] || return 0
    eof=""
    if [ "$(tail -c 1 "$CHUNK" | od -An -c | tr -d ' ')" != '\n' ]; then
      if [ "$closed" = 1 ] && [ $((CUR_OFF + n)) -ge "$size" ]; then eof=eof                  # the last, unterminated line of a rotated file
      else
        partial="$(tail -n 1 "$CHUNK" | wc -c)"; keep=$((n - partial))
        if [ "$keep" -le 0 ]; then
          [ "$n" -ge "$MAXB" ] || return 0                                                      # a line still being written: wait
          keep="$n"                                                                              # a line longer than a chunk: ship it cut
        fi
        head -c "$keep" "$CHUNK" >"$CHUNK.cut"; mv "$CHUNK.cut" "$CHUNK"; n="$keep"
      fi
    elif [ "$closed" = 1 ] && [ $((CUR_OFF + n)) -ge "$size" ]; then eof=eof
    fi
    put "$ino" "$CUR_OFF" $((CUR_OFF + n)) "$eof" "$CHUNK" || return 1
    CUR_OFF=$((CUR_OFF + n)); [ -z "$eof" ] || CLOSED_AT_EOF=1
  done
}

cycle() {
  DAYNOW="${DAY:-$(date -u +%F)}"
  ensure_repo "$DAYNOW" || return 1
  [ "$LOADED" = 1 ] || load_state || return 1
  # The files, oldest first: rotated (kube-apiserver-<ts>.log) then the live one. mtime, then name.
  list="$(for f in "$SRC_DIR"/kube-apiserver*.log; do [ -f "$f" ] && printf '%s %s %s\n' "$(stat -c %Y "$f")" "$(stat -c %i "$f")" "$f"; done | sort -k1,1n -k3,3)"
  [ -n "$list" ] || { log "no audit file under $SRC_DIR yet"; return 0; }
  start=1; total="$(printf '%s\n' "$list" | wc -l)"
  if [ -n "$CUR_INO" ]; then
    idx="$(printf '%s\n' "$list" | awk -v i="$CUR_INO" '$2 == i {print NR; exit}')"
    if [ -z "$idx" ]; then
      # The file we were in is gone: whatever came after CUR_OFF in it, and in any file already deleted, is lost. Say so, then ship what is left.
      gap "$CUR_INO:$CUR_OFF:file-gone" || return 1
      CUR_INO=""; CUR_OFF=0; CLOSED_AT_EOF=0
    else start="$idx"; fi
  fi
  k=0   # not `n`: ship_file uses it, and sh has no local variables
  while [ "$k" -lt "$total" ]; do
    k=$((k + 1)); [ "$k" -ge "$start" ] || continue
    line="$(printf '%s\n' "$list" | sed -n "${k}p")"; ino="$(printf '%s' "$line" | cut -d' ' -f2)"; f="$(printf '%s' "$line" | cut -d' ' -f3-)"
    closed=1; [ "$(basename "$f")" != "$LIVE" ] || closed=0
    if [ "$ino" != "$CUR_INO" ]; then CUR_INO="$ino"; CUR_OFF=0; CLOSED_AT_EOF=0; fi
    ship_file "$f" "$ino" "$closed" || return 1
  done
  return 0
}

log "audit-sink-shipper on $NODE: $BASE/$NODE/<day>, every ${INTERVAL}s, chunks of at most ${MAXB} bytes"
while :; do
  if cycle; then touch "$WORK/alive"; rc=0; else rc=1; log "cycle incomplete: retried in ${INTERVAL}s; nothing is dropped (the apiserver's files are the buffer)"; fi
  [ -z "${ONCE:-}" ] || exit "$rc"
  sleep "$INTERVAL"
done
