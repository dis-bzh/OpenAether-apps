#!/usr/bin/env python3
"""The audit sink (infra #318) keeps its narrow, opt-in shape. Static: it reads the manifests, not a cluster.

The shipper mounts a node path and runs as root, the sink is class A (zero loss), and the whole thing must cost a default install nothing.
What keeps that acceptable is checked here:
  - OFF: no profile, no base Kustomization and no Flux base file mentions it; only the component brings it;
  - the shipper: the only hostPath is /var/log/audit/kube, read-only, on control planes only, read-only root filesystem, ALL capabilities
    dropped but DAC_READ_SEARCH, no host namespace, no API token, limits; it runs ship.sh, which uses --no-lock and never writes the source;
  - the namespace holds the shipper and the check and nothing else, no Role/ClusterRole at all, and Kyverno exempts the shipper BY NAME
    (audit-sink-shipper-*) in all three policies, never the namespace and never the check;
  - the check is non-root, read-only and is not given the repository password;
  - the network policies allow DNS and the sink hosts on 443 only, hosts from cluster-identity with an impossible default;
  - the substitution never touches the scripts (two Flux Kustomizations);
  - the sink's alerts carry a runbook and severity critical for the class-A ones.
Each rule has a self-test that proves it would catch the regression.
"""
import copy
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
BASE = REPO / "apps/base/observability-audit-sink"
POLICY = REPO / "apps/base/observability-audit-sink-policy"
COMP = REPO / "apps/flux/components/audit-log-sink"
NS = "services-observability-audit-sink"
PATH = "/var/log/audit/kube"


def docs(path):
    return [d for d in yaml.safe_load_all(Path(path).read_text()) if isinstance(d, dict)]


def load():
    d = {"shipper": docs(BASE / "shipper.yaml")[0], "shipper2": docs(REPO / "apps/base/observability-audit-sink-secondary/shipper.yaml")[0], "checker": docs(BASE / "checker.yaml"), "ns": docs(BASE / "namespace.yaml")[0],
         "es": docs(BASE / "externalsecret.yaml")[0], "rule": docs(BASE / "vmrule.yaml")[0], "cnp": docs(POLICY / "networkpolicy.yaml"),
         "comp": docs(COMP / "audit-sink.yaml"), "ship": (BASE / "scripts/ship.sh").read_text(), "check": (BASE / "scripts/check.py").read_text(),
         "kust": yaml.safe_load((BASE / "kustomization.yaml").read_text()), "sa": docs(BASE / "serviceaccounts.yaml")}
    d["kyverno"] = {n: docs(REPO / f"apps/base/kyverno-policies/{n}.yaml")[0] for n in ("restrict-privileged", "disallow-root-user", "restrict-capabilities")}
    return d


def pod(d):
    return d["spec"]["template"]["spec"]


def problems(d):
    out = []
    for ds in (d["shipper"], d["shipper2"]):
        out += shipper_problems(d, ds)
    return out + rest_problems(d)


def shipper_problems(d, ds):
    out = []
    ps = pod(ds)
    host = [v for v in ps["volumes"] if "hostPath" in v]
    if len(host) != 1 or host[0]["hostPath"] != {"path": PATH, "type": "DirectoryOrCreate"}:
        out.append(f"the only hostPath must be {PATH} (DirectoryOrCreate), found {host}")
    else:
        m = [x for c in ps["containers"] for x in c["volumeMounts"] if x["name"] == host[0]["name"]]
        if len(m) != 1 or m[0].get("readOnly") is not True or m[0]["mountPath"] != PATH:
            out.append("the hostPath must be mounted read-only at its own path")
    if ps.get("nodeSelector") != {"node-role.kubernetes.io/control-plane": ""}:
        out.append("the shipper must be restricted to control planes")
    if ps.get("automountServiceAccountToken") is not False:
        out.append("the shipper must not mount a ServiceAccount token")
    for flag in ("hostNetwork", "hostPID", "hostIPC"):
        if ps.get(flag):
            out.append(f"the shipper must not use {flag}")
    for c in ps["containers"]:
        sc = c.get("securityContext") or {}
        if sc.get("readOnlyRootFilesystem") is not True or sc.get("allowPrivilegeEscalation") is not False or sc.get("privileged"):
            out.append("shipper: readOnlyRootFilesystem, no privilege escalation, not privileged")
        if sc.get("capabilities", {}).get("drop") != ["ALL"] or sc.get("capabilities", {}).get("add") != ["DAC_READ_SEARCH"]:
            out.append("shipper: drop ALL, add DAC_READ_SEARCH and nothing else")
        if sc.get("runAsUser") != 0 or sc.get("runAsNonRoot") is not False:
            out.append("shipper: root is the claim (the audit file is 0600): runAsUser 0, runAsNonRoot false")
        r = c.get("resources", {})
        if not r.get("limits", {}).get("memory") or not r.get("limits", {}).get("cpu") or not r.get("requests"):
            out.append("shipper: requests and limits")
        if not re.search(r"@sha256:|:\d", c["image"]):
            out.append(f"shipper image not pinned: {c['image']}")
        if c["command"] != ["/bin/sh", "/scripts/ship.sh"]:
            out.append("the shipper must run /scripts/ship.sh")
    work = [v for v in ps["volumes"] if v["name"] == "work"]
    if not work or "sizeLimit" not in work[0].get("emptyDir", {}):
        out.append("the work volume must be an emptyDir with a sizeLimit (the buffer is bounded)")
    return out


