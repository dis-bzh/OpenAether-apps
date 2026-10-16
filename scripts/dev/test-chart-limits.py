#!/usr/bin/env python3
"""Every container of some charts has a memory request and limit, on what the chart really renders.

Lab 11: several charts read `resources` under a key of their own (`alloy.resources`, `webhook.resources`, `cainjector.resources`,
`login.resources`, `sidecar.resources`, `configReloader.resources`); a block at the top of the HelmRelease values was silently
ignored, so the workers' Alloy ran at 340 Mi with no limit and Kyverno require-limits reported 46 containers. Renders each HelmRelease
below with the shipped values (`helm template`) and fails if a named workload has a container, init containers included, without a
memory request and limit, or (Alloy) a limit under the 380 Mi seen on the busiest worker (below real use is an OOM loop).
Not covered, on purpose: the CNPG `zitadel-db` Cluster (a resources change rolls every instance), operator-managed VictoriaMetrics
pods, Longhorn, Cilium. Needs helm, PyYAML and network for the charts. Rung: mocked/static.
Usage: python3 scripts/dev/test-chart-limits.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
BASE = REPO / "apps/base"
MI = {"Mi": 1, "Gi": 1024}
# HelmRelease file -> {workload name suffix: alloy floor in Mi or 0}; every container of these workloads is checked
TARGETS = {
    "observability/alloy.yaml": {"DaemonSet/alloy": 380},
    "observability/alloy-audit.yaml": {"DaemonSet/alloy-audit": 0},
    "cert-manager/helmrelease.yaml": {"Deployment/cert-manager-cainjector": 0, "Deployment/cert-manager-webhook": 0},
    "identity/zitadel/helmrelease.yaml": {"Deployment/zitadel-login": 0},
    "observability/grafana.yaml": {"Deployment/grafana": 0},
}
PASS = FAIL = 0


def check(desc, ok, detail=""):
    global PASS, FAIL
    PASS += bool(ok)
    FAIL += not ok
    print(("  ok   " if ok else "  FAIL ") + desc + ("" if ok else f" -> {detail}"))


def mi(q):
    return int(q[:-2]) * MI[q[-2:]] if q and q[-2:] in MI else None


def repo_url(name):
    for f in BASE.rglob("*.yaml"):
        try:
            for d in yaml.safe_load_all(f.read_text()):
                if d and d.get("kind") == "HelmRepository" and d["metadata"]["name"] == name:
                    return d["spec"]["url"]
        except yaml.YAMLError:
            continue
    sys.exit(f"no HelmRepository {name}")


def main():
    for rel, workloads in TARGETS.items():
        hr = next(d for d in yaml.safe_load_all((BASE / rel).read_text()) if d and d["kind"] == "HelmRelease")
        spec = hr["spec"]["chart"]["spec"]
        with tempfile.TemporaryDirectory() as tmp:
            values = Path(tmp) / "values.yaml"
            values.write_text(yaml.safe_dump(hr["spec"].get("values", {})))
            r = subprocess.run(["helm", "template", hr["metadata"]["name"], spec["chart"], "--repo", repo_url(spec["sourceRef"]["name"]),
                                "--version", spec["version"], "-n", hr["metadata"]["namespace"], "-f", str(values)],
                               capture_output=True, text=True)
        if r.returncode:
            check(f"{rel}: helm template", False, r.stderr.strip()[-200:])
            continue
        rendered = {f"{d['kind']}/{d['metadata']['name']}": d for d in yaml.safe_load_all(r.stdout) if d}
        for wl, floor in workloads.items():
            d = rendered.get(wl)
            check(f"{rel}: renders {wl}", d is not None, sorted(k for k in rendered if "/" in k and k.split("/")[0] in ("Deployment", "DaemonSet")))
            if d is None:
                continue
            pod = d["spec"]["template"]["spec"]
            for c in pod["containers"] + pod.get("initContainers", []):
                res = c.get("resources") or {}
                lim, req = mi(res.get("limits", {}).get("memory")), mi(res.get("requests", {}).get("memory"))
                check(f"{wl}/{c['name']} has a memory request and limit", bool(lim and req and lim >= req), res)
                if floor and c["name"] == "alloy":
                    check(f"{wl}/alloy: limit at least {floor} Mi (the busiest worker uses about 350 Mi)", bool(lim) and lim >= floor, lim)
    print(f"{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


main()
