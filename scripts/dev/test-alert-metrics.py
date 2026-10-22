#!/usr/bin/env python3
"""Blind-rule check, offline (0.4.0 M1): every metric an alert reads is catalogued, comes from a scrape that exists, and
is honest about whether a cluster ever showed it.

A rule on a metric nobody scrapes never fires and never complains. This cannot tell that a series EXISTS (that needs a
cluster: `task check-alerts` of OpenAether-infra), but it fails when
  - an alert reads a metric missing from scripts/dev/alert-metrics.yaml (add it, with its source and status);
  - the catalogue holds a metric no alert reads;
  - a source names a scrape (VMServiceScrape, VMPodScrape or VMNodeScrape) that no manifest defines;
  - a metric marked UNVERIFIED is missing from docs/monitoring-0.4.md (what the next lab must read).
Each rule has a self-test that proves it would catch the regression.
"""
import copy
import importlib.util
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("alert_metrics", REPO / "scripts/dev/alert-metrics.py")
am = importlib.util.module_from_spec(spec)
spec.loader.exec_module(am)

SCRAPES = {  # source -> (kind, name)
    "kube-state-metrics": ("VMServiceScrape", "kube-state-metrics"), "node-exporter": ("VMServiceScrape", "node-exporter"),
    "cadvisor": ("VMNodeScrape", "cadvisor"), "kubelet": ("VMNodeScrape", "kubelet"), "etcd": ("VMNodeScrape", "etcd"),
    "cilium": ("VMPodScrape", "cilium"), "cert-manager": ("VMServiceScrape", "cert-manager"),
    "cnpg": ("VMPodScrape", "cnpg"), "kyverno": ("VMServiceScrape", "kyverno"), "alloy": ("VMPodScrape", "alloy"),
    "loki": ("VMPodScrape", "loki"), "grafana": ("VMPodScrape", "grafana"), "openbao": ("VMServiceScrape", "openbao"),
    "vmalert": ("VMServiceScrape", "vmalert"), "alertmanager": ("VMServiceScrape", "vmalert"),
    "flux": ("VMPodScrape", "flux"), "istio-gateway": ("VMPodScrape", "istio-gateway"),
    "longhorn": ("VMPodScrape", "longhorn-manager"), "backup-verify": ("VMServiceScrape", "backup-verify"),
    "audit-sink": ("VMServiceScrape", "audit-sink-check"),
}
NO_SCRAPE = {"scrape"}   # `up`, which every scrape makes


def defined_scrapes():
    out = set()
    for f in (REPO / "apps/base").rglob("*.yaml"):
        if "cnpg-1." in f.name:
            continue
        for d in yaml.safe_load_all(f.read_text()):
            if isinstance(d, dict) and str(d.get("kind", "")).startswith("VM") and d["kind"].endswith("Scrape"):
                out.add((d["kind"], d["metadata"]["name"]))
    return out


def problems(read, catalogue, scrapes, doc):
    out = []
    for m in sorted(read - set(catalogue)):
        out.append(f"{m} is read by an alert but not in scripts/dev/alert-metrics.yaml")
    for m in sorted(set(catalogue) - read):
        out.append(f"{m} is in the catalogue but no alert reads it")
    for m, v in sorted(catalogue.items()):
        if v["status"] not in ("existing", "UNVERIFIED"):
            out.append(f"{m}: status must be existing or UNVERIFIED")
        if v["source"] not in NO_SCRAPE and SCRAPES.get(v["source"]) not in scrapes:
            out.append(f"{m}: source {v['source']} has no scrape ({SCRAPES.get(v['source'])})")
        if v["status"] == "UNVERIFIED" and m not in doc:
            out.append(f"{m}: UNVERIFIED but not listed in docs/monitoring-0.4.md")
    return out


def main():
    read = set()
    for expr in am.rules(REPO).values():
        read |= am.metrics(expr)
    catalogue = yaml.safe_load((REPO / "scripts/dev/alert-metrics.yaml").read_text())
    scrapes, doc = defined_scrapes(), (REPO / "docs/monitoring-0.4.md").read_text()
    bad = problems(read, catalogue, scrapes, doc)
    for b in bad:
        print("FAIL", b)
    if "--selftest" in sys.argv:
        some = next((m for m, v in catalogue.items() if v["status"] == "UNVERIFIED"), "made_up_unverified")  # none left is the goal
        cases = {
            "metric not catalogued": lambda r, c, s, d: c.pop("kube_pod_status_phase"),
            "stale catalogue entry": lambda r, c, s, d: c.update({"made_up_metric": {"source": "cnpg", "status": "existing"}}) or r,
            "scrape deleted": lambda r, c, s, d: s.discard(("VMPodScrape", "istio-gateway")),
            "unverified not documented": lambda r, c, s, d: (
                c.setdefault(some, {"source": "cnpg", "status": "UNVERIFIED"}), r.add(some), d.replace(some, ""))[-1],
            "bad status": lambda r, c, s, d: c["up"].update(status="maybe"),
        }
        for name, f in cases.items():
            r, c, s, d = set(read), copy.deepcopy(catalogue), set(scrapes), doc
            d = f(r, c, s, d) if name == "unverified not documented" else (f(r, c, s, d), d)[1]
            got = problems(r, c, s, d if isinstance(d, str) else doc)
            print(("selftest ok   " if got else "selftest FAIL (not caught) ") + name)
            bad += [] if got else [name]
    print(f"{len(bad)} failed")
    sys.exit(1 if bad else 0)


main()
