#!/usr/bin/env python3
"""node-exporter runs in its own privileged namespace; the three places that must agree on it do (ANSSI FT082 R4/R6).

The HelmRelease, the VMServiceScrape that selects its Service and the Namespace declaration name the same namespace; that
namespace is the only privileged one of the observability stack, and no other workload of the brick is deployed there.
A scrape that selects the wrong namespace leaves no `up` series at all (node_* silently absent, apps #47 history).
Static: it reads the manifests, not a cluster.
"""
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
OBS = REPO / "apps/base/observability"


def docs(path):
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def main():
    problems = []
    hr = next(d for d in docs(OBS / "node-exporter.yaml") if d["kind"] == "HelmRelease")
    ns = hr["metadata"]["namespace"]
    scrape = next(d for d in docs(OBS / "vm-customresources/vmscapes.yaml")
                  if d["kind"] == "VMServiceScrape" and d["metadata"]["name"] == "node-exporter")
    if scrape["spec"]["namespaceSelector"]["matchNames"] != [ns]:
        problems.append(f"VMServiceScrape/node-exporter selects {scrape['spec']['namespaceSelector']}, the HelmRelease is in {ns}")
    namespaces = {d["metadata"]["name"]: d for d in docs(REPO / "apps/base/namespaces/namespaces.yaml") if d["kind"] == "Namespace"}
    if ns not in namespaces:
        problems.append(f"Namespace {ns} is not declared in apps/base/namespaces")
    elif namespaces[ns]["metadata"]["labels"].get("pod-security.kubernetes.io/enforce") != "privileged":
        problems.append(f"Namespace {ns} must enforce the privileged Pod Security level (node-exporter needs hostPath and hostNetwork)")
    if ns == "services-observability":
        problems.append("node-exporter is back in services-observability: that namespace cannot then leave the privileged level")
    for path in OBS.rglob("*.yaml"):
        for d in docs(path):
            if d.get("kind") in ("HelmRelease", "Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job") \
                    and d["metadata"].get("namespace") == ns \
                    and d["metadata"]["name"] != hr["metadata"]["name"]:
                problems.append(f"{d['kind']}/{d['metadata']['name']} shares the privileged namespace {ns}")
    if problems:
        print("\n".join(problems))
        return 1
    print(f"ok: node-exporter, its scrape and the privileged Namespace agree on {ns}, nothing else of the brick is there")
    return 0


if __name__ == "__main__":
    sys.exit(main())
