#!/usr/bin/env bash
# Stage 1 of the dump restore test (restore/restic-dump.sh) with a real restic and two fake S3 stores that open only to their own
# key: it restores the latest snapshot of BOTH repositories, makes the files readable for the next stage's user, and when one
# repository cannot be read it says so in an `error` file for that store and still restores the other. A repository that DOES NOT EXIST
# (a fresh install before the first cnpg-dump, restic exit code 10) leaves an `absent` marker instead and nothing else; every other way a
# repository can fail (refused key, wrong password, no snapshot in it) stays an `error`. Needs docker and the restic image; skipped
# with a notice without them unless CI=true. Rung: mocked stores, real restic.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
D="$ROOT/apps/base/backup/databases/restore"
W="$(mktemp -d)"; trap 'kill $(jobs -p) 2>/dev/null; docker run --rm -v "$W:/w" --entrypoint sh "$RESTIC" -c "rm -rf /w/*" >/dev/null 2>&1; rm -rf "$W"' EXIT
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok   $*"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL $*"; }
chk() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }
RESTIC="$(python3 -I - "$ROOT/apps/base/backup/databases/dump-load-test-cronjob.yaml" <<'PY'
import sys, yaml
for d in yaml.safe_load_all(open(sys.argv[1])):
    if d and d.get("kind") == "CronJob":
        print(next(c["image"] for c in d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["initContainers"] if c["name"] == "restic"))
PY
)"
if ! command -v docker >/dev/null || ! { docker image inspect "$RESTIC" >/dev/null 2>&1 || docker pull -q "$RESTIC" >/dev/null 2>&1; }; then
  echo "  SKIPPED the dump restore stage test: docker or $RESTIC not available"
  [ "${CI:-}" = true ] && { echo "  FAIL required in CI"; exit 1; }
  exit 0
fi
U="--user $(id -u):$(id -g)"
mkdir -p "$W"/{P/bkp,P/bke,R/bkr,R/bke,R/bkn,seed,restore,tmp}; chmod 777 "$W/restore" "$W/tmp"
echo '{"bkp":["AKP"],"bke":["AKP"]}' >"$W/P/.acl"; echo '{"bkr":["AKR"],"bke":["AKR"],"bkn":["AKR"]}' >"$W/R/.acl"
python3 -I "$ROOT/scripts/dev/testdata/fake_s3_server.py" --root "$W/P" --log "$W/P.log" >"$W/P.url" &
python3 -I "$ROOT/scripts/dev/testdata/fake_s3_server.py" --root "$W/R" --log "$W/R.log" >"$W/R.url" &
for _ in $(seq 50); do [ -s "$W/P.url" ] && [ -s "$W/R.url" ] && break; sleep 0.1; done
PU="$(cat "$W/P.url")"; RU="$(cat "$W/R.url")"
head -c 5000 /dev/urandom >"$W/seed/zitadel-db.dump"; head -c 3000 /dev/urandom >"$W/seed/grafana-db.dump"
seed() { # endpoint bucket key
  docker run --rm --network host $U -v "$W/seed:/backup:ro" -v "$W/tmp:/tmp" -e RESTIC_PASSWORD=pw -e AWS_ACCESS_KEY_ID="$3" -e AWS_SECRET_ACCESS_KEY=s \
    -e RESTIC_REPOSITORY="s3:$1/$2/lab/cnpg" -e RESTIC_CACHE_DIR=/tmp/c --entrypoint sh "$RESTIC" -c "restic init -q && restic backup -q /backup --host cnpg" >"$W/seed.out" 2>&1 || { cat "$W/seed.out"; return 1; }
}
seed "$PU" bkp AKP || bad "seed primary"; seed "$RU" bkr AKR || bad "seed replica"
# bke: a bucket that exists with no repository in it (nothing was ever pushed); bkn: a repository that exists with NO snapshot (init ran, backup did not)
docker run --rm --network host $U -v "$W/tmp:/tmp" -e RESTIC_PASSWORD=pw -e AWS_ACCESS_KEY_ID=AKR -e AWS_SECRET_ACCESS_KEY=s \
  -e RESTIC_REPOSITORY="s3:$RU/bkn/lab/cnpg" -e RESTIC_CACHE_DIR=/tmp/c --entrypoint restic "$RESTIC" init -q >"$W/seed.out" 2>&1 || { cat "$W/seed.out"; bad "seed empty repository"; }
