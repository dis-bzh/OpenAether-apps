#!/usr/bin/env bash
# The database benchmark and the zero-loss writer/checker (apps/components/cnpg-profile-bench) on a REAL PostgreSQL (the CNPG image
# of the Clusters): db-bench.sh gives a row per target and mode; db-zeroloss.sh acknowledges only committed ids, survives a crash
# restart of the server, and its checker CATCHES a lost id (one injected in the log, one deleted from the table). Needs docker and
# the image; skipped with a notice without them unless CI=true. Rung: real PostgreSQL, one node (no replication, no cluster).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
D="$ROOT/apps/components/cnpg-profile-bench"
IMG="$(grep -rhoE 'ghcr.io/cloudnative-pg/postgresql:[0-9.]+' "$ROOT/apps/components/cnpg-profile-ha/cluster.yaml" | head -n 1)"
N=oa-dbbench-$$
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok   $*"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL $*"; }
chk() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }
trap 'docker rm -f "$N" >/dev/null 2>&1' EXIT
if ! command -v docker >/dev/null || ! { docker image inspect "$IMG" >/dev/null 2>&1 || docker pull -q "$IMG" >/dev/null 2>&1; }; then
  echo "  SKIPPED the db-bench test: docker or $IMG not available"
  [ "${CI:-}" = true ] && { echo "  FAIL required in CI"; exit 1; }
  exit 0
