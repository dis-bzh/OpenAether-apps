#!/usr/bin/env bash
# db-bench.sh: pgbench read-write and read-only against one or more endpoints of a database, one result row each
# (TPS, average and p50/p95/p99 latency, failed transactions), in the table format of docs/database-profiles.md.
# Connection settings are the libpq environment (PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE PGSSLMODE PGSSLROOTCERT); the
# password is never an argument. Compare the pooler and the direct service by naming both targets:
#   PGUSER=app PGPASSWORD=... PGDATABASE=app PGSSLMODE=verify-full PGSSLROOTCERT=ca.crt \
#   db-bench.sh -P ha -I -s 50 -c 32 -t 120 -T direct=app-db-rw.shop.svc -T pooler=app-db-pooler-rw.shop.svc
# Options: -P profile label, -L path label for the environment's endpoint (default direct), -T label=host[:port] (repeatable),
#   -s scale (10), -c clients (16), -j threads (4), -t seconds (60), -m modes "rw ro" ("rw"), -M simple|extended|prepared (simple),
#   -I initialise the tables first on the first endpoint (pgbench_* tables of the database; use a scratch database), -o FILE
#   (append the rows, Markdown), -H print the table header first.
# Through a transaction-mode pooler use -M simple or extended; -M prepared needs max_prepared_statements on the pooler.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARSE="${DB_BENCH_PARSE:-$HERE/db-bench-parse.py}"
profile=unlabelled plabel=direct scale=10 clients=16 jobs=4 secs=60 modes="rw ro" proto=simple init=0 out="" header=0
targets=()
while getopts "P:L:T:s:c:j:t:m:M:Io:H" o; do
  case "$o" in
    P) profile=$OPTARG;; L) plabel=$OPTARG;; T) targets+=("$OPTARG");; s) scale=$OPTARG;; c) clients=$OPTARG;; j) jobs=$OPTARG;;
    t) secs=$OPTARG;; m) modes=$OPTARG;; M) proto=$OPTARG;; I) init=1;; o) out=$OPTARG;; H) header=1;;
    *) sed -n '2,16p' "$0"; exit 2;;
  esac
done
for n in scale clients jobs secs; do [[ "${!n}" =~ ^[1-9][0-9]*$ ]] || { echo "db-bench: -$n must be a positive integer" >&2; exit 2; }; done
[[ "$proto" =~ ^(simple|extended|prepared)$ ]] || { echo "db-bench: -M simple, extended or prepared" >&2; exit 2; }
[ "${#targets[@]}" -gt 0 ] || targets=("$plabel=${PGHOST:-localhost}${PGPORT:+:$PGPORT}")
[ "$jobs" -le "$clients" ] || jobs=$clients
PGBENCH="$(command -v pgbench || ls /usr/lib/postgresql/*/bin/pgbench 2>/dev/null | tail -n 1)"
[ -n "$PGBENCH" ] || { echo "db-bench: pgbench not found" >&2; exit 2; }

emit() { if [ -n "$out" ]; then tee -a "$out"; else cat; fi; }
if [ "$header" = 1 ]; then python3 "$PARSE" header --md | emit; fi
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
first=1
for t in "${targets[@]}"; do
  label="${t%%=*}"; hp="${t#*=}"; host="${hp%%:*}"; port="${hp#*:}"; [ "$port" = "$hp" ] && port="${PGPORT:-5432}"
  export PGHOST="$host" PGPORT="$port"
  if [ "$init" = 1 ] && [ "$first" = 1 ]; then "$PGBENCH" -i -q -s "$scale" >"$work/init.out" 2>&1 || { cat "$work/init.out" >&2; exit 1; }; fi
  first=0
  for mode in $modes; do
    case "$mode" in rw) flag=();; ro) flag=(-S);; *) echo "db-bench: modes are rw and ro" >&2; exit 2;; esac
    d="$work/$label-$mode"; mkdir -p "$d"
    "$PGBENCH" "${flag[@]}" -M "$proto" -c "$clients" -j "$jobs" -T "$secs" -l --log-prefix "$d/pgb" >"$d/stdout" 2>"$d/stderr" \
      || { cat "$d/stderr" >&2; echo "db-bench: pgbench failed on $label" >&2; exit 1; }
    python3 "$PARSE" row --dir "$d" --stdout "$d/stdout" --profile "$profile" --path "$label" --mode "$mode" --scale "$scale" \
      --clients "$clients" --secs "$secs" --md | emit
  done
done
