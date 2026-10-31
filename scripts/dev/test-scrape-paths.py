#!/usr/bin/env python3
"""The scrapes added for RED and Longhorn health (0.4.0 M1) can actually pass the network policies.

For each pair (scrape, target policy, port): VMAgent's egress to the cluster allows the port, and the target's
CiliumNetworkPolicy lets the vmagent pods in services-observability in on it. Static: a policy that is missing makes the
rules blind without a sound (`*MetricsAbsent` then fires). The metric and port NAMES are UNVERIFIED until a lab.
"""
import copy
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
B = REPO / "apps/base"
CASES = [  # (scrape, namespace of the target, CNP name, port)
    ("istio-gateway", "services-gateway", "openaether-gateway", "15090"),
    ("longhorn-manager", "longhorn-system", "longhorn", "9500"),
    # the M2 verifier's exporter (a VMServiceScrape, not a pod scrape): lab 19 found vmagent's egress closed on 9101
    ("backup-verify", "foundation-databases", "backup-verify-exporter", "9101"),
    # the Forgejo forge (a VMPodScrape in vmscrapes-pipeline.yaml; the CNP is in the forge's own brick)
    ("forgejo", "services-git", "forgejo", "3000"),
]


def docs(p):
    return [d for d in yaml.safe_load_all((B / p).read_text()) if isinstance(d, dict)]


def ports(rule):
    return {p["port"] for tp in rule.get("toPorts", []) for p in tp["ports"]}


def problems(scrapes, netpols):
    out = []
    agent = next(d for d in netpols if d["metadata"]["name"] == "vmagent" and d["metadata"]["namespace"] == "services-observability")
    egress = set().union(*[ports(r) for r in agent["spec"]["egress"] if r.get("toEntities") == ["cluster"]])
    for scrape, ns, cnp, port in CASES:
        s = next((d for d in scrapes if d["metadata"]["name"] == scrape), None)
        if not s or s["spec"]["namespaceSelector"]["matchNames"] != [ns]:
            out.append(f"no VMPodScrape {scrape} for namespace {ns}")
        if port not in egress:
            out.append(f"vmagent egress does not allow {port} ({scrape})")
        pol = next((d for d in netpols if d["metadata"]["name"] == cnp and d["metadata"]["namespace"] == ns), None)
        ok = pol and any(
            port in ports(r) and any(
                (e.get("matchLabels", {}).get("io.kubernetes.pod.namespace") or e.get("matchLabels", {}).get("k8s:io.kubernetes.pod.namespace")) == "services-observability"
                and e["matchLabels"].get("app.kubernetes.io/name") == "vmagent" for e in r.get("fromEndpoints", []))
            for r in pol["spec"].get("ingress", []))
        if not ok:
            out.append(f"CiliumNetworkPolicy {ns}/{cnp} does not let vmagent in on {port}")
    return out


scrapes = [d for d in docs("observability/vm-customresources/vmscapes.yaml") + docs("observability/vm-customresources/vmscrapes-pipeline.yaml")
           if d["kind"] in ("VMPodScrape", "VMServiceScrape")]
netpols = [d for p in ("observability/networkpolicy.yaml", "istio/networkpolicy.yaml", "storage/networkpolicy.yaml", "backup/databases/networkpolicy.yaml", "forgejo/networkpolicy.yaml")
           for d in docs(p) if d["kind"] == "CiliumNetworkPolicy"]
bad = problems(scrapes, netpols)
for b in bad:
    print("FAIL", b)
if "--selftest" in sys.argv:
    for name, mut in {
        "gateway ingress removed": lambda n: [d["spec"]["ingress"].pop() for d in n if d["metadata"]["name"] == "openaether-gateway"],
        "agent egress port removed": lambda n: [r["toPorts"][0]["ports"].pop(i) for d in n if d["metadata"]["name"] == "vmagent"
                                                for r in d["spec"]["egress"] if r.get("toEntities") == ["cluster"]
                                                for i, p in reversed(list(enumerate(r["toPorts"][0]["ports"]))) if p["port"] == "9500"],
    }.items():
        n = copy.deepcopy(netpols)
        mut(n)
        got = problems(scrapes, n)
        print(("selftest ok   " if got else "selftest FAIL (not caught) ") + name)
        bad += [] if got else [name]
print(f"{len(bad)} failed")
sys.exit(1 if bad else 0)
