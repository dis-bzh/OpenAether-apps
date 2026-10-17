#!/usr/bin/env python3
"""The Longhorn CiliumNetworkPolicy lets the managers reach each other's webhooks (static, no cluster).

Lab 11 counted 66 denied flows, longhorn-system to longhorn-system on TCP 9501 and 9502: the policy description
claimed those ports, but the in-namespace rules listed only 9500 and 8500-8503. Cilium needs the port on BOTH the egress of
the caller and the ingress of the callee, so both in-namespace rules must carry every port the description names.
Rung: mocked/static.   Usage: python3 scripts/dev/test-longhorn-netpol.py   (needs PyYAML)
"""
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
# 9503: share-manager -> manager recovery backend (Longhorn 1.12); 9600: manager -> share-manager gRPC; 10000-30000: instance-manager replica/engine data path
# between nodes. Lab 12 saw both denied: RWX never mounted, 2-replica volumes stayed degraded.
NEED = {"9500", "9501", "9502", "9503", "9600", "8500", "8501", "8502", "8503", "10000-30000"}
pol = next(d for d in yaml.safe_load_all((REPO / "apps/base/storage/networkpolicy.yaml").read_text())
           if d and d["kind"] == "CiliumNetworkPolicy" and d["metadata"]["name"] == "longhorn")
fails = 0


def ports_of(rules, key):
    """Ports of the rule that selects every endpoint of the namespace (matchLabels {})."""
    for r in rules:
        if r.get(key) == [{"matchLabels": {}}] and r["toPorts"][0]["ports"]:
            return {(p["port"] + "-" + str(p["endPort"])) if "endPort" in p else p["port"] for tp in r["toPorts"] for p in tp["ports"]} - {"3260"}
    return set()


for direction, rules, key in (("ingress", pol["spec"]["ingress"], "fromEndpoints"), ("egress", pol["spec"]["egress"], "toEndpoints")):
    got = ports_of(rules, key)
    ok = NEED <= got
    print(("ok   " if ok else "FAIL ") + f"in-namespace {direction} carries {sorted(NEED)}" + ("" if ok else f" (missing {sorted(NEED - got)})"))
    fails += not ok
# RWX: the NFS client is the node's kernel: the share-manager must accept 2049 from the host and remote-node entities (lab 13).
nfs = any({"host", "remote-node"} <= set(r.get("fromEntities", [])) and
          any(p["port"] == "2049" for tp in r.get("toPorts", []) for p in tp["ports"]) for r in pol["spec"]["ingress"])
print(("ok   " if nfs else "FAIL ") + "ingress accepts NFS 2049 from host + remote-node")
fails += not nfs
sys.exit(1 if fails else 0)
