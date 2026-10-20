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
if pod.get("serviceAccountName") != "etcd-snapshot":
    errs.append("the Job must run as its own ServiceAccount etcd-snapshot (not a shared one)")
disc = pod["initContainers"][0]
if disc["name"] != "discover-controlplane" or pod["initContainers"][1]["name"] != "talos-snapshot":
    errs.append("init order must be discover-controlplane then talos-snapshot")
tokmounts = [c["name"] for c in pod["initContainers"] + pod["containers"] for m in c.get("volumeMounts", []) if m["name"] == "token"]
if tokmounts != ["discover-controlplane"]:
    errs.append(f"only the discovery container may mount the Kubernetes token, got {tokmounts}")
cr = by["ClusterRole"]
if cr["rules"] != [{"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "list"]}]:
    errs.append(f"the ClusterRole must be get/list on nodes only, got {cr['rules']}")
crb = by["ClusterRoleBinding"]
if crb["roleRef"]["name"] != cr["metadata"]["name"] or [s["name"] for s in crb["subjects"]] != ["etcd-snapshot"]:
    errs.append("the ClusterRoleBinding must bind etcd-snapshot to that ClusterRole only")
if {"TALOSCONFIG": "/work/config"} != {e["name"]: e["value"] for e in pod["initContainers"][1]["env"] if e["name"] == "TALOSCONFIG"}:
    errs.append("talosctl must read the per-run config written by the discovery container (/work/config)")
if any(m["name"] == "talos" for m in pod["initContainers"][1]["volumeMounts"]):
    errs.append("talosctl must not read the raw grant (its endpoint talos.default resolves to nothing)")
vols = {v["name"]: v for v in pod["volumes"]}
if vols["snap"]["emptyDir"].get("medium") != "Memory":
    errs.append("the snapshot volume must be memory-backed (the snapshot is every Secret in clear)")
if vols["talos"]["secret"]["secretName"] != sa["metadata"]["name"]:
    errs.append("the mounted Secret must be the one Talos names after the grant")
init = pod["initContainers"][1]
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
if not any("kube-apiserver" in e.get("toEntities", []) for e in np["spec"]["egress"]):
    errs.append("network policy must open the Kubernetes API for the node discovery")
for e in errs: print("FAIL", e)
sys.exit(1 if errs else 0)
PY

echo "=== the control-plane discovery (real alpine/k8s image, fake kubectl, nc and talosctl) ==="
K8S="$(python3 -I - "$ROOT/apps/base/backup/etcd/cronjob.yaml" <<'PY'
import sys, yaml
d = next(d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d.get("kind") == "CronJob")
print(d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["initContainers"][0]["image"])
PY
)"
if command -v docker >/dev/null && { docker image inspect "$K8S" >/dev/null 2>&1 || docker pull -q "$K8S" >/dev/null 2>&1; }; then
  python3 -I - "$ROOT/apps/base/backup/etcd/cronjob.yaml" >"$W/discover.sh" <<'PY'
import sys, yaml
d = next(d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d.get("kind") == "CronJob")
print(d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["initContainers"][0]["command"][2])
PY
  mkdir -p "$W/d/bin" "$W/d/work" "$W/d/grant"; chmod 777 "$W/d/work"
  cat >"$W/d/grant/config" <<'CFG'
context: oa
contexts:
    oa:
        endpoints:
            - talos.default
        ca: Q0E=
        crt: Q1JU
        key: S0VZ
CFG
  # fake kubectl: prints the InternalIPs in $FAKE_IPS (one per line), or fails when FAKE_KUBECTL_FAIL=1; records its args
  cat >"$W/d/bin/kubectl" <<'SH'
#!/bin/sh
echo "$*" >>/d/kubectl.args
[ "${FAKE_KUBECTL_FAIL:-0}" = 1 ] && { echo "forbidden" >&2; exit 1; }
printf '%s' "$FAKE_IPS"
SH
  # fake nc: only the addresses in $FAKE_UP answer (port 50000 only), and every probe is logged
  cat >"$W/d/bin/nc" <<'SH'
#!/bin/sh
ip="$3"; port="$4"
echo "$ip:$port" >>/d/nc.log
[ "$port" = 50000 ] || exit 1
case " $FAKE_UP " in *" $ip "*) exit 0;; esac
exit 1
SH
  # fake talosctl: refuses without a node, like the real one; prints the endpoint and node it would use
  cat >"$W/d/bin/talosctl" <<'SH'
