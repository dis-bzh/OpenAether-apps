#!/usr/bin/env python3
"""The alert-delivery chain keeps its shape (OpenAether 0.4.0 M1; docs/alert-delivery.md).

Static: reads the manifests and renders the opt-in component, never a cluster. Checked:
  - the Watchdog route is FIRST and goes to the heartbeat receiver, repeating strictly slower than it groups;
  - every route names a receiver that exists, and every url_file/api_url_file comes from a secret the CR mounts;
  - the probe label of the opt-in rule is the one the quiet receiver is routed on, and that receiver sends no "resolved";
  - the inhibit rule stops the Watchdog on an alert the opt-in rules define;
  - the component is OFF in the default profiles and adds exactly one Flux Kustomization, which waits for the VMRule CRDs;
  - the egress policy of Alertmanager still allows the chat and heartbeat hosts it is configured with, and those hosts are ONE
    documented input (cluster-identity: ALERT_CHAT_HOST, ALERT_HEARTBEAT_HOST, defaults hooks.slack.com and hc-ping.com), read
    by a Kustomization of their own because Flux substitution would blank the `$` expressions of the files around (apps #146);
  - vmalert hands its alerts to EVERY Alertmanager pod, one URL per replica, not through the headless Service (apps #207).
Each rule has a self-test that proves it would catch the regression.
"""
import copy
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
VMA = REPO / "apps/base/observability/vm-customresources/vmalert.yaml"
PROBE = REPO / "apps/base/observability-alert-probe/vmrule.yaml"
COMPONENT = REPO / "apps/flux/components/alert-delivery-probe"
NETPOL = REPO / "apps/base/observability/networkpolicy.yaml"
EGRESS = REPO / "apps/base/observability-alert-egress/networkpolicy.yaml"
OBS_FLUX = REPO / "apps/flux/base/30-observability.yaml"
DOC = REPO / "docs/alert-delivery.md"
HOST_VARS = ("ALERT_CHAT_HOST", "ALERT_HEARTBEAT_HOST")


def docs(path):
    return [d for d in yaml.safe_load_all(Path(path).read_text()) if isinstance(d, dict)]


def load():
    am = next(d for d in docs(VMA) if d["kind"] == "VMAlertmanager")
    cfg = yaml.safe_load(am["spec"]["configRawYaml"])
    rules = {r["alert"]: r for d in docs(PROBE) for g in d["spec"]["groups"] for r in g["rules"]}
    return am, cfg, rules


def notifier_problems(vma, am):
    """apps #207: a Service URL is resolved to one pod, so one replica held the alerts and the other none (n=1, 0.4.3 candidate).
    The pods share silences and the notification log, not an alert that was never delivered to them. The URL is the pod FQDN the
    VM operator builds itself (asPodFQDN, operator 0.75, for its own notifier discovery); vmalert sends to every URL it is given."""
    sts = "vmalertmanager-" + am["metadata"]["name"]
    want = sorted(f"http://{sts}-{i}.{sts}.{am['metadata']['namespace']}.svc:9093" for i in range(int(am["spec"]["replicaCount"])))
    got = sorted(str(n.get("url")) for n in vma["spec"].get("notifiers") or [])
    if got != want:
        return [f"vmalert notifiers must be exactly one URL per Alertmanager pod {want}, found {got}"]
    return []


def egress_hosts(policy):
    """{matchName as written: the host with the default of its `${VAR:=default}` applied} of a policy's toFQDNs."""
    out = {}
    for e in policy["spec"].get("egress", []):
        for m in e.get("toFQDNs", []):
            n = m["matchName"]
            out[n] = (re.fullmatch(r"\$\{(\w+):=([^}]+)\}", n) or [None, None, n])[2]
    return out


def egress_problems(main_cnp, egress_cnp, kusts, doc):
    """apps #146: the hosts Alertmanager may reach are one input of cluster-identity with defaults, in a policy of its own."""
    out = []
    if any("toFQDNs" in e for e in main_cnp["spec"].get("egress", [])):
        out.append("the vmalertmanager policy lists hosts itself: a second list defeats the single input (observability-alert-egress)")
    written = list(egress_hosts(egress_cnp))
    if sorted(re.findall(r"\$\{(\w+):=", " ".join(written))) != sorted(HOST_VARS) or len(written) != len(HOST_VARS):
        out.append(f"the egress policy must allow exactly ${{{HOST_VARS[0]}:=...}} and ${{{HOST_VARS[1]}:=...}}, no host written in full: {written}")
    k = kusts.get("observability-alert-egress")
    if not k:
        out.append("no Flux Kustomization observability-alert-egress")
    else:
        sub = (k["spec"].get("postBuild") or {}).get("substituteFrom") or []
        if not any(x.get("kind") == "ConfigMap" and x.get("name") == "cluster-identity" and x.get("optional") is True for x in sub):
            out.append("observability-alert-egress must substitute from cluster-identity, optional: a cluster without the keys keeps the defaults")
        if k["spec"].get("path") != "./apps/base/observability-alert-egress":
            out.append("observability-alert-egress must read ./apps/base/observability-alert-egress")
    # The reason for the separate brick: substitution applies to the WHOLE render, and these files are full of `$` (Grafana
    # dashboards `${datasource}`, VMRule templates `$labels`, Loki/Alloy configs).
    for name in ("observability", "observability-vm-customresources"):
        if "postBuild" in (kusts.get(name) or {}).get("spec", {}):
            out.append(f"{name} must not substitute: its files hold `$` expressions (dashboards, VMRule templates) that would be blanked")
    for v in HOST_VARS:
        if v not in doc:
            out.append(f"docs/alert-delivery.md does not name {v}: the input must be documented")
    return out


