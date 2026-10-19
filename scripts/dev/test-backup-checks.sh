#!/usr/bin/env bash
# Tests of the backup brick's checks: (1) check-pg-dump-version.sh catches a stale
# client image, (2) the embedded scripts of the two monthly restore-test CronJobs
# accept an intact dump / raft snapshot and reject a damaged one. The scripts are
# extracted from the manifests, so what is tested is what ships; restic is a stub that
# "restores" a fixture. Needs python3 with PyYAML; docker for the pg_restore part
# (skipped with a notice without it, unless REQUIRE_PG_RESTORE=1 or CI=true: then a skip is a
# failure, so an image pull that fails on a runner cannot leave the main check unrun and green).
# Rung: mocked/static, nothing runs on a cluster.
#   Usage: scripts/dev/test-backup-checks.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
W="$(mktemp -d)"; trap 'chmod -R u+w "$W" 2>/dev/null; rm -rf "$W"' EXIT
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok   $*"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL $*"; }
expect() { # DESC WANT_RC CMD...
  local d="$1" want="$2"; shift 2
  "$@" >"$W/out" 2>&1; local rc=$?
  if { [ "$want" = 0 ] && [ $rc = 0 ]; } || { [ "$want" = fail ] && [ $rc != 0 ]; }; then ok "$d"; else bad "$d (rc=$rc: $(tail -2 "$W/out" | tr '\n' ' '))"; fi
}

# One retry: a registry rate limit or a blip is the usual reason for a missing image on a runner.
have_image() { # IMAGE
  command -v docker >/dev/null || return 1
  docker image inspect "$1" >/dev/null 2>&1 && return 0
  docker pull -q "$1" >/dev/null 2>&1 && return 0
  sleep 5; docker pull -q "$1" >/dev/null 2>&1
}
# A part that needs docker and could not run: a notice, but a failure where the check is required.
skipped() { # WHAT
  echo "  SKIPPED $1: docker or its image is not available"
  if [ "${REQUIRE_PG_RESTORE:-}" = 1 ] || [ "${CI:-}" = true ]; then bad "$1 not run but required here (CI / REQUIRE_PG_RESTORE=1)"; fi
}

echo "=== check-pg-dump-version.sh ==="
mk() { rm -rf "$W/t"; mkdir -p "$W/t/apps/base"; cp -r "$ROOT/apps/base/cnpg" "$ROOT/apps/base/backup" "$W/t/apps/base/"; }
CHK="$ROOT/scripts/dev/check-pg-dump-version.sh"
mk; expect "shipped manifests pass" 0 "$CHK" "$W/t"
mk; sed -i 's#postgresql:18\.[0-9]*#postgresql:16.3#' "$W/t/apps/base/backup/databases/cronjob.yaml"
expect "dump image below the highest Cluster major fails" fail "$CHK" "$W/t"
mk; sed -i 's#postgresql:18\.[0-9]*#postgresql:16.3#' "$W/t/apps/base/backup/databases/restore-test-cronjob.yaml"
expect "restore-test image below the highest Cluster major fails" fail "$CHK" "$W/t"
mk; sed -i 's#imageName: \(.*\)postgresql:16\.[0-9]*#imageName: \1postgresql:19.0#' "$W/t/apps/base/cnpg/grafana-db-cluster.yaml"
expect "a Cluster bumped above the dump image fails" fail "$CHK" "$W/t"
mk; rm -rf "$W/t/apps/base/backup"; mkdir "$W/t/apps/base/backup"
expect "no client image found fails instead of passing vacuously" fail "$CHK" "$W/t"

# Embedded script of a container, by CronJob file and container name.
script() { python3 -I - "$1" "$2" <<'PY'
import sys, yaml
doc = next(d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d.get("kind") == "CronJob")
pod = doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]
for c in pod.get("initContainers", []) + pod["containers"]:
    if c["name"] == sys.argv[2]:
        print(c["command"][2]); break
else:
    sys.exit("no container " + sys.argv[2])
