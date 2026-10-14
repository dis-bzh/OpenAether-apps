#!/usr/bin/env bash
# Pins the CiliumLoadBalancerIPPool apiVersion to cilium.io/v2 (#49): from Cilium 1.19 the API server warns on every
# apply of v2alpha1. The L2 policy has no v2 type and must stay v2alpha1, so only the pool kind is checked.
# Static render only. REPO_ROOT overrides the tree (used by the mutation checks).
set -euo pipefail
root="${REPO_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
base="$(kubectl kustomize "$root/apps/base/platform/cilium-lb-ipam")"
local_="$(kubectl kustomize "$root/apps/base/platform/cilium-lb-ipam-local")"

BASE="$base" LOCAL="$local_" ROOT="$root" python3 -I - <<'PY'
import os, pathlib, sys
import yaml

def pools(text):
    return [d for d in yaml.safe_load_all(text) if d and d.get("kind") == "CiliumLoadBalancerIPPool"]

def blocks(pool):
    return [(b["start"], b["stop"]) for b in pool["spec"]["blocks"]]

errs = []
for label, text, want in (("base", os.environ["BASE"], "172.16.12."), ("local", os.environ["LOCAL"], "10.5.0.")):
    found = [p for p in pools(text) if p["metadata"]["name"] == "private-vpc"]
    if len(found) != 1:
        errs.append(f"{label}: expected one pool private-vpc, found {len(found)}")
        continue
    if found[0]["apiVersion"] != "cilium.io/v2":
        errs.append(f"{label}: private-vpc is {found[0]['apiVersion']}, want cilium.io/v2")
    if not all(s.startswith(want) and e.startswith(want) for s, e in blocks(found[0])):
        errs.append(f"{label}: blocks {blocks(found[0])} do not sit in {want}* (overlay patch not applied?)")

# Any pool manifest in the tree, rendered or not, must not be on an alpha apiVersion.
for f in pathlib.Path(os.environ["ROOT"], "apps").rglob("*.yaml"):
    for d in yaml.safe_load_all(f.read_text()):
        if isinstance(d, dict) and d.get("kind") == "CiliumLoadBalancerIPPool" and "alpha" in d.get("apiVersion", ""):
            errs.append(f"{f}: CiliumLoadBalancerIPPool on {d['apiVersion']}")

if errs:
    print("\n".join("FAIL " + e for e in errs)); sys.exit(1)
print("ok: CiliumLoadBalancerIPPool is cilium.io/v2 in base and local renders")
PY
