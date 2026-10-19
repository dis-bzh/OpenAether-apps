#!/usr/bin/env bash
# Render the Loki chart with the values the repository ships (base and local overlay) and assert
# what the manifests only claim: the compactor enforces retention, the S3 link is not forced to
# http, a PDB exists, no canary DaemonSet, no ClusterRole, and the CiliumNetworkPolicy lets the ring
# form and the S3 FQDN rule learn its addresses. Needs kubectl, helm and python3 with PyYAML (network
# for the chart). Rung: mocked/static, nothing runs on a cluster.
#   Usage: scripts/dev/test-loki-render.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
for bin in kubectl helm python3; do command -v "$bin" >/dev/null || { echo "$bin is required" >&2; exit 2; }; done
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

kubectl kustomize "$ROOT/apps/base/observability" > "$WORK/base.yaml"
kubectl kustomize "$ROOT/apps/base/observability-local" > "$WORK/local.yaml"

# The HelmRelease's own spec.values, plus stand-ins for what valuesFrom injects at run time.
extract() { python3 -I - "$1" "$2" "$3" <<'PY'
import sys, yaml
src, values_out, version_out = sys.argv[1:4]
repos = {d["metadata"]["name"]: d["spec"]["url"] for d in yaml.safe_load_all(open(src)) if d and d.get("kind") == "HelmRepository"}
for d in yaml.safe_load_all(open(src)):
    if d and d.get("kind") == "HelmRelease" and d["metadata"]["name"] == "loki":
        open(version_out + ".repo", "w").write(repos[d["spec"]["chart"]["spec"]["sourceRef"]["name"]])
        v = d["spec"]["values"]
        s3 = v.setdefault("loki", {}).setdefault("storage", {}).setdefault("s3", {})
        # What valuesFrom injects; a literal in spec.values would win over it (checked below).
        fed = {(f.get("valuesKey"), f.get("targetPath")) for f in d["spec"].get("valuesFrom", [])}
        for key, target in (("region", "loki.storage.s3.region"), ("endpoint", "loki.storage.s3.endpoint"),
                            ("bucket", "loki.storage.bucketNames.chunks"), ("bucket", "loki.storage.bucketNames.ruler"),
                            ("bucket", "loki.storage.bucketNames.admin")):
            if (key, target) not in fed:
                sys.exit("valuesFrom does not feed %s from the Secret" % target)
        # spec.values is merged AFTER valuesFrom and wins: a literal here silently replaces the seeded value.
        for k in ("region", "endpoint"):
            if k in s3:
                sys.exit("spec.values sets loki.storage.s3.%s: it would override the seeded one" % k)
        if v["loki"]["storage"].get("bucketNames"):
            sys.exit("spec.values sets loki.storage.bucketNames: it would override the seeded one")
        s3.update(endpoint="https://s3.example.invalid", accessKeyId="AK", secretAccessKey="SK", region="fr-par")
        v["loki"]["storage"]["bucketNames"] = {"chunks": "bkt-chunks", "ruler": "bkt-ruler", "admin": "bkt-admin"}
        yaml.safe_dump(v, open(values_out, "w"))
        open(version_out, "w").write(d["spec"]["chart"]["spec"]["version"])
        break
else:
    sys.exit("no HelmRelease loki in " + src)
PY
}
extract "$WORK/base.yaml"  "$WORK/base-values.yaml"  "$WORK/version"
extract "$WORK/local.yaml" "$WORK/local-values.yaml" "$WORK/version"
VERSION="$(cat "$WORK/version")"; REPO="$(cat "$WORK/version.repo")"   # the chart repository the HelmRelease names

helm pull loki --repo "$REPO" --version "$VERSION" --untar --untardir "$WORK/chart" >/dev/null
for env in base local; do
  helm template loki "$WORK/chart/loki" --namespace services-observability -f "$WORK/$env-values.yaml" > "$WORK/$env-rendered.yaml"
done

