#!/usr/bin/env python3
"""Flux failures must reach Alertmanager (#139). Static: it proves the manifests and the two network openings, not the delivery.

Checks: one Provider of type alertmanager per Alertmanager pod, whose addresses are exactly the per-pod URLs vmalert sends to plus
/api/v2/alerts, their count equal to the VMAlertmanager replicaCount (apps #218); one Alert per Provider, all with the same sources and
eventSeverity error, that cover GitRepository, Kustomization and a HelmRelease source in EVERY namespace where apps/base has a
HelmRelease (the controller matches one namespace per source); the flux-system egress and the Alertmanager ingress on :9093; the
Slack template that reads Flux's `message` annotation. `--selftest` mutates the manifests and requires a failure.
"""
import copy
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
B = REPO / "apps/base"


def load(p):
    return [d for d in yaml.safe_load_all(Path(p).read_text()) if d]


def helmrelease_namespaces():
    ns = set()
    for f in (REPO / "apps").rglob("*.yaml"):
        if "cluster-api-clusters" in f.parts:  # content of CHILD clusters, not applied here
            continue
        try:
            for d in load(f):
                if isinstance(d, dict) and d.get("kind") == "HelmRelease":
                    ns.add(d["metadata"]["namespace"])
        except yaml.YAMLError:
            pass
    return ns


def check(objs, egress, ingress, am_cfg, hr_ns, notifiers, replicas):
    bad = []
    prov = {o["metadata"]["name"]: o for o in objs if o.get("kind") == "Provider"}
    alerts = [o for o in objs if o.get("kind") == "Alert"]
    if not prov or any(p["spec"].get("type") != "alertmanager" for p in prov.values()):
        return ["expected Providers of type alertmanager only"]
    if not alerts:
        return ["expected one Alert per Provider"]
    # apps #218: an event is held only by the Alertmanager pods it was posted to, so every pod needs its own Provider.
    if len(prov) != replicas:
        bad.append("%d Provider(s) for %d Alertmanager replicas: one per pod" % (len(prov), replicas))
    got = sorted(p["spec"].get("address", "") for p in prov.values())
    want = sorted(n + "/api/v2/alerts" for n in notifiers)
    if got != want:
        bad.append("Provider addresses %s are not the per-pod URLs vmalert sends to %s" % (got, want))
    refs = sorted(a["spec"]["providerRef"]["name"] for a in alerts)
    if refs != sorted(prov) or any(o["metadata"]["namespace"] != "flux-system" for o in alerts + list(prov.values())):
        bad.append("each Provider needs exactly one Alert in flux-system referencing it, found %s for %s" % (refs, sorted(prov)))
    if any(a["spec"].get("eventSeverity") != "error" for a in alerts):
        bad.append("eventSeverity must be error")
    srcs = [sorted((s["kind"], s.get("name"), s.get("namespace")) for s in a["spec"]["eventSources"]) for a in alerts]
    if any(x != srcs[0] for x in srcs):
        bad.append("the Alerts do not list the same eventSources: one Alertmanager pod would miss some events")
    src = {(s["kind"], s.get("namespace")) for s in alerts[0]["spec"]["eventSources"]}
    for k in ("GitRepository", "Kustomization"):
        if (k, "flux-system") not in src:
            bad.append("no %s source in flux-system" % k)
    for n in sorted(hr_ns):
        if ("HelmRelease", n) not in src:
            bad.append("no HelmRelease source in %s" % n)
    if not any(r.get("toEndpoints") and any(pp["port"] == "9093" for t in r.get("toPorts", []) for pp in t["ports"])
               and r["toEndpoints"][0]["matchLabels"].get("app.kubernetes.io/name") == "vmalertmanager" for r in egress):
        bad.append("flux-system has no egress to vmalertmanager :9093")
    if not any(f["matchLabels"].get("io.kubernetes.pod.namespace") == "flux-system"
               and f["matchLabels"].get("app") == "notification-controller"
               for r in ingress for f in r.get("fromEndpoints", [])):
        bad.append("vmalertmanager has no ingress from the notification controller")
    if ".Annotations.message" not in am_cfg:
        bad.append("the Slack template does not read the Flux `message` annotation")
    return bad