def problems(am, cfg, rules, egress_fqdns):
    out = []
    routes = cfg["route"]["routes"]
    receivers = {r["name"]: r for r in cfg["receivers"]}
    if not routes or routes[0].get("matchers") != ['alertname="Watchdog"'] or routes[0]["receiver"] != "deadmansswitch":
        out.append("the first route must send alertname=Watchdog to the deadmansswitch receiver")
    else:
        w = routes[0]
        gi, ri = (int(str(w[k]).rstrip("ms")) for k in ("group_interval", "repeat_interval"))
        if ri <= gi:
            out.append("Watchdog repeat_interval must be strictly greater than group_interval (one ping, never again)")
        # Lab 19: repeat 5m + group 1m gave a ping every 360 s, not the documented 5 min. A ping is sent at the first group_interval
        # tick at or after repeat_interval, so the gap is at most repeat_interval + group_interval: keep that within 5 min.
        if ri + gi > 5:   # both values are in minutes ("3m", "1m")
            out.append(f"Watchdog repeat_interval + group_interval is {ri + gi} min: the heartbeat cadence would exceed the documented 5 min (lab 19 measured 360 s with 5m + 1m)")
    for r in routes + [cfg["route"]]:
        if r["receiver"] not in receivers:
            out.append(f"route sends to the unknown receiver {r['receiver']}")
    mounted = set(am["spec"].get("secrets", []))
    for name, rec in receivers.items():
        for key, cfgs in rec.items():
            for c in cfgs if isinstance(cfgs, list) else []:
                f = c.get("url_file") or c.get("api_url_file")
                if f and f.split("/")[4] not in mounted:
                    out.append(f"receiver {name}: {f} is not under a secret the VMAlertmanager mounts")
    probe_rule = rules.get("AlertDeliveryProbe", {})
    label = (probe_rule.get("labels") or {}).get("probe")
    routed = [r for r in routes if r.get("matchers") == [f'probe="{label}"']]
    if not label or len(routed) != 1 or routed[0]["receiver"] != "delivery-probe":
        out.append("the probe label of AlertDeliveryProbe must be routed to the delivery-probe receiver")
    quiet = receivers.get("delivery-probe", {}).get("slack_configs", [{}])[0]
    if quiet.get("send_resolved") is not False:
        out.append("the delivery-probe receiver must not send resolved messages")
    inh = cfg.get("inhibit_rules") or []
    ok = [i for i in inh if i.get("target_matchers") == ['alertname="Watchdog"']
          and len(i.get("source_matchers", [])) == 1
          and i["source_matchers"][0].removeprefix('alertname="').removesuffix('"') in rules]
    if len(ok) != 1:
        out.append("one inhibit rule must silence the Watchdog on an alert defined by the opt-in rules")
    for h in ("hooks.slack.com", "hc-ping.com"):
        if h not in egress_fqdns:
            out.append(f"the Alertmanager egress policy no longer allows {h}")
    return out


def render(component):
    with tempfile.TemporaryDirectory() as t:
        k = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
             "resources": [os.path.relpath(REPO / "apps/flux/workload", t)]}
        if component:
            k["components"] = [os.path.relpath(COMPONENT, t)]
        (Path(t) / "kustomization.yaml").write_text(yaml.safe_dump(k))
        r = subprocess.run(["kubectl", "kustomize", "--load-restrictor", "LoadRestrictionsNone", t],
                           capture_output=True, text=True)
        if r.returncode:
            sys.exit(f"kubectl kustomize failed: {r.stderr}")
        return [d for d in yaml.safe_load_all(r.stdout) if d]


def problems_component():
    out = []
    off = {d["metadata"]["name"] for d in render(False) if d["kind"] == "Kustomization"}
    if "observability-alert-probe" in off:
        out.append("the probe Kustomization is in the default workload profile: it must be opt-in")
    on = [d for d in render(True) if d["kind"] == "Kustomization" and d["metadata"]["name"] == "observability-alert-probe"]
    if len(on) != 1:
        return out + ["the component must add exactly one Kustomization observability-alert-probe"]
    spec = on[0]["spec"]
    if {"name": "observability-vm-customresources"} not in spec.get("dependsOn", []):
        out.append("the probe Kustomization must depend on observability-vm-customresources (the VMRule CRD)")
    if not (REPO / spec["path"]).joinpath("kustomization.yaml").is_file():
        out.append(f"the probe Kustomization path {spec['path']} has no kustomization.yaml")
    return out


