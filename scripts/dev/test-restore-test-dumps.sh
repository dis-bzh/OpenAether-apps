#!/usr/bin/env bash
# Stage 1 of the dump restore test (restore/restic-dump.sh) with a real restic and two fake S3 stores that open only to their own
# key: it restores the latest snapshot of BOTH repositories, makes the files readable for the next stage's user, and when one
# repository cannot be read it says so in an `error` file for that store and still restores the other. Needs docker and the restic
# image; skipped with a notice without them unless CI=true. Rung: mocked stores, real restic.
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
mkdir -p "$W"/{P/bkp,R/bkr,seed,restore,tmp}; chmod 777 "$W/restore" "$W/tmp"
echo '{"bkp":["AKP"]}' >"$W/P/.acl"; echo '{"bkr":["AKR"]}' >"$W/R/.acl"
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
stage() { # replica key
  rm -rf "$W/restore"/*; mkdir -p "$W/restore"
  docker run --rm --network host $U -v "$D:/scripts:ro" -v "$W/restore:/restore" -v "$W/tmp:/tmp" -e CLUSTER_NAME=lab -e RESTIC_PASSWORD=pw \
    -e PRIMARY_ENDPOINT="$PU" -e PRIMARY_BUCKET=bkp -e PRIMARY_ACCESS_KEY=AKP -e PRIMARY_SECRET_KEY=s \
    -e REPLICA_ENDPOINT="$RU" -e REPLICA_BUCKET=bkr -e REPLICA_ACCESS_KEY="$1" -e REPLICA_SECRET_KEY=s \
    --entrypoint sh "$RESTIC" /scripts/restic-dump.sh >"$W/stage.out" 2>&1
}
echo "=== restic-dump.sh ==="
stage AKR; rc=$?
chk "exits 0 and restores both repositories" "[ $rc = 0 ] && [ -f '$W/restore/primary/backup/zitadel-db.dump' ] && [ -f '$W/restore/replica/backup/grafana-db.dump' ]"
chk "the restored dumps are the seeded ones, byte for byte" "cmp -s '$W/seed/zitadel-db.dump' '$W/restore/replica/backup/zitadel-db.dump' && cmp -s '$W/seed/grafana-db.dump' '$W/restore/primary/backup/grafana-db.dump'"
chk "they are readable by another user" "[ \"\$(stat -c %a '$W/restore/primary/backup/zitadel-db.dump')\" -ge 644 ] 2>/dev/null || stat -c %A '$W/restore/primary/backup/zitadel-db.dump' | grep -q 'r--\$'"
chk "no error file after a clean run" "[ ! -e '$W/restore/primary/error' ] && [ ! -e '$W/restore/replica/error' ]"
stage AKWRONG; rc=$?
chk "a replica that refuses the key: exit 0, an error file for the replica, the primary restored" "[ $rc = 0 ] && [ -s '$W/restore/replica/error' ] && [ -f '$W/restore/primary/backup/zitadel-db.dump' ] && [ ! -e '$W/restore/primary/error' ]"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
