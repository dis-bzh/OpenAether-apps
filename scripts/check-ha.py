#!/usr/bin/env python3
"""HA audit of the platform bricks (0.4.0 M4): can a worker be lost, or drained, without a customer-visible interruption?

Renders every brick of the Flux DAG (kustomize, then every HelmRelease through `helm template` with its values and
post-renderer patches), and for each Deployment and StatefulSet (and the pods an operator makes from a VMCluster, VMAgent,
VMAlert, VMAlertmanager or CNPG Cluster) asserts:

  1. replicas >= 2 (a HorizontalPodAutoscaler's minReplicas counts) is true, or the workload is listed under
     `single_replica` in scripts/ha-allowlist.yaml with a reason and what its loss means;
  2. when replicas >= 2: the pods spread over nodes (a topologySpreadConstraint or a podAntiAffinity on
     kubernetes.io/hostname whose selector selects the workload's own pods);
  3. when replicas >= 2: exactly ONE PodDisruptionBudget selects the pods and it allows exactly one disruption (zero blocks
     every drain, two or more takes the quorum or the service down at once);
  4. (pdb_exception lists the workloads whose budget deliberately allows more, with the reason.)
  5. nothing can strand a pod on a 3-worker cluster: no required podAffinity, no required hostname anti-affinity or hard
     (DoNotSchedule) hostname spread for more replicas than 2 unless listed under `strict_spread`, no nodeSelector but the OS one.

The allow-list cannot rot: an entry for a workload that is no longer single-replica, or that no longer exists, fails the
check, and so does an entry without a reason. `known_gaps` lists what is not fixed yet and must shrink to nothing.
It also prints the CPU requests of what it renders (the shipped 3 x 4 vCPU example must stay roll-able: infra capacity gate).

Static: it proves the manifests, not the scheduler. Needs kubectl, helm (network for the charts) and PyYAML.
Usage: python3 scripts/check-ha.py [--dump FILE | --load FILE] [--cpu]
  --dump saves the rendered stream, --load audits a saved one without rendering (the unit tests do not need the network).
"""
import math
import os
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
HOST = "kubernetes.io/hostname"
ALLOWLIST = REPO / "scripts/ha-allowlist.yaml"
WORKLOADS = ("Deployment", "StatefulSet")
CPU_BUDGET_M = 3300  # rendered Deployment/StatefulSet CPU requests, in millicores; raising it is a capacity decision


def ident(d):
    return "%s/%s/%s" % (d["kind"], d["metadata"].get("namespace", ""), d["metadata"]["name"])


def subset(sel, labels):
    """Does a matchLabels selector select pods carrying these labels? An empty selector selects everything."""
    return all(labels.get(k) == v for k, v in (sel or {}).items())


def pod_labels(d):
    return d["spec"]["template"]["metadata"].get("labels", {}) or {}


def normalise(docs):
    """Workloads as dicts: id, replicas, labels, ns, spread (list), affinity, nodeSelector, pdb (CR-embedded) or None, cpu."""
    hpas = {}
    for d in docs:
        if d.get("kind") == "HorizontalPodAutoscaler":
            t = d["spec"]["scaleTargetRef"]
            hpas[(t["kind"], d["metadata"].get("namespace", ""), t["name"])] = d["spec"].get("minReplicas", 1)
    out = []
    for d in docs:
        k, ns, name = d.get("kind"), d["metadata"].get("namespace", ""), d["metadata"]["name"]
        if k in WORKLOADS:
            spec = d["spec"]["template"]["spec"]
            r = hpas.get((k, ns, name), d["spec"].get("replicas"))
            out.append(dict(id=ident(d), ns=ns, name=name, replicas=1 if r is None else r, labels=pod_labels(d),
                            spread=spec.get("topologySpreadConstraints", []) or [], affinity=spec.get("affinity", {}) or {},
                            nodeSelector=spec.get("nodeSelector", {}) or {}, pdb="external",
                            cpu=sum(cpu_m(c) for c in spec.get("containers", []))))
        elif k == "VMCluster":
            for comp in ("vminsert", "vmselect", "vmstorage"):
                c = d["spec"].get(comp)
                if c:
                    out.append(cr_workload(d, comp, c, "app.kubernetes.io/name", comp))
        elif k == "Cluster" and d.get("apiVersion", "").startswith("postgresql.cnpg.io"):
            sp = d["spec"]
            aff = sp.get("affinity", {}) or {}
            anti = aff.get("enablePodAntiAffinity", True) is not False and aff.get("topologyKey", HOST) == HOST
            pdb = {"maxUnavailable": 1} if sp.get("enablePDB", True) is not False else {}
            # the operator selects its own pods; model it as a hostname anti-affinity on the cluster label
            sel = {"cnpg.io/cluster": d["metadata"]["name"]}
            out.append(dict(id=ident(d), ns=ns, name=name, replicas=sp.get("instances", 1), labels=sel, spread=[],
                            affinity={"podAntiAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": [
                                {"podAffinityTerm": {"topologyKey": HOST, "labelSelector": {"matchLabels": sel}}}]}} if anti else {},
                            nodeSelector={}, pdb=pdb, cpu=0))
        elif k in ("VMAgent", "VMAlert", "VMAlertmanager"):
            out.append(cr_workload(d, k.lower(), d["spec"], "app.kubernetes.io/name", k.lower()))
    return out