def main():
    am, cfg, rules = load()
    vma = next(d for d in docs(VMA) if d["kind"] == "VMAlert")
    main_cnp = next(d for d in docs(NETPOL) if d["metadata"]["name"] == "vmalertmanager")
    egress_cnp = next(d for d in docs(EGRESS) if d["kind"] == "CiliumNetworkPolicy")
    kusts = {d["metadata"]["name"]: d for d in docs(OBS_FLUX) if d["kind"] == "Kustomization"}
    doc = DOC.read_text()
    fq = set(egress_hosts(egress_cnp).values())
    bad = (problems(am, cfg, rules, fq) + notifier_problems(vma, am) + egress_problems(main_cnp, egress_cnp, kusts, doc)
           + problems_component())
    for b in bad:
        print("FAIL", b)
    if "--selftest" in sys.argv:
        def mut(f):
            a, c, r = copy.deepcopy(am), yaml.safe_load(yaml.safe_dump(cfg)), copy.deepcopy(rules)
            f(a, c, r)
            return problems(a, c, r, fq)
        cases = {
            "watchdog not first": lambda a, c, r: c["route"]["routes"].reverse(),
            "repeat not greater": lambda a, c, r: c["route"]["routes"][0].update(repeat_interval="1m"),
            "heartbeat slower than 5 min": lambda a, c, r: c["route"]["routes"][0].update(repeat_interval="5m"),
            "unknown receiver": lambda a, c, r: c["route"]["routes"][1].update(receiver="nope"),
            "secret not mounted": lambda a, c, r: a["spec"].update(secrets=["alertmanager-slack"]),
            "probe label drift": lambda a, c, r: r["AlertDeliveryProbe"]["labels"].update(probe="other"),
            "probe resolves": lambda a, c, r: c["receivers"][1]["slack_configs"][0].update(send_resolved=True),
            "inhibit gone": lambda a, c, r: c.pop("inhibit_rules"),
            "inhibit source renamed": lambda a, c, r: r.pop("AlertDeliveryNotAcknowledged"),
        }
        for name, f in cases.items():
            got = mut(f)
            print(("selftest ok   " if got else "selftest FAIL (not caught) ") + name)
            bad += [] if got else [name]
        if not problems(am, cfg, rules, set()):
            print("selftest FAIL (egress not caught)")
            bad.append("egress")
        def egress_mut(f):
            m, e, k = copy.deepcopy(main_cnp), copy.deepcopy(egress_cnp), copy.deepcopy(kusts)
            f(m, e, k)
            return egress_problems(m, e, k, doc)
        for name, f in {
                "the main policy lists a host again": lambda m, e, k: m["spec"]["egress"].append({"toFQDNs": [{"matchName": "hooks.slack.com"}]}),
                "a host written in full in the egress policy": lambda m, e, k: e["spec"]["egress"][-1]["toFQDNs"].append({"matchName": "example.org"}),
                "the substitution is not optional": lambda m, e, k: k["observability-alert-egress"]["spec"]["postBuild"]["substituteFrom"][0].update(optional=False),
                "no substitution at all": lambda m, e, k: k["observability-alert-egress"]["spec"].pop("postBuild"),
                "observability substitutes (dashboards would be blanked)": lambda m, e, k: k["observability"]["spec"].update(postBuild={"substituteFrom": []}),
        }.items():
            got = egress_mut(f)
            print(("selftest ok   " if got else "selftest FAIL (not caught) ") + name)
            bad += [] if got else [name]
        if not [x for x in egress_problems(main_cnp, egress_cnp, kusts, "") if "does not name" in x]:
            print("selftest FAIL (undocumented input not caught)")
            bad.append("doc")
        base = "http://vmalertmanager-alertmanager-%d.vmalertmanager-alertmanager.services-observability.svc:9093"
        for name, urls, replicas in (
                ("notifier is the headless Service", ["http://vmalertmanager-alertmanager.services-observability:9093"], 2),
                ("notifier misses one pod", [base % 0], 2),
                ("a replica was added without its URL", [base % 0, base % 1], 3)):
            a, v = copy.deepcopy(am), copy.deepcopy(vma)
            a["spec"]["replicaCount"] = replicas
            v["spec"]["notifiers"] = [{"url": u} for u in urls]
            got = notifier_problems(v, a)
            print(("selftest ok   " if got else "selftest FAIL (not caught) ") + name)
            bad += [] if got else [name]
    print(f"{len(bad)} failed")
    sys.exit(1 if bad else 0)


main()
