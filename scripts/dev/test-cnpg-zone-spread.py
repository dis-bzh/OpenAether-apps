#!/usr/bin/env python3
"""The CNPG clusters and the CNPG operator must prefer different failure domains, not only different hosts (lab 21).

Lab 21 found both grafana-db instances and both CNPG operator replicas in ONE zone (spread by hostname only): losing that zone
took the whole database and the thing that would fail it over. Static: the manifests carry a zone-level preference that never
blocks scheduling (a preferred anti-affinity term, a ScheduleAnyway spread); it does not prove where a scheduler puts the pods.
"""
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
ZONE = "topology.kubernetes.io/zone"
CLUSTERS = {"apps/base/cnpg/cluster.yaml": "zitadel-db", "apps/base/cnpg/grafana-db-cluster.yaml": "grafana-db",
            "apps/base/forgejo-db/cluster.yaml": "forgejo-db"}


def docs(path):
    return [d for d in yaml.safe_load_all((REPO / path).read_text()) if d]


def main():
    bad = 0
    for path, name in CLUSTERS.items():
        spec = next(d for d in docs(path) if d.get("kind") == "Cluster")["spec"]
        terms = (spec.get("affinity", {}).get("additionalPodAntiAffinity", {})
                 .get("preferredDuringSchedulingIgnoredDuringExecution", []))
        ok = any(t["podAffinityTerm"]["topologyKey"] == ZONE
                 and t["podAffinityTerm"]["labelSelector"]["matchLabels"].get("cnpg.io/cluster") == name for t in terms)
        print(("ok   " if ok else "FAIL ") + f"{path}: a preferred zone anti-affinity term selects {name}")
        bad += not ok
        hard = spec.get("affinity", {}).get("additionalPodAntiAffinity", {}).get("requiredDuringSchedulingIgnoredDuringExecution")
        print(("ok   " if not hard else "FAIL ") + f"{path}: nothing in the zone term is required (a lost zone must not block a pod)")
        bad += bool(hard)
    patch = next(d for d in docs("apps/base/cnpg/patch-ha.yaml") if d.get("kind") == "Deployment")
    cons = patch["spec"]["template"]["spec"]["topologySpreadConstraints"]
    ok = any(c["topologyKey"] == ZONE and c["whenUnsatisfiable"] == "ScheduleAnyway" for c in cons)
    print(("ok   " if ok else "FAIL ") + "apps/base/cnpg/patch-ha.yaml: the operator spreads over zones, ScheduleAnyway")
    bad += not ok
    print(f"{bad} failed")
    return 1 if bad else 0


sys.exit(main())
