#!/usr/bin/env bash
# Least privilege of the observability service accounts, checked on what the manifests really
# produce: the charts rendered with the shipped values (alloy, grafana, kube-state-metrics) plus the brick built by
# kustomize. Fails if any role reachable from these components reads Secrets, nodes/proxy, or
# if a binding points at a role that does not exist. Loki's roles are in test-loki-render.sh.
# Needs kubectl, helm, python3 with PyYAML (network for the charts). Rung: mocked/static.
#   Usage: scripts/dev/test-observability-rbac.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
for bin in kubectl helm python3; do command -v "$bin" >/dev/null || { echo "$bin is required" >&2; exit 2; }; done
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
CHARTS="alloy grafana kube-state-metrics"   # HelmRelease names rendered below

kubectl kustomize "$ROOT/apps/base/observability" > "$WORK/brick.yaml"
kubectl kustomize "$ROOT/apps/base/observability/vm-customresources" > "$WORK/vmcr.yaml"

for name in $CHARTS; do
  python3 -I - "$WORK/brick.yaml" "$name" "$WORK" <<'PY'
import sys, yaml
src, name, work = sys.argv[1:4]
for d in yaml.safe_load_all(open(src)):
    if d and d.get("kind") == "HelmRelease" and d["metadata"]["name"] == name:
        spec = d["spec"]["chart"]["spec"]
        repo = next(r["spec"]["url"] for r in yaml.safe_load_all(open(src))
                    if r and r.get("kind") == "HelmRepository" and r["metadata"]["name"] == spec["sourceRef"]["name"])
        yaml.safe_dump(d["spec"].get("values", {}), open(f"{work}/{name}-values.yaml", "w"))
        open(f"{work}/{name}.chart", "w").write(f'{spec["chart"]} {spec["version"]} {repo}\n')
        break
else:
    sys.exit("no HelmRelease " + name)
PY
  read -r chart version repo < "$WORK/$name.chart"
  helm pull "$chart" --repo "$repo" --version "$version" --untar --untardir "$WORK/chart" >/dev/null
  helm template "$name" "$WORK/chart/$chart" --namespace services-observability -f "$WORK/$name-values.yaml" > "$WORK/$name-rendered.yaml"
done

python3 -I - "$WORK" $CHARTS <<'PY'
import glob, sys, yaml
work, charts = sys.argv[1], sys.argv[2:]
fails = 0
def check(label, ok):
    global fails
    print(("ok   " if ok else "FAIL ") + label)
    fails += 0 if ok else 1

docs = []
for f in [f"{work}/brick.yaml", f"{work}/vmcr.yaml"] + [f"{work}/{c}-rendered.yaml" for c in charts]:
    docs += [d for d in yaml.safe_load_all(open(f)) if d]
roles = {(d["kind"], d["metadata"]["name"]): d for d in docs if d["kind"] in ("Role", "ClusterRole")}
bindings = [d for d in docs if d["kind"] in ("RoleBinding", "ClusterRoleBinding")]
NS = "services-observability"

def grants(role):
    for r in role.get("rules") or []:
        for res in r.get("resources") or []:
            yield r.get("apiGroups") or [], res, r.get("verbs") or []

# Every role bound to a service account of this namespace, and every role the brick defines.
bound = set()
for b in bindings:
    for s in b.get("subjects") or []:
        if s["kind"] == "ServiceAccount" and s.get("namespace") == NS:
            ref = b["roleRef"]
            check(f'binding {b["metadata"]["name"]} -> {ref["kind"]}/{ref["name"]} exists',
                  (ref["kind"], ref["name"]) in roles)
            bound.add((ref["kind"], ref["name"]))
for key in sorted(bound | {k for k in roles if k[0] == "ClusterRole"}):
    role = roles.get(key)
    if role is None:
        continue
    bad = [res for groups, res, verbs in grants(role)
           if res in ("secrets", "nodes/proxy", "*") or res.startswith("secrets/")]
    check(f"{key[0]}/{key[1]}: no secrets, nodes/proxy or wildcard resource", not bad)

def roles_of(sa):
    out = []
    for b in bindings:
        if any(s["kind"] == "ServiceAccount" and s["name"] == sa and s.get("namespace") == NS for s in b.get("subjects") or []):
            out.append(roles.get((b["roleRef"]["kind"], b["roleRef"]["name"])))
    return [r for r in out if r]

def reads(sa, resource):
    return any(resource in res and "get" in verbs or resource == res and "list" in verbs
               for r in roles_of(sa) for _, res, verbs in grants(r))

if "alloy" in charts:
    rs = roles_of("alloy")
    check("alloy: bound to exactly the hand-written alloy-pod-logs ClusterRole",
          [r["metadata"]["name"] for r in rs] == ["alloy-pod-logs"])
    check("alloy: the chart creates no ClusterRole of its own",
          not [k for k in roles if k[1] == "alloy"])
    check("alloy: can read pods and pods/log", reads("alloy", "pods") and reads("alloy", "pods/log"))
    check("alloy: cannot read configmaps", not reads("alloy", "configmaps"))

if "grafana" in charts:
    rs = roles_of("grafana")
    check("grafana: bound to exactly the hand-written grafana-dashboard-reader ClusterRole",
          [r["metadata"]["name"] for r in rs] == ["grafana-dashboard-reader"])
    check("grafana: the chart creates no Role nor ClusterRole of its own",
          not [k for k in roles if k[1].startswith("grafana") and k[1] != "grafana-dashboard-reader"])
    check("grafana: can list configmaps (dashboards sidecar)", reads("grafana", "configmaps"))
    check("grafana: cannot read secrets", not reads("grafana", "secrets"))
    sidecar = [c for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "grafana"
               for c in d["spec"]["template"]["spec"]["containers"] if c["name"] == "grafana-sc-dashboard"]
    env = {e["name"]: e.get("value") for c in sidecar for e in c.get("env", [])}
    check("grafana: the dashboards sidecar asks for configmaps only (else 403 on secrets)", env.get("RESOURCE") == "configmap")

if "kube-state-metrics" in charts:
    rs = roles_of("kube-state-metrics")
    check("kube-state-metrics: bound to a ClusterRole", len(rs) >= 1)
    check("kube-state-metrics: cannot read secrets (collector off, --resources has no secrets)",
          not reads("kube-state-metrics", "secrets"))
    check("kube-state-metrics: cannot read configmaps", not reads("kube-state-metrics", "configmaps"))
    check("kube-state-metrics: still lists the four Flux kinds (gotk_resource_info) and cronjobs",
          all(reads("kube-state-metrics", k) for k in ("kustomizations", "helmreleases", "gitrepositories", "helmrepositories", "cronjobs")))
    args = [a for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "kube-state-metrics"
            for c in d["spec"]["template"]["spec"]["containers"] for a in c.get("args", []) if a.startswith("--resources")]
    check("kube-state-metrics: --resources lists neither secrets nor configmaps",
          len(args) == 1 and not {"secrets", "configmaps"} & set(args[0].split("=", 1)[1].split(",")))

print(f"{fails} failed")
sys.exit(1 if fails else 0)
PY
