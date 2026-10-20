#!/usr/bin/env python3
"""The restore tests' manifests (backup/databases, cnpg) keep the shape that makes them safe, rendered with kubectl kustomize:
  - the ServiceAccount can write to the scratch namespace and to ONE ConfigMap of foundation-databases, nothing else;
  - the scratch namespace is restricted, has a quota, and its ObjectStores read the same paths as the live ones, with no
    retention policy (the plugin must not delete anything through them);
  - every ObjectStore and secret the script names exists; the Jobs have a deadline longer than their work and a grace period
    longer than the cleanup; only the restore container holds the ServiceAccount token, only the source container the
    database credentials; the CronJob names end in -restore-test (the M1 alert watches them);
  - the exporter serves both ConfigMaps.
Usage: python3 -I scripts/dev/test-restore-test-brick.py   (kubectl and PyYAML)
"""
import importlib.util
import os
import subprocess
import sys

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    PASS += bool(cond)
    FAIL += not cond
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f" -> {detail}"))


def render(d):
    r = subprocess.run(["kubectl", "kustomize", os.path.join(ROOT, "apps/base", d)], capture_output=True, text=True)
    if r.returncode:
        sys.exit(r.stderr)
    return [x for x in yaml.safe_load_all(r.stdout) if x]


def spec_of(rc_path):
    s = importlib.util.spec_from_file_location("rc", rc_path)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


rc = spec_of(os.path.join(ROOT, "apps/base/backup/databases/restore/restore_cnpg.py"))
bk, cn = render("backup/databases"), render("cnpg")


def get(docs, kind, name=None, ns=None):
    return [d for d in docs if d["kind"] == kind and (name is None or d["metadata"]["name"] == name) and (ns is None or d["metadata"].get("namespace") == ns)]


print("=== RBAC ===")
roles = [d for d in get(bk, "Role") if d["metadata"]["name"] == "cnpg-restore-test"]
by_ns = {r["metadata"]["namespace"]: r for r in roles}
check("one Role in the scratch namespace and one in foundation-databases", set(by_ns) == {rc.SCRATCH_NS, rc.LIVE_NS}, list(by_ns))
live = by_ns[rc.LIVE_NS]["rules"]
check("in foundation-databases the SA only reads Clusters and Backups and patches ONE named ConfigMap",
      all(set(r["verbs"]) <= {"get", "list"} for r in live if r["resources"] != ["configmaps"])
      and [r for r in live if r["resources"] == ["configmaps"]][0].get("resourceNames") == ["oa-restore-test-metrics"]
      and set([r for r in live if r["resources"] == ["configmaps"]][0]["verbs"]) == {"get", "patch"}, live)
check("it can create nothing in foundation-databases", not any("create" in r["verbs"] or "delete" in r["verbs"] or "update" in r["verbs"] for r in live), live)
check("no ClusterRole or ClusterRoleBinding for the restore test", not [d for d in bk if d["kind"] in ("ClusterRole", "ClusterRoleBinding") and "restore" in d["metadata"]["name"]])
check("the bindings name the ServiceAccount of foundation-databases only",
      all(s["name"] == "cnpg-restore-test" and s["namespace"] == rc.LIVE_NS for b in get(bk, "RoleBinding", "cnpg-restore-test") for s in b["subjects"]))

print("=== scratch namespace ===")
ns = get(cn, "Namespace", rc.SCRATCH_NS)[0]
check("the scratch namespace enforces restricted", ns["metadata"]["labels"].get("pod-security.kubernetes.io/enforce") == "restricted")
q = get(cn, "ResourceQuota", ns=rc.SCRATCH_NS)
check("it has a quota with CPU, memory, storage and one Cluster",
      len(q) == 1 and all(k in q[0]["spec"]["hard"] for k in ("requests.cpu", "limits.cpu", "requests.memory", "limits.memory", "requests.storage", "count/clusters.postgresql.cnpg.io")))
