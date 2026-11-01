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
# zone coverage (apps #155): a warning list, never an error
zsp = {"maxSkew": 1, "topologyKey": ha.ZONE, "whenUnsatisfiable": "ScheduleAnyway", "labelSelector": {"matchLabels": L}}
check(ha.zone_gaps([dep()]) == ["Deployment/ns/web"], "a two-replica workload with a hostname spread only is a zone gap")
check(ha.zone_gaps([dep(topologySpreadConstraints=[dict(zsp, topologyKey=ha.HOST), zsp])]) == [], "a zone spread constraint covers the zones")
check(ha.zone_gaps([dep(1)]) == [], "a single replica has no zone gap")
check(errs([dep(), pdb(maxUnavailable=1)]) == [], "a zone gap is not an audit error")
cz = copy.deepcopy(cn); cz["spec"]["affinity"]["additionalPodAntiAffinity"] = {"preferredDuringSchedulingIgnoredDuringExecution": [
    {"weight": 100, "podAffinityTerm": {"topologyKey": ha.ZONE, "labelSelector": {"matchLabels": {"cnpg.io/cluster": "db"}}}}]}
check(ha.zone_gaps([cn]) == ["Cluster/ns/db"] and ha.zone_gaps([cz]) == [], "a CNPG Cluster is covered by its zone anti-affinity term")
# the CPU of the CNPG instance pods (a Cluster is no Deployment): instances x (instance + Barman sidecar), 0.4.0 synchronous replication
cs = {"apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster", "metadata": {"name": "db", "namespace": "ns"},
      "spec": {"instances": 3, "resources": {"requests": {"cpu": "50m"}},
               "plugins": [{"name": "barman-cloud.cloudnative-pg.io", "parameters": {"barmanObjectName": "db-primary"}}]}}
st = {"apiVersion": "barmancloud.cnpg.io/v1", "kind": "ObjectStore", "metadata": {"name": "db-primary", "namespace": "ns"},
      "spec": {"instanceSidecarConfiguration": {"resources": {"requests": {"cpu": "50m"}}}}}
check(ha.cnpg_cpu([cs, st])[0] == 300, "CNPG instance CPU counts the sidecar of the ObjectStore: 3 x (50 + 50) m")
check(ha.cnpg_cpu([cs])[0] == 150, "a Cluster without its ObjectStore counts the instance only")
# the default install keeps its budgets; only the render with the opt-in Forgejo brick has the higher CNPG one, and an allow-list entry of
# an opt-in workload may name nothing in the default render only
check(ha.CNPG_CPU_BUDGET_M == 600 and ha.CPU_BUDGET_M == 3300, "the default budgets are 600 m (CNPG) and 3300 m (Deployments)")
check(ha.CNPG_CPU_BUDGET_FORGE_M == 900, "the render with the forge has its own CNPG budget, 900 m")
oi = {"single_replica": [{"workload": "Deployment/ns/gone", "reason": "r", "loss": "l", "opt_in": "forge"}]}
check(ha.audit(good, oi, opt_in_absent=True)[0] == [] and ha.audit(good, oi)[0] != [],
      "an opt_in allow-list entry may be absent from the default render only")
# the database profiles (apps #157): the shipped examples pass the audit, and the audit sees a Pooler
pl = {"apiVersion": "postgresql.cnpg.io/v1", "kind": "Pooler", "metadata": {"name": "db-pooler-rw", "namespace": "ns"},
      "spec": {"instances": 2, "cluster": {"name": "db"}, "template": {"spec": {
          "containers": [{"name": "pgbouncer", "resources": {"requests": {"cpu": "100m"}}}],
          "topologySpreadConstraints": [{"maxSkew": 1, "topologyKey": ha.HOST, "whenUnsatisfiable": "ScheduleAnyway",
                                         "labelSelector": {"matchLabels": {"cnpg.io/poolerName": "db-pooler-rw"}}}]}}}}
