#!/usr/bin/env bash
# The SQL side of the CNPG restore test on a real PostgreSQL (the image of the Cluster): the canary writer, the live
# snapshot stage and snapshot.sql, then the comparison of restore_cnpg.py on a "restored" server (a dump of the live
# one) intact and under each damage (a flipped hash, a deleted row, a dropped table, a rewound table). Needs docker and the
# CNPG image; skipped with a notice without them unless CI=true. Rung: real PostgreSQL, mocked cluster.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
D="$ROOT/apps/base/backup/databases/restore"
W="$(mktemp -d)"; trap '[ -n "${KEEP:-}" ] && exit; docker rm -f oa-rt-sql >/dev/null 2>&1; chmod -R u+rwx "$W" 2>/dev/null; rm -rf "$W"' EXIT
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok   $*"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL $*"; }
chk() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }
IMG="$(python3 -I - "$ROOT/apps/base/backup/databases/verify-cronjob.yaml" <<'PY'
import sys, yaml
for d in yaml.safe_load_all(open(sys.argv[1])):
    if d and d.get("kind") == "CronJob":
        print(next(c["image"] for c in d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["initContainers"] if c["name"] == "barman-stage"))
PY
)"
if ! command -v docker >/dev/null || ! { docker image inspect "$IMG" >/dev/null 2>&1 || docker pull -q "$IMG" >/dev/null 2>&1; }; then
  echo "  SKIPPED the restore-test SQL test: docker or $IMG not available"
  [ "${CI:-}" = true ] && { echo "  FAIL required in CI"; exit 1; }
  exit 0
fi
mkdir -p "$W"/{creds/zitadel,creds/grafana,work}; chmod 777 "$W/work"
for c in zitadel grafana; do echo "$c" >"$W/creds/$c/username"; echo "pw" >"$W/creds/$c/password"; done
cat >"$W/up.sh" <<'UP'
set -eu
BIN=/usr/lib/postgresql/18/bin
for p in 5432 5433; do
  d=/tmp/pg$p; $BIN/initdb -D $d >/dev/null
  printf "port=$p\nlisten_addresses='127.0.0.1'\nunix_socket_directories='/tmp'\n" >>$d/postgresql.conf
  printf "host all all 127.0.0.1/32 trust\nlocal all all trust\n" >$d/pg_hba.conf
  $BIN/pg_ctl -D $d -l $d/log -w start >/dev/null
  for r in zitadel grafana; do psql -h 127.0.0.1 -p $p -d postgres -qc "create role $r login"; psql -h 127.0.0.1 -p $p -d postgres -qc "create database $r owner $r"; done
done
sleep infinity
UP
docker run -d --name oa-rt-sql --network host --user 26:26 --tmpfs /tmp:rw,mode=1777,size=512m -v "$W/up.sh:/up.sh:ro" -v "$D:/scripts:ro" \
  -v "$W/creds:/creds:ro" -v "$W/work:/work" --entrypoint bash "$IMG" /up.sh >/dev/null
for _ in $(seq 60); do docker exec oa-rt-sql psql -h 127.0.0.1 -p 5433 -d grafana -qAtc 'select 1' >/dev/null 2>&1 && break; sleep 1; done
live() { docker exec -i oa-rt-sql psql -X -qAt -h 127.0.0.1 -p 5432 -U postgres -d "$1" "${@:2}"; }
rest() { docker exec -i oa-rt-sql psql -X -qAt -F "$(printf '\t')" -h 127.0.0.1 -p 5433 -U postgres -d "$1" "${@:2}"; }
W2() { docker exec -e ZITADEL_DB_HOST=127.0.0.1 -e GRAFANA_DB_HOST=127.0.0.1 -e PGPORT=5432 oa-rt-sql sh /scripts/canary-write.sh; }

echo "=== canary writer ==="
for i in $(seq 25); do W2 >/dev/null 2>"$W/err" || { bad "canary write $i: $(head -c 200 "$W/err")"; break; }; done
chk "25 writes leave 25 rows with contiguous counters in both databases" "[ \"\$(live zitadel -c 'select count(*), max(counter)-min(counter)+1 from oa_canary')\" = '25|25' ] && [ \"\$(live grafana -c 'select count(*) from oa_canary')\" = 25 ]"
live grafana -c "set role grafana; create table app_t as select g from generate_series(1,2000) g; create table app_u(x int)" >/dev/null
live zitadel -c "set role zitadel; create table app_t as select g from generate_series(1,50) g" >/dev/null
W2 >/dev/null 2>&1

