#!/usr/bin/env python3
"""VictoriaMetrics images are pinned in the custom resources, and the operator values are keys its chart reads (apps #117).

(a) VMCluster (vminsert, vmselect, vmstorage), VMAgent, VMAlert and VMAlertmanager carry `image.tag`. Unset, the operator picks its
    own default, so an operator bump (a Renovate PR that looks harmless) would silently move the metrics storage engine too.
    The cluster components use the `-cluster` build of the same release as vmagent and vmalert.
(b) Every top-level key of the victoria-metrics-operator HelmRelease values exists in the pinned chart's values.yaml (the old
    probePath / livenessProbe / readinessProbe were dropped in silence).
Needs `helm` and network for the chart (like test-loki-render.sh). Rung: mocked/static.
Usage: python3 -I scripts/dev/test-vm-pins.py   (needs PyYAML)
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
OBS = REPO / "apps/base/observability"
fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    fails += not cond


crs = [d for f in (OBS / "vm-customresources").glob("*.yaml") for d in yaml.safe_load_all(f.read_text()) if d]
by_kind = {k: [d for d in crs if d["kind"] == k] for k in ("VMCluster", "VMAgent", "VMAlert", "VMAlertmanager")}
for k, v in by_kind.items():
    check(len(v) == 1, f"one {k}")

metrics = set()
if by_kind["VMCluster"]:
    for comp in ("vminsert", "vmselect", "vmstorage"):
        tag = by_kind["VMCluster"][0]["spec"][comp].get("image", {}).get("tag", "")
        check(re.fullmatch(r"v\d+\.\d+\.\d+-cluster", tag) is not None, f"VMCluster {comp} image.tag is a pinned -cluster release ({tag or 'unset'})")
        metrics.add(tag.removesuffix("-cluster"))
for k in ("VMAgent", "VMAlert"):
    for d in by_kind[k]:
        tag = d["spec"].get("image", {}).get("tag", "")
        check(re.fullmatch(r"v\d+\.\d+\.\d+", tag) is not None, f"{k} image.tag is a pinned release ({tag or 'unset'})")
        metrics.add(tag)
check(len(metrics) == 1, f"VMCluster, VMAgent and VMAlert are on one VictoriaMetrics release ({sorted(metrics)})")
for d in by_kind["VMAlertmanager"]:
    tag = d["spec"].get("image", {}).get("tag", "")
    check(re.fullmatch(r"v\d+\.\d+\.\d+", tag) is not None, f"VMAlertmanager image.tag is a pinned release ({tag or 'unset'})")

docs = list(yaml.safe_load_all((OBS / "vm-operator.yaml").read_text()))
repos = {d["metadata"]["name"]: d["spec"]["url"] for d in docs if d and d["kind"] == "HelmRepository"}
hr = next(d for d in docs if d and d["kind"] == "HelmRelease")
ch = hr["spec"]["chart"]["spec"]
with tempfile.TemporaryDirectory() as tmp:
    subprocess.run(["helm", "pull", ch["chart"], "--repo", repos[ch["sourceRef"]["name"]], "--version", ch["version"],
                    "--untar", "--untardir", tmp], check=True, capture_output=True)
    chart_values = yaml.safe_load((Path(tmp) / ch["chart"] / "values.yaml").read_text())
for key in hr["spec"]["values"]:
    check(key in chart_values, f"operator values.{key} is a key of chart {ch['version']}")
sys.exit(1 if fails else 0)