def cr_workload(d, comp, c, lkey, lval):
    r = c.get("replicaCount")
    return dict(id="%s/%s/%s#%s" % (d["kind"], d["metadata"].get("namespace", ""), d["metadata"]["name"], comp),
                ns=d["metadata"].get("namespace", ""), name=comp, replicas=1 if r is None else r, labels={lkey: lval},
                spread=c.get("topologySpreadConstraints", []) or [], affinity=c.get("affinity", {}) or {},
                nodeSelector=c.get("nodeSelector", {}) or {}, pdb=c.get("podDisruptionBudget") or {}, cpu=cpu_m(c))


def cpu_m(c):
    q = ((c.get("resources") or {}).get("requests") or {}).get("cpu")
    if q is None:
        return 0
    q = str(q)
    return int(q[:-1]) if q.endswith("m") else int(float(q) * 1000)


def pdbs_for(w, docs):
    return [p for p in docs if p.get("kind") == "PodDisruptionBudget" and p["metadata"].get("namespace", "") == w["ns"]
            and (p["spec"].get("selector") or {}).get("matchLabels") and subset(p["spec"]["selector"]["matchLabels"], w["labels"])]


def allowed(spec, r):
    """Disruptions a PodDisruptionBudget spec allows with r healthy pods."""
    def num(v, up):
        if isinstance(v, str) and v.endswith("%"):
            x = r * int(v[:-1]) / 100
            return math.ceil(x) if up else math.floor(x)
        return int(v)
    if "maxUnavailable" in spec:
        return num(spec["maxUnavailable"], True)
    if "minAvailable" in spec:
        return r - num(spec["minAvailable"], True)
    return r  # neither set: the API default is minAvailable 1 only for pdbs created by controllers; treat as unbounded


def spreads(w):
    for c in w["spread"]:
        if c.get("topologyKey") == HOST and subset((c.get("labelSelector") or {}).get("matchLabels"), w["labels"]) \
                and (c.get("labelSelector") or {}).get("matchLabels"):
            return True
    pa = w["affinity"].get("podAntiAffinity", {})
    terms = [t for t in pa.get("requiredDuringSchedulingIgnoredDuringExecution", [])] + \
            [t["podAffinityTerm"] for t in pa.get("preferredDuringSchedulingIgnoredDuringExecution", [])]
    return any(t.get("topologyKey") == HOST and (t.get("labelSelector") or {}).get("matchLabels")
               and subset(t["labelSelector"]["matchLabels"], w["labels"]) for t in terms)


