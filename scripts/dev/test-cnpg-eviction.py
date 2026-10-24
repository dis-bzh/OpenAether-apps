#!/usr/bin/env python3
"""A CNPG primary on a lost node must be evicted after seconds, not after the default 300 s (apps #154, lab 21).

CNPG promotes a replica when the primary's pod is deleted; on a dead node that happens when the TaintManager evicts it.
The Kubernetes default (tolerationSeconds 300 for unreachable and not-ready) gave write gaps of 328 and 333 s. Static: the
Cluster manifests carry both tolerations with a value between the liveness window of the instance manager (30 s: an
isolated primary stops itself first) and 60 s; it does not prove what a node loss does on a cluster (the lab drill does).
"""
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
CLUSTERS = ("apps/base/cnpg/cluster.yaml", "apps/base/cnpg/grafana-db-cluster.yaml")
KEYS = ("node.kubernetes.io/unreachable", "node.kubernetes.io/not-ready")
LOW, HIGH = 30, 60


def check(spec, label):
    """Return the list of problems of one Cluster spec."""
    tols = spec.get("affinity", {}).get("tolerations", [])
    bad = []
    for key in KEYS:
        mine = [t for t in tols if t.get("key") == key]
        if len(mine) != 1:
            bad.append(f"{label}: expected one toleration for {key}, found {len(mine)}")
            continue
        t = mine[0]
        if t.get("effect") != "NoExecute" or t.get("operator") != "Exists":
            bad.append(f"{label}: {key} must be operator Exists, effect NoExecute")
        sec = t.get("tolerationSeconds")
        if not isinstance(sec, int) or not LOW <= sec <= HIGH:
            bad.append(f"{label}: {key} tolerationSeconds {sec!r} outside {LOW}..{HIGH}")
    if spec.get("failoverDelay", 0) != 0:
        bad.append(f"{label}: failoverDelay adds to the gap, keep it 0")
    return bad


def main():
    if "--selftest" in sys.argv:
        good = {"affinity": {"tolerations": [{"key": k, "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 30} for k in KEYS]}}
        cases = {"default": {}, "300": {"affinity": {"tolerations": [dict(t, tolerationSeconds=300) for t in good["affinity"]["tolerations"]]}},
                 "one key": {"affinity": {"tolerations": good["affinity"]["tolerations"][:1]}}, "good": good}
        for name, spec in cases.items():
            print(f"selftest {name}: {len(check(spec, name))} problem(s)")
        return 0 if not check(good, "good") and all(check(cases[n], n) for n in ("default", "300", "one key")) else 1
    bad = []
    for path in CLUSTERS:
        spec = next(d for d in yaml.safe_load_all((REPO / path).read_text()) if d and d.get("kind") == "Cluster")["spec"]
        probs = check(spec, path)
        print(("ok   " if not probs else "FAIL ") + f"{path}: unreachable and not-ready evict after {LOW}..{HIGH} s")
        bad += probs
    for b in bad:
        print("  " + b)
    print(f"{len(bad)} failed")
    return 1 if bad else 0


sys.exit(main())
