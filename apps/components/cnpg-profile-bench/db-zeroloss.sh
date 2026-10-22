#!/usr/bin/env bash
# db-zeroloss.sh: the acknowledged-commit writer and checker of docs/resilience.md ("Zero-loss proof"), for any PostgreSQL.
#   setup                         create the table zl_ack (run_id, seq) in the current database (a scratch database or schema)
#   write RUN SECS LOGDIR [N] [R] N workers (10) at R commits/s each (10): a FRESH connection per commit, the same seq retried
#                                 until it commits, and only AFTER the commit returned is "seq epoch_ms" appended to LOGDIR/ack.<w>
#   check RUN LOGDIR              compare the acknowledged ids with the table: prints one line and PASS or FAIL (exit 1 when an
#                                 acknowledged id is missing, or nothing was acknowledged)
#   teardown                      drop zl_ack
# The log of acknowledged ids lives with the writer, never in the database. libpq environment for the connection; the writer
# must run outside the node under test. Rows present but never acknowledged are counted and allowed (a commit whose
# acknowledgement the crash swallowed). A commit whose wait for the synchronous replica was cancelled is NOT an acknowledgement.
set -uo pipefail
export LC_ALL=C   # comm needs the same byte order as sort
cmd="${1:-}"; shift || true
PSQL=(psql -X -qAt -v ON_ERROR_STOP=1)
export PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-2}"
ok_run() { [[ "$1" =~ ^[A-Za-z0-9_-]{1,60}$ ]] || { echo "db-zeroloss: the run id is letters, digits, - and _" >&2; exit 2; }; }
now_ms() { date +%s%3N; }

worker() { # run secs logdir workers rate w
  local run=$1 secs=$2 dir=$3 n=$4 rate=$5 w=$6 log i=0 seq start end next now period err
  log="$dir/ack.$w"; : >"$log"; err="$(mktemp)"
  start=$(now_ms); end=$((start + secs * 1000)); period=$((1000 / rate))
  while [ "$(now_ms)" -lt "$end" ]; do
    seq=$((w + i * n))
    until timeout 10 "${PSQL[@]}" -v run="$run" -v seq="$seq" >/dev/null 2>"$err" \
        <<<"INSERT INTO zl_ack(run_id, seq) VALUES (:'run', :seq) ON CONFLICT DO NOTHING;" \
        && ! grep -qi "canceling wait for synchronous replication" "$err"; do
      [ "$(now_ms)" -lt "$end" ] || { rm -f "$err"; return 0; }
      sleep 0.1
    done
    echo "$seq $(now_ms)" >>"$log"
    i=$((i + 1)); next=$((start + i * period)); now=$(now_ms)
    [ "$next" -le "$now" ] || sleep "$(awk -v ms=$((next - now)) 'BEGIN{printf "%.3f", ms/1000}')"
  done
  rm -f "$err"
}

case "$cmd" in
  setup) "${PSQL[@]}" -c "CREATE TABLE IF NOT EXISTS zl_ack(run_id text NOT NULL, seq bigint NOT NULL, PRIMARY KEY (run_id, seq))";;
  teardown) "${PSQL[@]}" -c "DROP TABLE IF EXISTS zl_ack";;
  write)
    run=${1:?run id}; secs=${2:?seconds}; dir=${3:?log dir}; n=${4:-10}; rate=${5:-10}; ok_run "$run"; mkdir -p "$dir"
    pids=(); for w in $(seq 0 $((n - 1))); do worker "$run" "$secs" "$dir" "$n" "$rate" "$w" & pids+=($!); done
    wait "${pids[@]}"; echo "db-zeroloss: $(cat "$dir"/ack.* | wc -l) acknowledged in ${secs}s by $n workers at $rate/s";;
  check)
    run=${1:?run id}; dir=${2:?log dir}; ok_run "$run"; t="$(mktemp -d)"; trap 'rm -rf "$t"' EXIT
    cat "$dir"/ack.* 2>/dev/null | cut -d' ' -f1 | sort >"$t/acked"
    "${PSQL[@]}" -v run="$run" >"$t/db.raw" <<<"SELECT seq FROM zl_ack WHERE run_id = :'run';" || { echo "FAIL: cannot read zl_ack"; exit 1; }
    sort "$t/db.raw" >"$t/db"
    acked=$(wc -l <"$t/acked"); indb=$(wc -l <"$t/db")
    missing=$(comm -23 "$t/acked" "$t/db" | wc -l); unacked=$(comm -13 "$t/acked" "$t/db" | wc -l)
    gap=$(cat "$dir"/ack.* 2>/dev/null | cut -d' ' -f2 | sort -n | awk 'NR>1{d=$1-p; if(d>m)m=d}{p=$1}END{printf "%.1f", m/1000}')
    echo "run=$run acked=$acked in_db=$indb missing=$missing unacked_present=$unacked max_gap_s=${gap:-0}"
    [ "$missing" -gt 0 ] && comm -23 "$t/acked" "$t/db" | head -5 | sed 's/^/missing id /'
    if [ "$missing" -eq 0 ] && [ "$acked" -gt 0 ]; then echo PASS; else echo FAIL; exit 1; fi;;
  *) sed -n '2,13p' "$0"; exit 2;;
esac
