#!/usr/bin/env python3
"""Every container of the two Alloy DaemonSets has a memory limit, on what the chart really renders.

Lab 11: a `resources:` block at the top of the HelmRelease values is ignored by the Alloy chart (it reads `alloy.resources`
and `configReloader.resources`), so the workers' Alloy ran at 340 Mi with no limit. Renders each HelmRelease with the shipped
values (`helm template`) and fails if a container has no memory limit, no memory request, or a limit under the 380 Mi seen on
the busiest worker (a limit below real use is an OOM loop; checked for alloy.yaml only, alloy-audit reads one file).
Needs helm, kubectl, PyYAML and network for the charts. Rung: mocked/static.   Usage: python3 scripts/dev/test-alloy-resources.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
BRICK = REPO / "apps/base/observability"
MI = {"Mi": 1, "Gi": 1024}
PASS = FAIL = 0


def check(desc, ok, detail=""):
    global PASS, FAIL
    PASS += bool(ok)
    FAIL += not ok
    print(("  ok   " if ok else "  FAIL ") + desc + ("" if ok else f" -> {detail}"))


def mi(q):
    return int(q[:-2]) * MI[q[-2:]] if q and q[-2:] in MI else None


def main():
    for release_file, floor in (("alloy.yaml", 380), ("alloy-audit.yaml", 0)):
        hr = next(d for d in yaml.safe_load_all((BRICK / release_file).read_text()) if d and d["kind"] == "HelmRelease")
        spec = hr["spec"]["chart"]["spec"]
        repo = next(d["spec"]["url"] for f in BRICK.glob("*.yaml") for d in yaml.safe_load_all(f.read_text())
                    if d and d.get("kind") == "HelmRepository" and d["metadata"]["name"] == spec["sourceRef"]["name"])
        with tempfile.TemporaryDirectory() as tmp:
            values = Path(tmp) / "values.yaml"
            values.write_text(yaml.safe_dump(hr["spec"].get("values", {})))
            r = subprocess.run(["helm", "template", hr["metadata"]["name"], spec["chart"], "--repo", repo, "--version", spec["version"],
                                "-n", hr["metadata"]["namespace"], "-f", str(values)], capture_output=True, text=True)
        if r.returncode:
            check(f"{release_file}: helm template", False, r.stderr.strip()[-200:])
            continue
        ds = [d for d in yaml.safe_load_all(r.stdout) if d and d["kind"] == "DaemonSet"]
        check(f"{release_file}: renders one DaemonSet", len(ds) == 1, len(ds))
        for c in ds[0]["spec"]["template"]["spec"]["containers"]:
            res = c.get("resources") or {}
            lim, req = mi(res.get("limits", {}).get("memory")), mi(res.get("requests", {}).get("memory"))
            check(f"{release_file}: container {c['name']} has a memory limit and a request", lim and req and lim >= req, res)
            if c["name"] == "alloy" and floor:
                check(f"{release_file}: the alloy limit is at least {floor} Mi (the busiest worker uses about 350 Mi)",
                      bool(lim) and lim >= floor, lim)
    print(f"{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


main()
