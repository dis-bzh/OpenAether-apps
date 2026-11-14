#!/usr/bin/env python3
"""The PostgreSQL pre-pull DaemonSet (apps/base/cnpg-image-prepull) must pull exactly the images the CNPG Clusters run (apps #180).

A pre-pull of an image the Clusters no longer use is dead weight, and a Cluster image the DaemonSet does not name is pulled cold again
(about 65-90 s per node, lab 46). Also pinned: the Flux Kustomization is `wait: false` (nothing may wait for the pulls) and `cnpg`
depends on it so that a profile which picks cnpg carries it.
Run: python3 -I scripts/dev/test-cnpg-image-prepull.py   (needs PyYAML)"""
import sys, pathlib, yaml
R = pathlib.Path(__file__).resolve().parents[2]
fail = 0
def ok(m): print(f"  ✓ {m}")
def bad(m):
    global fail; fail += 1; print(f"  ✗ {m}")
def docs(p): return [d for d in yaml.safe_load_all(open(R / p)) if d]
clusters = set()
for p in ("apps/base/cnpg/cluster.yaml", "apps/base/cnpg/grafana-db-cluster.yaml", "apps/base/forgejo-db/cluster.yaml"):
    for d in docs(p):
        if d.get("kind") == "Cluster" and d["spec"].get("imageName"):
            clusters.add(d["spec"]["imageName"])
ds = [d for d in docs("apps/base/cnpg-image-prepull/daemonset.yaml") if d["kind"] == "DaemonSet"][0]
pod = ds["spec"]["template"]["spec"]
pulled = {c["image"] for c in pod.get("initContainers", []) + pod["containers"]}
if clusters and pulled == clusters: ok(f"the DaemonSet pulls exactly the {len(clusters)} Cluster images")
else: bad(f"Cluster images {sorted(clusters)} != pre-pulled {sorted(pulled)}")
if all(c.get("imagePullPolicy") == "IfNotPresent" for c in pod.get("initContainers", []) + pod["containers"]): ok("imagePullPolicy IfNotPresent everywhere")
else: bad("a pre-pull container does not use IfNotPresent")
ns_early = {"kube-system", "flux-system"} | {d["metadata"]["name"] for d in docs("apps/base/namespaces/namespaces.yaml") if d.get("kind") == "Namespace"}
if ds["metadata"]["namespace"] in ns_early: ok(f"the DaemonSet namespace ({ds['metadata']['namespace']}) exists before cnpg runs (lab 49: foundation-databases is created BY cnpg: deadlock)")
else: bad(f"the DaemonSet is in {ds['metadata']['namespace']}, which only the cnpg Kustomization creates: cnpg depends on this one, so it deadlocks (lab 49)")
ks = {d["metadata"]["name"]: d for d in docs("apps/flux/base/20-cnpg.yaml") if d["kind"] == "Kustomization"}
pre = ks.get("cnpg-image-prepull")
if pre and pre["spec"].get("wait") is False: ok("cnpg-image-prepull is wait: false")
else: bad("cnpg-image-prepull must exist and be wait: false (nothing may wait for the pulls)")
if pre and [x["name"] for x in pre["spec"].get("dependsOn", [])] == ["namespaces"]: ok("cnpg-image-prepull depends only on namespaces (applied early)")
else: bad("cnpg-image-prepull must depend only on namespaces")
if "cnpg-image-prepull" in [x["name"] for x in ks["cnpg"]["spec"]["dependsOn"]]: ok("cnpg depends on cnpg-image-prepull")
else: bad("cnpg must depend on cnpg-image-prepull")
print(f"{'FAIL' if fail else 'OK'}: {fail} failure(s)")
sys.exit(1 if fail else 0)
