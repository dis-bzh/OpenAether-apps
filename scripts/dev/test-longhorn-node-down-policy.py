#!/usr/bin/env python3
"""Longhorn `node-down-pod-deletion-policy` is delivered to fresh AND upgraded clusters (lab 27 sitting 3, owner decision 2026-10-27).

The default `do-nothing` kept the forge away 341-372 s (the replacement pod waits for the volume to detach from the lost node);
`delete-both-statefulset-and-deployment-pod` gave 128 s in one lab. The chart's `defaultSettings` reach a fresh install; an upgraded
0.3.x cluster gets the value from a Flux-managed Setting object (the object IS the setting). This test pins, offline:
  (a) the Setting object: group/kind, name, namespace, value;
  (b) the HelmRelease defaultSettings carries the SAME value (so the two never fight over an existing object);
  (c) the Kustomization storage-settings: path, wait, dependsOn storage (the CRD comes from the chart), in the base DAG, rendered in
      the management profile with that path;
  (d) the brick is a companion of storage: kept when storage is picked, removed when it is not;
  (e) the object is not in apps/base/storage itself (its first apply would fail on the missing CRD).
Rung: mocked/static. Usage: python3 -I scripts/dev/test-longhorn-node-down-policy.py   (needs PyYAML, kubectl)
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WANT = "delete-both-statefulset-and-deployment-pod"
fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    fails += not cond


def load(p):
    return [d for d in yaml.safe_load_all((REPO / p).read_text()) if d]


# (a)
sdir = REPO / "apps/base/storage/settings"
kust = load("apps/base/storage/settings/kustomization.yaml")[0]
settings = [d for f in kust["resources"] for d in load(f"apps/base/storage/settings/{f}")]
s = next((d for d in settings if d.get("kind") == "Setting" and d["metadata"]["name"] == "node-down-pod-deletion-policy"), None)
check(s is not None, "a Setting node-down-pod-deletion-policy is rendered by apps/base/storage/settings")
if s:
    check(s["apiVersion"] == "longhorn.io/v1beta2", "apiVersion longhorn.io/v1beta2")
    check(s["metadata"]["namespace"] == "longhorn-system", "namespace longhorn-system")
    check(s.get("value") == WANT, f"value is {WANT}")
    check("spec" not in s, "no spec: a Setting carries its value at the top level (a spec would be dropped)")

# (b)
hr = next(d for d in load("apps/base/storage/longhorn.yaml") if d["kind"] == "HelmRelease")
dv = hr["spec"]["values"]["defaultSettings"].get("nodeDownPodDeletionPolicy")
check(dv == WANT and s and dv == s.get("value"), "the HelmRelease defaultSettings.nodeDownPodDeletionPolicy equals the Setting value")

# (e)
base = [d for f in yaml.safe_load((REPO / "apps/base/storage/kustomization.yaml").read_text())["resources"]
        if f.endswith(".yaml") for d in load(f"apps/base/storage/{f}")]
check(not any(d.get("kind") == "Setting" for d in base), "no Setting in apps/base/storage (the CRD is created by its HelmRelease)")

# (c)
ks = next((d for d in load("apps/flux/base/21d-longhorn-settings.yaml") if d["kind"] == "Kustomization"), None)
check(ks is not None and ks["metadata"]["name"] == "storage-settings", "Kustomization storage-settings exists")
if ks:
    check(ks["spec"]["path"] == "./apps/base/storage/settings" and sdir.is_dir(), "its path is apps/base/storage/settings, which exists")
    check([x["name"] for x in ks["spec"].get("dependsOn", [])] == ["storage"], "it depends on storage (the Setting CRD)")
    check(ks["spec"].get("wait") is True and ks["spec"].get("prune") is True, "wait and prune are on")
check("21d-longhorn-settings.yaml" in yaml.safe_load((REPO / "apps/flux/base/kustomization.yaml").read_text())["resources"],
      "the file is in the base DAG")
out = subprocess.run(["kubectl", "kustomize", str(REPO / "apps/flux/management")], capture_output=True, text=True)
rendered = [d for d in yaml.safe_load_all(out.stdout) if d] if out.returncode == 0 else []
r = next((d for d in rendered if d.get("kind") == "Kustomization" and d["metadata"]["name"] == "storage-settings"), None)
check(r is not None and r["spec"]["path"] == "./apps/base/storage/settings", "the management profile renders storage-settings with that path")

# (d)
def profile_names(brick):
    with tempfile.TemporaryDirectory() as t:
        subprocess.run([sys.executable, "-I", str(REPO / "scripts/pick.py"), brick, "-o", t], check=True, capture_output=True, text=True)
        k = yaml.safe_load((Path(t) / "kustomization.yaml").read_text())
    return {yaml.safe_load(p["patch"])["metadata"]["name"] for p in k.get("patches", []) if "$patch: delete" in p["patch"]}


check("storage-settings" not in profile_names("storage"), "picking storage keeps storage-settings (companion)")
check("storage-settings" in profile_names("cnpg"), "a profile without storage drops storage-settings")
sys.exit(1 if fails else 0)