stage() { # replica-key [replica-bucket] [primary-bucket] [password]
  rm -rf "$W/restore"/*; mkdir -p "$W/restore"
  docker run --rm --network host $U -v "$D:/scripts:ro" -v "$W/restore:/restore" -v "$W/tmp:/tmp" -e CLUSTER_NAME=lab -e RESTIC_PASSWORD="${4:-pw}" \
    -e PRIMARY_ENDPOINT="$PU" -e PRIMARY_BUCKET="${3:-bkp}" -e PRIMARY_ACCESS_KEY=AKP -e PRIMARY_SECRET_KEY=s \
    -e REPLICA_ENDPOINT="$RU" -e REPLICA_BUCKET="${2:-bkr}" -e REPLICA_ACCESS_KEY="$1" -e REPLICA_SECRET_KEY=s -e PROBE_TIMEOUT="${PT:-}" \
    --entrypoint sh "$RESTIC" /scripts/restic-dump.sh >"$W/stage.out" 2>&1
}
echo "=== restic-dump.sh ==="
stage AKR; rc=$?
chk "exits 0 and restores both repositories" "[ $rc = 0 ] && [ -f '$W/restore/primary/backup/zitadel-db.dump' ] && [ -f '$W/restore/replica/backup/grafana-db.dump' ]"
chk "the restored dumps are the seeded ones, byte for byte" "cmp -s '$W/seed/zitadel-db.dump' '$W/restore/replica/backup/zitadel-db.dump' && cmp -s '$W/seed/grafana-db.dump' '$W/restore/primary/backup/grafana-db.dump'"
chk "they are readable by another user" "[ \"\$(stat -c %a '$W/restore/primary/backup/zitadel-db.dump')\" -ge 644 ] 2>/dev/null || stat -c %A '$W/restore/primary/backup/zitadel-db.dump' | grep -q 'r--\$'"
chk "no error file and no absent marker after a clean run" "[ ! -e '$W/restore/primary/error' ] && [ ! -e '$W/restore/replica/error' ] && [ ! -e '$W/restore/primary/absent' ] && [ ! -e '$W/restore/replica/absent' ]"
stage AKWRONG; rc=$?
chk "a replica that refuses the key: exit 0, an error file for the replica, the primary restored" "[ $rc = 0 ] && [ -s '$W/restore/replica/error' ] && [ -f '$W/restore/primary/backup/zitadel-db.dump' ] && [ ! -e '$W/restore/primary/error' ]"
chk "a refused key is an error, NOT an absent repository" "[ ! -e '$W/restore/replica/absent' ]"

echo "=== no dump pushed yet (a fresh install before the first cnpg-dump) ==="
stage AKR bke bke; rc=$?
chk "both repositories absent: exit 0, an absent marker for each, no error file, nothing restored" "[ $rc = 0 ] && [ -s '$W/restore/primary/absent' ] && [ -s '$W/restore/replica/absent' ] && [ ! -e '$W/restore/primary/error' ] && [ ! -e '$W/restore/replica/error' ] && ! find '$W/restore' -name '*.dump' | grep -q ."
stage AKR bke; rc=$?
chk "one repository absent, the other restored: the primary's dumps are there, the replica has the marker only" "[ $rc = 0 ] && [ -s '$W/restore/replica/absent' ] && [ ! -e '$W/restore/replica/error' ] && [ -f '$W/restore/primary/backup/zitadel-db.dump' ] && [ ! -e '$W/restore/primary/absent' ]"
stage AKR bkr bke; rc=$?
chk "the primary absent, the replica restored: the marker is the primary's" "[ $rc = 0 ] && [ -s '$W/restore/primary/absent' ] && [ ! -e '$W/restore/primary/error' ] && [ -f '$W/restore/replica/backup/grafana-db.dump' ] && [ ! -e '$W/restore/replica/absent' ]"
echo "=== every other way to fail stays an error ==="
stage AKR bkn; rc=$?
chk "a repository that exists with no snapshot is an error (the dump job's push failed after init), not an absent one" "[ $rc = 0 ] && [ -s '$W/restore/replica/error' ] && [ ! -e '$W/restore/replica/absent' ] && [ -f '$W/restore/primary/backup/zitadel-db.dump' ]"
PT=4 stage AKR nobucket; rc=$?  # restic retries a missing bucket until the timeout stops it (120 s in the Job, 4 s here; exit 130 in this image): not rc 10
chk "a missing bucket (restic is stopped by the timeout) is an error, NOT an absent repository" "[ $rc = 0 ] && [ -s '$W/restore/replica/error' ] && [ ! -e '$W/restore/replica/absent' ] && [ -f '$W/restore/primary/backup/zitadel-db.dump' ]"
stage AKR bkr bkp wrong; rc=$?
chk "a repository that exists but whose password is wrong is an error on both stores, never an absent one" "[ $rc = 0 ] && [ -s '$W/restore/primary/error' ] && [ -s '$W/restore/replica/error' ] && [ ! -e '$W/restore/primary/absent' ] && [ ! -e '$W/restore/replica/absent' ]"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