PY
}
# restic stand-in: `check` succeeds, `stats` reports $STUB_BYTES (default 1000) as the snapshot size
# (STUB_STATS=fail: no answer), `restore --target T` copies $FIXTURE under T/backup.
mkdir -p "$W/bin"; cat > "$W/bin/restic" <<'EOS'
#!/bin/sh
case "$1" in
  check) exit 0 ;;
  stats) [ "${STUB_STATS:-}" = fail ] && exit 1
         printf '{"total_size":%s,"total_file_count":1,"snapshots_count":1}\n' "${STUB_BYTES:-1000}" ;;
  backup) echo "backup $RESTIC_REPOSITORY" >> "${CALLS:-/dev/null}"; exit "${STUB_RC_BACKUP:-0}" ;;
  forget) echo "forget $RESTIC_REPOSITORY" >> "${CALLS:-/dev/null}"; exit "${STUB_RC_FORGET:-0}" ;;
  restore) while [ $# -gt 0 ]; do [ "$1" = --target ] && t="$2"; shift; done
           mkdir -p "$t/backup"; cp -r "$FIXTURE"/. "$t/backup/" ;;
esac
EOS
chmod +x "$W/bin/restic"
run_script() { # SCRIPTFILE  (env: FIXTURE)
  env PATH="$W/bin:$PATH" CLUSTER_NAME=t PRIMARY_ENDPOINT=e PRIMARY_BUCKET=b PRIMARY_ACCESS_KEY=a PRIMARY_SECRET_KEY=s \
      REPLICA_ENDPOINT=e REPLICA_BUCKET=b REPLICA_ACCESS_KEY=a REPLICA_SECRET_KEY=s FIXTURE="$FIXTURE" sh "$1"
}

echo "=== openbao restore-test: raft snapshot integrity ==="
OB="$ROOT/apps/base/backup/openbao/restore-test-cronjob.yaml"
script "$OB" restore-test | sed "s#/tmp/restore-#$W/r-#" > "$W/ob.sh"
# A snapshot shaped like the real one (gzip'd tar: meta.json, state.bin, SHA256SUMS, SHA256SUMS.sealed).
mksnap() { # DIR
  mkdir -p "$1/s"; printf '{"Version":1}' > "$1/s/meta.json"; head -c 4096 /dev/urandom > "$1/s/state.bin"
  (cd "$1/s" && sha256sum meta.json state.bin > SHA256SUMS && printf sealed > SHA256SUMS.sealed \
     && tar -czf ../openbao-raft.snap meta.json state.bin SHA256SUMS SHA256SUMS.sealed)
}
FIXTURE="$W/f1"; mksnap "$FIXTURE"; rm -rf "$FIXTURE/s"
expect "intact snapshot passes" 0 run_script "$W/ob.sh"
FIXTURE="$W/f2"; mksnap "$FIXTURE"; (cd "$FIXTURE/s" && head -c 4096 /dev/urandom > state.bin && tar -czf ../openbao-raft.snap meta.json state.bin SHA256SUMS SHA256SUMS.sealed); rm -rf "$FIXTURE/s"
expect "state.bin altered (checksum mismatch) fails" fail run_script "$W/ob.sh"
FIXTURE="$W/f3"; mksnap "$FIXTURE"; rm -rf "$FIXTURE/s"; head -c 300 "$FIXTURE/openbao-raft.snap" > "$FIXTURE/t"; mv "$FIXTURE/t" "$FIXTURE/openbao-raft.snap"
expect "truncated snapshot fails" fail run_script "$W/ob.sh"
FIXTURE="$W/f4"; mkdir -p "$FIXTURE"; echo junk > "$FIXTURE/other.txt"
expect "non-empty restore without a .snap fails" fail run_script "$W/ob.sh"
FIXTURE="$W/f5"; mkdir -p "$FIXTURE"; echo not-a-tar > "$FIXTURE/openbao-raft.snap"
expect "a .snap that is not an archive fails" fail run_script "$W/ob.sh"

# The cluster runs these scripts under the restic image's busybox, not the host's dash/GNU tools:
# run the intact and the damaged snapshot there too (same stub restic, mounted over the real one).
RIMG="$(python3 -I -c "
import sys, yaml
d = next(d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d.get('kind') == 'CronJob')
print(next(c['image'] for c in d['spec']['jobTemplate']['spec']['template']['spec']['containers'] if c['name'] == 'restore-test'))" "$OB")"
if have_image "$RIMG"; then
  bb_run() { # SCRIPT  (env: FIXTURE)
    docker run --rm --user "$(id -u):$(id -g)" --entrypoint sh -v "$W:$W" -e CLUSTER_NAME=t -e PRIMARY_ENDPOINT=e -e PRIMARY_BUCKET=b -e PRIMARY_ACCESS_KEY=a \
      -e PRIMARY_SECRET_KEY=s -e REPLICA_ENDPOINT=e -e REPLICA_BUCKET=b -e REPLICA_ACCESS_KEY=a -e REPLICA_SECRET_KEY=s \
      -e FIXTURE="$FIXTURE" -e "PATH=$W/bin:/usr/bin:/bin:/usr/sbin:/sbin" "$RIMG" "$1"; }
  FIXTURE="$W/f1"; expect "busybox: intact snapshot passes" 0 bb_run "$W/ob.sh"
  FIXTURE="$W/f2"; expect "busybox: state.bin altered fails" fail bb_run "$W/ob.sh"
  FIXTURE="$W/f3"; expect "busybox: truncated snapshot fails" fail bb_run "$W/ob.sh"
else
  skipped "busybox run of the openbao script ($RIMG)"
fi

echo "=== databases restore-test: pg_restore --list ==="
DB="$ROOT/apps/base/backup/databases/restore-test-cronjob.yaml"
IMG="$(python3 -I -c "
import sys, yaml
d = next(d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d.get('kind') == 'CronJob')
print(next(c['image'] for c in d['spec']['jobTemplate']['spec']['template']['spec']['containers'] if c['name'] == 'pg-restore-list'))" "$DB")"
script "$DB" restic-restore | sed "s#/restore/#$W/restore/#" > "$W/dbr.sh"
script "$DB" pg-restore-list > "$W/dbl.sh"
FIXTURE="$W/g1"; mkdir -p "$FIXTURE"; echo x > "$FIXTURE/zitadel-db.dump"
rm -rf "$W/restore"; mkdir "$W/restore"
expect "restic-restore step restores both repositories" 0 run_script "$W/dbr.sh"
rm -rf "$W/restore"; mkdir "$W/restore"
STUB_BYTES=1000000000 expect "two restores of 1000 MB each exceed the 2Gi volume: refused with a message" fail run_script "$W/dbr.sh"
grep -q "raise the sizeLimit" "$W/out" && ok "and the message says what to change" || bad "no size message: $(tail -2 "$W/out")"
rm -rf "$W/restore"; mkdir "$W/restore"
STUB_BYTES=800000000 expect "two restores of 800 MB each fit" 0 run_script "$W/dbr.sh"
rm -rf "$W/restore"; mkdir "$W/restore"
STUB_STATS=fail expect "an unreadable snapshot size fails instead of restoring blind" fail run_script "$W/dbr.sh"
rm -rf "$W/restore"; mkdir "$W/restore"
expect "restic-restore step runs again with the default size" 0 run_script "$W/dbr.sh"
if [ -f "$W/restore/primary/backup/zitadel-db.dump" ] && [ -f "$W/restore/replica/backup/zitadel-db.dump" ]; then ok "both repositories land under /restore/<label>"; else bad "restore layout"; fi

if have_image "$IMG"; then
  # Real dumps from a throwaway server of the same image, as the cnpg-dump init container writes them.
  mkdir -p "$W/dumps"; chmod 777 "$W/dumps"
  docker run --rm -v "$W/dumps:/out" --entrypoint sh "$IMG" -c '
    set -e; D=/tmp/pg; /usr/lib/postgresql/*/bin/initdb -D $D >/dev/null
    /usr/lib/postgresql/*/bin/pg_ctl -D $D -o "-k /tmp" -l /tmp/log -w start >/dev/null
    for n in zitadel grafana; do psql -h /tmp -d postgres -qc "create database $n"; psql -h /tmp -d $n -qc "create table t(i int); insert into t values (1)"
      pg_dump -h /tmp -Fc -f /out/$n-db.dump $n; done' >"$W/gen.out" 2>&1 || { bad "could not generate fixtures: $(tail -3 "$W/gen.out")"; }
  docker_run() { # DIR, mounted as /restore like in the pod
    docker run --rm -v "$1:/restore:ro" -v "$W/dbl.sh:/dbl.sh:ro" --entrypoint sh "$IMG" /dbl.sh; }
  layout() { rm -rf "$W/p"; for l in primary replica; do mkdir -p "$W/p/$l/backup"; cp "$W/dumps"/*.dump "$W/p/$l/backup/"; done; }
  layout; expect "intact dumps pass pg_restore --list" 0 docker_run "$W/p"
  layout; head -c 100 "$W/dumps/grafana-db.dump" > "$W/p/replica/backup/grafana-db.dump"
  expect "a truncated dump fails" fail docker_run "$W/p"
  layout; rm "$W/p/primary/backup/zitadel-db.dump"
  expect "a snapshot missing a database dump fails" fail docker_run "$W/p"
  layout; echo garbage > "$W/p/primary/backup/grafana-db.dump"
  expect "a non-archive dump fails" fail docker_run "$W/p"
else
  skipped "pg_restore part ($IMG)"
fi

echo "=== restic-push scripts: restic 0.19 exit codes ==="
# restic 0.19: `backup` exits 3 on a missing/unreadable source, `forget` exits 3 when it cannot remove snapshots,
# 130 on SIGINT. A bad backup must fail the Job at once; a failed retention must fail it only after the replica got its snapshot.
for spec in "openbao/cronjob.yaml openbao" "databases/cronjob.yaml cnpg"; do
  set -- $spec; F="$ROOT/apps/base/backup/$1"; N="$2"
  script "$F" restic-push | sed 's#/tmp/restic-cache#'"$W"'/rc#' > "$W/push-$N.sh"
  push() { env PATH="$W/bin:$PATH" CALLS="$W/calls-$N" CLUSTER_NAME=t PRIMARY_ENDPOINT=e PRIMARY_BUCKET=b PRIMARY_ACCESS_KEY=a \
      PRIMARY_SECRET_KEY=s REPLICA_ENDPOINT=e REPLICA_BUCKET=b REPLICA_ACCESS_KEY=a REPLICA_SECRET_KEY=s "$@" sh "$W/push-$N.sh"; }
  rm -f "$W/calls-$N"; expect "$N push: all exit codes 0 passes" 0 push
  [ "$(grep -c '^backup' "$W/calls-$N")" = 2 ] && ok "$N push: both repositories got a snapshot" || bad "$N push: backup calls: $(cat "$W/calls-$N")"
  rm -f "$W/calls-$N"; expect "$N push: backup exit 3 (missing source) fails" fail push STUB_RC_BACKUP=3
  [ "$(grep -c '^backup' "$W/calls-$N")" = 2 ] && ok "$N push: an incomplete snapshot fails the Job but the other repository is still tried" || bad "$N push: calls: $(cat "$W/calls-$N")"
  push STUB_RC_BACKUP=130 >/dev/null 2>&1; [ $? = 130 ] && ok "$N push: SIGINT code 130 is passed on" || bad "$N push: 130 not passed on"
  rm -f "$W/calls-$N"; expect "$N push: forget exit 3 fails the Job" fail push STUB_RC_FORGET=3
  [ "$(grep -c '^backup' "$W/calls-$N")" = 2 ] && ok "$N push: ... but only after the replica got its snapshot" || bad "$N push: calls: $(cat "$W/calls-$N")"
done

printf '%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$PASS" -gt 0 ] && [ "$FAIL" -eq 0 ]
