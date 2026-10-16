#!/usr/bin/env python3
"""The control-plane audit-log collector keeps its narrow shape (ANSSI PA-012 R2/R9; batch B22 step 1).

alloy-audit mounts a node path and runs as root, so what keeps that acceptable is checked here, statically:
  - the hostPath is exactly /var/log/audit/kube, mounted read-only, DirectoryOrCreate, and nothing else is a hostPath;
  - it runs on control planes only, with a read-only root filesystem and ALL capabilities dropped but DAC_READ_SEARCH;
  - it lives in its own privileged namespace, and no other workload of the brick shares that namespace;
  - Kyverno exempts it BY NAME (alloy-audit-*) for hostPath and root, never its whole namespace;
  - its CiliumNetworkPolicy allows DNS and Loki :3100 only (no kube-apiserver), and Loki's accepts it;
  - the labels it sets are log_type and node only; the local Docker overlay removes it;
  - it is SUSPENDED by default and the opt-in component audit-log-collector un-suspends exactly that release.
Static: it reads the manifests, not a cluster. Each rule has a self-test that proves it would catch the regression.
"""
import copy
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
OBS = REPO / "apps/base/observability"
NS = "services-observability-audit"
PATH = "/var/log/audit/kube"


def docs(path):
    return [d for d in yaml.safe_load_all(Path(path).read_text()) if isinstance(d, dict)]


def release(ds):
    return next(d for d in ds if d["kind"] == "HelmRelease" and d["metadata"]["name"] == "alloy-audit")


def problems_release(hr):
    out = []
    v = hr["spec"]["values"]
    if hr["spec"].get("suspend") is not True:
        out.append("alloy-audit must be suspended by default (opt-in through apps/flux/components/audit-log-collector)")
    if hr["metadata"]["namespace"] != NS:
        out.append(f"HelmRelease alloy-audit is in {hr['metadata']['namespace']}, not {NS}")
    host = [x for x in v["controller"]["volumes"]["extra"] if "hostPath" in x]
    if len(host) != 1 or host[0]["hostPath"] != {"path": PATH, "type": "DirectoryOrCreate"}:
        out.append(f"the only hostPath must be {PATH} (DirectoryOrCreate), found {host}")
    else:
        mounts = [m for m in v["alloy"]["mounts"]["extra"] if m["name"] == host[0]["name"]]
        if len(mounts) != 1 or mounts[0].get("readOnly") is not True or mounts[0]["mountPath"] != PATH:
            out.append("the hostPath must be mounted read-only at its own path")
    if v["controller"].get("nodeSelector") != {"node-role.kubernetes.io/control-plane": ""}:
        out.append("alloy-audit must be restricted to control planes (nodeSelector)")
    sc = v["alloy"].get("securityContext") or {}
    if sc.get("readOnlyRootFilesystem") is not True or sc.get("allowPrivilegeEscalation") is not False \
            or sc.get("capabilities", {}).get("drop") != ["ALL"] or sc.get("capabilities", {}).get("add") != ["DAC_READ_SEARCH"]:
        out.append("alloy-audit container: readOnlyRootFilesystem, no privilege escalation, drop ALL, add DAC_READ_SEARCH and nothing else")
    # Root alone cannot read a 0600 file owned by the apiserver user: both halves are the claim (review L2).
    if sc.get("runAsUser") != 0 or sc.get("runAsNonRoot") is not False:
        out.append("alloy-audit must run as root (runAsUser 0, runAsNonRoot false): the audit file is not world-readable")
    if sc.get("privileged"):
        out.append("alloy-audit must not be privileged")
    if v["controller"].get("hostNetwork") or v["controller"].get("hostPID") or v.get("hostNetwork"):
        out.append("alloy-audit must not use a host namespace")
    content = v["alloy"]["configMap"]["content"]
    keys = set(re.findall(r'^\s*"(\w+)"\s*=', content.split("local.file_match", 1)[1].split("]", 1)[0], re.M)) - {"__path__"}
    if keys != {"log_type", "node"}:
        out.append(f"the audit stream may carry the labels log_type and node only, found {sorted(keys)}")
    if "tail_from_end" not in content:
        out.append("tail_from_end must be set (a re-created pod would re-read the whole file)")
    if v.get("rbac", {}).get("create") is not False:
        out.append("rbac.create must be false: nothing to discover")
    return out


def problems_kyverno(priv, root, caps):
    out = []
    for label, policy in (("restrict-privileged", priv), ("disallow-root-user", root), ("restrict-capabilities", caps)):
        rule_name = {"restrict-privileged": "deny-hostpath", "disallow-root-user": "check-runasnonroot",
                     "restrict-capabilities": "deny-sensitive-capabilities"}[label]
        excl = [r["resources"] for rule in policy["spec"]["rules"] if rule["name"] == rule_name
                for r in rule["exclude"]["any"]]
        named = [e for e in excl if NS in e.get("namespaces", []) and "alloy-audit-*" in e.get("names", [])]
        whole = [e for e in excl if NS in e.get("namespaces", []) and not e.get("names")]
        if not named:
            out.append(f"{label}: no name-scoped exclusion for alloy-audit-* in {NS}")
        if whole:
            out.append(f"{label}: {NS} is excluded as a whole, it must be by pod name")
    deny_priv = next(r for r in priv["spec"]["rules"] if r["name"] == "deny-privileged")
    if any(NS in e["resources"].get("namespaces", []) for e in deny_priv["exclude"]["any"]):
        out.append(f"deny-privileged must not exempt {NS}")
    return out