fi
docker run -d --name "$N" --user 26:26 --tmpfs /tmp:rw,mode=1777,size=512m -v "$D:/bench:ro" --entrypoint bash "$IMG" -c '
set -eu
BIN=$(ls -d /usr/lib/postgresql/*/bin | tail -n 1)
$BIN/initdb -D /tmp/pg >/dev/null
printf "port=5432\nlisten_addresses='"'"'127.0.0.1'"'"'\nunix_socket_directories='"'"'/tmp'"'"'\nmax_connections=100\n" >>/tmp/pg/postgresql.conf
printf "host all all 127.0.0.1/32 trust\nlocal all all trust\n" >/tmp/pg/pg_hba.conf
$BIN/pg_ctl -D /tmp/pg -l /tmp/pg.log -w start >/dev/null
psql -h 127.0.0.1 -d postgres -qc "create role app login" && psql -h 127.0.0.1 -d postgres -qc "create database app owner app"
echo ready >/tmp/ready; sleep infinity' >/dev/null
for _ in $(seq 60); do docker exec "$N" test -f /tmp/ready 2>/dev/null && break; sleep 1; done
x() { docker exec -e PGHOST=127.0.0.1 -e PGUSER=app -e PGDATABASE=app "$N" "$@"; }

echo "=== db-bench.sh ==="
out="$(x bash /bench/db-bench.sh -P ha -H -I -s 1 -c 2 -j 1 -t 3 -m "rw ro" -T direct=127.0.0.1:5432 -T pooler=127.0.0.1:5432 2>&1)"; rc=$?
chk "the benchmark exits 0" "[ $rc = 0 ]"
rows="$(grep -c '^| ha |' <<<"$out")"
chk "two targets x two modes give four rows (got $rows)" "[ '$rows' = 4 ]"
chk "the header is printed once" "[ \"\$(grep -c '^| profile |' <<<\"\$out\")\" = 1 ]"
chk "every row has 12 cells, TPS above 0 and p99 >= p95 >= p50" "awk -F'|' '/^\\| ha /{ if (NF!=14 || \$8+0<=0 || \$12+0<\$11+0 || \$11+0<\$10+0) b=1 } END{exit b}' <<<\"\$out\""
chk "no failed transaction on a healthy server" "! awk -F'|' '/^\\| ha /{ if (\$13+0>0) f=1 } END{exit !f}' <<<\"\$out\""
x bash /bench/db-bench.sh -P ha -c 2 -t 2 -m rw -T gone=127.0.0.1:5999 >/dev/null 2>&1
chk "an unreachable target fails the run (non-zero), it does not print a zero row" "[ $? != 0 ]"
x bash /bench/db-bench.sh -c 0 >/dev/null 2>&1
chk "a bad parameter is refused" "[ $? = 2 ]"

echo "=== db-zeroloss.sh ==="
x bash /bench/db-zeroloss.sh setup
x bash -c 'bash /bench/db-zeroloss.sh write r1 4 /tmp/l1 4 10' >"$ROOT/.zl.out" 2>&1; cat "$ROOT/.zl.out"; rm -f "$ROOT/.zl.out"
res="$(x bash /bench/db-zeroloss.sh check r1 /tmp/l1)"; rc=$?
echo "      $res" | head -2
chk "a clean run passes: every acknowledged id is in the table" "[ $rc = 0 ] && grep -q PASS <<<\"\$res\" && grep -q 'missing=0' <<<\"\$res\""
chk "the writer acknowledged a plausible number of commits (4 workers x 10/s x 4 s, at least 60)" "[ \"\$(sed -n 's/.*acked=\\([0-9]*\\) .*/\\1/p' <<<\"\$res\" | head -n 1)\" -ge 60 ]"
x bash -c 'echo "9999999 1700000000000" >>/tmp/l1/ack.0'
res="$(x bash /bench/db-zeroloss.sh check r1 /tmp/l1)"; rc=$?
chk "an acknowledged id that never reached the table (injected in the log) is CAUGHT" "[ $rc = 1 ] && grep -q 'missing=1' <<<\"\$res\" && grep -q FAIL <<<\"\$res\""
x bash -c 'sed -i "\$d" /tmp/l1/ack.0'
x psql -qc "delete from zl_ack where run_id='r1' and seq = (select min(seq) from zl_ack where run_id='r1')"
res="$(x bash /bench/db-zeroloss.sh check r1 /tmp/l1)"; rc=$?
chk "a row deleted from the table after its acknowledgement is CAUGHT" "[ $rc = 1 ] && grep -q 'missing=1' <<<\"\$res\""
x psql -qc "insert into zl_ack values ('r1', 8888888)" ; x psql -qc "insert into zl_ack select 'r1', min(seq) from (select 0 as seq) s on conflict do nothing"
x bash -c 'echo "0 1700000000000" >>/tmp/l1/ack.0' >/dev/null
res="$(x bash /bench/db-zeroloss.sh check r1 /tmp/l1)"
chk "a row present but never acknowledged is counted and allowed" "grep -q 'unacked_present=1' <<<\"\$res\""
x bash /bench/db-zeroloss.sh check 'r1;drop table zl_ack' /tmp/l1 >/dev/null 2>&1
chk "a run id with SQL in it is refused" "[ $? = 2 ] && x psql -qAtc 'select count(*) from zl_ack' >/dev/null"

echo "=== a crash of the server during the writer ==="
( sleep 3; x bash -c 'BIN=$(ls -d /usr/lib/postgresql/*/bin | tail -n 1); $BIN/pg_ctl -D /tmp/pg -m immediate -w restart >/dev/null 2>&1' ) &
x bash /bench/db-zeroloss.sh write r2 9 /tmp/l2 4 10 >/dev/null 2>&1; wait
res="$(x bash /bench/db-zeroloss.sh check r2 /tmp/l2)"; rc=$?
echo "      $res" | head -2
chk "after an immediate restart (a crash) no acknowledged id is missing" "[ $rc = 0 ] && grep -q 'missing=0' <<<\"\$res\""
gap="$(sed -n 's/.*max_gap_s=\([0-9.]*\).*/\1/p' <<<"$res")"
chk "the writer saw the outage as a gap in its acknowledgements (max_gap_s $gap >= 0.5) and resumed" "awk -v g=\"$gap\" 'BEGIN{exit !(g>=0.5)}'"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
