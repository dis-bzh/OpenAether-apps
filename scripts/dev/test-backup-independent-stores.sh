#!/usr/bin/env bash
# The restic-push scripts of the three restic backup Jobs (openbao-snapshot, cnpg-dump, etcd-snapshot), extracted from their
# manifests, against two fake S3 stores: with both up each store gets its snapshot under its OWN key and the Job succeeds; with
# one store refusing (a down provider, a revoked key) the OTHER still gets its snapshot and the Job fails, so that the alert
# fires and the replica does what it exists for. Before this test the first failing destination ended the Job and the second
# never received a copy. Needs docker (restic image); skipped with a notice without it, unless CI=true.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
W="$(mktemp -d)"; trap 'kill $(jobs -p) 2>/dev/null; chmod -R u+rwx "$W" 2>/dev/null; rm -rf "$W"' EXIT
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok   $*"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL $*"; }
chk() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }
extract() { python3 -I - "$ROOT/$1" "$2" <<'PY'
import sys, yaml
d = next(d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d.get("kind") == "CronJob" and d["metadata"]["name"] == sys.argv[2])
c = next(c for c in d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"] if c["name"] == "restic-push")
print(c["image"]); print(c["command"][2])
PY
}
IMG="$(extract apps/base/backup/etcd/cronjob.yaml etcd-snapshot | head -1)"
if ! command -v docker >/dev/null || ! { docker image inspect "$IMG" >/dev/null 2>&1 || docker pull -q "$IMG" >/dev/null 2>&1; }; then
  echo "  SKIPPED: docker or the restic image is not available"; [ "${CI:-}" = true ] && { echo "  FAIL required in CI"; exit 1; }; exit 0
fi
mkdir -p "$W"/{P,R,data,tmp} "$W/P/bkp" "$W/R/bkr"; chmod 777 "$W/tmp"
python3 -I "$ROOT/scripts/dev/testdata/fake_s3_server.py" --root "$W/P" --log "$W/P.log" >"$W/P.url" &
python3 -I "$ROOT/scripts/dev/testdata/fake_s3_server.py" --root "$W/R" --log "$W/R.log" >"$W/R.url" &
for _ in $(seq 50); do [ -s "$W/P.url" ] && [ -s "$W/R.url" ] && break; sleep 0.1; done
PU="$(cat "$W/P.url")"; RU="$(cat "$W/R.url")"
head -c 20000 /dev/urandom >"$W/data/payload"; cp "$ROOT/scripts/dev/testdata/etcd-snapshot-v3.6.db" "$W/data/etcd.snap"
# state: which keys each store admits ("both" = normal)
acl() { echo "{\"bkp\":[\"${1}\"]}" >"$W/P/.acl"; echo "{\"bkr\":[\"${2}\"]}" >"$W/R/.acl"; }
run() { # script-file mountpoint
  docker run --rm --network host --user "$(id -u):$(id -g)" -v "$W/$1:/push.sh:ro" -v "$W/data:$2:ro" -v "$W/tmp:/tmp" --read-only \
    -e PRIMARY_ENDPOINT="$PU" -e PRIMARY_BUCKET=bkp -e PRIMARY_ACCESS_KEY=AKP -e PRIMARY_SECRET_KEY=s \
    -e REPLICA_ENDPOINT="$RU" -e REPLICA_BUCKET=bkr -e REPLICA_ACCESS_KEY=AKR -e REPLICA_SECRET_KEY=s \
    -e CLUSTER_NAME=lab -e RESTIC_PASSWORD=pw -e RESTIC_CAT_TIMEOUT=20 --entrypoint /bin/sh "$IMG" /push.sh >"$W/out" 2>&1
}
count() { # store-dir dataset -> number of snapshot files
  find "$1/lab/$2/snapshots" -type f 2>/dev/null | wc -l; }
for job in "openbao backup/openbao/cronjob.yaml openbao-snapshot /backup payload" "cnpg backup/databases/cronjob.yaml cnpg-dump /backup payload" "etcd backup/etcd/cronjob.yaml etcd-snapshot /snap etcd.snap"; do
  set -- $job; ds=$1; file=apps/base/$2; name=$3; mnt=$4
  extract "$file" "$name" | tail -n +2 >"$W/$ds.sh"
  # the Job's data dir holds one file; the etcd job reads /snap/etcd.snap, the others back up the whole dir
  rm -rf "$W/P/bkp/lab" "$W/R/bkr/lab"
  echo "=== $name ==="
  acl AKP AKR; run "$ds.sh" "$mnt"; rc=$?
  chk "both stores up: exit 0, one snapshot on each ($(tail -1 "$W/out" | cut -c1-70))" "[ $rc = 0 ] && [ \"\$(count '$W/P/bkp' $ds)\" = 1 ] && [ \"\$(count '$W/R/bkr' $ds)\" = 1 ]"
  rm -rf "$W/P/bkp/lab" "$W/R/bkr/lab"
  acl nobody AKR; run "$ds.sh" "$mnt"; rc=$?
  chk "primary refuses: the Job fails, the replica still gets its snapshot" "[ $rc != 0 ] && [ \"\$(count '$W/R/bkr' $ds)\" = 1 ] && grep -q 'ERROR' '$W/out'"
  rm -rf "$W/P/bkp/lab" "$W/R/bkr/lab"
  acl AKP nobody; run "$ds.sh" "$mnt"; rc=$?
  chk "replica refuses: the Job fails, the primary still has its snapshot" "[ $rc != 0 ] && [ \"\$(count '$W/P/bkp' $ds)\" = 1 ]"
  chk "no secret of the pair appears in the Job's output" "! grep -q 'AKP\|AKR\|=pw' '$W/out'"
done
echo "$PASS passed, $FAIL failed"; [ "$FAIL" = 0 ]
