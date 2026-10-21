#!/usr/bin/env python3
"""Unit tests of scripts/check-ha.py on synthetic manifests (no network): each rule must catch the regression it exists for,
and the real allow-list must be well formed. The audit of the real platform is `python3 scripts/check-ha.py` (needs helm).
Usage: python3 -I scripts/dev/test-check-ha.py
"""
import copy
import importlib.util
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("check_ha", REPO / "scripts/check-ha.py")
ha = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ha)
fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    fails += not cond


L = {"app": "web"}


def dep(replicas=2, **pod):
    spec_ = {"containers": [{"name": "c", "resources": {"requests": {"cpu": "10m"}}}],
             "topologySpreadConstraints": [{"maxSkew": 1, "topologyKey": ha.HOST, "whenUnsatisfiable": "ScheduleAnyway",
                                            "labelSelector": {"matchLabels": L}}]}
    spec_.update(pod)
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": "ns"},
            "spec": {"replicas": replicas, "template": {"metadata": {"labels": L}, "spec": spec_}}}


def pdb(**s):
    return {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": {"name": "web", "namespace": "ns"},
            "spec": dict({"selector": {"matchLabels": L}}, **s)}


def errs(docs, **allow):
    e, _ = ha.audit(docs, allow)
    return e


good = [dep(), pdb(maxUnavailable=1)]
check(errs(good) == [], "a spread two-replica Deployment with a one-disruption budget passes")
check(errs([dep(topologySpreadConstraints=[])]) != [], "no spread is caught")
check(any("selects its own pods" in e for e in errs([dep(topologySpreadConstraints=[
    {"topologyKey": ha.HOST, "whenUnsatisfiable": "ScheduleAnyway", "labelSelector": {"matchLabels": {"app": "other"}}}]),
    pdb(maxUnavailable=1)])), "a spread constraint that selects other pods is caught")
check(errs([dep(topologySpreadConstraints=[], affinity={"podAntiAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": [
    {"weight": 1, "podAffinityTerm": {"topologyKey": ha.HOST, "labelSelector": {"matchLabels": L}}}]}}), pdb(maxUnavailable=1)]) == [],
      "a hostname anti-affinity counts as spread")
check(any("exactly one is needed" in e for e in errs([dep()])), "no PodDisruptionBudget is caught")
check(any("allows 0" in e for e in errs([dep(), pdb(minAvailable=2)])), "a budget that allows zero disruptions is caught")
check(any("allows 2" in e for e in errs([dep(3), pdb(minAvailable=1)])), "a budget that allows two of three is caught")
check(errs([dep(3), pdb(minAvailable=2)]) == [], "minAvailable replicas-1 passes")
check(errs([dep(), pdb(maxUnavailable="50%")]) == [], "a percentage that rounds to one passes")
check(any("2 PodDisruptionBudget" in e or "exactly one" in e for e in errs([dep(), pdb(maxUnavailable=1), pdb(minAvailable=1)])),
      "two budgets on the same pods are caught")
check(any("single_replica" in e for e in errs([dep(1)])), "a single replica outside the allow-list is caught")
one = {"single_replica": [{"workload": "Deployment/ns/web", "reason": "r", "loss": "l"}]}
check(errs([dep(1)], **one) == [], "a listed single replica passes")
check(any("stale" in e for e in errs(good, **one)), "a listed workload that has 2 replicas is a stale entry")
check(any("no reason" in e for e in errs([dep(1)], single_replica=[{"workload": "Deployment/ns/web", "loss": "l"}])),
      "an entry without a reason is caught")
check(any("loss" in e for e in errs([dep(1)], single_replica=[{"workload": "Deployment/ns/web", "reason": "r"}])),
      "an entry without a loss is caught")
check(any("names no workload" in e for e in errs(good, single_replica=[{"workload": "Deployment/ns/gone", "reason": "r", "loss": "l"}])),
      "an entry for a workload that no longer exists is caught")