def rest_problems(d):
    out = []
    # the script
    s = d["ship"]
    if "--no-lock" not in s:
        out.append("ship.sh must run restic with --no-lock (the key pair has no delete right)")
    if re.search(r"\brestic\b[^\n]*\b(forget|prune|unlock|rewrite)\b", s) or re.search(r"\brm\b[^\n]*\$SRC_DIR|\bmv\b[^\n]*\$SRC_DIR|>\s*\"?\$SRC_DIR", s):
        out.append("ship.sh must never delete or write: no forget/prune/unlock, nothing written under SRC_DIR")
    # the check
    chk = [x for x in d["checker"] if x["kind"] == "Deployment"][0]
    cps = pod(chk)
    for c in cps["containers"]:
        sc = c["securityContext"]
        if sc.get("runAsNonRoot") is not True or sc.get("readOnlyRootFilesystem") is not True or sc.get("capabilities", {}).get("drop") != ["ALL"]:
            out.append("the check must run non-root, read-only, no capability")
    for v in cps["volumes"]:
        sec = v.get("secret")
        if sec and "items" not in sec:
            out.append(f"the check must project only the keys it needs from {sec['secretName']} (it must not get RESTIC_PASSWORD)")
        if sec and any(i["key"] == "RESTIC_PASSWORD" for i in sec.get("items", [])):
            out.append("the check is given the repository password")
    # the namespace: two workloads, no RBAC, nothing else
    kinds = {}
    for f in BASE.glob("*.yaml"):
        if f.name == "kustomization.yaml":
            continue
        for x in docs(f):
            kinds.setdefault(x["kind"], []).append(x["metadata"]["name"])
    for k in ("Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding"):
        if k in kinds:
            out.append(f"the sink needs no {k}: neither pod talks to the Kubernetes API")
    sec = {x["kind"]: x["metadata"]["name"] for f in (REPO / "apps/base/observability-audit-sink-secondary").glob("*.yaml") if f.name != "kustomization.yaml" for x in docs(f)}
    if sec != {"DaemonSet": "audit-sink-shipper-secondary", "ExternalSecret": "audit-sink-secondary-credentials"}:
        out.append(f"the secondary directory holds the second shipper and its ExternalSecret only: {sec}")
    if sorted(kinds.get("DaemonSet", []) + kinds.get("Deployment", []) + kinds.get("StatefulSet", []) + kinds.get("CronJob", [])) != \
            ["audit-sink-check", "audit-sink-shipper"]:
        out.append(f"only the shipper and the check may run in {NS}: {kinds}")
    if d["ns"]["metadata"]["name"] != NS:
        out.append("namespace name")
    # kyverno: by name, the shipper only
    for n, pol in d["kyverno"].items():
        ok = False
        for rule in pol["spec"]["rules"]:
            for blk in rule.get("exclude", {}).get("any", []):
                r = blk["resources"]
                if NS in r.get("namespaces", []):
                    if r.get("names") != ["audit-sink-shipper-*"]:
                        out.append(f"{n}: the exemption in {NS} must be exactly audit-sink-shipper-* (found {r.get('names')})")
                    else:
                        ok = True
        if not ok:
            out.append(f"{n}: no exemption by name for audit-sink-shipper-*")
    # the policies
    for c in d["cnp"]:
        for e in c["spec"].get("egress", []):
            for bad in ("toEntities", "toCIDR", "toCIDRSet", "toServices"):
                if bad in e:
                    out.append(f"{c['metadata']['name']}: egress {bad} is not allowed (DNS and the sink's FQDN only)")
            for f in e.get("toFQDNs", []):
                if "matchPattern" in f or not re.fullmatch(r"\$\{AUDIT_SINK(_SECONDARY)?_HOST:=[a-z.-]+\.invalid\}", f.get("matchName", "")):
                    out.append(f"{c['metadata']['name']}: an FQDN must be matchName ${{AUDIT_SINK_HOST:=...invalid}} (found {f})")
            if "toFQDNs" in e:
                if [p["port"] for t in e["toPorts"] for p in t["ports"]] != ["443"]:
                    out.append(f"{c['metadata']['name']}: the sink is reached on 443 only")
        if c["metadata"]["name"] != "audit-sink-check" and c["spec"].get("ingress"):
            out.append(f"{c['metadata']['name']}: the shipper serves nothing, no ingress")
    # flux
    byname = {x["metadata"]["name"]: x for x in d["comp"]}
    main_, pol_ = byname.get("observability-audit-sink"), byname.get("observability-audit-sink-policy")
    if not main_ or not pol_:
        out.append("the component must carry the two Kustomizations")
    else:
        if "postBuild" in main_["spec"]:
            out.append("substitution on the Kustomization that holds the scripts would blank their shell variables")
        if not pol_["spec"].get("postBuild", {}).get("substituteFrom"):
            out.append("the policy Kustomization must substitute the host names from cluster-identity")
    # secret
    if d["es"]["spec"]["dataFrom"] != [{"extract": {"key": "audit/sink"}}]:
        out.append("the ExternalSecret must read audit/sink and nothing else")
    # alerts
    for g in d["rule"]["spec"]["groups"]:
        for r in g["rules"]:
            if not r["annotations"].get("runbook_url", "").endswith("#" + r["alert"].lower()):
                out.append(f"{r['alert']}: runbook_url must end in #{r['alert'].lower()}")
            if r["labels"]["severity"] != "critical":
                out.append(f"{r['alert']}: class A, critical")
    return out


