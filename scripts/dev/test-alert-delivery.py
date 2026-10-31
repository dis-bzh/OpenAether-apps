#!/usr/bin/env python3
"""The alert-delivery chain keeps its shape (OpenAether 0.4.0 M1; docs/alert-delivery.md).

Static: reads the manifests and renders the opt-in component, never a cluster. Checked:
  - the Watchdog route is FIRST and goes to the heartbeat receiver, repeating strictly slower than it groups;
  - every route names a receiver that exists, and every url_file/api_url_file comes from a secret the CR mounts;
  - the probe label of the opt-in rule is the one the quiet receiver is routed on, and that receiver sends no "resolved";
  - the inhibit rule stops the Watchdog on an alert the opt-in rules define;
  - the component is OFF in the default profiles and adds exactly one Flux Kustomization, which waits for the VMRule CRDs;
  - the egress policy of Alertmanager still allows the chat and heartbeat hosts it is configured with.
Each rule has a self-test that proves it would catch the regression.
"""
import copy
import os
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


def docs(path):
    return [d for d in yaml.safe_load_all(Path(path).read_text()) if isinstance(d, dict)]


def load():
    am = next(d for d in docs(VMA) if d["kind"] == "VMAlertmanager")
    cfg = yaml.safe_load(am["spec"]["configRawYaml"])
    rules = {r["alert"]: r for d in docs(PROBE) for g in d["spec"]["groups"] for r in g["rules"]}
    return am, cfg, rules


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
    fq = {m["matchName"] for d in docs(NETPOL) if d["metadata"]["name"] == "vmalertmanager"
          for e in d["spec"]["egress"] for m in e.get("toFQDNs", [])}
    bad = problems(am, cfg, rules, fq) + problems_component()
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
    print(f"{len(bad)} failed")
    sys.exit(1 if bad else 0)


main()