python3 -I - "$WORK" <<'PY'
import re, sys, yaml
work = sys.argv[1]
fails = 0
def check(label, ok):
    global fails
    print(("ok   " if ok else "FAIL ") + label)
    fails += 0 if ok else 1

def load(env):
    docs = [d for d in yaml.safe_load_all(open(f"{work}/{env}-rendered.yaml")) if d]
    kinds = {}
    for d in docs:
        kinds.setdefault(d["kind"], []).append(d)
    cfg = None
    for d in kinds.get("ConfigMap", []) + kinds.get("Secret", []):
        raw = (d.get("data") or {}).get("config.yaml")
        if raw:
            cfg = yaml.safe_load(raw)
    return kinds, cfg

def hours(v):
    return int(v[:-1]) * {"h": 1, "d": 24}[v[-1]]

for env, replicas, rf, retention, insecure, pdb in (("base", 3, 2, 8760, False, True), ("local", 1, 1, 168, True, False)):
    kinds, cfg = load(env)
    sts = [d for d in kinds.get("StatefulSet", []) if d["metadata"]["name"] == "loki"]
    anti = ((sts[0]["spec"]["template"]["spec"].get("affinity") or {}).get("podAntiAffinity") or {}) if sts else {}
    check(f"{env}: no hard pod anti-affinity (replicas may share a node when there are fewer nodes than replicas)",
          not anti.get("requiredDuringSchedulingIgnoredDuringExecution"))
    check(f"{env}: one StatefulSet loki with {replicas} replica(s)", len(sts) == 1 and sts[0]["spec"]["replicas"] == replicas)
    check(f"{env}: no ClusterRole nor ClusterRoleBinding (no cluster-wide read of ConfigMaps/Secrets)",
          not kinds.get("ClusterRole") and not kinds.get("ClusterRoleBinding"))
    check(f"{env}: no rules sidecar container (loki-sc-rules)",
          not [c for d in sts for c in d["spec"]["template"]["spec"]["containers"] if "sc-rules" in c["name"]])
    # The container does not exist (rules sidecar off), so only the values can prove the 0.2.0 context is still
    # there for the day someone re-enables it.
    sc = (yaml.safe_load(open(f"{work}/{env}-values.yaml")).get("sidecar") or {}).get("securityContext") or {}
    check(f"{env}: sidecar.securityContext kept (non-root, no escalation, drop ALL, RuntimeDefault) next to rules.enabled false",
          sc.get("runAsNonRoot") is True and sc.get("allowPrivilegeEscalation") is False
          and sc.get("capabilities", {}).get("drop") == ["ALL"] and sc.get("seccompProfile", {}).get("type") == "RuntimeDefault")
    check(f"{env}: no loki-canary DaemonSet", not [d for d in kinds.get("DaemonSet", []) if "canary" in d["metadata"]["name"]])
    check(f"{env}: PodDisruptionBudget {'present with minAvailable 1' if pdb else 'absent'}",
          (len(kinds.get("PodDisruptionBudget", [])) == 1 and kinds["PodDisruptionBudget"][0]["spec"].get("minAvailable") == 1) if pdb
          else not kinds.get("PodDisruptionBudget"))
    comp = (cfg or {}).get("compactor", {})
    check(f"{env}: compactor enforces retention (retention_enabled, delete_request_store s3, working_directory)",
          comp.get("retention_enabled") is True and comp.get("delete_request_store") == "s3" and comp.get("working_directory"))
    got = cfg["limits_config"]["retention_period"]
    check(f"{env}: retention_period {got} == {retention}h", hours(got) == retention)
    check(f"{env}: replication_factor {rf}", cfg["common"]["replication_factor"] == rf)
    s3 = cfg["common"]["storage"]["s3"]
    check(f"{env}: s3 insecure {insecure}", bool(s3.get("insecure", False)) is insecure)
    check(f"{env}: s3 path style kept", s3.get("s3forcepathstyle") is True)
    check(f"{env}: s3 region is the injected one, not a literal", s3.get("region") == "fr-par")
    # The rule that makes enabling the compactor safe: it deletes what the limit says is old.
    check(f"{env}: compactor never runs with a retention under 168h", hours(got) >= 168)
    if env == "base":
        check("base: a replica can be down and a write still reaches RF (replicas > RF)", replicas > rf)
        check("base: the shipped retention is the one-year baseline", hours(got) >= 8760)
        check("base: s3 endpoint is https in the render", s3["endpoint"].startswith("https://"))