sc_stores = {d["metadata"]["name"]: d for d in get(cn, "ObjectStore", ns=rc.SCRATCH_NS)}
live_stores = {d["metadata"]["name"]: d for d in get(cn, "ObjectStore", ns=rc.LIVE_NS)}
want = {f"{c}-{s}" for c in rc.CLUSTERS for s in rc.STORES}
check("the scratch ObjectStores are exactly <cluster>-<store> for each cluster and store", set(sc_stores) == want, sorted(sc_stores))
check("they read the same bucket path and credentials as the live ObjectStores",
      all(sc_stores[n]["spec"]["configuration"]["destinationPath"] == live_stores[n]["spec"]["configuration"]["destinationPath"]
          and sc_stores[n]["spec"]["configuration"]["s3Credentials"] == live_stores[n]["spec"]["configuration"]["s3Credentials"] for n in want if n in live_stores),
      sorted(set(want) - set(live_stores)))
check("none carries a retention policy (nothing is deleted through them)", not any("retentionPolicy" in d["spec"] for d in sc_stores.values()))
es = {d["metadata"]["name"] for d in get(cn, "ExternalSecret", ns=rc.SCRATCH_NS)}
check("the credentials Secrets the ObjectStores name are rebuilt from OpenBao in that namespace",
      {d["spec"]["configuration"]["s3Credentials"]["accessKeyId"]["name"] for d in sc_stores.values()} <= es, es)
check("the CNPG operator may reach the scratch instances",
      any("backup-restore-test" in str(e) for p in get(cn, "CiliumNetworkPolicy", "cnpg", "cnpg-system") for e in p["spec"]["egress"]))
store = [d for d in yaml.safe_load_all(open(os.path.join(ROOT, "apps/base/external-secrets/stores/clustersecretstore.yaml"))) if d][0]
check("the ClusterSecretStore lists the scratch namespace", rc.SCRATCH_NS in store["spec"]["conditions"][0]["namespaces"])

