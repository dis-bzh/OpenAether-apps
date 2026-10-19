#!/usr/bin/env python3
"""Flux failures must reach Alertmanager (#139). Static: it proves the manifests and the two network openings, not the delivery.

Checks: a Provider of type alertmanager whose address is the notifier vmalert uses (host, port, /api/v2/alerts); an Alert on it with
eventSeverity error that covers GitRepository, Kustomization and a HelmRelease source in EVERY namespace where apps/base has a
HelmRelease (the controller matches one namespace per source); the flux-system egress and the Alertmanager ingress on :9093; the
Slack template that reads Flux's `message` annotation. `--selftest` mutates the manifests and requires a failure.
"""
import copy
import re
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


def check(objs, egress, ingress, am_cfg, hr_ns, notifier):
    bad = []
    prov = [o for o in objs if o.get("kind") == "Provider"]
    alert = [o for o in objs if o.get("kind") == "Alert"]
    if len(prov) != 1 or prov[0]["spec"].get("type") != "alertmanager":
        return ["expected one Provider of type alertmanager"]
    if len(alert) != 1:
        return ["expected one Alert"]
    p, a = prov[0], alert[0]
    m = re.fullmatch(r"(http://[^/]+:9093)/api/v2/alerts", p["spec"].get("address", ""))
    if not m or m.group(1) != notifier:
        bad.append("Provider address %r is not %s/api/v2/alerts (the notifier of vmalert)" % (p["spec"].get("address"), notifier))
    if a["spec"]["providerRef"]["name"] != p["metadata"]["name"] or a["metadata"]["namespace"] != p["metadata"]["namespace"]:
        bad.append("the Alert does not reference the Provider")
    if a["spec"].get("eventSeverity") != "error":
        bad.append("eventSeverity must be error")
    src = {(s["kind"], s.get("namespace")) for s in a["spec"]["eventSources"]}
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
    notifier = next(d for d in va if d["kind"] == "VMAlert")["spec"]["notifiers"][0]["url"]
    kust = yaml.safe_load((B / "flux/kustomization.yaml").read_text())["resources"]
    return objs, egress, ingress, am_cfg, helmrelease_namespaces(), notifier, kust


def main():
    objs, egress, ingress, am_cfg, hr_ns, notifier, kust = inputs()
    if "--selftest" in sys.argv:
        rc = 0
        def run(name, fn):
            nonlocal rc
            o, e, i, c = copy.deepcopy(objs), copy.deepcopy(egress), copy.deepcopy(ingress), am_cfg
            o, e, i, c = fn(o, e, i, c)
            ok = bool(check(o, e, i, c, hr_ns, notifier)); print("selftest: %s %s" % (name, "detected" if ok else "NOT detected")); rc |= (not ok)
        run("wrong type", lambda o, e, i, c: ([x for x in o if x["kind"] != "Provider"] + [{"kind": "Provider", "spec": {"type": "generic"}}], e, i, c))
        run("severity info", lambda o, e, i, c: ([x.update(spec={**x["spec"], "eventSeverity": "info"}) or x if x["kind"] == "Alert" else x for x in o], e, i, c))
        run("a HelmRelease namespace missing", lambda o, e, i, c: ([x.update(spec={**x["spec"], "eventSources": [s for s in x["spec"]["eventSources"] if s.get("namespace") != "cert-manager"]}) or x if x["kind"] == "Alert" else x for x in o], e, i, c))
        run("no egress", lambda o, e, i, c: (o, e[:-1], i, c))
        run("no ingress", lambda o, e, i, c: (o, e, i[:-1], c))
        run("template ignores message", lambda o, e, i, c: (o, e, i, c.replace(".Annotations.message", ".Annotations.summary")))
        return rc
    bad = check(objs, egress, ingress, am_cfg, hr_ns, notifier)
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