docs = [d for d in yaml.safe_load_all(open(work + "/base.yaml")) if d]
# Chart hop safety (lab 15c): an upgrade must not be rolled back by Helm and needs a budget over the 5 minute default.
hr = next((d for d in docs if d.get("kind") == "HelmRelease" and d["metadata"]["name"] == "loki"), {})
up = hr.get("spec", {}).get("upgrade", {})
def minutes(v):
    return int(v[:-1]) * {"m": 1, "h": 60}[v[-1]] if v else 5
check("helmrelease: upgrade.timeout is at least 15m (a rolling update of the replicas takes longer than the 5m default)",
      minutes(up.get("timeout")) >= 15)
check("helmrelease: upgrade.remediation.retries 0 and remediateLastFailure false (no automatic Helm rollback of a chart hop)",
      up.get("remediation", {}).get("retries") == 0 and up.get("remediation", {}).get("remediateLastFailure") is False)
check("helmrelease: install keeps its retries (a failed first install has nothing to lose)",
      hr.get("spec", {}).get("install", {}).get("remediation", {}).get("retries", 0) >= 1)
bsts = [d for d in load("base")[0].get("StatefulSet", []) if d["metadata"]["name"] == "loki"]
check("base: the pod advertises its own IP to memberlist (a stale address is what stalled the lab hop)",
      bool(bsts) and any("-memberlist.advertise-addr=$(POD_IP)" in (c.get("args") or []) for c in bsts[0]["spec"]["template"]["spec"]["containers"]))

# The ExternalSecret: the region key is emitted, with the host regex and the us-east-1 fallback (the stand-in region above
# would hide the removal of this template).
es = next((d for d in docs if d.get("kind") == "ExternalSecret" and d["metadata"]["name"] == "loki-s3-credentials"), None)
check("externalsecret: loki-s3-credentials exists", es is not None)
if es:
    tpl = es["spec"].get("target", {}).get("template", {}).get("data", {})
    check("externalsecret: the Secret carries accessKey, secretKey, endpoint, bucket and region",
          {"accessKey", "secretKey", "endpoint", "bucket", "region"} <= set(tpl))
    check("externalsecret: extract (an absent optional region must not fail it)",
          es["spec"].get("dataFrom") == [{"extract": {"key": "observability/loki-s3"}}] and not es["spec"].get("data"))
    region = tpl.get("region", "")
    check("externalsecret: region falls back to us-east-1 and prefers a seeded value",
          "us-east-1" in region and 'index . "region"' in region and "regexMatch" in region)
    # Re-evaluate the template's regexes on fixtures: Go string literals unescape \\ to \.
    lit = lambda name: re.search(r"\$%s := \"(.*?)\" -}}" % name, region)
    if lit("hosts") and lit("oos"):
        un = lambda m: m.group(1).replace("\\\\", "\\")
        # The printf format is read from the template too: a hard-coded copy would not notice an unanchored template.
        fmt = re.search(r'regexMatch \(printf "(.*?)" \$hosts \$oos\)', region)
        check("externalsecret: the host match is a readable printf of $hosts and $oos", fmt is not None and fmt.group(1).count("%s") == 2)
        pat = re.compile(un(fmt) % (un(lit("hosts")), un(lit("oos"))) if fmt else "$^")
        sub = re.search(r'regexReplaceAll "(.*?)" \.endpoint', region)
        ext = re.compile(sub.group(1).replace("\\\\", "\\")) if sub else None
        def derive(ep):
            m = pat.match(ep)
            return ext.sub(r"\2", ep) if m and ext else "us-east-1"
        for ep, want in (("https://s3.fr-par.scw.cloud", "fr-par"), ("https://s3.eu-west-par.io.cloud.ovh.net", "eu-west-par"),
                         ("https://oos.eu-west-2.outscale.com", "eu-west-2"), ("http://minio.foundation-storage:9000", "us-east-1"),
                         ("https://s3.fr-par.scw.cloud.evil.example", "us-east-1"), ("https://evil.example/s3.fr-par.scw.cloud", "us-east-1")):
            check("externalsecret: %s gives %s" % (ep, want), derive(ep) == want)
        # The hosts the egress patterns admit (test-s3-egress-fqdns.py proves the policy side) must each yield a region.
        for h in ("s3.fr-par.scw.cloud", "s3.nl-ams.scw.cloud", "s3.eu-west-par.io.cloud.ovh.net", "s3.gra.io.cloud.ovh.net",
                  "oos.eu-west-2.outscale.com"):
            check("externalsecret: the S3 host %s yields a non-default region" % h, derive("https://" + h) != "us-east-1")
    else:
        check("externalsecret: the host regexes are readable", False)