#!/bin/sh
awk '/^ *endpoints:/{e=1;next} /^ *nodes:/{n=1;next} /^ *- /{if(e==1){ep=$2;e=0} else if(n==1){nd=$2;n=0}} END{if(nd==""){print "nodes are not set for the command"; exit 1} print ep, nd}' "$TALOSCONFIG"
SH
  chmod +x "$W/d/bin/"*
  disc() {  # $1 = FAKE_IPS, $2 = FAKE_UP, $3 = FAKE_KUBECTL_FAIL
    rm -f "$W/d/work/config" "$W/d/nc.log" "$W/d/kubectl.args"
    docker run --rm --user "$(id -u):$(id -g)" -v "$W/d:/d" -v "$W/discover.sh:/discover.sh:ro" -e PATH="/d/bin:/usr/local/bin:/usr/bin:/bin" -e HOME=/tmp \
      -e TALOS_GRANT_CONFIG=/d/grant/config -e TALOS_WORK_CONFIG=/d/work/config -e FAKE_IPS="$1" -e FAKE_UP="$2" -e FAKE_KUBECTL_FAIL="${3:-0}" \
      --entrypoint /bin/sh "$K8S" /discover.sh >"$W/dout" 2>&1
  }
  disc "10.0.0.1
10.0.0.2
10.0.0.3
" "10.0.0.2"; rc=$?
chk "discovery: the node that answers is used (10.0.0.2)" "[ $rc = 0 ] && grep -q 'snapshot from this node' '$W/dout' && grep -q -- '- 10.0.0.2' '$W/d/work/config'"
chk "discovery: the written config has endpoint AND node set to that address, the credentials untouched" "[ \"\$(grep -c '10.0.0.2' '$W/d/work/config')\" = 2 ] && ! grep -q 'talos.default' '$W/d/work/config' && grep -q 'crt: Q1JU' '$W/d/work/config' && grep -q 'key: S0VZ' '$W/d/work/config'"
chk "discovery: the control-plane label selector is used" "grep -q 'node-role.kubernetes.io/control-plane' '$W/d/kubectl.args'"
chk "discovery: the raw grant stays untouched" "grep -q 'talos.default' '$W/d/grant/config'"
chk "discovery: talosctl accepts the written config (it refuses the raw grant)" "TALOSCONFIG='$W/d/work/config' sh '$W/d/bin/talosctl' | grep -q '10.0.0.2 10.0.0.2' && ! TALOSCONFIG='$W/d/grant/config' sh '$W/d/bin/talosctl' >/dev/null"
disc "10.0.0.1
10.0.0.2
" "10.0.0.1 10.0.0.2"; rc=$?
chk "discovery: first answering node wins, an unreachable one is skipped, a single answer is enough" "[ $rc = 0 ] && [ -s '$W/d/work/config' ]"
disc "10.0.0.1
10.0.0.2
" ""; rc=$?
chk "discovery: no control plane answering fails loudly, every candidate tried, no config written" "[ $rc != 0 ] && grep -q 'no control plane answers' '$W/dout' && [ \"\$(grep -c ':50000' '$W/d/nc.log')\" = 2 ] && [ ! -e '$W/d/work/config' ]"
disc "" "10.0.0.1"; rc=$?
chk "discovery: no control-plane node found fails loudly" "[ $rc != 0 ] && grep -q 'no control-plane node' '$W/dout'"
disc "10.0.0.1
" "10.0.0.1" 1; rc=$?
chk "discovery: a refused Kubernetes API call fails loudly" "[ $rc != 0 ] && grep -q 'cannot list the control-plane nodes' '$W/dout'"
else
  echo "  SKIPPED the discovery test: docker or the alpine/k8s image is not available"
  [ "${CI:-}" = true ] && { echo "  FAIL required in CI"; exit 1; }
fi

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