echo "=== live snapshot stage ==="
docker exec -e ZITADEL_DB_HOST=127.0.0.1 -e GRAFANA_DB_HOST=127.0.0.1 -e PGPORT=5432 oa-rt-sql sh /scripts/source-snapshot.sh >"$W/ss.out" 2>&1
chk "stage 1 leaves one snapshot per cluster" "[ -s '$W/work/zitadel-db.src' ] && [ -s '$W/work/grafana-db.src' ]"
chk "stage 1 does not write to the database" "[ \"\$(live zitadel -c 'select count(*) from oa_canary')\" = 26 ]"

cat >"$W/cmp.py" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("rc", sys.argv[1] + "/restore_cnpg.py"); rc = importlib.util.module_from_spec(spec); spec.loader.exec_module(rc)
src = rc.parse_snapshot(open(sys.argv[2]).read()); res = rc.parse_snapshot(open(sys.argv[3]).read())
ok, why, info = rc.compare(src, res, sys.argv[4], "latest")
print(("PASS " if ok else "FAIL ") + "; ".join(why)); sys.exit(0 if ok else 1)
PY
snap_restored() { rest "$1" -f /scripts/snapshot.sql >"$W/res.$1" 2>"$W/res.err"; }
copy() { # <db>: a dump of the live database into the "restored" server
  rest "$1" -c 'drop schema public cascade; create schema public; alter schema public owner to pg_database_owner' >/dev/null 2>&1
  docker exec oa-rt-sql sh -c "pg_dump -h 127.0.0.1 -p 5432 -U postgres -d $1 -O --no-acl | psql -X -q -h 127.0.0.1 -p 5433 -U postgres -d $1" >/dev/null 2>&1
}
cmp() { python3 -I "$W/cmp.py" "$D" "$W/work/$1-db.src" "$W/res.$1" "${2:-primary}" >"$W/cmp.out" 2>&1; }

echo "=== comparison on real data ==="
copy grafana; snap_restored grafana
cmp grafana; r=$?; chk "an intact restored copy passes ($(cat "$W/cmp.out"))" "[ $r = 0 ]"
chk "the snapshot lists the application tables and their row counts" "grep -q '^public.app_t	2000$' '$W/res.grafana' && grep -q '^public.app_u	0$' '$W/res.grafana'"
rest grafana -c "update oa_canary set sha = repeat('0', 64) where counter = (select min(counter)+10 from oa_canary)" >/dev/null
snap_restored grafana; cmp grafana; r=$?; chk "a flipped hash is found ($(cat "$W/cmp.out"))" "[ $r = 1 ]"
copy grafana; rest grafana -c "delete from oa_canary where counter = (select min(counter)+10 from oa_canary)" >/dev/null
snap_restored grafana; cmp grafana; r=$?; chk "a deleted canary row is found ($(cat "$W/cmp.out"))" "[ $r = 1 ]"
copy grafana; rest grafana -c "drop table app_t" >/dev/null
snap_restored grafana; chk "a dropped table shows in the snapshot" "! grep -q 'public.app_t' '$W/res.grafana'"
copy grafana; rest grafana -c "truncate app_t" >/dev/null
snap_restored grafana; cmp grafana; r=$?; chk "a table emptied by the restore is found ($(cat "$W/cmp.out"))" "[ $r = 1 ]"
copy grafana; rest grafana -c "delete from oa_canary where counter > (select max(counter)-2 from oa_canary)" >/dev/null
snap_restored grafana; cmp grafana; r=$?; chk "a copy 2 rows behind passes ($(cat "$W/cmp.out"))" "[ $r = 0 ]"
rest grafana -c "drop table oa_canary" >/dev/null
snap_restored grafana; cmp grafana; r=$?; chk "a restored database without the canary table fails ($(cat "$W/cmp.out"))" "[ $r = 1 ]"
W2 >/dev/null 2>&1; docker exec -e ZITADEL_DB_HOST=127.0.0.1 -e GRAFANA_DB_HOST=127.0.0.1 oa-rt-sql sh /scripts/source-snapshot.sh >/dev/null 2>&1
copy zitadel; snap_restored zitadel; cmp zitadel; r=$?; chk "the second database passes too ($(cat "$W/cmp.out"))" "[ $r = 0 ]"

echo "=== retention ==="
live grafana -c "insert into oa_canary select g, now(), 'x' from generate_series(1000,5100) g" >/dev/null
W2 >/dev/null 2>&1
chk "rows older than 4032 behind the newest are pruned, the newest kept" "[ \"\$(live grafana -c 'select count(*) from oa_canary where counter < (select max(counter) from oa_canary) - 4032')\" = 0 ] && [ \"\$(live grafana -c 'select max(counter) from oa_canary')\" = 5101 ]"

echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