def inputs():
    objs = load(B / "flux/notification-alertmanager.yaml")
    egress = next(d for d in load(B / "flux/flux-egress-restricted.yaml") if d["kind"] == "CiliumNetworkPolicy")["spec"]["egress"]
    ingress = next(d for d in load(B / "observability/networkpolicy.yaml")
                   if d["kind"] == "CiliumNetworkPolicy" and d["metadata"]["name"] == "vmalertmanager")["spec"]["ingress"]
    va = load(B / "observability/vm-customresources/vmalert.yaml")
    am_cfg = next(d for d in va if d["kind"] == "VMAlertmanager")["spec"]["configRawYaml"]
    # vmalert already lists one URL per Alertmanager pod (apps #207); test-alert-delivery.py ties that list to replicaCount.
    notifiers = [n["url"] for n in next(d for d in va if d["kind"] == "VMAlert")["spec"]["notifiers"]]
    replicas = int(next(d for d in va if d["kind"] == "VMAlertmanager")["spec"]["replicaCount"])
    kust = yaml.safe_load((B / "flux/kustomization.yaml").read_text())["resources"]
    return objs, egress, ingress, am_cfg, helmrelease_namespaces(), notifiers, replicas, kust


def main():
    objs, egress, ingress, am_cfg, hr_ns, notifiers, replicas, kust = inputs()
    if "--selftest" in sys.argv:
        rc = 0
        def run(name, fn):
            nonlocal rc
            o, e, i, c = copy.deepcopy(objs), copy.deepcopy(egress), copy.deepcopy(ingress), am_cfg
            o, e, i, c = fn(o, e, i, c)
            ok = bool(check(o, e, i, c, hr_ns, notifiers, replicas)); print("selftest: %s %s" % (name, "detected" if ok else "NOT detected")); rc |= (not ok)
        run("wrong type", lambda o, e, i, c: ([x for x in o if x["kind"] != "Provider"] + [
            {"kind": "Provider", "metadata": {"name": "alertmanager-0", "namespace": "flux-system"}, "spec": {"type": "generic"}}], e, i, c))
        prov = lambda o: [x for x in o if x["kind"] == "Provider"]  # noqa: E731
        alrt = lambda o: [x for x in o if x["kind"] == "Alert"]  # noqa: E731
        run("one Provider on the Service (apps #218)", lambda o, e, i, c: (
            [x for x in o if x["kind"] != "Provider"][:1] + [dict(prov(o)[0], spec={"type": "alertmanager", "address":
             "http://vmalertmanager-alertmanager.services-observability:9093/api/v2/alerts"})], e, i, c))
        run("a Provider and its Alert dropped", lambda o, e, i, c: (prov(o)[:1] + alrt(o)[:1], e, i, c))
        run("a pod address wrong", lambda o, e, i, c: (
            [dict(x, spec={**x["spec"], "address": x["spec"]["address"].replace("-1.", "-0.")}) if x["kind"] == "Provider" else x for x in o], e, i, c))
        run("one Alert misses a source", lambda o, e, i, c: (
            alrt(o)[:1] + [dict(a, spec={**a["spec"], "eventSources": a["spec"]["eventSources"][:-1]}) for a in alrt(o)[1:]] + prov(o), e, i, c))
        run("severity info", lambda o, e, i, c: ([x.update(spec={**x["spec"], "eventSeverity": "info"}) or x if x["kind"] == "Alert" else x for x in o], e, i, c))
        run("a HelmRelease namespace missing", lambda o, e, i, c: ([x.update(spec={**x["spec"], "eventSources": [s for s in x["spec"]["eventSources"] if s.get("namespace") != "cert-manager"]}) or x if x["kind"] == "Alert" else x for x in o], e, i, c))
        run("no egress", lambda o, e, i, c: (o, e[:-1], i, c))
        run("no ingress", lambda o, e, i, c: (o, e, i[:-1], c))
        run("template ignores message", lambda o, e, i, c: (o, e, i, c.replace(".Annotations.message", ".Annotations.summary")))
        # A third Alertmanager replica (and its vmalert URL) without a third Provider and Alert.
        url3 = notifiers[-1].replace("-%d." % (replicas - 1), "-%d." % replicas)
        ok = bool(check(objs, egress, ingress, am_cfg, hr_ns, notifiers + [url3], replicas + 1))
        print("selftest: a replica added without its Provider %s" % ("detected" if ok else "NOT detected"))
        rc |= not ok
        return rc
    bad = check(objs, egress, ingress, am_cfg, hr_ns, notifiers, replicas)
    if "notification-alertmanager.yaml" not in kust:
        bad.append("apps/base/flux/kustomization.yaml does not list notification-alertmanager.yaml")
    r = subprocess.run(["kubectl", "kustomize", str(B / "flux")], capture_output=True, text=True)
    if r.returncode or "kind: Provider" not in r.stdout:
        bad.append("kubectl kustomize apps/base/flux does not render the Provider")
    for b in bad:
        print("FAIL", b)
    print("flux notifications: %s (HelmRelease namespaces: %s)" % ("FAIL" if bad else "ok", ", ".join(sorted(hr_ns))))
    return 1 if bad else 0


sys.exit(main())