# The network policy (kustomize output of the base): the 2026-10-13 failure was a missing rule, so
# deleting one of these must fail the gate.
policy = next((d for d in yaml.safe_load_all(open(f"{work}/base.yaml"))
               if d and d.get("kind") == "CiliumNetworkPolicy" and d["metadata"]["name"] == "loki"), None)
check("netpol: CiliumNetworkPolicy loki exists", policy is not None)
if policy:
    spec = policy["spec"]
    def ports(rule):
        return {(p["port"], p["protocol"]) for tp in rule.get("toPorts", []) for p in tp.get("ports", [])}
    def peers(rule, key):
        return any(e.get("matchLabels", {}).get("app.kubernetes.io/name") == "loki" for e in rule.get(key, []))
    def from_app(app):
        return [r for r in spec["ingress"]
                if any(e.get("matchLabels", {}).get("app.kubernetes.io/name") == app for e in r.get("fromEndpoints", []))]
    ring = {("3100", "TCP"), ("9095", "TCP"), ("7946", "TCP"), ("7946", "UDP")}
    dns = [r for r in spec["egress"] if any(e.get("matchLabels", {}).get("k8s-app") == "kube-dns" for e in r.get("toEndpoints", []))]
    check("netpol: DNS egress carries rules.dns (Cilium learns the S3 addresses from the lookup)",
          bool(dns) and all(tp.get("rules", {}).get("dns") for r in dns for tp in r["toPorts"]))
    check("netpol: Loki egress to Loki peers on 3100, 9095, 7946/tcp and 7946/udp",
          any(peers(r, "toEndpoints") and ring <= ports(r) for r in spec["egress"]))
    check("netpol: Loki ingress from Loki peers on 3100, 9095, 7946/tcp and 7946/udp",
          any(peers(r, "fromEndpoints") and ring <= ports(r) for r in spec["ingress"]))
    check("netpol: ingress from Alloy and Grafana on 3100",
          all(any(("3100", "TCP") in ports(r) for r in from_app(a)) for a in ("alloy", "grafana")))
    fq = [r for r in spec["egress"] if r.get("toFQDNs")]
    check("netpol: S3 egress is FQDN-scoped on 443/tcp, not toEntities world",
          len(fq) == 1 and ports(fq[0]) == {("443", "TCP")} and not any("world" in r.get("toEntities", []) for r in spec["egress"]))
    check("netpol: FQDN list covers every region of the Scaleway, OVH and Outscale S3 hosts",
          {"s3.*.scw.cloud", "s3.*.io.cloud.ovh.net", "oos.*.outscale.com"}
          <= {f.get("matchPattern") for r in fq for f in r["toFQDNs"]})
    check("netpol: local MinIO egress on 9000/tcp",
          any(("9000", "TCP") in ports(r) for r in spec["egress"] if any(e.get("matchLabels", {}).get("io.kubernetes.pod.namespace") == "foundation-storage" for e in r.get("toEndpoints", []))))

print(f"{fails} failed")
sys.exit(1 if fails else 0)
PY