def off_by_default():
    out = []
    scan = [REPO / "apps/flux/base", REPO / "apps/flux/workload", REPO / "apps/flux/management", REPO / "apps/flux/local", REPO / "apps/base/observability",
            REPO / "apps/base/observability-local", REPO / "apps/base/namespaces"]
    for root in scan:
        for f in [root] if root.is_file() else sorted(root.rglob("*")):
            if f.is_file() and f.suffix in (".yaml", ".yml", ".py", ".json") and re.search(r"audit-(log-)?sink", f.read_text()):
                out.append(f"{f.relative_to(REPO)} mentions the audit sink: it must come only with the component")
    if "audit-sink" in (REPO / "apps/flux/bricks.yaml").read_text():
        out.append("apps/flux/bricks.yaml (the pick) must not list the audit sink")
    return out


def main():
    d = load()
    bad = problems(d) + off_by_default()
    for b in bad:
        print("FAIL", b)
    if "--selftest" in sys.argv:
        def mut(label, fn):
            c = copy.deepcopy(d)
            fn(c)
            if not problems(c):
                bad.append(label)
                print("FAIL selftest: not caught:", label)
        mut("extra hostPath", lambda c: pod(c["shipper"])["volumes"].append({"name": "root", "hostPath": {"path": "/"}}))
        mut("writable audit mount", lambda c: pod(c["shipper"])["containers"][0]["volumeMounts"][0].pop("readOnly"))
        mut("extra capability", lambda c: pod(c["shipper"])["containers"][0]["securityContext"]["capabilities"]["add"].append("SYS_ADMIN"))
        mut("not read-only rootfs", lambda c: pod(c["shipper"])["containers"][0]["securityContext"].update(readOnlyRootFilesystem=False))
        mut("privileged", lambda c: pod(c["shipper"])["containers"][0]["securityContext"].update(privileged=True))
        mut("secondary privileged", lambda c: pod(c["shipper2"])["containers"][0]["securityContext"].update(privileged=True))
        mut("secondary on every node", lambda c: pod(c["shipper2"]).pop("nodeSelector"))
        mut("token mounted", lambda c: pod(c["shipper"]).update(automountServiceAccountToken=True))
        mut("hostNetwork", lambda c: pod(c["shipper"]).update(hostNetwork=True))
        mut("all nodes", lambda c: pod(c["shipper"]).pop("nodeSelector"))
        mut("unbounded work volume", lambda c: pod(c["shipper"])["volumes"][1].update(emptyDir={}))
        mut("locks used", lambda c: c.update(ship=c["ship"].replace("--no-lock", "")))
        mut("prune", lambda c: c.update(ship=c["ship"] + "\nrestic prune\n"))
        mut("check gets the password", lambda c: [v for v in pod([x for x in c["checker"] if x["kind"] == "Deployment"][0])["volumes"] if "secret" in v][0]["secret"].pop("items"))
        mut("kyverno exempts the namespace", lambda c: c["kyverno"]["restrict-privileged"]["spec"]["rules"][1]["exclude"]["any"].append(
            {"resources": {"namespaces": [NS], "names": ["*"]}}))
        mut("world egress", lambda c: c["cnp"][0]["spec"]["egress"].append({"toEntities": ["world"]}))
        mut("fqdn pattern", lambda c: c["cnp"][0]["spec"]["egress"][1]["toFQDNs"].append({"matchPattern": "*.example"}))
        mut("substitution on the scripts", lambda c: [x for x in c["comp"] if x["metadata"]["name"] == "observability-audit-sink"][0]["spec"].update(
            postBuild={"substituteFrom": []}))
        mut("severity", lambda c: c["rule"]["spec"]["groups"][0]["rules"][0]["labels"].update(severity="warning"))
    print("audit sink: ok" if not bad else f"audit sink: {len(bad)} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
