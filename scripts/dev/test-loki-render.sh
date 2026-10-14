#!/usr/bin/env bash
# Render the Loki chart with the values the repository ships and assert what the manifests only claim (#53): the seeded
# S3 destination (endpoint, buckets, region) is not overridden by spec.values, the ExternalSecret builds the region, and
# the CiliumNetworkPolicy lets the ring form and the S3 FQDN rule learn its addresses. Needs kubectl, helm and python3
# with PyYAML (network for the chart). Rung: mocked/static, nothing runs on a cluster; the ExternalSecret template is
# checked as text plus a Python re-evaluation of its host regex, never by ESO.
#   Usage: scripts/dev/test-loki-render.sh [REPO_ROOT]
set -euo pipefail
ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
for bin in kubectl helm python3; do command -v "$bin" >/dev/null || { echo "$bin is required" >&2; exit 2; }; done
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

kubectl kustomize "$ROOT/apps/base/observability" > "$WORK/base.yaml"

# The HelmRelease's own spec.values, plus stand-ins for what valuesFrom injects at run time.
python3 -I - "$WORK/base.yaml" "$WORK" <<'PY'
import sys, yaml
src, work = sys.argv[1:3]
for d in yaml.safe_load_all(open(src)):
    if d and d.get("kind") == "HelmRelease" and d["metadata"]["name"] == "loki":
        v = d["spec"]["values"]
        s3 = v.setdefault("loki", {}).setdefault("storage", {}).setdefault("s3", {})
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
        yaml.safe_dump(v, open(work + "/values.yaml", "w"))
        open(work + "/version", "w").write(d["spec"]["chart"]["spec"]["version"])
        break
else:
    sys.exit("no HelmRelease loki in " + src)
PY
helm pull loki --repo https://grafana.github.io/helm-charts --version "$(cat "$WORK/version")" --untar --untardir "$WORK/chart" >/dev/null
helm template loki "$WORK/chart/loki" --namespace services-observability -f "$WORK/values.yaml" > "$WORK/rendered.yaml"

python3 -I - "$WORK" <<'PY'
import re, sys, yaml
work = sys.argv[1]
fails = 0
def check(label, ok):
    global fails
    print(("ok   " if ok else "FAIL ") + label)
    fails += 0 if ok else 1

cfg = None
for d in yaml.safe_load_all(open(work + "/rendered.yaml")):
    if d and d["kind"] in ("ConfigMap", "Secret"):
        raw = (d.get("data") or {}).get("config.yaml")
        if raw:
            cfg = yaml.safe_load(raw)
s3 = cfg["common"]["storage"]["s3"]
check("config: s3 region is the injected one, not a literal", s3.get("region") == "fr-par")
check("config: s3 endpoint is the injected one", s3.get("endpoint") == "https://s3.example.invalid")
check("config: s3 chunks bucket is the injected one", s3.get("bucketnames") == "bkt-chunks")
check("config: replication_factor 2 (the ring egress below depends on it)", cfg["common"]["replication_factor"] == 2)

docs = [d for d in yaml.safe_load_all(open(work + "/base.yaml")) if d]
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
        fq = next((r for d in docs if d.get("kind") == "CiliumNetworkPolicy" and d["metadata"]["name"] == "loki"
                   for r in d["spec"]["egress"] if r.get("toFQDNs")), {"toFQDNs": []})
        for f in fq["toFQDNs"]:
            h = f["matchName"]
            check("externalsecret: the listed S3 host %s yields a non-default region" % h, derive("https://" + h) != "us-east-1")
    else:
        check("externalsecret: the host regexes are readable", False)

# The network policy: the 2026-10-13 failure was a missing rule, so deleting one of these must fail the gate.
policy = next((d for d in docs if d.get("kind") == "CiliumNetworkPolicy" and d["metadata"]["name"] == "loki"), None)
check("netpol: CiliumNetworkPolicy loki exists", policy is not None)
if policy:
    spec = policy["spec"]
    def ports(rule):
        return {(p["port"], p["protocol"]) for tp in rule.get("toPorts", []) for p in tp.get("ports", [])}
    def peers(rule, key):
        return any(e.get("matchLabels", {}).get("app.kubernetes.io/name") == "loki" for e in rule.get(key, []))
    ring = {("3100", "TCP"), ("9095", "TCP"), ("7946", "TCP"), ("7946", "UDP")}
    dns = [r for r in spec["egress"] if any(e.get("matchLabels", {}).get("k8s-app") == "kube-dns" for e in r.get("toEndpoints", []))]
    check("netpol: DNS egress carries rules.dns (Cilium learns the S3 addresses from the lookup)",
          bool(dns) and all(tp.get("rules", {}).get("dns") for r in dns for tp in r["toPorts"]))
    check("netpol: Loki egress to Loki peers on 3100, 9095, 7946/tcp and 7946/udp",
          any(peers(r, "toEndpoints") and ring <= ports(r) for r in spec["egress"]))
    check("netpol: Loki ingress from Loki peers on 3100, 9095, 7946/tcp and 7946/udp",
          any(peers(r, "fromEndpoints") and ring <= ports(r) for r in spec["ingress"]))
    fq = [r for r in spec["egress"] if r.get("toFQDNs")]
    check("netpol: S3 egress is FQDN-scoped on 443/tcp, not toEntities world",
          len(fq) == 1 and ports(fq[0]) == {("443", "TCP")} and not any("world" in r.get("toEntities", []) for r in spec["egress"]))
    check("netpol: FQDN list names the Scaleway, OVH and Outscale S3 hosts",
          {"s3.fr-par.scw.cloud", "s3.eu-west-par.io.cloud.ovh.net", "oos.eu-west-2.outscale.com"}
          <= {f.get("matchName") for r in fq for f in r["toFQDNs"]})
    check("netpol: local MinIO egress on 9000/tcp",
          any(("9000", "TCP") in ports(r) for r in spec["egress"]
              if any(e.get("matchLabels", {}).get("io.kubernetes.pod.namespace") == "foundation-storage" for e in r.get("toEndpoints", []))))

print(f"{fails} failed")
sys.exit(1 if fails else 0)
PY
