#!/usr/bin/env python3
"""The Longhorn HelmRelease values are keys the pinned chart reads, and its remediation is the deliberate one (apps #115, #134).

(a) Every `defaultSettings` key must be one the chart's default-setting template reads: a key it does not know is dropped in
    silence (orphanAutoDeletion was, so the comment above it promised a behaviour that never existed).
(b) install and upgrade remediation stay off, and `deletingConfirmationFlag` is not set: Longhorn's pre-delete uninstall Job
    refuses to run while the flag is false, so Helm's install remediation (an uninstall) would strand the release instead of
    retrying it; and with the flag true it could delete data. A failure is read and reset by a person (docs/patching.md).
(c) The three snapshot.storage.k8s.io CRDs are shipped (the sidecar needs them; no snapshot-controller, apps #137).
Needs `helm` and network for the chart (the same as test-loki-render.sh). Rung: mocked/static.
Usage: python3 -I scripts/dev/test-longhorn-values.py   (needs PyYAML)
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
docs = list(yaml.safe_load_all((REPO / "apps/base/storage/longhorn.yaml").read_text()))
repos = {d["metadata"]["name"]: d["spec"]["url"] for d in docs if d and d["kind"] == "HelmRepository"}
hr = next(d for d in docs if d and d["kind"] == "HelmRelease" and d["metadata"]["name"] == "longhorn")
spec = hr["spec"]
fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    fails += not cond


with tempfile.TemporaryDirectory() as tmp:
    ch = spec["chart"]["spec"]
    subprocess.run(["helm", "pull", ch["chart"], "--repo", repos[ch["sourceRef"]["name"]], "--version", ch["version"],
                    "--untar", "--untardir", tmp], check=True, capture_output=True)
    tpl = (Path(tmp) / ch["chart"] / "templates/default-setting.yaml").read_text()
known = set(re.findall(r"\.Values\.defaultSettings\.(\w+)", tpl))
for key in spec["values"].get("defaultSettings", {}):
    check(key in known, f"defaultSettings.{key} is read by chart {ch['version']}")

for phase in ("install", "upgrade"):
    rem = spec.get(phase, {}).get("remediation", {})
    check(rem.get("retries") == 0, f"{phase}.remediation.retries is 0 (an uninstall remediation is refused by the chart's pre-delete Job)")
check(spec["upgrade"]["remediation"].get("remediateLastFailure") is False, "upgrade.remediateLastFailure is false")
check("deletingConfirmationFlag" not in spec["values"].get("defaultSettings", {}), "deletingConfirmationFlag is not set (the uninstall Job needs a person to set it)")

# (c) The chart cannot switch the csi-snapshotter sidecar off (apps #137): it lists the snapshot.storage.k8s.io resources and
#     logged an error every ~16 s while they were absent. The storage brick must therefore ship the three CRDs.
kust = yaml.safe_load((REPO / "apps/base/storage/kustomization.yaml").read_text())
check("external-snapshotter-crds.yaml" in kust["resources"], "the storage brick lists the external-snapshotter CRDs (apps #137)")
crds = REPO / "apps/base/storage/external-snapshotter-crds.yaml"
names = {d["metadata"]["name"] for d in yaml.safe_load_all(crds.read_text()) if d} if crds.exists() else set()
for plural in ("volumesnapshotclasses", "volumesnapshotcontents", "volumesnapshots"):
    check(f"{plural}.snapshot.storage.k8s.io" in names, f"CRD {plural}.snapshot.storage.k8s.io is shipped")
sys.exit(1 if fails else 0)
