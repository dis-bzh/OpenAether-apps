#!/usr/bin/env bash
# Render the Loki chart with the values the repository ships (base and local overlay) and assert
# what the manifests only claim: the compactor enforces retention, the S3 link is not forced to
# http, a PDB exists, no canary DaemonSet. Needs kubectl, helm and python3 with PyYAML (network
# for the chart). Rung: mocked/static, nothing runs on a cluster.
#   Usage: scripts/dev/test-loki-render.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
for bin in kubectl helm python3; do command -v "$bin" >/dev/null || { echo "$bin is required" >&2; exit 2; }; done
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

kubectl kustomize "$ROOT/apps/base/observability" > "$WORK/base.yaml"
kubectl kustomize "$ROOT/apps/base/observability-local" > "$WORK/local.yaml"

# The HelmRelease's own spec.values, plus stand-ins for what valuesFrom injects at run time.
extract() { python3 -I - "$1" "$2" "$3" <<'PY'
import sys, yaml
src, values_out, version_out = sys.argv[1:4]
for d in yaml.safe_load_all(open(src)):
    if d and d.get("kind") == "HelmRelease" and d["metadata"]["name"] == "loki":
        v = d["spec"]["values"]
        s3 = v.setdefault("loki", {}).setdefault("storage", {}).setdefault("s3", {})
        s3.update(endpoint="https://s3.example.invalid", accessKeyId="AK", secretAccessKey="SK")
        v["loki"]["storage"]["bucketNames"] = {"chunks": "b", "ruler": "b", "admin": "b"}
        yaml.safe_dump(v, open(values_out, "w"))
        open(version_out, "w").write(d["spec"]["chart"]["spec"]["version"])
        break
else:
    sys.exit("no HelmRelease loki in " + src)
PY
}
extract "$WORK/base.yaml"  "$WORK/base-values.yaml"  "$WORK/version"
extract "$WORK/local.yaml" "$WORK/local-values.yaml" "$WORK/version"
VERSION="$(cat "$WORK/version")"

helm pull loki --repo https://grafana.github.io/helm-charts --version "$VERSION" --untar --untardir "$WORK/chart" >/dev/null
for env in base local; do
  helm template loki "$WORK/chart/loki" --namespace services-observability -f "$WORK/$env-values.yaml" > "$WORK/$env-rendered.yaml"
done

python3 -I - "$WORK" <<'PY'
import sys, yaml
work = sys.argv[1]
fails = 0
def check(label, ok):
    global fails
    print(("ok   " if ok else "FAIL ") + label)
    fails += 0 if ok else 1

def load(env):
    docs = [d for d in yaml.safe_load_all(open(f"{work}/{env}-rendered.yaml")) if d]
    kinds = {}
    for d in docs:
        kinds.setdefault(d["kind"], []).append(d)
    cfg = None
    for d in kinds.get("ConfigMap", []) + kinds.get("Secret", []):
        raw = (d.get("data") or {}).get("config.yaml")
        if raw:
            cfg = yaml.safe_load(raw)
    return kinds, cfg

def hours(v):
    return int(v[:-1]) * {"h": 1, "d": 24}[v[-1]]

for env, replicas, rf, retention, insecure, pdb in (("base", 3, 2, 8760, False, True), ("local", 1, 1, 168, True, False)):
    kinds, cfg = load(env)
    sts = [d for d in kinds.get("StatefulSet", []) if d["metadata"]["name"] == "loki"]
    check(f"{env}: one StatefulSet loki with {replicas} replica(s)", len(sts) == 1 and sts[0]["spec"]["replicas"] == replicas)
    check(f"{env}: no loki-canary DaemonSet", not [d for d in kinds.get("DaemonSet", []) if "canary" in d["metadata"]["name"]])
    check(f"{env}: PodDisruptionBudget {'present with minAvailable 1' if pdb else 'absent'}",
          (len(kinds.get("PodDisruptionBudget", [])) == 1 and kinds["PodDisruptionBudget"][0]["spec"].get("minAvailable") == 1) if pdb
          else not kinds.get("PodDisruptionBudget"))
    comp = (cfg or {}).get("compactor", {})
    check(f"{env}: compactor enforces retention (retention_enabled, delete_request_store s3, working_directory)",
          comp.get("retention_enabled") is True and comp.get("delete_request_store") == "s3" and comp.get("working_directory"))
    got = cfg["limits_config"]["retention_period"]
    check(f"{env}: retention_period {got} == {retention}h", hours(got) == retention)
    check(f"{env}: replication_factor {rf}", cfg["common"]["replication_factor"] == rf)
    s3 = cfg["common"]["storage"]["s3"]
    check(f"{env}: s3 insecure {insecure}", bool(s3.get("insecure", False)) is insecure)
    check(f"{env}: s3 path style kept", s3.get("s3forcepathstyle") is True)
    # The rule that makes enabling the compactor safe: it deletes what the limit says is old.
    check(f"{env}: compactor never runs with a retention under 168h", hours(got) >= 168)
    if env == "base":
        check("base: a replica can be down and a write still reaches RF (replicas > RF)", replicas > rf)
        check("base: the shipped retention is the one-year baseline", hours(got) >= 8760)
        check("base: s3 endpoint is https in the render", s3["endpoint"].startswith("https://"))

print(f"{fails} failed")
sys.exit(1 if fails else 0)
PY
