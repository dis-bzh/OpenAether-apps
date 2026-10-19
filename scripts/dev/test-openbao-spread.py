#!/usr/bin/env python3
"""The OpenBao replicas must spread over the workers (#129; lab 15a: all three pods and their local-path volumes sat on one worker).

Static: it proves the manifest, not the scheduler. Required: a hostname constraint that is DoNotSchedule with maxSkew 1, that counts
registered-but-not-Ready workers (nodeTaintsPolicy Ignore) and leaves the control planes out of the count (node affinity plus
nodeAffinityPolicy Honor), selecting the pods of the StatefulSet itself. `--selftest` mutates the manifest and requires a failure.
"""
import copy
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "apps/base/foundation/vault/statefulset.yaml"


def check(sts):
    bad = []
    spec = sts["spec"]["template"]["spec"]
    labels = sts["spec"]["template"]["metadata"]["labels"]
    host = [c for c in spec.get("topologySpreadConstraints", []) if c.get("topologyKey") == "kubernetes.io/hostname"]
    if len(host) != 1:
        return ["exactly one hostname topologySpreadConstraint expected, found %d" % len(host)]
    c = host[0]
    if c.get("whenUnsatisfiable") != "DoNotSchedule":
        bad.append("hostname constraint is %s, not DoNotSchedule" % c.get("whenUnsatisfiable"))
    if c.get("maxSkew") != 1:
        bad.append("maxSkew is %s, not 1" % c.get("maxSkew"))
    if c.get("nodeTaintsPolicy") != "Ignore":
        bad.append("nodeTaintsPolicy must be Ignore (a not-Ready worker must count as a domain)")
    if c.get("nodeAffinityPolicy", "Honor") != "Honor":
        bad.append("nodeAffinityPolicy must be Honor (the control planes are left out by the node affinity)")
    sel = c.get("labelSelector", {}).get("matchLabels", {})
    if not sel or any(labels.get(k) != v for k, v in sel.items()):
        bad.append("the constraint selector does not select the StatefulSet's own pods")
    terms = (spec.get("affinity", {}).get("nodeAffinity", {})
             .get("requiredDuringSchedulingIgnoredDuringExecution", {}).get("nodeSelectorTerms", []))
    excl = any(e.get("key") == "node-role.kubernetes.io/control-plane" and e.get("operator") == "DoesNotExist"
               for t in terms for e in t.get("matchExpressions", []))
    if not excl:
        bad.append("no required node affinity excluding the control planes")
    return bad


def main():
    sts = next(d for d in yaml.safe_load_all(SRC.read_text()) if d and d["kind"] == "StatefulSet")
    if "--selftest" in sys.argv:
        rc = 0
        def mut(name, fn):
            nonlocal rc
            m = copy.deepcopy(sts); fn(m["spec"]["template"]["spec"])
            ok = bool(check(m)); print("selftest: %s %s" % (name, "detected" if ok else "NOT detected")); rc |= (not ok)
        host = lambda s: next(c for c in s["topologySpreadConstraints"] if c["topologyKey"] == "kubernetes.io/hostname")
        mut("ScheduleAnyway", lambda s: host(s).update(whenUnsatisfiable="ScheduleAnyway"))
        mut("taints Honor", lambda s: host(s).update(nodeTaintsPolicy="Honor"))
        mut("no control-plane exclusion", lambda s: s.pop("affinity"))
        mut("no hostname constraint", lambda s: s.update(topologySpreadConstraints=[]))
        return rc
    bad = check(sts)
    for b in bad:
        print("FAIL", b)
    print("openbao spread: %s" % ("FAIL" if bad else "ok"))
    return 1 if bad else 0


sys.exit(main())