def problems_policies(cnps):
    out = []
    mine = next((c for c in cnps if c["metadata"]["name"] == "alloy-audit" and c["metadata"]["namespace"] == NS), None)
    if not mine:
        return [f"no CiliumNetworkPolicy alloy-audit in {NS}"]
    dests = []
    for rule in mine["spec"].get("egress", []):
        if "toEntities" in rule or "toFQDNs" in rule or "toCIDR" in rule:
            out.append("alloy-audit egress must name endpoints only (no entity, FQDN or CIDR: no kube-apiserver, no world)")
        for ep in rule.get("toEndpoints", []):
            dests.append((ep["matchLabels"].get("k8s-app") or ep["matchLabels"].get("app.kubernetes.io/name"),
                          tuple(sorted(p["port"] for t in rule.get("toPorts", []) for p in t["ports"]))))
    if sorted(dests) != [("kube-dns", ("53", "53")), ("loki", ("3100",))]:
        out.append(f"alloy-audit egress must be DNS and Loki :3100 only, found {dests}")
    loki = next(c for c in cnps if c["metadata"]["name"] == "loki")
    if not any(f.get("matchLabels", {}).get("io.kubernetes.pod.namespace") == NS
               for r in loki["spec"]["ingress"] for f in r.get("fromEndpoints", [])):
        out.append(f"the loki CiliumNetworkPolicy does not accept pushes from {NS}")
    return out


def problems_namespace(namespaces, brick):
    out = []
    ns = namespaces.get(NS)
    if not ns:
        return [f"Namespace {NS} is not declared in apps/base/namespaces"]
    if ns["metadata"]["labels"].get("pod-security.kubernetes.io/enforce") != "privileged":
        out.append(f"Namespace {NS} must enforce privileged (hostPath)")
    for d in brick:
        if d.get("kind") in ("HelmRelease", "Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job") \
                and d["metadata"].get("namespace") == NS and d["metadata"]["name"] != "alloy-audit":
            out.append(f"{d['kind']}/{d['metadata']['name']} shares the privileged namespace {NS}")
    return out


def problems_local(local):
    patches = (local.get("patches") or [])
    ok = any(p["target"].get("name") == "alloy-audit" and "$patch: delete" in p["patch"] for p in patches)
    return [] if ok else ["the local overlay must delete alloy-audit (no Talos audit file on a Docker node)"]