print("=== Jobs ===")
cj = {d["metadata"]["name"]: d for d in get(bk, "CronJob")}
for n, mode in (("cnpg-recovery-restore-test", "latest"), ("cnpg-pitr-restore-test", "pitr")):
    j = cj[n]
    p = j["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    c = {x["name"]: x for x in p["containers"] + p["initContainers"]}
    check(f"{n}: restores in mode {mode}, publishes, runs with the restore ServiceAccount", c["restore"]["command"][-3:] == ["--mode", mode, "--publish"] and p["serviceAccountName"] == "cnpg-restore-test")
    check(f"{n}: a deadline, no retry, a grace period longer than the cleanup",
          j["spec"]["jobTemplate"]["spec"].get("activeDeadlineSeconds", 0) > 3600 and j["spec"]["jobTemplate"]["spec"]["backoffLimit"] == 0
          and p["terminationGracePeriodSeconds"] > rc.DELETE_TIMEOUT and j["spec"]["concurrencyPolicy"] == "Forbid")
    mounts = lambda x: {m["name"] for m in c[x]["volumeMounts"]}  # noqa: E731
    check(f"{n}: only `restore` holds the token, only `source` the database credentials",
          "token" in mounts("restore") and "token" not in mounts("source") and {"zitadel-db-creds", "grafana-db-creds"} <= mounts("source")
          and not ({"zitadel-db-creds", "grafana-db-creds"} & mounts("restore")) and p["automountServiceAccountToken"] is False)
    check(f"{n}: limits on every container", all("limits" in x["resources"] for x in c.values()))
    check(f"{n}: its name ends in -restore-test (the M1 alert RestoreTestNotSucceeding watches that)", n.endswith("-restore-test"))
check("the two schedules do not overlap (one scratch Cluster at a time)", cj["cnpg-recovery-restore-test"]["spec"]["schedule"] != cj["cnpg-pitr-restore-test"]["spec"]["schedule"]
      and cj["cnpg-pitr-restore-test"]["spec"]["schedule"].split()[1] > cj["cnpg-recovery-restore-test"]["spec"]["schedule"].split()[1])
check("the canary writer runs at least every 10 minutes and cannot pile up",
      cj["cnpg-canary"]["spec"]["schedule"] == "*/5 * * * *" and cj["cnpg-canary"]["spec"]["concurrencyPolicy"] == "Forbid")

print("=== dump load test ===")
dj = cj["cnpg-dump-load-restore-test"]
dp = dj["spec"]["jobTemplate"]["spec"]["template"]["spec"]
dc = {x["name"]: x for x in dp["containers"] + dp["initContainers"]}
check("three stages in order: restic, load, publish", [x["name"] for x in dp["initContainers"]] == ["restic", "load"] and [x["name"] for x in dp["containers"]] == ["publish"])
dm = lambda x: {m["name"] for m in dc[x]["volumeMounts"]}  # noqa: E731
check("only `publish` holds the token; the database credentials are in no container (the load needs none)",
      "token" in dm("publish") and "token" not in dm("restic") | dm("load") and not any(v.get("secret", {}).get("secretName", "").endswith(("db-app", "db-credentials")) for v in dp["volumes"]))
check("`load` runs as postgres (26) with the CNPG image, read-only on the restore, with no network rule of its own", dc["load"]["securityContext"]["runAsUser"] == 26
      and dc["load"]["image"] == [x for x in cj["cnpg-dump"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["initContainers"] if x["name"] == "pg-dump"][0]["image"] and "restore" in dm("load") and next(m for m in dc["load"]["volumeMounts"] if m["name"] == "restore")["readOnly"])
check("its ServiceAccount patches one ConfigMap and nothing else",
      [r for r in get(bk, "Role", "cnpg-dump-restore-test")][0]["rules"] == [{"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["oa-restore-test-metrics"], "verbs": ["get", "patch"]}])
check("it has a network policy", any(p["spec"]["endpointSelector"]["matchLabels"].get("app") == "cnpg-dump-load-restore-test" for p in get(bk, "CiliumNetworkPolicy")))
check("deadline, no retry, limits", dj["spec"]["jobTemplate"]["spec"]["backoffLimit"] == 0 and dj["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"] > 0 and all("limits" in x["resources"] for x in dc.values()))

print("=== exporter and scripts ===")
dep = get(bk, "Deployment", "backup-verify-exporter")[0]["spec"]["template"]["spec"]
cm = {v["name"]: v.get("configMap", {}).get("name") for v in dep["volumes"]}
check("the exporter serves the verifier's and the restore tests' ConfigMaps", {"oa-backup-verify-metrics", "oa-restore-test-metrics"} <= set(cm.values()), cm)
check("and runs the merging server", "serve_metrics.py" in " ".join(dep["containers"][0]["command"]))
gen = [d for d in bk if d["kind"] == "ConfigMap" and d["metadata"]["name"].startswith("backup-restore-scripts")][0]
check("the scripts ConfigMap holds every script the Jobs run", {"canary-write.sh", "source-snapshot.sh", "snapshot.sql", "restore_cnpg.py", "serve_metrics.py", "restic-dump.sh", "dump_load.py"} <= set(gen["data"]), list(gen["data"]))
check("the live metrics ConfigMap is never overwritten by Flux", get(bk, "ConfigMap", "oa-restore-test-metrics")[0]["metadata"]["annotations"].get("kustomize.toolkit.fluxcd.io/ssa") == "IfNotPresent")
dump = [x for x in cj["cnpg-dump"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["initContainers"] if x["name"] == "pg-dump"][0]["image"]
rt = cj["cnpg-recovery-restore-test"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["initContainers"][0]["image"]
check("the live snapshot uses the image of the dump (psql at least as new as the servers)", rt == dump, (rt, dump))

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
