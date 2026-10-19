#!/usr/bin/env bash
# The scheduled etcd snapshot (apps/base/backup/etcd): the manifest keeps its promises, and the restic-push script of the Job,
# extracted from it, refuses what is not an etcd snapshot, and pushes a real one (a snapshot taken by etcd 3.6) to BOTH fake
# stores with the right key each, into repositories that restic reads back. The Talos side (the grant, the talosctl call) is
# the lab's. Needs docker (restic image) for the push part; skipped with a notice without it, unless CI=true.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
W="$(mktemp -d)"; trap 'kill $(jobs -p) 2>/dev/null; chmod -R u+rwx "$W" 2>/dev/null; rm -rf "$W"' EXIT
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  ok   $*"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL $*"; }
chk() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }

echo "=== manifest ==="
python3 -I - "$ROOT" <<'PY' && ok "manifest promises hold" || bad "manifest promises (see above)"
import subprocess, sys, yaml
root = sys.argv[1]
docs = [d for d in yaml.safe_load_all(subprocess.run(["kubectl", "kustomize", f"{root}/apps/base/backup/etcd"], capture_output=True, text=True, check=True).stdout) if d]
by = {d["kind"]: d for d in docs}
errs = []
sa = by["ServiceAccount"] if by.get("ServiceAccount", {}).get("apiVersion") == "talos.dev/v1alpha1" else next(d for d in docs if d.get("apiVersion") == "talos.dev/v1alpha1")
if sa["spec"]["roles"] != ["os:etcd:backup"]:
    errs.append(f"the Talos grant must be exactly os:etcd:backup, got {sa['spec']['roles']}")
cj = by["CronJob"]; pod = cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]
if pod.get("automountServiceAccountToken") is not False:
    errs.append("the pod must not mount a ServiceAccount token")
vols = {v["name"]: v for v in pod["volumes"]}
if vols["snap"]["emptyDir"].get("medium") != "Memory":
    errs.append("the snapshot volume must be memory-backed (the snapshot is every Secret in clear)")
if vols["talos"]["secret"]["secretName"] != sa["metadata"]["name"]:
    errs.append("the mounted Secret must be the one Talos names after the grant")
init = pod["initContainers"][0]
if init["command"][:3] != ["/talosctl", "etcd", "snapshot"]:
    errs.append("the init container must run talosctl etcd snapshot (nothing broader)")
if not init["image"].startswith("ghcr.io/siderolabs/talosctl:v"):
    errs.append("talosctl image must be pinned to a version")
script = pod["containers"][0]["command"][2]
for need in ("PRIMARY_ENDPOINT", "REPLICA_ENDPOINT", "restic forget", "--keep-daily", "restic check", "--host etcd"):
    if need not in script:
        errs.append(f"push script lacks {need!r}")
if "aws s3 rm" in script or "--force" in script:
    errs.append("push script must not delete outside restic's own retention")
np = by["CiliumNetworkPolicy"]
ports = [p["port"] for e in np["spec"]["egress"] for tp in e.get("toPorts", []) for p in tp["ports"]]
if "50000" not in ports or "443" not in ports:
    errs.append("network policy must open the Talos API (50000) and S3 (443)")
for e in errs: print("FAIL", e)
sys.exit(1 if errs else 0)
PY

echo "=== the push script ==="
RESTIC="$(python3 -I - "$ROOT/apps/base/backup/etcd/cronjob.yaml" <<'PY'
import sys, yaml
d = next(d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d.get("kind") == "CronJob")
print(d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"])
PY
)"
if ! command -v docker >/dev/null || ! { docker image inspect "$RESTIC" >/dev/null 2>&1 || docker pull -q "$RESTIC" >/dev/null 2>&1; }; then
  echo "  SKIPPED the push test: docker or the restic image is not available"
  [ "${CI:-}" = true ] && { echo "  FAIL required in CI"; exit 1; }
  echo "$PASS passed, $FAIL failed"; [ "$FAIL" = 0 ]; exit
