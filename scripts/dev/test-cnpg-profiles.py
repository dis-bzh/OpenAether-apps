#!/usr/bin/env python3
"""The CNPG database profiles (apps/components/cnpg-profile-*, docs/database-profiles.md) are optional, render with the values
mechanism, fit the vendored CRDs, carry the guarantees they claim and pass the platform policies. Static: manifests and a
Kyverno CLI run, no cluster. What a profile does under load or a fault is the lab (docs/database-profiles.md, "Lab pass criteria").
Run: python3 -I scripts/dev/test-cnpg-profiles.py [--selftest]   (KYVERNO=path to the 1.19.x CLI; skipped without it unless CI=true)
"""
import copy
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
COMP = REPO / "apps/components"
PROFILES = ("light", "standard", "ha")
fails = 0


def check(cond, msg):
    global fails
    print(("ok   " if cond else "FAIL ") + msg)
    fails += not cond


def render(path):
    r = subprocess.run(["kubectl", "kustomize", str(path)], capture_output=True, text=True)
    if r.returncode:
        return None, r.stderr.strip()[-400:]
    return [d for d in yaml.safe_load_all(r.stdout) if d], ""


def crd_schemas():
    out = {}
    for f in ("apps/base/cnpg/cnpg-1.30.1.yaml", "apps/base/cnpg/barman-cloud-plugin-0.15.1.yaml"):
        for d in yaml.safe_load_all((REPO / f).read_text()):
            if d and d.get("kind") == "CustomResourceDefinition":
                v = next(v for v in d["spec"]["versions"] if v.get("storage"))
                out[(d["spec"]["group"], d["spec"]["names"]["kind"])] = v["schema"]["openAPIV3Schema"]
    return out


def validate(obj, sch, path="$"):
    """Errors of obj against an openAPIV3 schema: unknown field, wrong type, bad enum, missing required."""
    errs = []
    if sch.get("x-kubernetes-int-or-string"):
        return [] if isinstance(obj, (int, str)) else ["%s: not int-or-string" % path]
    t = sch.get("type")
    if t == "object" or "properties" in sch:
        if not isinstance(obj, dict):
            return ["%s: not an object" % path]
        props = sch.get("properties", {})
        for k, v in obj.items():
            if k in props:
                errs += validate(v, props[k], "%s.%s" % (path, k))
            elif isinstance(sch.get("additionalProperties"), dict):
                errs += validate(v, sch["additionalProperties"], "%s.%s" % (path, k))
            elif not sch.get("x-kubernetes-preserve-unknown-fields") and "additionalProperties" not in sch:
                errs.append("%s.%s: unknown field" % (path, k))
        for k in sch.get("required", []):
            if k not in obj:
                errs.append("%s: missing required %s" % (path, k))
    elif t == "array":
        if not isinstance(obj, list):
            return ["%s: not an array" % path]
        for i, v in enumerate(obj):
            errs += validate(v, sch.get("items", {}), "%s[%d]" % (path, i))
    elif t == "string" and not isinstance(obj, str):
        errs.append("%s: not a string" % path)
    elif t == "integer" and (not isinstance(obj, int) or isinstance(obj, bool)):
        errs.append("%s: not an integer" % path)
    elif t == "boolean" and not isinstance(obj, bool):
        errs.append("%s: not a boolean" % path)
    if "enum" in sch and obj not in sch["enum"]:
        errs.append("%s: %r not in %s" % (path, obj, sch["enum"]))
    return errs


def by_kind(docs, kind):
    return [d for d in docs if d["kind"] == kind]


def selftest():
    sch = crd_schemas()[("postgresql.cnpg.io", "Cluster")]
    good, _ = render(COMP / "cnpg-profile-examples/ha")
    c = by_kind(good, "Cluster")[0]
    check(validate(c["spec"], sch["properties"]["spec"]) == [], "selftest: the real ha Cluster validates")
    bad = copy.deepcopy(c)
    bad["spec"]["postgresql"]["synchronous"]["dataDurability"] = "always"
    bad["spec"]["postgresql"]["synchronous"]["numbre"] = 1
    bad["spec"]["instances"] = "3"
    e = validate(bad["spec"], sch["properties"]["spec"])
    check(len(e) == 3, "selftest: a bad enum, an unknown field and a wrong type are all caught (%d)" % len(e))