def audit(docs, allow):
    """Returns (errors, notes)."""
    errs, notes = [], []
    singles = {e["workload"]: e for e in allow.get("single_replica", [])}
    stricts = {e["workload"]: e for e in allow.get("strict_spread", [])}
    gaps = {e["workload"]: e for e in allow.get("known_gaps", [])}
    pdbx = {e["workload"]: e for e in allow.get("pdb_exception", [])}
    ws = normalise(docs)
    ids = {w["id"] for w in ws}
    for lst, label in ((singles, "single_replica"), (stricts, "strict_spread"), (gaps, "known_gaps"), (pdbx, "pdb_exception")):
        for k, e in lst.items():
            if not str(e.get("reason", "")).strip():
                errs.append("allow-list %s %s has no reason" % (label, k))
            if label == "single_replica" and not str(e.get("loss", "")).strip():
                errs.append("allow-list single_replica %s does not say what its loss means" % k)
            if k not in ids:
                errs.append("allow-list %s %s names no workload of the platform (renamed or removed: delete the entry)" % (label, k))
    for w in ws:
        wid, r, probs = w["id"], w["replicas"], []
        if r < 2:
            if wid in singles:
                continue
            if wid in gaps:
                notes.append("known gap: %s is single-replica" % wid)
                continue
            errs.append("%s: %s replica and not in single_replica of scripts/ha-allowlist.yaml" % (wid, r))
            continue
        if wid in singles:
            errs.append("%s has %s replicas but is still listed under single_replica (stale entry)" % (wid, r))
        if not spreads(w):
            probs.append("no topologySpreadConstraint or podAntiAffinity on %s that selects its own pods" % HOST)
        if wid in pdbx:
            ps = pdbs_for(w, docs)
            if len(ps) != 1 or allowed(ps[0]["spec"], r) == 1:
                errs.append("%s is listed under pdb_exception but has %d budget(s) %s: delete the entry" % (
                    wid, len(ps), "allowing exactly one disruption" if ps else ""))
        elif w["pdb"] == "external":
            ps = pdbs_for(w, docs)
            if len(ps) != 1:
                probs.append("%d PodDisruptionBudgets select its pods, exactly one is needed" % len(ps))
            else:
                a = allowed(ps[0]["spec"], r)
                if a != 1:
                    probs.append("PodDisruptionBudget %s allows %d disruption(s) with %d replicas, exactly one is needed"
                                 % (ps[0]["metadata"]["name"], a, r))
        else:
            if not w["pdb"]:
                probs.append("no podDisruptionBudget")
            else:
                a = allowed(w["pdb"], r)
                if a != 1:
                    probs.append("podDisruptionBudget allows %d disruption(s) with %d replicas, exactly one is needed" % (a, r))
        aff = w["affinity"]
        if aff.get("podAffinity", {}).get("requiredDuringSchedulingIgnoredDuringExecution"):
            probs.append("required podAffinity can strand a pod")
        req_anti = [t for t in aff.get("podAntiAffinity", {}).get("requiredDuringSchedulingIgnoredDuringExecution", [])
                    if t.get("topologyKey") == HOST]
        hard = [c for c in w["spread"] if c.get("topologyKey") == HOST and c.get("whenUnsatisfiable") == "DoNotSchedule"]
        if (req_anti or hard) and r > 2 and wid not in stricts:
            probs.append("a required hostname anti-affinity or DoNotSchedule hostname spread with %d replicas can strand a pod "
                         "when a worker is lost (list it under strict_spread with a reason, or make it preferred)" % r)
        extra = {k: v for k, v in w["nodeSelector"].items() if k != "kubernetes.io/os"}
        if extra:
            probs.append("nodeSelector %s restricts the nodes it can run on" % extra)
        for p in probs:
            (notes if wid in gaps else errs).append(("known gap: " if wid in gaps else "") + "%s: %s" % (wid, p))
        if wid in gaps and not probs:
            errs.append("%s is compliant but still listed under known_gaps (delete the entry)" % wid)
    return errs, notes


def render():
    from ha_render import render_platform
    from pick import load_dag
    only = os.environ.get("HA_ONLY", "")  # a path substring: render only those bricks (development; CI renders all)
    return render_platform(sorted({b.path for b in load_dag().values() if b.path and only in b.path}))


def main():
    args = sys.argv[1:]
    if "--load" in args:
        docs = [d for d in yaml.safe_load_all(Path(args[args.index("--load") + 1]).read_text()) if isinstance(d, dict)]
    else:
        docs = render()
        if "--dump" in args:
            Path(args[args.index("--dump") + 1]).write_text(yaml.safe_dump_all(docs))
    allow = yaml.safe_load(ALLOWLIST.read_text()) or {}
    errs, notes = audit(docs, allow)
    ws = normalise(docs)
    cpu = sum(w["cpu"] * w["replicas"] for w in ws if w["pdb"] == "external")
    for n in notes:
        print("note: " + n)
    print("%d workloads audited, %d single-replica listed, CPU requests of the rendered Deployments/StatefulSets %d m (budget %d m)"
          % (len(ws), len(allow.get("single_replica", [])), cpu, CPU_BUDGET_M))
    if cpu > CPU_BUDGET_M:
        errs.append("CPU requests %d m exceed the budget %d m: the shipped 3 x 4 vCPU example must stay roll-able "
                    "(infra capacity gate, docs/capacity.md); raise CPU_BUDGET_M only with that arithmetic" % (cpu, CPU_BUDGET_M))
    for e in errs:
        print("FAIL " + e)
    print("%d failed" % len(errs))
    sys.exit(1 if errs else 0)


if __name__ == "__main__":
    main()
