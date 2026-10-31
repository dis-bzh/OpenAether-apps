#!/usr/bin/env python3
"""A planned shutdown of a database instance is bounded (lab 27, FI).

Lab 27: the in-place restart of the zitadel-db primary (forced by the Barman plugin sidecar) waited out the 180 s default
`smartShutdownTimeout` because the Zitadel pool kept sessions open: a 206.4 s write gap, not the 17-22 s measured without clients.
The shared Kustomize component apps/components/cnpg-bounded-shutdown sets `spec.smartShutdownTimeout: 5` on every Cluster; this
test renders every brick and profile that defines a Cluster and fails when one lacks it, when the value leaves the 30 s class, when
it is not smaller than `stopDelay`, when the vendored CRD does not know the field, or when a Cluster file sets it by hand (one
place only). The infra repository's cnpg-presync patches the SAME number on the live 0.3.x Clusters before the move
(test-cnpg-presync.sh there). Static: it proves the value is on every Cluster, not how long a restart takes (UNVERIFIED until the
next lab).
Run: python3 -I scripts/dev/test-cnpg-shutdown.py [--selftest]
"""
import copy
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WANT = 5
MAX_BOUND = 30          # the target class of the planned-restart write gap, seconds
BRICKS = ["apps/base/cnpg", "apps/base/forgejo-db"]
PROFILES = ["apps/components/cnpg-profile-examples/" + n for n in ("ha", "light", "standard", "ha-bench")]
CLUSTER_FILES = ["apps/base/cnpg/cluster.yaml", "apps/base/cnpg/grafana-db-cluster.yaml", "apps/base/forgejo-db/cluster.yaml",
                 "apps/components/cnpg-profile-ha/cluster.yaml", "apps/components/cnpg-profile-light/cluster.yaml",
                 "apps/components/cnpg-profile-standard/cluster.yaml"]


def render(path):
    r = subprocess.run(["kubectl", "kustomize", str(REPO / path)], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"{path}: {r.stderr.strip()[-300:]}")
    return [d for d in yaml.safe_load_all(r.stdout) if isinstance(d, dict)]


def problems(clusters, crd_props, files):
    """clusters: {path: [Cluster, ...]}; files: {path: Cluster spec as written}."""
    errs = []
    if "smartShutdownTimeout" not in crd_props or "stopDelay" not in crd_props:
        errs.append("the vendored Cluster CRD does not know smartShutdownTimeout and stopDelay")
    n = 0
    for path, cs in clusters.items():
        for c in cs:
            n += 1
            spec, name = c["spec"], c["metadata"]["name"]
            t = spec.get("smartShutdownTimeout")
            if t != WANT:
                errs.append(f"{path}: Cluster {name} has smartShutdownTimeout {t!r}, want {WANT} (the default 180 made a planned primary restart wait 180 s, lab 27)")
            elif not isinstance(t, int) or t < 0 or t > MAX_BOUND:
                errs.append(f"{path}: Cluster {name} smartShutdownTimeout {t!r} is outside 0-{MAX_BOUND} s")
            stop = spec.get("stopDelay", 1800)
            if isinstance(t, int) and not t < stop:
                errs.append(f"{path}: Cluster {name} smartShutdownTimeout {t} is not below stopDelay {stop}: no time is left for the fast shutdown")
            if stop < 15:
                errs.append(f"{path}: Cluster {name} stopDelay {stop} is below the 15 s CloudNativePG needs")
    if n < 7:
        errs.append(f"only {n} Cluster(s) rendered; expected zitadel-db, grafana-db, forgejo-db and the four profile examples")
    for path, spec in files.items():
        if "smartShutdownTimeout" in spec:
            errs.append(f"{path} sets smartShutdownTimeout by hand: it belongs to the shared component apps/components/cnpg-bounded-shutdown only")
    return errs


def inputs():
    clusters = {p: [d for d in render(p) if d.get("kind") == "Cluster"] for p in BRICKS + PROFILES}
    crd = next(d for d in yaml.safe_load_all((REPO / "apps/base/cnpg/cnpg-1.30.1.yaml").read_text())
               if d and d.get("kind") == "CustomResourceDefinition" and d["metadata"]["name"] == "clusters.postgresql.cnpg.io")
    props = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]
    files = {f: next(d for d in yaml.safe_load_all((REPO / f).read_text()) if d and d.get("kind") == "Cluster")["spec"] for f in CLUSTER_FILES}
    return clusters, props, files


def selftest(inp):
    clusters, props, files = inp
    ok = not problems(*inp)
    print(("ok   " if ok else "FAIL ") + "the real renders pass")

    def mut(fn):
        c, p, f = copy.deepcopy(clusters), copy.deepcopy(props), copy.deepcopy(files)
        fn(c, p, f)
        return problems(c, p, f)

    cases = {
        "one Cluster without the value (component not listed)": lambda c, p, f: c["apps/base/forgejo-db"][0]["spec"].pop("smartShutdownTimeout"),
        "a profile Cluster without the value": lambda c, p, f: c[PROFILES[0]][0]["spec"].pop("smartShutdownTimeout"),
        "the 180 s default written back": lambda c, p, f: c["apps/base/cnpg"][0]["spec"].update(smartShutdownTimeout=180),
        "a value above the 30 s class": lambda c, p, f: c["apps/base/cnpg"][0]["spec"].update(smartShutdownTimeout=60),
        "a stopDelay not above the smart window": lambda c, p, f: c["apps/base/cnpg"][0]["spec"].update(stopDelay=5),
        "the field set by hand in a Cluster file": lambda c, p, f: f["apps/base/cnpg/cluster.yaml"].update(smartShutdownTimeout=5),
        "a CRD without the field": lambda c, p, f: p.pop("smartShutdownTimeout"),
    }
    for name, fn in cases.items():
        caught = bool(mut(fn))
        print(("ok   " if caught else "FAIL ") + f"caught: {name}")
        ok = ok and caught
    return ok


def main():
    inp = inputs()
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest(inp) else 1)
    errs = problems(*inp)
    for e in errs:
        print("FAIL " + e)
    print(f"{len(errs)} failed" if errs else f"ok   every platform and profile Cluster has smartShutdownTimeout {WANT}")
    sys.exit(1 if errs else 0)


main()
