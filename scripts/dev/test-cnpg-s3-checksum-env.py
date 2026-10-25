#!/usr/bin/env python3
"""Every CNPG ObjectStore must keep boto3 (barman-cloud) from sending the trailing checksum (apps #132).

Newer boto3 adds `x-amz-checksum` trailers by default; Outscale OOS answers `PutObject: trailing checksum is not supported`,
so continuous archiving stays False and every Backup stays "running". `AWS_REQUEST_CHECKSUM_CALCULATION` and
`AWS_RESPONSE_CHECKSUM_VALIDATION` set to `when_required` cure it and are harmless elsewhere. With the Barman Cloud plugin
boto3 runs in the sidecar, so the variables live in the ObjectStore (`instanceSidecarConfiguration.env`), not in the Cluster.
Static: it proves the manifests carry the variables, not what a store does with them.
"""
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
FILES = ("apps/base/cnpg/objectstores.yaml", "apps/base/forgejo-db/objectstores.yaml")
WANT = {"AWS_REQUEST_CHECKSUM_CALCULATION": "when_required", "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required"}


def main():
    bad = 0
    stores = [d for f in FILES for d in yaml.safe_load_all((REPO / f).read_text()) if d and d.get("kind") == "ObjectStore"]
    if len(stores) < 6:
        print(f"FAIL {FILES}: want a primary and a replica ObjectStore per Cluster (3 Clusters), found {len(stores)}"); bad += 1
    for d in stores:
        env = {e["name"]: e.get("value") for e in d["spec"].get("instanceSidecarConfiguration", {}).get("env", [])}
        for k, v in WANT.items():
            if env.get(k) != v:
                print(f"FAIL {d['metadata']['name']}: sidecar env {k} is {env.get(k)!r}, want {v!r}"); bad += 1
    print("ok" if not bad else f"{bad} failure(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