def problems_component():
    """Render the component over the Flux DAG, then do what Flux does with the patch it adds to `observability`."""
    import os
    import subprocess
    import tempfile

    def build(d):
        r = subprocess.run(["kubectl", "kustomize", "--load-restrictor=LoadRestrictionsNone", str(d)], capture_output=True, text=True)
        return [x for x in yaml.safe_load_all(r.stdout) if x] if r.returncode == 0 else None

    with tempfile.TemporaryDirectory() as tmp:
        def render(resources, components=(), patches=()):
            k = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
                 "resources": [os.path.relpath(p, tmp) for p in resources]}
            if components:
                k["components"] = [os.path.relpath(p, tmp) for p in components]
            if patches:
                k["patches"] = list(patches)
            Path(tmp, "kustomization.yaml").write_text(yaml.safe_dump(k))
            return build(tmp)
        flux = REPO / "apps/flux/base"
        comp = REPO / "apps/flux/components/audit-log-collector"
        plain, with_c = render([flux]), render([flux], [comp])
        if plain is None or with_c is None:
            return ["kubectl kustomize failed on the Flux base or the audit-log-collector component"]
        ks = lambda docs: {d["metadata"]["name"]: d for d in docs if d["kind"] == "Kustomization"}  # noqa: E731
        p, w = ks(plain), ks(with_c)
        out = []
        if {n for n in w if w[n] != p.get(n)} != {"observability"}:
            out.append("the component must change the observability Kustomization and nothing else")
        if "patches" in p["observability"]["spec"]:
            out.append("the observability Kustomization already patches: the component would replace that list")
        brick = REPO / w["observability"]["spec"]["path"]
        before = render([brick])
        after = render([brick], patches=w["observability"]["spec"].get("patches", []))
        if before is None or after is None:
            return out + ["kubectl kustomize failed on the observability brick"]
        rel = lambda docs: {(d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"]): d for d in docs}  # noqa: E731
        b, a = rel(before), rel(after)
        key = ("HelmRelease", NS, "alloy-audit")
        if b[key]["spec"].get("suspend") is not True or a[key]["spec"].get("suspend") is not False:
            out.append("the component must turn alloy-audit from suspended to running")
        if {k: v for k, v in b.items() if k != key} != {k: v for k, v in a.items() if k != key}:
            out.append("the component must touch no other object of the observability brick")
        return out


def load():
    brick = [d for p in OBS.glob("*.yaml") if p.name != "kustomization.yaml" for d in docs(p)]
    namespaces = {d["metadata"]["name"]: d for d in docs(REPO / "apps/base/namespaces/namespaces.yaml") if d["kind"] == "Namespace"}
    pol = REPO / "apps/base/kyverno-policies"
    return (release(brick), yaml.safe_load((pol / "restrict-privileged.yaml").read_text()),
            yaml.safe_load((pol / "disallow-root-user.yaml").read_text()), yaml.safe_load((pol / "restrict-capabilities.yaml").read_text()),
            [d for d in brick if d["kind"] == "CiliumNetworkPolicy"], namespaces, brick,
            yaml.safe_load((REPO / "apps/base/observability-local/kustomization.yaml").read_text()))


def selftest(hr, priv, root, caps, cnps, namespaces, brick, local):
    """Each mutation must produce a problem, or the check above is blind."""
    def mut(fn, target):
        c = copy.deepcopy(target)
        fn(c)
        return c
    cases = [
        ("writable mount", problems_release, mut(lambda h: h["spec"]["values"]["alloy"]["mounts"]["extra"][1].pop("readOnly"), hr)),
        ("all control planes and workers", problems_release, mut(lambda h: h["spec"]["values"]["controller"].pop("nodeSelector"), hr)),
        ("second hostPath", problems_release, mut(lambda h: h["spec"]["values"]["controller"]["volumes"]["extra"].append(
            {"name": "x", "hostPath": {"path": "/"}}), hr)),
        ("capability added", problems_release, mut(lambda h: h["spec"]["values"]["alloy"]["securityContext"]["capabilities"].update(
            add=["DAC_READ_SEARCH", "SYS_ADMIN"]), hr)),
        ("not root", problems_release, mut(lambda h: h["spec"]["values"]["alloy"]["securityContext"].update(runAsUser=1), hr)),
        ("DAC_READ_SEARCH missing", problems_release, mut(lambda h: h["spec"]["values"]["alloy"]["securityContext"]["capabilities"].pop("add"), hr)),
        ("privileged", problems_release, mut(lambda h: h["spec"]["values"]["alloy"]["securityContext"].update(privileged=True), hr)),
        ("cluster-wide ClusterRole", problems_release, mut(lambda h: h["spec"]["values"]["rbac"].update(create=True), hr)),
        ("hostNetwork", problems_release, mut(lambda h: h["spec"]["values"]["controller"].update(hostNetwork=True), hr)),
    ]
    for label, fn, bad in cases:
        if not fn(bad):
            return f"self-test: '{label}' went unnoticed"
    k = copy.deepcopy(root)
    k["spec"]["rules"][0]["exclude"]["any"][0]["resources"]["namespaces"].append(NS)
    k["spec"]["rules"][0]["exclude"]["any"] = [e for e in k["spec"]["rules"][0]["exclude"]["any"] if e["resources"].get("names") != ["alloy-audit-*"]]
    if not problems_kyverno(priv, k, caps):
        return "self-test: a namespace-wide root exemption went unnoticed"
    kc = copy.deepcopy(caps)
    kc["spec"]["rules"][0]["exclude"]["any"][-1] = {"resources": {"namespaces": [NS]}}
    if not problems_kyverno(priv, root, kc):
        return "self-test: a namespace-wide capabilities exemption went unnoticed"
    c = copy.deepcopy(cnps)
    next(x for x in c if x["metadata"]["name"] == "alloy-audit")["spec"]["egress"].append({"toEntities": ["kube-apiserver"]})
    if not problems_policies(c):
        return "self-test: kube-apiserver egress went unnoticed"
    if not problems_namespace(namespaces, brick + [{"kind": "Deployment", "metadata": {"name": "x", "namespace": NS}}]):
        return "self-test: a second workload in the privileged namespace went unnoticed"
    if not problems_release(mut(lambda h: h["spec"].pop("suspend"), hr)):
        return "self-test: an alloy-audit that is not suspended by default went unnoticed"
    if not problems_local({}):
        return "self-test: a local overlay that keeps alloy-audit went unnoticed"
    return None


def main():
    hr, priv, root, caps, cnps, namespaces, brick, local = load()
    err = selftest(hr, priv, root, caps, cnps, namespaces, brick, local)
    problems = [err] if err else []
    problems += problems_release(hr) + problems_kyverno(priv, root, caps) + problems_policies(cnps) \
        + problems_namespace(namespaces, brick) + problems_local(local) + problems_component()
    if problems:
        print("\n".join(problems))
        return 1
    print("ok: alloy-audit reads one read-only hostPath on control planes, as root with DAC_READ_SEARCH only, "
          "exempt by name only, and may talk to DNS and Loki only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
