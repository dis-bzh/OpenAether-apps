#!/usr/bin/env bash
# The script of the cnpg-store-replicate CronJob (extracted from the manifest, so what ships is what runs) against two fake
# S3 stores that only open to their own key pair: it copies the CNPG archive to the replica store (data before backup.info,
# server-side encryption requested), copies nothing twice, repairs a short object, prunes only what left the primary AND
# is older than the grace period, and refuses to prune against an empty primary or more than half of the replica.
# Needs aws (v1 or v2) and python3 with PyYAML. Rung: mocked (no provider).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
W="$(mktemp -d)"; trap 'kill $(jobs -p) 2>/dev/null; rm -rf "$W"' EXIT
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok   $*"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL $*"; }
chk() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }
command -v aws >/dev/null || { echo "aws CLI not found"; exit 2; }

python3 -I - "$ROOT/apps/base/backup/databases/cnpg-store-replicate-cronjob.yaml" >"$W/script.sh" <<'PY'
import sys, yaml
doc = next(d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d.get("kind") == "CronJob")
print(doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"][2])
PY
for d in P R; do mkdir -p "$W/$d"; done
mkdir "$W/P/bp" "$W/R/br"
echo '{"bp":["AKP"]}' >"$W/P/.acl"; echo '{"br":["AKR"]}' >"$W/R/.acl"
python3 -I "$ROOT/scripts/dev/testdata/fake_s3_server.py" --root "$W/P" --log "$W/P.log" >"$W/P.url" & 
python3 -I "$ROOT/scripts/dev/testdata/fake_s3_server.py" --root "$W/R" --log "$W/R.log" >"$W/R.url" &
for _ in $(seq 50); do [ -s "$W/P.url" ] && [ -s "$W/R.url" ] && break; sleep 0.1; done
PU="$(cat "$W/P.url")"; RU="$(cat "$W/R.url")"

put() { mkdir -p "$(dirname "$W/P/bp/$1")"; head -c "$2" /dev/urandom >"$W/P/bp/$1"; }
for c in zitadel-db grafana-db; do
  put cnpg/$c/$c/base/20261001T031000/backup.info 300
  put cnpg/$c/$c/base/20261001T031000/data.tar.gz 5000
  for i in 1 2 3; do put cnpg/$c/$c/wals/0000000100000000/00000001000000000000000$i.gz 800; done
done
run() { env PRIMARY_ENDPOINT="$PU" PRIMARY_BUCKET=bp PRIMARY_ACCESS_KEY=AKP PRIMARY_SECRET_KEY=s \
  REPLICA_ENDPOINT="$RU" REPLICA_BUCKET=br REPLICA_ACCESS_KEY="${RK:-AKR}" REPLICA_SECRET_KEY=s \
  AWS_EC2_METADATA_DISABLED=true AWS_PAGER= "$@" bash "$W/script.sh" >"$W/out" 2>&1; }
same() { diff -rq "$W/P/bp/cnpg" "$W/R/br/cnpg" >/dev/null 2>&1; }

echo "=== cnpg-store-replicate ==="
run; rc=$?
chk "first run exits 0 ($(tail -2 "$W/out" | tr '\n' ' '))" "[ $rc = 0 ]"
chk "the replica holds every object of both clusters, byte for byte" same
chk "the primary store only ever saw the primary key" "! grep -v 'ak=AKP' '$W/P.log' | grep -q ."
chk "the replica store only ever saw the replica key" "! grep -v 'ak=AKR' '$W/R.log' | grep -q ."
chk "every upload to the replica asks for server-side encryption" "! grep -E '^PUT .*ak=AKR' '$W/R.log' | grep -v 'sse=AES256' | grep -q ."
d="$(grep -n 'PUT /br/cnpg/zitadel-db/zitadel-db/base/.*/data.tar.gz' "$W/R.log" | head -1 | cut -d: -f1)"
i="$(grep -n 'PUT /br/cnpg/zitadel-db/zitadel-db/base/.*/backup.info' "$W/R.log" | head -1 | cut -d: -f1)"
chk "backup.info is written after the data of its base backup" "[ -n '$d' ] && [ -n '$i' ] && [ '$d' -lt '$i' ]"
: >"$W/R.log"
run; rc=$?
chk "a second run copies nothing" "[ $rc = 0 ] && ! grep -q '^PUT' '$W/R.log' && grep -q 'copied 0' '$W/out'"
head -c 100 /dev/urandom >"$W/R/br/cnpg/grafana-db/grafana-db/base/20261001T031000/data.tar.gz"
run; rc=$?
chk "an object of another size is copied again" "[ $rc = 0 ] && same"
# prune
rm "$W/P/bp/cnpg/zitadel-db/zitadel-db/wals/0000000100000000/000000010000000000000001.gz"
run; rc=$?
chk "an object gone from the primary is kept inside the grace period" "[ $rc = 0 ] && [ -f '$W/R/br/cnpg/zitadel-db/zitadel-db/wals/0000000100000000/000000010000000000000001.gz' ]"
touch -d '60 days ago' "$W/R/br/cnpg/zitadel-db/zitadel-db/wals/0000000100000000/000000010000000000000001.gz"
run; rc=$?
chk "and removed once older than the grace period" "[ $rc = 0 ] && [ ! -f '$W/R/br/cnpg/zitadel-db/zitadel-db/wals/0000000100000000/000000010000000000000001.gz' ] && grep -q 'pruned 1' '$W/out'"
# empty primary
mv "$W/P/bp/cnpg" "$W/P/cnpg.away"; mkdir "$W/P/bp/cnpg"
find "$W/R/br/cnpg" -type f -exec touch -d '90 days ago' {} +
run; rc=$?
chk "an empty primary prunes nothing" "[ $rc = 0 ] && [ \"\$(find '$W/R/br/cnpg' -type f | wc -l)\" -ge 9 ] && grep -q 'WARN' '$W/out'"
# more than half: the primary keeps one cluster only, the replica has >20 objects
rm -rf "$W/P/bp/cnpg"; mkdir -p "$W/P/bp/cnpg/zitadel-db"; cp -r "$W/P/cnpg.away/zitadel-db/." "$W/P/bp/cnpg/zitadel-db/"
for n in $(seq 25); do head -c 50 /dev/urandom >"$W/R/br/cnpg/grafana-db/extra$n"; touch -d '90 days ago' "$W/R/br/cnpg/grafana-db/extra$n"; done
mkdir -p "$W/P/bp/cnpg/grafana-db/grafana-db/wals"; put cnpg/grafana-db/grafana-db/wals/keep.gz 10
before="$(find "$W/R/br/cnpg" -type f | wc -l)"
run; rc=$?
chk "a prune of more than half of a cluster's replica is refused and fails the job" "[ $rc != 0 ] && grep -q 'refused' '$W/out' && [ \"\$(find '$W/R/br/cnpg' -type f | wc -l)\" -ge '$before' ]"
# wrong key
RK=AKP run; rc=$?
chk "the primary key does not open the replica store: the job fails" "[ $rc != 0 ]"
# The same script in the image the Job runs (busybox awk/date/sort, the image's aws), against the same fake stores.
IMG="$(python3 -I - "$ROOT/apps/base/backup/databases/cnpg-store-replicate-cronjob.yaml" <<'PY'
import sys, yaml
d = next(d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d.get("kind") == "CronJob")
print(d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"])
PY
)"
if command -v docker >/dev/null && { docker image inspect "$IMG" >/dev/null 2>&1 || docker pull -q "$IMG" >/dev/null 2>&1; }; then
  rm -rf "$W/R/br" "$W/P/bp"; mkdir "$W/R/br" "$W/P/bp"; echo '{"br":["AKR"]}' >"$W/R/.acl"
  for c in zitadel-db grafana-db; do put cnpg/$c/$c/base/20261001T031000/backup.info 300; put cnpg/$c/$c/wals/0000000100000000/000000010000000000000001.gz 800; done
  touch -d '90 days ago' "$W/P/bp/cnpg/zitadel-db/zitadel-db/wals/0000000100000000/000000010000000000000001.gz"
  docker run --rm --network host --user 1000:1000 -v "$W/script.sh:/s.sh:ro" -e PRIMARY_ENDPOINT="$PU" -e PRIMARY_BUCKET=bp \
    -e PRIMARY_ACCESS_KEY=AKP -e PRIMARY_SECRET_KEY=s -e REPLICA_ENDPOINT="$RU" -e REPLICA_BUCKET=br -e REPLICA_ACCESS_KEY=AKR \
    -e REPLICA_SECRET_KEY=s --tmpfs /tmp:rw,mode=1777 --read-only --entrypoint bash "$IMG" /s.sh >"$W/out" 2>&1; rc=$?
  chk "in the Job's image (read-only root, uid 1000): exit 0 and the replica equals the primary ($(tail -1 "$W/out"))" "[ $rc = 0 ] && same"
else
  echo "  SKIPPED the run in the Job's image: docker or the image is not available"
  if [ "${CI:-}" = true ]; then bad "image run required in CI"; fi
fi
echo "$PASS passed, $FAIL failed"; [ "$FAIL" = 0 ]