def main():
    sch = crd_schemas()
    # 1. off by default: nothing outside apps/components names a profile, and no brick renders one
    users = [str(p.relative_to(REPO)) for p in (REPO / "apps").rglob("*")
             if p.is_file() and COMP not in p.parents and "cnpg-profile" in p.read_text(errors="ignore")]
    check(users == [], "off by default: no manifest outside apps/components references a profile (%s)" % users)
    base, err = render(REPO / "apps/base/cnpg")
    check(base is not None and not any("openaether.io/db-profile" in (d["metadata"].get("labels") or {}) for d in base),
          "off by default: the cnpg brick renders no profile object " + err)

    pol = [d for d in yaml.safe_load_all((REPO / "apps/base/kyverno-policies-cnpg/restrict-restore-test-clusters.yaml").read_text()) if d]
    rendered = {}
    for p in PROFILES:
        docs, err = render(COMP / "cnpg-profile-examples" / p)
        check(docs is not None, "%s: the example renders %s" % (p, err))
        if docs is None:
            continue
        rendered[p] = docs
        text = yaml.safe_dump_all(docs)
        left = sorted(set(re.findall(r"\b(?:NAME|CLIENT_NAMESPACE|CLIENT_APP|OWNER|DATABASE|STORAGE_CLASS|SIZE|CPU_REQUEST|CPU_LIMIT|MEMORY|ARCHIVE_SERVER_NAME)\b", text)))
        check(left == [], "%s: no placeholder is left after the values are applied %s" % (p, left))
        cl = by_kind(docs, "Cluster")
        check(len(cl) == 1, "%s: exactly one Cluster" % p)
        c = cl[0]
        spec = c["spec"]
        for d in docs:
            if d["kind"] in ("Namespace",):
                continue
            check(d["metadata"].get("namespace") == "shop", "%s: %s/%s is in the application's namespace" % (p, d["kind"], d["metadata"]["name"]))
            if (d["apiVersion"].startswith(("postgresql.cnpg.io", "barmancloud.cnpg.io", "cilium.io"))
                    or d["kind"] in ("ExternalSecret", "PodDisruptionBudget")):
                check((d["metadata"].get("labels") or {}).get("openaether.io/db-profile"), "%s: %s carries the db-profile label" % (p, d["kind"]))
            crd = sch.get((d["apiVersion"].split("/")[0], d["kind"]))
            if crd:
                e = validate(d.get("spec", {}), crd["properties"]["spec"])
                check(e == [], "%s: %s/%s fits the vendored CRD %s" % (p, d["kind"], d["metadata"]["name"], e[:3]))
        ns = by_kind(docs, "Namespace")[0]["metadata"]["labels"]
        check(ns.get("pod-security.kubernetes.io/enforce") == "restricted" and "istio.io/dataplane-mode" not in ns,
              "%s: the example namespace is restricted PSA and outside the ambient mesh" % p)
        check(not c["metadata"]["name"].startswith("rt-") and "openaether.io/restore-test" not in c["metadata"].get("labels", {}),
              "%s: the Cluster is not mistaken for a restore-test scratch Cluster (Kyverno guard)" % p)
        check(spec.get("enableSuperuserAccess") is False, "%s: enableSuperuserAccess is false" % p)
        check(spec["imageName"].startswith("ghcr.io/cloudnative-pg/postgresql:") and re.search(r":\d+\.\d+$", spec["imageName"]),
              "%s: the engine image is the pinned CNPG image" % p)
        hba = spec["postgresql"]["pg_hba"]
        check(hba[0] == "hostnossl all all all reject" and "md5" not in " ".join(hba) and "trust" not in " ".join(hba)
              and hba[-1] == "hostssl all all all scram-sha-256", "%s: TLS and SCRAM only (no md5, no trust, plain TCP rejected)" % p)
        check(spec["postgresql"]["parameters"].get("password_encryption") == "scram-sha-256", "%s: new passwords are SCRAM" % p)
        r = spec["resources"]
        check(r["requests"]["memory"] == r["limits"]["memory"] and "cpu" in r["requests"] and "cpu" in r["limits"],
              "%s: CPU and memory bounded, memory request = limit" % p)
        check(spec["bootstrap"]["initdb"]["secret"]["name"] == "shop-db-credentials"
              and any(d["metadata"]["name"] == "shop-db-credentials" for d in by_kind(docs, "ExternalSecret")),
              "%s: the owner's password comes from an ExternalSecret, not git" % p)
        check(not any(k in spec for k in ("superuserSecret", "minSyncReplicas", "maxSyncReplicas")), "%s: no superuser secret, no legacy sync keys" % p)
        # the database policy: default deny both ways, the application is rule 0, nothing open to the world
        np = next(d for d in by_kind(docs, "CiliumNetworkPolicy") if d["metadata"]["labels"]["openaether.io/db-netpol"] == "database")
        app = np["spec"]["ingress"][0]["fromEndpoints"][0]["matchLabels"]
        check(app == {"k8s:io.kubernetes.pod.namespace": "shop", "k8s:app.kubernetes.io/name": "shop-api"}, "%s: ingress rule 0 is the application only" % p)
        check(np["spec"]["endpointSelector"]["matchLabels"] == {"cnpg.io/cluster": "shop-db"} and np["spec"].get("egress"),
              "%s: the policy selects this Cluster's pods and restricts egress too" % p)
        check("0.0.0.0/0" not in yaml.safe_dump(np) and "world" not in yaml.safe_dump(np) and "fromEntities" not in yaml.safe_dump(np),
              "%s: no CIDR, world or entity in the database policy" % p)
        # the restore-test guard leaves a profile Cluster alone: the rule's preconditions are false for it
        pre = pol[0]["spec"]["rules"][0]["preconditions"]["any"]
        check(not c["metadata"]["name"].startswith("rt-") and len(pre) == 2, "%s: the guard's two preconditions do not match the Cluster" % p)

    # 2. what each profile promises
    L, S, H = (rendered[p] for p in PROFILES)
    cl, cs, ch = (by_kind(x, "Cluster")[0]["spec"] for x in (L, S, H))
    check(cl["instances"] == 1 and "synchronous" not in cl["postgresql"] and "plugins" not in cl and not by_kind(L, "Pooler")
          and not by_kind(L, "ScheduledBackup") and cl["enablePDB"] is False, "light: 1 instance, no replication, no backup, no pooler")
    sy = cs["postgresql"]["synchronous"]
    check(cs["instances"] == 2 and sy == {"method": "any", "number": 1, "dataDurability": "preferred"} and not by_kind(S, "Pooler"),
          "standard: 2 instances, quorum any 1, PREFERRED durability, no pooler")
    sy = ch["postgresql"]["synchronous"]
    check(ch["instances"] == 3 and sy["method"] == "any" and sy["number"] == 1 and sy["dataDurability"] == "required" and sy["failoverQuorum"] is True,
          "ha: 3 instances, quorum any 1, REQUIRED durability, failover quorum")
    check(1 <= sy["number"] and ch["instances"] - 1 - sy["number"] >= 1, "ha: one replica can be lost without blocking writes")
    for p, spec in (("standard", cs), ("ha", ch)):
        tol = {t["key"]: t["tolerationSeconds"] for t in spec["affinity"]["tolerations"]}
        check(tol == {"node.kubernetes.io/unreachable": 30, "node.kubernetes.io/not-ready": 30}, "%s: 30 s tolerations (failover in 30 s, not 300 s)" % p)
        terms = spec["affinity"]["additionalPodAntiAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]
        check(spec["affinity"]["podAntiAffinityType"] == "preferred" and spec["affinity"]["topologyKey"] == "kubernetes.io/hostname"
              and any(t["podAffinityTerm"]["topologyKey"] == "topology.kubernetes.io/zone" for t in terms)
              and "requiredDuringSchedulingIgnoredDuringExecution" not in spec["affinity"]["additionalPodAntiAffinity"],
              "%s: hostname and zone spread, both preferences (a lost node or zone never blocks a pod)" % p)
    for p, docs, spec in (("standard", S, cs), ("ha", H, ch)):
        pl = spec["plugins"][0]
        names = {d["metadata"]["name"] for d in by_kind(docs, "ObjectStore")}
        check(pl["name"] == "barman-cloud.cloudnative-pg.io" and pl["isWALArchiver"] and pl["parameters"]["barmanObjectName"] in names
              and pl["parameters"]["serverName"] == "shop-db", "%s: WAL archived through the Barman plugin to its ObjectStore" % p)
        sb = by_kind(docs, "ScheduledBackup")[0]
        check(sb["spec"]["method"] == "plugin" and sb["spec"]["cluster"]["name"] == "shop-db" and len(sb["spec"]["schedule"].split()) == 6,
              "%s: a base backup through the plugin on a 6-field schedule" % p)
        prim = next(d for d in by_kind(docs, "ObjectStore") if d["metadata"]["labels"].get("openaether.io/db-store") == "primary")
        check(prim["spec"]["configuration"]["destinationPath"].endswith("/cnpg/shop-db")
              and prim["spec"]["configuration"]["wal"]["encryption"] == "AES256", "%s: archive under cnpg/<name>, server-side encryption requested" % p)
        eg = yaml.safe_dump(next(d for d in by_kind(docs, "CiliumNetworkPolicy") if d["metadata"]["labels"]["openaether.io/db-netpol"] == "database")["spec"]["egress"])
        check("scw.cloud" in eg and "ovh.net" in eg, "%s: S3 egress by FQDN, on the database policy" % p)
    stores = {d["metadata"]["labels"]["openaether.io/db-store"] for d in by_kind(H, "ObjectStore")}
    check(stores == {"primary", "replica"}, "ha: both stores are declared (primary archived, replica read side)")
    check(not by_kind(S, "ObjectStore") or {d["metadata"]["labels"]["openaether.io/db-store"] for d in by_kind(S, "ObjectStore")} == {"primary"},
          "standard: one store")
    check(not any("scw.cloud" in yaml.safe_dump(d) for d in by_kind(L, "CiliumNetworkPolicy")), "light: no S3 egress (no backup)")

    # 3. the Pooler of ha
    po = by_kind(H, "Pooler")[0]
    ps = po["spec"]
    check(po["metadata"]["name"] == "shop-db-pooler-rw" and ps["cluster"]["name"] == "shop-db" and ps["type"] == "rw", "pooler: read-write, fronts the Cluster")
    check(ps["instances"] == 2 and ps["pgbouncer"]["poolMode"] == "transaction", "pooler: 2 pods, transaction pooling")
    # lab 23: PgBouncer serves the Cluster's server certificate; verify-full to the pooler needs its Service names in it
    alt = by_kind(H, "Cluster")[0]["spec"].get("certificates", {}).get("serverAltDNSNames", [])
    check(alt == ["shop-db-pooler-rw", "shop-db-pooler-rw.shop", "shop-db-pooler-rw.shop.svc", "shop-db-pooler-rw.shop.svc.cluster.local"],
          "pooler: the server certificate names the pooler Service (sslmode=verify-full works through it): %s" % alt)
    check("certificates" not in by_kind(S, "Cluster")[0]["spec"] and "certificates" not in by_kind(L, "Cluster")[0]["spec"],
          "light and standard: no extra certificate names (no pooler)")
    prm = ps["pgbouncer"]["parameters"]
    check(int(prm["query_wait_timeout"]) >= 120 and int(prm["server_login_retry"]) <= 2 and int(prm["max_prepared_statements"]) > 0
          and prm["server_reset_query_always"] == "0", "pooler: waits through a failover, retries a lost server at once, prepared statements on")
    check(int(prm["default_pool_size"]) * ps["instances"] < 100, "pooler: pool size x pods stays under PostgreSQL's default max_connections (100)")
    check(ps["pgbouncer"]["image"].startswith("ghcr.io/cloudnative-pg/pgbouncer:") and "latest" not in ps["pgbouncer"]["image"], "pooler: pinned image")
    tpl = ps["template"]["spec"]
    check(tpl["securityContext"]["runAsNonRoot"] and tpl["containers"][0]["securityContext"]["readOnlyRootFilesystem"]
          and tpl["containers"][0]["securityContext"]["capabilities"] == {"drop": ["ALL"]}, "pooler: restricted pod and container security context")
    keys = {c["topologyKey"] for c in tpl["topologySpreadConstraints"]}
    check(keys == {"kubernetes.io/hostname", "topology.kubernetes.io/zone"} and all(c["whenUnsatisfiable"] == "ScheduleAnyway" for c in tpl["topologySpreadConstraints"])
          and all(c["labelSelector"]["matchLabels"] == {"cnpg.io/poolerName": "shop-db-pooler-rw"} for c in tpl["topologySpreadConstraints"]),
          "pooler: spread over hostname and zone, soft, selecting its own pods")
    pdb = by_kind(H, "PodDisruptionBudget")[0]
    check(pdb["spec"]["maxUnavailable"] == 1 and pdb["spec"]["selector"]["matchLabels"] == {"cnpg.io/poolerName": "shop-db-pooler-rw"},
          "pooler: a budget of one disruption selecting its pods")
    pn = next(d for d in by_kind(H, "CiliumNetworkPolicy") if d["metadata"]["labels"]["openaether.io/db-netpol"] == "pooler")
    check(pn["spec"]["endpointSelector"]["matchLabels"] == {"cnpg.io/poolerName": "shop-db-pooler-rw"}
          and pn["spec"]["egress"][1]["toEndpoints"][0]["matchLabels"] == {"cnpg.io/cluster": "shop-db"}, "pooler: its policy selects the pooler pods and reaches only the Cluster")
    dn = next(d for d in by_kind(H, "CiliumNetworkPolicy") if d["metadata"]["labels"]["openaether.io/db-netpol"] == "database")
    check({"cnpg.io/poolerName": "shop-db-pooler-rw"} in [r["fromEndpoints"][0]["matchLabels"] for r in dn["spec"]["ingress"]],
          "pooler: the database policy admits the pooler pods")


    # 3b. the opt-in benchmark and zero-loss Jobs: suspended, restricted, admitted by the database policy, scripts identical to the files
    bench, err = render(COMP / "cnpg-profile-examples/ha-bench")
    check(bench is not None, "bench: the ha example with the Jobs renders " + err)
    if bench:
        jobs = {d["metadata"]["labels"]["openaether.io/bench-role"]: d for d in by_kind(bench, "Job")}
        check(set(jobs) == {"bench", "writer", "checker"}, "bench: a benchmark, a writer and a checker Job")
        check(all(j["spec"]["suspend"] is True and j["spec"]["backoffLimit"] == 0 for j in jobs.values()), "bench: every Job is suspended (nothing runs when applied)")
        btext = yaml.safe_dump_all(bench)
        check(not re.search(r"\b(?:NAME|CLIENT_APP|PROFILE|STORAGE_CLASS)\b", btext), "bench: no placeholder is left")
        check(all(j["metadata"]["namespace"] == "shop" and j["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/name"] == "shop-api"
                  for j in jobs.values()), "bench: the Jobs run as the application's pod (the database policy admits them)")
        for r, j in jobs.items():
            pod = j["spec"]["template"]["spec"]
            c = pod["containers"][0]
            check(pod["securityContext"]["runAsNonRoot"] and c["securityContext"]["readOnlyRootFilesystem"] and c["securityContext"]["capabilities"] == {"drop": ["ALL"]}
                  and c["resources"]["limits"]["memory"] and c["image"] == ch["imageName"],
                  "bench: the %s Job is restricted, bounded and uses the engine image of the Cluster" % r)
            envs = {e["name"]: e for e in c["env"]}
            check(envs["PGSSLMODE"]["value"] == "verify-full" and "password" not in str(c.get("args", "")) and "value" not in envs["PGPASSWORD"],
                  "bench: the %s Job verifies the server certificate and takes the password from the Secret, not an argument" % r)
        check(jobs["bench"]["spec"]["template"]["spec"]["containers"][0]["args"][1] == "ha" and any(
            e["name"] == "POOLER_HOST" and e["value"] == "shop-db-pooler-rw" for e in jobs["bench"]["spec"]["template"]["spec"]["containers"][0]["env"]),
            "bench: the benchmark names the profile and both endpoints")
        bn = [d for d in by_kind(bench, "CiliumNetworkPolicy") if d["metadata"]["labels"].get("openaether.io/db-profile") == "bench"]
        check(len(bn) == 1 and bn[0]["metadata"]["name"] == "shop-db-bench" and bn[0]["metadata"]["namespace"] == "shop"
              and bn[0]["spec"]["endpointSelector"]["matchLabels"] == {"openaether.io/db-profile": "bench"}
              and bn[0]["spec"]["egress"][0]["toEndpoints"][0]["matchLabels"] == {"cnpg.io/cluster": "shop-db"}
              and bn[0]["spec"]["egress"][0]["toPorts"][0]["ports"] == [{"port": "5432", "protocol": "TCP"}],
              "bench: the Job pods may reach their database and its pooler on 5432 (lab 23: a pod selected by a policy denies the rest)")
        cm = next(d for d in by_kind(bench, "ConfigMap") if d["metadata"]["name"] == "shop-db-bench-scripts")
        for f in ("db-bench.sh", "db-bench-parse.py", "db-zeroloss.sh"):
            check(cm["data"][f] == (COMP / "cnpg-profile-bench" / f).read_text() and (REPO / "scripts/ops" / f).resolve() == (COMP / "cnpg-profile-bench" / f).resolve(),
                  "bench: %s in the ConfigMap is the file, and scripts/ops/%s is the same file" % (f, f))
        check({v["persistentVolumeClaim"]["claimName"] for j in (jobs["writer"], jobs["checker"]) for v in j["spec"]["template"]["spec"]["volumes"] if "persistentVolumeClaim" in v}
              == {"shop-db-zeroloss-log"} and by_kind(bench, "PersistentVolumeClaim")[0]["spec"]["storageClassName"] == "local-path-retain",
              "bench: the acknowledged ids live on a volume of their own, shared by the writer and the checker")

    # 4. the values mechanism: other values reach every field, nothing of the example's stays
    tmp = Path(tempfile.mkdtemp())
    try:
        shutil.copytree(COMP, tmp / "components")
        k = tmp / "components/cnpg-profile-examples/ha/kustomization.yaml"
        s = k.read_text()
        for a, b in (("shop-db", "billing-db"), ("shop-api", "billing-web"), ("=shop", "=billing"), ("name: shop", "name: billing"), ("namespace: shop", "namespace: billing"), ("20Gi", "50Gi"),
                     ("local-path-retain", "longhorn-x1-rwo"), ("250m", "750m"), ("2Gi", "4Gi")):
            s = s.replace(a, b)
        k.write_text(s)
        ns = k.parent / "namespace.yaml"
        ns.write_text(ns.read_text().replace("shop", "billing"))
        docs, err = render(k.parent)
        check(docs is not None, "values: another set of values renders " + err)
        if docs:
            text = yaml.safe_dump_all(docs)
            check("shop" not in text, "values: nothing of the example's name, namespace or client is left (%s)" % sorted(set(re.findall(r"[^\n]*shop[^\n]*", text)))[:3])
            c = by_kind(docs, "Cluster")[0]["spec"]
            check(c["storage"] == {"storageClass": "longhorn-x1-rwo", "size": "50Gi"} and c["resources"]["requests"] == {"cpu": "750m", "memory": "4Gi"},
                  "values: size, storage class and resources reach the Cluster")
            check(c["bootstrap"]["initdb"]["database"] == "billing" and c["bootstrap"]["initdb"]["owner"] == "billing", "values: database and owner")
    finally:
        shutil.rmtree(tmp)

    # 5. the platform policies on the pods a profile makes (Pooler: its own template; instances: built from the
    # Cluster the way the
    # operator does, UNVERIFIED against the real pods until the lab reads the policy reports)
    ky = os.environ.get("KYVERNO") or shutil.which("kyverno")
    if not ky:
        print("SKIPPED the Kyverno run: no CLI (KYVERNO=...)")
        if os.environ.get("CI") == "true":
            check(False, "the Kyverno CLI is required in CI")
    else:
        work = Path(tempfile.mkdtemp())
        try:
            sec = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "runAsNonRoot": True,
                   "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}}
            pods = []
            for p, docs in rendered.items():
                spec = by_kind(docs, "Cluster")[0]["spec"]
                cons = [{"name": "postgres", "image": spec["imageName"], "resources": spec["resources"], "securityContext": sec}]
                for st in by_kind(docs, "ObjectStore")[:1]:
                    cons.append({"name": "plugin-barman-cloud", "image": "ghcr.io/cloudnative-pg/plugin-barman-cloud-sidecar:v0.15.1",
                                 "resources": st["spec"]["instanceSidecarConfiguration"]["resources"], "securityContext": sec})
                pods.append({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "i-" + p, "namespace": "shop"},
                             "spec": {"securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
                                      "initContainers": [{"name": "bootstrap-controller", "image": "ghcr.io/cloudnative-pg/cloudnative-pg:1.30.1",
                                                          "resources": spec["resources"], "securityContext": sec}],
                                      "containers": cons}})
            for j in jobs.values():
                pods.append({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": j["metadata"]["name"], "namespace": "shop"}, "spec": j["spec"]["template"]["spec"]})
            t = ps["template"]["spec"]
            pods.append({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "pooler", "namespace": "shop"},
                         "spec": dict(t, containers=[dict(t["containers"][0], image=ps["pgbouncer"]["image"])])})
            res = work / "pods.yaml"
            res.write_text(yaml.safe_dump_all(pods))
            r = subprocess.run([ky, "apply", str(REPO / "apps/base/kyverno-policies"), "--resource", str(res)], capture_output=True, text=True)
            m = re.search(r"pass: (\d+), fail: (\d+), warn: (\d+), error: (\d+)", r.stdout)
            check(bool(m) and m.group(2) == "0" and m.group(4) == "0" and int(m.group(1)) > 0,
                  "kyverno: the instance pods (built from each Cluster), the pooler pod and the bench Job pods pass every platform policy (%s)" % (m.group(0) if m else r.stderr[-200:]))
            bad = copy.deepcopy(pods[-1])  # the pooler
            bad["spec"]["containers"][0].pop("resources")
            res.write_text(yaml.safe_dump(bad))
            r = subprocess.run([ky, "apply", str(REPO / "apps/base/kyverno-policies"), "--resource", str(res)], capture_output=True, text=True)
            m = re.search(r"fail: (\d+)", r.stdout)
            check(bool(m) and int(m.group(1)) > 0, "kyverno: a pooler without resource limits is reported (the run can fail)")
            # the Cluster guard: a profile Cluster passes, a profile Cluster named rt-x outside the scratch
            # namespace is denied
            cl = by_kind(rendered["ha"], "Cluster")[0]
            good, evil = copy.deepcopy(cl), copy.deepcopy(cl)
            evil["metadata"]["name"] = "rt-shop"
            for name, obj, want in (("a profile Cluster", good, 0), ("a profile Cluster named rt-*", evil, 1)):
                res.write_text(yaml.safe_dump(obj))
                r = subprocess.run([ky, "apply", str(REPO / "apps/base/kyverno-policies-cnpg"), "--resource", str(res)], capture_output=True, text=True)
                m = re.search(r"pass: (\d+), fail: (\d+)", r.stdout)
                check(bool(m) and (int(m.group(2)) > 0) == bool(want), "kyverno: restrict-restore-test-clusters on %s -> %s" % (name, m.group(0) if m else r.stdout[-200:]))
        finally:
            shutil.rmtree(work)
    print("%d failed" % fails)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        print("%d failed" % fails)
        sys.exit(1 if fails else 0)
    main()