ppdb = {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": {"name": "db-pooler-rw", "namespace": "ns"},
        "spec": {"maxUnavailable": 1, "selector": {"matchLabels": {"cnpg.io/poolerName": "db-pooler-rw"}}}}
check(errs([pl, ppdb]) == [], "a two-pod Pooler with a spread and a one-disruption budget passes")
check(errs([pl]) != [], "a Pooler without a budget is caught")
pl2 = copy.deepcopy(pl); pl2["spec"]["template"]["spec"]["topologySpreadConstraints"] = []
check(errs([pl2, ppdb]) != [], "a Pooler without a spread is caught")
check(ha.profile_cpu([pl, ppdb]) == 200, "profile CPU counts the pooler pods: 2 x 100 m")
pe, plines = ha.audit_profiles(__import__("ha_render").kustomize)
check(pe == [] and len(plines) == 3, "the shipped light, standard and ha examples pass the HA audit within their CPU budgets %s" % pe)
# critical_path pins (0.4.0 critical-path audit): replicas and a zone term, as errors
zone_c = {"maxSkew": 1, "topologyKey": ha.ZONE, "whenUnsatisfiable": "ScheduleAnyway", "labelSelector": {"matchLabels": L}}
host_c = {"maxSkew": 1, "topologyKey": ha.HOST, "whenUnsatisfiable": "ScheduleAnyway", "labelSelector": {"matchLabels": L}}
cp = {"critical_path": [{"workload": "Deployment/ns/web", "min_replicas": 2, "why": "w"}]}
zoned = [dep(topologySpreadConstraints=[host_c, zone_c]), pdb(maxUnavailable=1)]
check(errs(zoned, **cp) == [], "a critical-path workload with 2 replicas, a host and a zone term passes")
check(any("no zone spread" in e for e in errs(good, **cp)), "a critical-path workload without a zone term is caught (a warning for the others)")
check(errs(good) == [], "...and the same workload is not an error when it is not pinned")
check(any("the pin is 3" in e for e in errs(zoned, critical_path=[{"workload": "Deployment/ns/web", "min_replicas": 3, "why": "w"}])),
      "a critical-path workload below its pinned replicas is caught")
check(any("the pin is 2" in e for e in errs([dep(1)], critical_path=cp["critical_path"], single_replica=[
    {"workload": "Deployment/ns/web", "reason": "r", "loss": "l"}])), "moving a pinned workload to single_replica is caught")
check(any("names no workload" in e for e in errs(zoned, critical_path=[{"workload": "Deployment/ns/gone", "min_replicas": 2, "why": "w"}])),
      "a pin on a workload that no longer exists is caught")
check(any("no why" in e for e in errs(zoned, critical_path=[{"workload": "Deployment/ns/web", "min_replicas": 2}])), "a pin without a why is caught")
# the real allow-list
real = yaml.safe_load((REPO / "scripts/ha-allowlist.yaml").read_text())
check(all(str(e.get("reason", "")).strip() for k in ("single_replica", "strict_spread", "known_gaps") for e in real.get(k) or []),
      "every entry of scripts/ha-allowlist.yaml has a reason")
check(all(str(e.get("loss", "")).strip() for e in real["single_replica"]), "every single_replica entry says what its loss means")
check(not set(e["workload"] for e in real["single_replica"]) & set(e["workload"] for e in real.get("known_gaps") or []),
      "no workload is both single_replica and a known gap")
check(all(str(e.get("why", "")).strip() and e["min_replicas"] >= 2 for e in real["critical_path"]),
      "every critical_path pin has a why and at least 2 replicas")
need = ("kyverno-admission-controller", "istiod", "cnpg-controller-manager", "barman-cloud", "openbao", "zitadel-db", "cert-manager-webhook",
        "external-secrets-webhook", "metrics-server", "loki", "alertmanager")
check(all(any(n in e["workload"] for e in real["critical_path"]) for n in need), "the pins cover the admission, mesh, database, secrets and alert paths")
print("%d failed" % fails)
sys.exit(1 if fails else 0)
