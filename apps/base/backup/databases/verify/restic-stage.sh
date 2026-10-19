#!/bin/sh
# Stage 1 of the backup verifier (restic image): for each restic dataset on each store, read a rotating part of the data
# (restic check --read-data-subset=n/M, so every pack is read within M days), and restore the latest snapshot of the
# datasets whose content the collector inspects. One TSV line per (dataset, store) in $RESULTS/restic.tsv:
#   dataset <TAB> store <TAB> 1|0|skip <TAB> time of the latest snapshot <TAB> message
# A repository that does not exist, or has no snapshot, is "skip" (a fresh cluster, or a brick that is not picked):
# the dedicated freshness alerts of the backup jobs own that case. Anything else wrong is 0.
set -u
RESULTS="${RESULTS:-/results}"; WORK="${WORK:-/work}"; OUT="$RESULTS/restic.tsv"; : > "$OUT"
: "${CLUSTER_NAME:?}"
M="${RESTIC_SUBSET_PARTS:-10}"
N=$(( $(date -u +%j | sed 's/^0*//') % M + 1 ))
export RESTIC_CACHE_DIR="${RESTIC_CACHE_DIR:-/tmp/restic-cache}"
emit() { printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5" >> "$OUT"; }
one() { # dataset store endpoint bucket path host restore(yes|no) ak sk
  ds="$1"; store="$2"; host="$6"; restore="$7"
  export RESTIC_REPOSITORY="s3:${3}/${4}/${CLUSTER_NAME}/${5}" AWS_ACCESS_KEY_ID="$8" AWS_SECRET_ACCESS_KEY="$9"
  err="$(mktemp)"
  if ! timeout "${RESTIC_CAT_TIMEOUT:-120}" restic cat config >/dev/null 2>"$err"; then
    if grep -qiE 'does not exist|Is there a repository|unable to open config file: .*(NoSuchKey|not found|does not exist)' "$err"; then
      emit "$ds" "$store" skip "" "no repository at ${5}"
    else
      emit "$ds" "$store" 0 "" "repository unreadable: $(tail -1 "$err" | tr '\t' ' ')"
    fi
    return
  fi
  snap="$(restic snapshots --json --latest 1 --host "$host" 2>"$err" | sed -n 's/.*"time":"\([^"]*\)".*/\1/p' | head -1)"
  if [ -z "$snap" ]; then emit "$ds" "$store" skip "" "no snapshot yet"; return; fi
  if ! timeout 3000 restic check --read-data-subset="${N}/${M}" >"$err" 2>&1; then
    emit "$ds" "$store" 0 "$snap" "restic check ${N}/${M} failed: $(grep -iE 'error|fatal|pack|id' "$err" | head -2 | tr '\n\t' '  ')"; return
  fi
  if [ "$restore" = yes ]; then
    rm -rf "${WORK:?}/$ds/$store"; mkdir -p "$WORK/$ds/$store"
    if ! restic restore latest --host "$host" --target "$WORK/$ds/$store" >"$err" 2>&1; then
      emit "$ds" "$store" 0 "$snap" "restore of the latest snapshot failed: $(tail -1 "$err" | tr '\t' ' ')"; return
    fi
  elif ! restic ls latest --host "$host" 2>/dev/null | grep -q '\.dump$'; then
    emit "$ds" "$store" 0 "$snap" "the latest snapshot holds no .dump file"; return
  fi
  emit "$ds" "$store" 1 "$snap" "restic check ${N}/${M} passed"
}
for ds in "openbao openbao yes" "cnpg-dump cnpg no" "etcd etcd yes"; do
  set -- $ds
  one "$1" primary "$PRIMARY_ENDPOINT" "$PRIMARY_BUCKET" "$2" "$2" "$3" "$PRIMARY_ACCESS_KEY" "$PRIMARY_SECRET_KEY"
  one "$1" replica "$REPLICA_ENDPOINT" "$REPLICA_BUCKET" "$2" "$2" "$3" "$REPLICA_ACCESS_KEY" "$REPLICA_SECRET_KEY"
done
