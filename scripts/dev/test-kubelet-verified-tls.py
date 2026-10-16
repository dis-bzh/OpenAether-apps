#!/usr/bin/env python3
"""Test of apps/flux/components/kubelet-verified-tls (opt-in: metrics-server and the kubelet scrapes verify the kubelet cert).

Renders the Flux DAG with and without the component, then applies the Flux `spec.patches` it carries to the real bricks, like
Flux does. Checks: default unchanged (insecure flags still there), the component flips exactly metrics-server and the two kubelet
VMNodeScrapes, and the kube-apiserver scrape verifies in BOTH states. It does NOT run Flux or a kubelet: that the kubelet certs
are signed by the cluster CA is infra's kubelet_serving_cert_rotation (LAB-CHECKLIST). Rung: mocked/static.
Usage: python3 scripts/dev/test-kubelet-verified-tls.py   (needs kubectl and PyYAML)
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
BASE = REPO / "apps/flux/base"
COMPONENT = REPO / "apps/flux/components/kubelet-verified-tls"
CA = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
PASS = FAIL = 0


def check(desc, ok, detail=""):
    global PASS, FAIL
    PASS += bool(ok)
    FAIL += not ok
    print(("  ok   " if ok else "  FAIL ") + desc + ("" if ok else f" -> {detail}"))


def build(directory):
    r = subprocess.run(["kubectl", "kustomize", "--load-restrictor=LoadRestrictionsNone", str(directory)],
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"kubectl kustomize {directory} failed: {r.stderr}")
    return [d for d in yaml.safe_load_all(r.stdout) if d]


def render(work, resources, components=(), patches=()):
    rel = lambda p: os.path.relpath(p, work)  # noqa: E731
    k = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
         "resources": [rel(p) for p in resources]}
    if components:
        k["components"] = [rel(p) for p in components]
    if patches:
        k["patches"] = list(patches)
    (Path(work) / "kustomization.yaml").write_text(yaml.safe_dump(k))
    return build(work)


def flux_ks(docs):
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == "Kustomization"}


def built(tmp, ks):
    spec = ks["spec"]
    return render(tmp, [REPO / spec["path"]], patches=spec.get("patches", []))


def pick(docs, kind, name):
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)


with tempfile.TemporaryDirectory() as tmp:
    plain = flux_ks(render(tmp, [BASE]))
    with_c = flux_ks(render(tmp, [BASE], [COMPONENT]))
    names = {"platform-metrics-server", "observability-vm-customresources"}
    check("the component changes exactly the two Kustomizations it names",
          {n for n in with_c if with_c[n] != plain.get(n)} == names)
    check("it changes nothing but spec.patches",
          all({k: v for k, v in with_c[n]["spec"].items() if k != "patches"}
              == {k: v for k, v in plain[n]["spec"].items() if k != "patches"} for n in names))

    for label, ks in (("default", plain), ("component", with_c)):
        ms = pick(built(tmp, ks["platform-metrics-server"]), "HelmRelease", "metrics-server")
        args = ms["spec"]["values"]["args"]
        vm = built(tmp, ks["observability-vm-customresources"])
        scr = {n: pick(vm, "VMNodeScrape", n)["spec"]["tlsConfig"] for n in ("kubelet", "cadvisor")}
        api = pick(vm, "VMServiceScrape", "kube-apiserver")["spec"]["endpoints"][0]["tlsConfig"]
        on = label == "component"
        check(f"{label}: metrics-server {'verifies' if on else 'keeps --kubelet-insecure-tls'}",
              ("--kubelet-insecure-tls" not in args) == on and (f"--kubelet-certificate-authority={CA}" in args) == on, args)
        if on:
            # The patch replaces the whole args list: an arg added to metrics-server.yaml later must not vanish silently.
            base_args = pick(built(tmp, plain["platform-metrics-server"]), "HelmRelease", "metrics-server")["spec"]["values"]["args"]
            check("component: metrics-server args are the default ones, minus --kubelet-insecure-tls, plus the CA flag",
                  sorted(args) == sorted([x for x in base_args if x != "--kubelet-insecure-tls"] + [f"--kubelet-certificate-authority={CA}"]),
                  args)
        check(f"{label}: kubelet and cAdvisor scrapes {'verify against the cluster CA' if on else 'stay insecure'}",
              all((t.get("insecureSkipVerify") is None and t.get("caFile") == CA) if on
                  else (t.get("insecureSkipVerify") is True and "caFile" not in t) for t in scr.values()), scr)
        check(f"{label}: kube-apiserver scrape verifies with CA and serverName",
              api.get("caFile") == CA and api.get("serverName") == "kubernetes.default.svc"
              and not api.get("insecureSkipVerify"), api)

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