check(any("required podAffinity" in e for e in errs([dep(affinity={"podAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{}]}}),
                                                     pdb(maxUnavailable=1)])), "a required podAffinity is caught")
hard = dep(3, topologySpreadConstraints=[{"maxSkew": 1, "topologyKey": ha.HOST, "whenUnsatisfiable": "DoNotSchedule",
                                          "labelSelector": {"matchLabels": L}}])
check(any("strand" in e for e in errs([hard, pdb(minAvailable=2)])), "a hard hostname spread over 3 replicas is caught")
check(errs([hard, pdb(minAvailable=2)], strict_spread=[{"workload": "Deployment/ns/web", "reason": "raft"}]) == [],
      "...unless listed under strict_spread with a reason")
check(any("nodeSelector" in e for e in errs([dep(nodeSelector={"disk": "ssd"}), pdb(maxUnavailable=1)])), "a nodeSelector that pins nodes is caught")
check(errs([dep(nodeSelector={"kubernetes.io/os": "linux"}), pdb(maxUnavailable=1)]) == [], "the OS nodeSelector is fine")
hpa = {"kind": "HorizontalPodAutoscaler", "metadata": {"name": "web", "namespace": "ns"},
       "spec": {"scaleTargetRef": {"kind": "Deployment", "name": "web"}, "minReplicas": 2}}
check(errs([dep(1), hpa, pdb(maxUnavailable=1)]) == [], "an HPA minReplicas of 2 counts as two replicas")
px = {"pdb_exception": [{"workload": "Deployment/ns/web", "reason": "r"}]}
check(errs([dep(3), pdb(minAvailable=1)], **px) == [], "a budget that allows two is tolerated when listed under pdb_exception")
check(any("pdb_exception" in e for e in errs([dep(3), pdb(minAvailable=2)], **px)), "...and the entry goes stale once the budget is right")
expr = {"matchExpressions": [{"key": "app", "operator": "In", "values": ["web"]}]}
check(errs([dep(topologySpreadConstraints=[{"topologyKey": ha.HOST, "labelSelector": expr}]), pdb(maxUnavailable=1)]) == [],
      "a matchExpressions selector that selects the pods counts")
expr2 = {"matchExpressions": [{"key": "app", "operator": "NotIn", "values": ["web"]}]}
check(errs([dep(topologySpreadConstraints=[{"topologyKey": ha.HOST, "labelSelector": expr2}]), pdb(maxUnavailable=1)]) != [],
      "a matchExpressions selector that excludes the pods does not")
# known gaps: tolerated while listed, an error once fixed
gap = {"known_gaps": [{"workload": "Deployment/ns/web", "reason": "r"}]}
check(errs([dep(topologySpreadConstraints=[]), pdb(maxUnavailable=1)], **gap) == [], "a listed gap is tolerated")
check(any("compliant but still listed" in e for e in errs(good, **gap)), "a fixed workload must leave known_gaps")
# operator-made pods
vmc = {"kind": "VMAlertmanager", "metadata": {"name": "am", "namespace": "ns"},
       "spec": {"replicaCount": 2, "affinity": {"podAntiAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": [
           {"weight": 1, "podAffinityTerm": {"topologyKey": ha.HOST, "labelSelector": {"matchLabels": {"app.kubernetes.io/name": "vmalertmanager"}}}}]}},
                "podDisruptionBudget": {"minAvailable": 1}}}
check(errs([vmc]) == [], "a VM custom resource with spread and a budget passes")
bad = copy.deepcopy(vmc); bad["spec"].pop("podDisruptionBudget")
check(any("no podDisruptionBudget" in e for e in errs([bad])), "a VM custom resource without a budget is caught")
cn = {"apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster", "metadata": {"name": "db", "namespace": "ns"},
      "spec": {"instances": 2, "affinity": {"podAntiAffinityType": "preferred", "topologyKey": ha.HOST}}}
check(errs([cn]) == [], "a CNPG Cluster with two instances passes (the operator makes the budget and the spread)")
cn2 = copy.deepcopy(cn); cn2["spec"]["enablePDB"] = False
check(errs([cn2]) != [], "a CNPG Cluster with enablePDB false is caught")
cn3 = copy.deepcopy(cn); cn3["spec"]["affinity"] = {"enablePodAntiAffinity": False}
check(errs([cn3]) != [], "a CNPG Cluster without anti-affinity is caught")
# the real allow-list
real = yaml.safe_load((REPO / "scripts/ha-allowlist.yaml").read_text())
check(all(str(e.get("reason", "")).strip() for k in ("single_replica", "strict_spread", "known_gaps") for e in real.get(k) or []),
      "every entry of scripts/ha-allowlist.yaml has a reason")
check(all(str(e.get("loss", "")).strip() for e in real["single_replica"]), "every single_replica entry says what its loss means")
check(not set(e["workload"] for e in real["single_replica"]) & set(e["workload"] for e in real.get("known_gaps") or []),
      "no workload is both single_replica and a known gap")
print("%d failed" % fails)
sys.exit(1 if fails else 0)
