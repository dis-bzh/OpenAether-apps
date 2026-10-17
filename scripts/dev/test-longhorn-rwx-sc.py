#!/usr/bin/env python3
"""The RWX StorageClasses give up on a vanished NFS server in about a minute (apps #124, static, no cluster).

Lab 12: a graceful reboot of a node hosting an RWX share-manager AND a client hung in unmountPodMounts for more
than 12 minutes. Longhorn's default timeo=600,retrans=5 means 60+120+180+240+300 s = 15 min before the client
gives up. The shipped classes must (a) be soft (softerr), (b) wait at most MAX_WAIT_S before erroring, (c) not be
so short that a share-manager restart errors the apps (MIN_WAIT_S), (d) carry the Flux force annotation (the
parameters of a StorageClass are immutable, so changing nfsOptions needs a delete and recreate).
Rung: mocked/static.   Usage: python3 -I scripts/dev/test-longhorn-rwx-sc.py   (needs PyYAML)
"""
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
MAX_WAIT_S, MIN_WAIT_S = 120, 30
classes = [d for d in yaml.safe_load_all((REPO / "apps/base/storage/storageclasses.yaml").read_text())
           if d and d["kind"] == "StorageClass" and d["metadata"]["name"].endswith(("-rwx", "-rwx-encrypted"))]
fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    fails += not cond


def wait_seconds(opts):
    o = dict(x.split("=", 1) if "=" in x else (x, True) for x in opts.split(","))
    timeo, retrans = int(o["timeo"]) / 10, int(o["retrans"])
    return sum(timeo * (i + 1) for i in range(retrans)), o  # linear backoff on TCP: timeo, 2*timeo, ...


check(len(classes) == 2, f"two RWX classes found ({[c['metadata']['name'] for c in classes]})")
for c in classes:
    n = c["metadata"]["name"]
    opts = c["parameters"].get("nfsOptions", "")
    check(bool(opts), f"{n}: nfsOptions set")
    if not opts:
        continue
    total, o = wait_seconds(opts)
    check("softerr" in o, f"{n}: softerr (an error instead of an endless hang)")
    check(MIN_WAIT_S <= total <= MAX_WAIT_S, f"{n}: gives up after {total:.0f} s (between {MIN_WAIT_S} and {MAX_WAIT_S})")
    check(o.get("vers") == "4.1", f"{n}: vers=4.1 kept")
    check(c["metadata"].get("annotations", {}).get("kustomize.toolkit.fluxcd.io/force") == "enabled",
          f"{n}: Flux force annotation (immutable parameters)")
sys.exit(1 if fails else 0)