fi
python3 -I - "$ROOT/apps/base/backup/etcd/cronjob.yaml" >"$W/push.sh" <<'PY'
import sys, yaml
d = next(d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d.get("kind") == "CronJob")
print(d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"][2])
PY
mkdir -p "$W"/{P,R,snap,tmp} "$W/P/bkp" "$W/R/bkr"; chmod 777 "$W/snap" "$W/tmp"
echo '{"bkp":["AKP"]}' >"$W/P/.acl"; echo '{"bkr":["AKR"]}' >"$W/R/.acl"
python3 -I "$ROOT/scripts/dev/testdata/fake_s3_server.py" --root "$W/P" --log "$W/P.log" >"$W/P.url" &
python3 -I "$ROOT/scripts/dev/testdata/fake_s3_server.py" --root "$W/R" --log "$W/R.log" >"$W/R.url" &
for _ in $(seq 50); do [ -s "$W/P.url" ] && [ -s "$W/R.url" ] && break; sleep 0.1; done
PU="$(cat "$W/P.url")"; RU="$(cat "$W/R.url")"
push() { docker run --rm --network host --user "$(id -u):$(id -g)" -v "$W/push.sh:/push.sh:ro" -v "$W/snap:/snap:ro" -v "$W/tmp:/tmp" --read-only \
  -e PRIMARY_ENDPOINT="$PU" -e PRIMARY_BUCKET=bkp -e PRIMARY_ACCESS_KEY=AKP -e PRIMARY_SECRET_KEY=s \
  -e REPLICA_ENDPOINT="$RU" -e REPLICA_BUCKET=bkr -e REPLICA_ACCESS_KEY=AKR -e REPLICA_SECRET_KEY=s \
  -e CLUSTER_NAME=lab -e RESTIC_PASSWORD=pw --entrypoint /bin/sh "$RESTIC" /push.sh >"$W/out" 2>&1; }
repo() { docker run --rm --network host --user "$(id -u):$(id -g)" -v "$W/tmp:/tmp" -e RESTIC_PASSWORD=pw -e AWS_ACCESS_KEY_ID="$2" -e AWS_SECRET_ACCESS_KEY=s \
  -e RESTIC_REPOSITORY="s3:$1/$3/lab/etcd" -e RESTIC_CACHE_DIR=/tmp/c --entrypoint restic "$RESTIC" "${@:4}" 2>&1; }

: >"$W/snap/etcd.snap"
push; rc=$?
chk "an empty file is not uploaded" "[ $rc != 0 ] && grep -q 'not an etcd snapshot' '$W/out' && [ ! -d '$W/P/bkp/lab' ]"
head -c 30000 /dev/urandom >"$W/snap/etcd.snap"
push; rc=$?
chk "a file that is not a bolt database is not uploaded" "[ $rc != 0 ] && grep -q 'not an etcd snapshot' '$W/out' && [ ! -d '$W/P/bkp/lab' ]"
head -c 12000 "$ROOT/scripts/dev/testdata/etcd-snapshot-v3.6.db" >"$W/snap/etcd.snap"
push; rc=$?
chk "a truncated snapshot (right magic, too short) is not uploaded" "[ $rc != 0 ] && [ ! -d '$W/P/bkp/lab' ]"
cp "$ROOT/scripts/dev/testdata/etcd-snapshot-v3.6.db" "$W/snap/etcd.snap"
push; rc=$?
chk "a real etcd snapshot is pushed to both stores ($(tail -1 "$W/out"))" "[ $rc = 0 ] && grep -q 'both destinations' '$W/out'"
chk "primary repository: one snapshot, host etcd, readable with the password" "repo '$PU' AKP bkp snapshots --host etcd | grep -q '1 snapshots'"
chk "replica repository: one snapshot, host etcd, readable with the password" "repo '$RU' AKR bkr snapshots --host etcd | grep -q '1 snapshots'"
chk "the stored data is not the plain snapshot (client-side encryption)" "grep -qa k1 '$ROOT/scripts/dev/testdata/etcd-snapshot-v3.6.db' && ! grep -rl -a 'k1' '$W/P/bkp/lab/etcd/data' '$W/R/bkr/lab/etcd/data' 2>/dev/null | grep -q ."
chk "each store only saw its own key" "! grep -v 'ak=AKP' '$W/P.log' | grep -q . && ! grep -v 'ak=AKR' '$W/R.log' | grep -q ."
echo "restored" >/dev/null
mkdir -p "$W/tmp/out"; repo "$PU" AKP bkp restore latest --target /tmp/out >/dev/null
chk "the restored file equals the snapshot" "cmp -s '$W/tmp/out/snap/etcd.snap' '$ROOT/scripts/dev/testdata/etcd-snapshot-v3.6.db'"
echo "$PASS passed, $FAIL failed"; [ "$FAIL" = 0 ]
