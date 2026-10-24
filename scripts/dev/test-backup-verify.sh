#!/usr/bin/env bash
# The backup verifier (apps/base/backup/databases/verify) end to end, offline: real restic repositories, a real Barman archive
# (barman-cloud-backup and -wal-archive from a throwaway PostgreSQL) and a real etcd snapshot, on two fake S3 stores, through
# the three stages as the CronJob runs them (the images the manifest names). Then each kind of damage: the verifier must say 0
# for exactly the dataset and store that is damaged, keep the last success, and say 1 again once repaired.
# Needs docker (the restic and PostgreSQL images), python3, aws not needed. Skipped with a notice without docker, unless
# CI=true (then a skip is a failure). Rung: mocked stores, real tools.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
V="$ROOT/apps/base/backup/databases/verify"
W="$(mktemp -d)"; trap 'kill $(jobs -p) 2>/dev/null; chmod -R u+rwx "$W" 2>/dev/null; rm -rf "$W"' EXIT
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok   $*"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL $*"; }
chk() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }
img() { python3 -I - "$ROOT/$1" "$2" <<'PY'
import sys, yaml
for d in yaml.safe_load_all(open(sys.argv[1])):
    if d and d.get("kind") == "CronJob":
        p = d["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        print(next(c["image"] for c in p.get("initContainers", []) + p["containers"] if c["name"] == sys.argv[2]))
PY
}
RESTIC="$(img apps/base/backup/databases/verify-cronjob.yaml restic-stage)"
PG="$(img apps/base/backup/databases/verify-cronjob.yaml barman-stage)"
have() { docker image inspect "$1" >/dev/null 2>&1 || docker pull -q "$1" >/dev/null 2>&1; }
if ! command -v docker >/dev/null || ! have "$RESTIC" || ! have "$PG"; then
  echo "  SKIPPED the verifier test: docker or its images are not available"
  [ "${CI:-}" = true ] && { echo "  FAIL required in CI"; exit 1; }
  exit 0
fi
U="--user $(id -u):$(id -g)"
mkdir -p "$W"/{P,R,res,work,tmp,bin} "$W/P/bkp" "$W/R/bkr"; chmod 777 "$W"/{res,work,tmp}
echo '{"bkp":["AKP"]}' >"$W/P/.acl"; echo '{"bkr":["AKR"]}' >"$W/R/.acl"
python3 -I "$ROOT/scripts/dev/testdata/fake_s3_server.py" --root "$W/P" --log "$W/P.log" >"$W/P.url" &
python3 -I "$ROOT/scripts/dev/testdata/fake_s3_server.py" --root "$W/R" --log "$W/R.log" >"$W/R.url" &
for _ in $(seq 50); do [ -s "$W/P.url" ] && [ -s "$W/R.url" ] && break; sleep 0.1; done
PU="$(cat "$W/P.url")"; RU="$(cat "$W/R.url")"
S3ENV=(-e PRIMARY_ENDPOINT="$PU" -e PRIMARY_BUCKET=bkp -e PRIMARY_ACCESS_KEY=AKP -e PRIMARY_SECRET_KEY=s
       -e REPLICA_ENDPOINT="$RU" -e REPLICA_BUCKET=bkr -e REPLICA_ACCESS_KEY=AKR -e REPLICA_SECRET_KEY=s
       -e CLUSTER_NAME=lab -e RESTIC_PASSWORD=pw -e AWS_DEFAULT_REGION=us-east-1)

# ── seed: restic repositories on both stores (openbao snapshot, cnpg dump, etcd snapshot) ──────────────────────────────
mkdir -p "$W/seed/openbao/u" "$W/seed/cnpg" "$W/seed/etcd"
echo '{"index":1}' >"$W/seed/openbao/u/meta.json"; head -c 4000 /dev/urandom >"$W/seed/openbao/u/state.bin"
(cd "$W/seed/openbao/u" && sha256sum meta.json state.bin >SHA256SUMS && tar -czf ../raft.snap meta.json state.bin SHA256SUMS); rm -rf "$W/seed/openbao/u"
head -c 20000 /dev/urandom >"$W/seed/cnpg/zitadel-db.dump"
cp "$ROOT/scripts/dev/testdata/etcd-snapshot-v3.6.db" "$W/seed/etcd/etcd.snap"
seed() { # store dataset
  local ep bk ak; if [ "$1" = primary ]; then ep=$PU bk=bkp ak=AKP; else ep=$RU bk=bkr ak=AKR; fi
  docker run --rm --network host $U -v "$W/seed:/seed:ro" -v "$W/tmp:/tmp" -e RESTIC_PASSWORD=pw -e AWS_ACCESS_KEY_ID=$ak -e AWS_SECRET_ACCESS_KEY=s \
    -e RESTIC_REPOSITORY="s3:$ep/$bk/lab/$2" -e RESTIC_CACHE_DIR=/tmp/c --entrypoint sh "$RESTIC" -c "restic cat config >/dev/null 2>&1 || restic init -q; restic backup -q /seed/$2 --host $2" >"$W/seed.out" 2>&1 || { cat "$W/seed.out"; return 1; }
}
for st in primary replica; do for ds in openbao cnpg etcd; do seed $st $ds || bad "seed $st $ds"; done; done

# ── seed: a real Barman archive (PostgreSQL -> barman-cloud) on the primary, copied to the replica ────────────────────
cat >"$W/gen.sh" <<'GEN'
set -eu
export AWS_ACCESS_KEY_ID=AKP AWS_SECRET_ACCESS_KEY=s AWS_DEFAULT_REGION=us-east-1 AWS_REQUEST_CHECKSUM_CALCULATION=when_required AWS_RESPONSE_CHECKSUM_VALIDATION=when_required
D=/tmp/pg; mkdir -p $D; BIN=/usr/lib/postgresql/18/bin
$BIN/initdb -D $D/data >/dev/null
cat >> $D/data/postgresql.conf <<C
archive_mode=on
archive_command='barman-cloud-wal-archive --cloud-provider aws-s3 --endpoint-url $S3URL --gzip s3://bkp/cnpg/$SRV $SRV %p'
listen_addresses=''
unix_socket_directories='$D'
C
$BIN/pg_ctl -D $D/data -l $D/log -w start >/dev/null
psql -h $D -d postgres -qc "create table t as select g from generate_series(1,200000) g" ; psql -h $D -d postgres -qc "select pg_switch_wal()" >/dev/null
barman-cloud-backup --cloud-provider aws-s3 --endpoint-url $S3URL --gzip -h $D -U $(id -un) s3://bkp/cnpg/$SRV $SRV
for i in 1 2 3 4; do psql -h $D -d postgres -qc "insert into t select g from generate_series(1,1000) g; select pg_switch_wal()" >/dev/null; done
sleep 3; $BIN/pg_ctl -D $D/data -w stop >/dev/null
GEN
docker run --rm --network host --user 26:26 -e S3URL="$PU" -e SRV=zitadel-db -v "$W/gen.sh:/gen.sh:ro" --tmpfs /tmp:rw,mode=1777 --entrypoint bash "$PG" /gen.sh >"$W/gen.out" 2>&1 \
  || { bad "barman archive generation"; tail -5 "$W/gen.out"; }
find "$W/P/bkp/cnpg" -type f 2>/dev/null | grep -q . || { bad "no barman archive generated"; tail -5 "$W/gen.out"; }
rm -rf "$W/P/.mp"; mkdir -p "$W/R/bkr"; cp -a "$W/P/bkp/cnpg" "$W/R/bkr/cnpg"
mkdir -p "$W/P/bkp/cnpg/grafana-db" "$W/R/bkr/cnpg/grafana-db"   # an empty cluster archive: skipped, not failed

# ── the Longhorn objects, through a stub kubectl ────────────────────────────────────────────────────────────────────────
cat >"$W/bin/kubectl" <<'KC'
#!/usr/bin/env bash
case "$*" in
  *"get backupvolumes.longhorn.io"*) cat "$FAKE_KUBE/volumes.json" ;;
  *"get backups.longhorn.io"*) cat "$FAKE_KUBE/backups.json" ;;
  *"get configmap"*jsonpath*) cat "$FAKE_KUBE/metrics" 2>/dev/null ;;
  *"patch configmap"*) python3 -I -c 'import json,sys; open(sys.argv[1]+"/metrics","w").write(json.loads(sys.argv[2])["data"]["metrics"])' "$FAKE_KUBE" "${@: -1}" ;;
  *) echo "stub kubectl: $*" >&2; exit 1 ;;
