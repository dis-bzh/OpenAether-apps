#!/usr/bin/env python3
"""Both CNPG clusters must roll their primary by switchover, not by restarting the only writer in place (lab 15c).

CNPG's default `primaryUpdateMethod: restart` restarts the primary pod in place on every operator upgrade, image bump or
restart-needing parameter: lab 15c measured about 195 s without a write per operator hop. `switchover` updates the replicas
first and promotes one. Static: it proves the manifests say so, not how long a failover takes.
"""
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
FILES = ["apps/base/cnpg/cluster.yaml", "apps/base/cnpg/grafana-db-cluster.yaml"]


def spec_of(path):
    for doc in yaml.safe_load_all((REPO / path).read_text()):
        if doc and doc.get("kind") == "Cluster":
            return doc["spec"]
    return None


def main():
    bad = 0
    for f in FILES:
        spec = spec_of(f)
        if spec is None:
            print(f"FAIL {f}: no Cluster document"); bad += 1; continue
        if spec.get("primaryUpdateMethod") != "switchover":
            print(f"FAIL {f}: primaryUpdateMethod is {spec.get('primaryUpdateMethod')!r}, want 'switchover'"); bad += 1
        if spec.get("primaryUpdateStrategy") != "unsupervised":
            print(f"FAIL {f}: primaryUpdateStrategy is {spec.get('primaryUpdateStrategy')!r}, want 'unsupervised'"); bad += 1
        if spec.get("instances", 1) < 2:
            print(f"FAIL {f}: a switchover needs at least 2 instances"); bad += 1
    print("ok" if not bad else f"{bad} failure(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
