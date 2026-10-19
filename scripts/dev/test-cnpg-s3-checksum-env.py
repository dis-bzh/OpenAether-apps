#!/usr/bin/env python3
"""Both CNPG clusters must keep boto3 (barman-cloud) from sending the trailing checksum (apps #132).

Newer boto3 adds `x-amz-checksum` trailers by default; Outscale OOS answers `PutObject: trailing checksum is not supported`,
so continuous archiving stays False and every Backup stays "running". `AWS_REQUEST_CHECKSUM_CALCULATION` and
`AWS_RESPONSE_CHECKSUM_VALIDATION` set to `when_required` on the Cluster (`spec.env`) cure it and are harmless elsewhere.
Static: it proves the manifests carry the variables, not what a store does with them.
"""
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
FILES = ["apps/base/cnpg/cluster.yaml", "apps/base/cnpg/grafana-db-cluster.yaml"]
WANT = {"AWS_REQUEST_CHECKSUM_CALCULATION": "when_required", "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required"}


def env_of(path):
    for doc in yaml.safe_load_all((REPO / path).read_text()):
        if doc and doc.get("kind") == "Cluster":
            return {e["name"]: e.get("value") for e in doc["spec"].get("env", [])}
    return None


def main():
    bad = 0
    for f in FILES:
        got = env_of(f)
        if got is None:
            print(f"FAIL {f}: no Cluster document"); bad += 1; continue
        for k, v in WANT.items():
            if got.get(k) != v:
                print(f"FAIL {f}: spec.env {k} is {got.get(k)!r}, want {v!r}"); bad += 1
    print("ok" if not bad else f"{bad} failure(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