esac
KC
chmod +x "$W/bin/kubectl"; mkdir "$W/kube"
lh() { # state
  echo '{"items":[{"metadata":{"name":"vol1"},"status":{"lastBackupName":"backup-1","lastBackupAt":"2026-10-19T03:37:00Z"}}]}' >"$W/kube/volumes.json"
  echo '{"items":[{"metadata":{"name":"backup-1"},"status":{"state":"'"$1"'","error":"","size":"1048576","backupCreatedAt":"2026-10-19T03:37:00Z","volumeName":"vol1"}}]}' >"$W/kube/backups.json"
}
lh Completed

run_all() { # runs the three stages as the CronJob does
  rm -rf "$W/res"/* "$W/work"/*
  docker run --rm --network host $U "${S3ENV[@]}" -e RESTIC_SUBSET_PARTS="${PARTS:-1}" -e RESTIC_CAT_TIMEOUT=8 -e RESTIC_CACHE_DIR=/tmp/rc -v "$V:/scripts:ro" -v "$W/res:/results" \
    -v "$W/work:/work" --tmpfs /tmp:rw,mode=1777 --read-only --entrypoint /bin/sh "$RESTIC" /scripts/restic-stage.sh >"$W/s1.out" 2>&1
  docker run --rm --network host $U "${S3ENV[@]}" -e PYTHONWARNINGS=ignore -e HOME=/tmp -e NOW="${NOW:-}" -v "$V:/scripts:ro" -v "$W/res:/results" --tmpfs /tmp:rw,mode=1777 \
    --read-only --entrypoint /bin/sh "$PG" -c 'python3 /scripts/barman_check.py > /results/cnpg.tsv' >"$W/s2.out" 2>&1
  PATH="$W/bin:$PATH" FAKE_KUBE="$W/kube" NOW="${NOW:-}" python3 -I "$V/collect.py" --results "$W/res" --work "$W/work" --publish >"$W/s3.out" 2>&1
}
m() { grep -E "^$1\{" "$W/kube/metrics" 2>/dev/null | sed -n "s/.*$2\"\? \([0-9.e+]*\)\$/\1/p" | head -1; }
okv() { grep -E "^oa_backup_verify_ok\{dataset=\"$1\"\}" "$W/kube/metrics" | awk '{print $2}'; }
storeok() { grep -E "^oa_backup_verify_store_ok\{dataset=\"$1\",store=\"$2\"\}" "$W/kube/metrics" | awk '{print $2}'; }
lastok() { grep -E "^oa_backup_verify_last_success_timestamp_seconds\{dataset=\"$1\"\}" "$W/kube/metrics" | awk '{print $2}'; }

echo "=== healthy: every dataset verified on both stores ==="
run_all
for ds in openbao cnpg-dump etcd cnpg longhorn; do chk "$ds: oa_backup_verify_ok 1 ($(grep -h "$ds/" "$W/s3.out" | head -2 | tr '\n' ' ' | cut -c1-150))" "[ \"\$(okv $ds)\" = 1 ]"; done
chk "the grafana-db archive without a base backup is skipped, not failed" "grep -q 'grafana-db' '$W/res/cnpg.tsv' && grep -P 'grafana-db' '$W/res/cnpg.tsv' | grep -qP '\tskip\t'"
chk "last success is set for every dataset" "[ -n \"\$(lastok openbao)\" ] && [ -n \"\$(lastok cnpg)\" ] && [ -n \"\$(lastok etcd)\" ]"
chk "the exposition holds the series of the contract and nothing without a dataset label but the run time" \
  "grep -c '^oa_backup_verify_' '$W/kube/metrics' | grep -qv '^0\$' && ! grep -E '^oa_backup_verify_(ok|last_success_timestamp_seconds)[^{]' '$W/kube/metrics'"
T0="$(lastok openbao)"

echo "=== damage ==="
# restic: a flipped byte in a data pack of the replica openbao repository
pack="$(find "$W/R/bkr/lab/openbao/data" -type f | head -1)"; cp "$pack" "$W/pack.bak"
printf '\xff\xff\xff\xff' | dd of="$pack" bs=1 seek=40 conv=notrunc 2>/dev/null
NOW=$(( $(date +%s) + 86400 )) run_all
chk "a corrupt pack on the replica: openbao fails on replica only" "[ \"\$(okv openbao)\" = 0 ] && [ \"\$(storeok openbao replica)\" = 0 ] && [ \"\$(storeok openbao primary)\" = 1 ]"
chk "the other datasets stay 1" "[ \"\$(okv etcd)\" = 1 ] && [ \"\$(okv cnpg)\" = 1 ]"
chk "the last success of openbao is kept, not moved to the failed run (a day later)" "[ -n \"$T0\" ] && [ \"\$(lastok openbao)\" = \"$T0\" ]"
cp "$W/pack.bak" "$pack"; run_all
chk "repaired: openbao is 1 again" "[ \"\$(okv openbao)\" = 1 ]"
# etcd: a snapshot that restic stores faithfully but is truncated, then one with a flipped meta byte
head -c 14000 "$ROOT/scripts/dev/testdata/etcd-snapshot-v3.6.db" >"$W/seed/etcd/etcd.snap"
seed primary etcd; run_all
chk "a truncated etcd snapshot (restic check passes): etcd fails on primary" "[ \"\$(okv etcd)\" = 0 ] && [ \"\$(storeok etcd primary)\" = 0 ] && [ \"\$(storeok etcd replica)\" = 1 ]"
cp "$ROOT/scripts/dev/testdata/etcd-snapshot-v3.6.db" "$W/seed/etcd/etcd.snap"; printf '\x07' | dd of="$W/seed/etcd/etcd.snap" bs=1 seek=56 conv=notrunc 2>/dev/null
seed primary etcd; run_all
chk "an etcd snapshot with a damaged meta page: etcd fails" "[ \"\$(okv etcd)\" = 0 ]"
cp "$ROOT/scripts/dev/testdata/etcd-snapshot-v3.6.db" "$W/seed/etcd/etcd.snap"; seed primary etcd; run_all
chk "a good etcd snapshot again: etcd is 1" "[ \"\$(okv etcd)\" = 1 ]"
# openbao: a snapshot whose SHA256SUMS does not match
mkdir "$W/x"; (cd "$W/x" && echo '{"index":1}' >meta.json && head -c 4000 /dev/urandom >state.bin && sha256sum meta.json state.bin >SHA256SUMS && echo corrupt >>state.bin && tar -czf "$W/seed/openbao/raft.snap" meta.json state.bin SHA256SUMS)
seed replica openbao; run_all
chk "a raft snapshot whose sums do not match: openbao fails on replica" "[ \"\$(storeok openbao replica)\" = 0 ] && [ \"\$(okv openbao)\" = 0 ]"
(cd "$W/x" && head -c 4000 /dev/urandom >state.bin && sha256sum meta.json state.bin >SHA256SUMS && tar -czf "$W/seed/openbao/raft.snap" meta.json state.bin SHA256SUMS); seed replica openbao; run_all
chk "repaired: openbao is 1" "[ \"\$(okv openbao)\" = 1 ]"
# barman: base backup data, a WAL gap, a missing end WAL, a replica that lags
bk="$(find "$W/P/bkp/cnpg/zitadel-db" -name 'data.tar*' | head -1)"; cp "$bk" "$W/data.bak"
printf '\x00\x01\x02\x03\x04\x05' | dd of="$bk" bs=1 seek=$(( $(stat -c %s "$bk") / 2 )) conv=notrunc 2>/dev/null
run_all
chk "a flipped byte inside the base backup data: cnpg fails on primary" "[ \"\$(okv cnpg)\" = 0 ] && [ \"\$(storeok cnpg primary)\" = 0 ] && grep -q 'corrupt' '$W/res/cnpg.tsv'"
cp "$W/data.bak" "$bk"
wal="$(find "$W/P/bkp/cnpg/zitadel-db" -path '*wals*' -name '*0004.gz' | head -1)"; cp "$wal" "$W/wal.bak"; rm "$wal"
cp -a "$W/P/bkp/cnpg/." "$W/R/bkr/cnpg/" 2>/dev/null; rm -f "$W/R/bkr/${wal#$W/P/bkp/}"
run_all
chk "a WAL segment missing in the middle: cnpg fails with the gap named" "[ \"\$(okv cnpg)\" = 0 ] && grep -q 'WAL gap' '$W/res/cnpg.tsv'"
cp "$W/wal.bak" "$wal"; cp "$W/wal.bak" "$W/R/bkr/${wal#$W/P/bkp/}"
touch -d '5 hours ago' "$W/P/bkp/cnpg/zitadel-db/zitadel-db/base" 2>/dev/null
lbl="$(find "$W/R/bkr/cnpg/zitadel-db" -name '*.backup.gz' | head -1)"; cp "$lbl" "$W/lbl.bak"; rm -f "$lbl"; find "$W/P/bkp/cnpg" -type f -exec touch -d '5 hours ago' {} +
run_all
chk "an object older than the grace period absent from the replica: cnpg fails on replica only" "[ \"\$(storeok cnpg replica)\" = 0 ] && [ \"\$(storeok cnpg primary)\" = 1 ] && grep -q 'missing from the replica' '$W/res/cnpg.tsv'"
cp "$W/lbl.bak" "$lbl"; run_all
chk "replica complete again: cnpg is 1" "[ \"\$(okv cnpg)\" = 1 ]"
# a wiped primary store (apps #151): every repository and the cnpg archive gone from the primary, the replica intact
mv "$W/P/bkp/lab" "$W/P/lab.away"; mv "$W/P/bkp/cnpg" "$W/P/cnpg.away"; mkdir "$W/P/bkp/cnpg"
run_all
for ds in openbao cnpg-dump etcd cnpg; do
  chk "a wiped primary: $ds is 0 (not a skip), store_ok primary 0, replica 1" "[ \"\$(okv $ds)\" = 0 ] && [ \"\$(storeok $ds primary)\" = 0 ] && [ \"\$(storeok $ds replica)\" = 1 ]"
done
chk "the message says that the primary lost its copy" "grep -q 'MISSING' '$W/s3.out' && grep -q 'FAIL etcd/primary' '$W/s3.out'"
chk "the grafana-db archive (empty on both stores) is still a skip, not a failure" "grep -q 'SKIP cnpg/primary: grafana-db' '$W/s3.out' || ! grep -q 'FAIL cnpg/primary: grafana-db' '$W/s3.out'"
rm -rf "$W/P/bkp/cnpg"; mv "$W/P/lab.away" "$W/P/bkp/lab"; mv "$W/P/cnpg.away" "$W/P/bkp/cnpg"; run_all
chk "the primary back: every dataset is 1 again" "[ \"\$(okv etcd)\" = 1 ] && [ \"\$(okv openbao)\" = 1 ] && [ \"\$(okv cnpg)\" = 1 ]"
# longhorn
lh Error; run_all
chk "a Longhorn backup in Error: longhorn fails" "[ \"\$(okv longhorn)\" = 0 ]"
lh Completed; run_all
chk "Completed again: longhorn is 1" "[ \"\$(okv longhorn)\" = 1 ]"
# a store that is down
kill "$(jobs -p | head -1)" 2>/dev/null; sleep 0.3; run_all
chk "a store that is down: the datasets of that store fail, the verifier itself still publishes" "[ \"\$(okv cnpg)\" = 0 ] && grep -q 'published' '$W/s3.out'"
echo "$PASS passed, $FAIL failed"; [ "$FAIL" = 0 ]
