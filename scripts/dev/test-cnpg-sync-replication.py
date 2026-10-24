#!/usr/bin/env python3
"""Class A data (identity, dashboards, users) must not lose an acknowledged commit when a primary dies (owner rule 2026-10-22).

Both CNPG Clusters ask for quorum synchronous replication with `dataDurability: required` and the failover quorum check, with room
to lose one replica without blocking writes, and the restore-test scratch Cluster must not carry any of it (a one-instance copy
with a required standby would block every write). Static: the keys are checked against the vendored CRD; that a failover really
refuses a replica without the acknowledged commits is the lab criteria of docs/resilience.md ("Zero-loss proof"), not proven here.
Run: python3 -I scripts/dev/test-cnpg-sync-replication.py
"""
import importlib.util
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
CLUSTERS = {"zitadel-db": "apps/base/cnpg/cluster.yaml", "grafana-db": "apps/base/cnpg/grafana-db-cluster.yaml"}
fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    fails += not cond


def load(path):
    return [d for d in yaml.safe_load_all((REPO / path).read_text()) if d]


crd = next(d for d in load("apps/base/cnpg/cnpg-1.30.1.yaml") if d.get("kind") == "CustomResourceDefinition"
           and d["metadata"]["name"] == "clusters.postgresql.cnpg.io")
schema = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]
sync_schema = schema["postgresql"]["properties"]["synchronous"]
props = sync_schema["properties"]

for name, path in CLUSTERS.items():
    spec = next(d for d in load(path) if d.get("kind") == "Cluster")["spec"]
    sync = (spec.get("postgresql") or {}).get("synchronous") or {}
    n, inst = sync.get("number", 0), spec.get("instances", 1)
    check(bool(sync), f"{name}: spec.postgresql.synchronous is set")
    check(set(sync) <= set(props), f"{name}: every synchronous key is in the vendored CRD ({sorted(set(sync) - set(props))})")
    check(all(k in sync for k in sync_schema["required"]), f"{name}: the required keys {sync_schema['required']} are set")
    check(sync.get("method") in props["method"]["enum"] and sync.get("method") == "any", f"{name}: method is any (quorum)")
    check(sync.get("dataDurability") == "required", f"{name}: dataDurability is required (a commit is never acknowledged unreplicated)")
    check(sync.get("failoverQuorum") is True, f"{name}: failoverQuorum is on (a failover refuses a replica that may miss a commit)")
    check(1 <= n <= inst - 1, f"{name}: number {n} is between 1 and instances - 1 ({inst - 1})")
    check(inst - 1 - n >= 1, f"{name}: one replica can be lost without blocking writes ({inst} instances, number {n})")
    check("minSyncReplicas" not in spec and "maxSyncReplicas" not in spec, f"{name}: the legacy min/maxSyncReplicas are not mixed in")

# the restore-test scratch Cluster
spec_ = importlib.util.spec_from_file_location("rc", REPO / "apps/base/backup/databases/restore/restore_cnpg.py")
rc = importlib.util.module_from_spec(spec_)
spec_.loader.exec_module(rc)
for name, path in CLUSTERS.items():
    live = next(d for d in load(path) if d.get("kind") == "Cluster")
    m = rc.recovery_manifest(name, "primary", "latest", live)["spec"]
    check("synchronous" not in (m.get("postgresql") or {}) and "minSyncReplicas" not in m and m["instances"] == 1,
          f"{name}: the restore-test scratch Cluster has one instance and no synchronous setting")
policy = (REPO / "apps/base/kyverno-policies-cnpg/restrict-restore-test-clusters.yaml").read_text()
check("postgresql.synchronous" in policy, "the Kyverno guard refuses a synchronous setting on a scratch Cluster")
print(f"{fails} failed")
sys.exit(1 if fails else 0)
