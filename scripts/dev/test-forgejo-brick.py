#!/usr/bin/env python3
"""The Forgejo git forge brick (apps/base/forgejo, apps/base/forgejo-db) keeps the shape that lets a DEFAULT install converge.

Each rule exists because the first version of the brick (PR #173, written to the 0.3.x patterns, never run on a cluster)
broke it:
  1. every OpenBao path an ExternalSecret of the brick reads (forgejo/db, forgejo/admin, forgejo/oidc, with its property) is
     created by the vault configure Job (configure.sh): otherwise the ExternalSecret stays SecretSyncedError, the CNPG
     bootstrap Secret never exists and the database never starts;
  2. the admin Secret carries `username` AND `password`: the chart reads both keys of gitea.admin.existingSecret, and the pod
     stays in CreateContainerConfigError with only the password;
  3. the HelmRepository is the OCI one (the https address of the first version has no index.yaml: HTTP 404), and Flux's
     egress policy admits its host;
  4. the HTTPRoute does not hand /metrics (unauthenticated, enabled for vmagent) to the Internet: a rule for it with no backend,
     before the catch-all;
  5. forgejo-db is its OWN Flux Kustomization (wait: true, the cluster-identity substitution), `forgejo` depends on it, and
     the `cnpg` directory holds no forgejo object: a profile without the forge must not install three database pods, and a
     slow forgejo-db must not hold `cnpg` (wait: true) or identity back.
  6. self-registration is off and sign-in is required to view (the route answers anyone who sets the Host header; Forgejo's
     defaults allow registration);
  7. the Gateway's egress policy admits every namespace a route sends traffic to (the first version routed to the forge and forgot
     the rule: the route would have timed out);
  8. the brick is OPT-IN (0.4.0 release gate): `apps/flux/management` and `apps/flux/workload` do not render forgejo and forgejo-db,
     `apps/flux/management-forge` renders exactly those two more, and the catalogue keeps the `forge` alias;
  9. the repositories' volume has a DEFAULT daily Longhorn backup: the PVC carries the group label (persistence.labels of the
     chart, checked on the rendered PVC when helm is available), a RecurringJob of type backup selects that group, runs daily
     at a time no other job uses and keeps a retention, and sits in longhorn-system (the namespace Longhorn reads);
  10. (with the Kyverno CLI, $KYVERNO or PATH, and helm with network for the chart) the pod the chart renders passes the platform
     policies except the one read-only-root-filesystem finding that is known and left open (every one is Audit).
Static: it proves the wiring, not a cluster. Run: python3 -I scripts/dev/test-forgejo-brick.py [--selftest]
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
CONFIGURE = "apps/base/foundation/vault/scripts/configure.sh"


def docs(rel):
    return [d for d in yaml.safe_load_all((REPO / rel).read_text()) if d]


def seeded(configure_text):
    """{openbao path: set of properties} from the `kv_create <path>` lines of configure.sh (the jq / JSON literal on the same line)."""
    out = {}
    for line in configure_text.splitlines():
        m = re.search(r"kv_create (\S+) D\s*$", line)
        if not m:
            continue
        props = set(re.findall(r"[{,]\s*\"?([A-Za-z_]+)\"?\s*:", line.split("kv_create")[0]))
        out[m.group(1)] = props
    return out


def route_namespaces():
    out = {}
    for f in sorted((REPO / "apps/base").rglob("*.yaml")):
        try:
            for d in yaml.safe_load_all(f.read_text()):
                if isinstance(d, dict) and d.get("kind") == "HTTPRoute":
                    for r in d["spec"]["rules"]:
                        for b in r.get("backendRefs", []):
                            out.setdefault(d["metadata"]["namespace"], set()).add(b["name"])
        except yaml.YAMLError:
            continue
    return out


def kustomization_names(profile):
    """The names of the Flux Kustomizations a profile (a kustomize overlay under apps/flux) renders."""
    sys.path.insert(0, str(REPO / "scripts"))
    import ha_render
    return {d["metadata"]["name"] for d in ha_render.kustomize(REPO / "apps/flux" / profile) if d.get("kind") == "Kustomization"}


BRICK = {"forgejo", "forgejo-db"}
GROUP = "oa-forgejo"
LABEL = f"recurring-job-group.longhorn.io/{GROUP}"


def optin_problems(profiles, catalog, hr, jobs, other_crons, pvc_labels=None):
    """profiles: {name: set of Kustomization names}; jobs: the RecurringJobs of the brick; other_crons: the schedules of the other jobs."""
    errs = []
    for name in ("management", "workload"):
        if profiles[name] & BRICK:
            errs.append(f"apps/flux/{name} renders {sorted(profiles[name] & BRICK)}: the Forgejo brick is OPT-IN, off by default")
    if profiles["management-forge"] - profiles["management"] != BRICK:
        errs.append("apps/flux/management-forge must render exactly forgejo and forgejo-db more than apps/flux/management")
    if profiles["management"] - profiles["management-forge"]:
        errs.append("apps/flux/management-forge must not remove anything apps/flux/management renders")
    if (catalog.get("aliases") or {}).get("forge") != "forgejo":
        errs.append("the `forge` alias of bricks.yaml (the picker's way to include the forge) is gone")
    labels = (((hr["spec"]["values"].get("persistence") or {}).get("labels")) or {})
    if labels.get(LABEL) != "enabled":
        errs.append(f"the Forgejo PVC does not carry {LABEL}=enabled (persistence.labels): Longhorn would not back it up")
    if pvc_labels is not None and pvc_labels.get(LABEL) != "enabled":
        errs.append("the PVC the chart renders does not carry the backup group label")
    sel = [j for j in jobs if j["spec"].get("task") == "backup" and GROUP in (j["spec"].get("groups") or [])]
    if len(sel) != 1:
        errs.append(f"exactly one RecurringJob of type backup must select the group {GROUP} (found {len(sel)})")
    for j in sel:
        sp = j["spec"]
        if j["metadata"].get("namespace") != "longhorn-system":
            errs.append("the RecurringJob must be in longhorn-system")
        f = sp.get("cron", "").split()
        if len(f) != 5 or f[2:] != ["*", "*", "*"] or not (f[0].isdigit() and f[1].isdigit()):
            errs.append(f"the RecurringJob must run daily (a fixed minute and hour), not {sp.get('cron')!r}")
        elif sp["cron"] in other_crons:
            errs.append(f"the RecurringJob schedule {sp['cron']!r} collides with another job's")
        if not isinstance(sp.get("retain"), int) or sp["retain"] < 1:
            errs.append("the RecurringJob must keep a retention (retain >= 1)")
    return errs


def other_schedules():
    """Every cron of the other jobs: the Longhorn RecurringJobs (cron), the Kubernetes CronJobs and CNPG ScheduledBackups (schedule,
    the CNPG one with a leading seconds field, put in the 5-field form)."""
    out = set()
    for f in sorted((REPO / "apps/base").rglob("*.yaml")):
        if "forgejo" in f.parts[-2] or f.stat().st_size > 400000:
            continue
        try:
            for d in yaml.safe_load_all(f.read_text()):
                if not isinstance(d, dict):
                    continue
                if d.get("kind") == "RecurringJob":
                    out.add(d["spec"]["cron"])
                elif d.get("kind") == "CronJob":
                    out.add(d["spec"]["schedule"])
                elif d.get("kind") == "ScheduledBackup":
                    out.add(" ".join(d["spec"]["schedule"].split()[1:]))
        except yaml.YAMLError:
            continue
    return out


def gateway_egress_namespaces(gateway_cnp):
    return {e["matchLabels"].get("io.kubernetes.pod.namespace") for r in gateway_cnp["spec"]["egress"] for e in r.get("toEndpoints", [])}


def problems(configure_text, externalsecrets, helmrelease, repos, egress_text, flux, cnpg_files, route, gw_ns, routed):
    errs = []
    for ns in sorted(set(routed) - gw_ns):
        errs.append(f"the Gateway's egress policy (istio/networkpolicy.yaml) does not admit namespace {ns}, which an HTTPRoute sends traffic to")
    rules = route["spec"]["rules"]
    idx = [i for i, r in enumerate(rules) if any(m.get("path", {}).get("value") == "/metrics" for m in r.get("matches", []))]
    if not idx or rules[idx[0]].get("backendRefs") or idx[0] != 0:
        errs.append("the HTTPRoute must start with a /metrics rule that has no backendRefs (the endpoint is for vmagent only)")
    seed = seeded(configure_text)
    names = set()
    for es in externalsecrets:
        for ref in es["spec"].get("data", []):
            r = ref["remoteRef"]
            names.add(r["key"])
            if r["property"] not in seed.get(r["key"], set()):
                errs.append(f"ExternalSecret {es['metadata']['name']}: secret/{r['key']} property {r['property']} is not created by configure.sh")
    if not names:
        errs.append("no forgejo ExternalSecret was read: the test is blind")
    for es in externalsecrets:
        if es["metadata"]["name"] == "forgejo-admin":
            keys = set(((es["spec"].get("target") or {}).get("template") or {}).get("data", {})) | {r["secretKey"] for r in es["spec"].get("data", [])}
            if not {"username", "password"} <= keys:
                errs.append(f"forgejo-admin carries {sorted(keys)}: the chart reads `username` and `password`")
            tpl = es["spec"]["target"]["template"]["data"]["username"] if "username" in keys and "template" in (es["spec"].get("target") or {}) else None
            if tpl is not None and tpl != helmrelease["spec"]["values"]["gitea"]["admin"]["username"]:
                errs.append("forgejo-admin username differs from gitea.admin.username in the HelmRelease")
    svc = (((helmrelease["spec"]["values"].get("gitea") or {}).get("config") or {}).get("service")) or {}
    if svc.get("DISABLE_REGISTRATION") is not True or svc.get("REQUIRE_SIGNIN_VIEW") is not True:
        errs.append("gitea.config.service must set DISABLE_REGISTRATION and REQUIRE_SIGNIN_VIEW to true (the forge is on the public route)")
    ref = helmrelease["spec"]["chart"]["spec"]["sourceRef"]["name"]
    repo = repos.get(ref)
    if not repo:
        errs.append(f"HelmRepository {ref} not found")
    else:
        if repo["spec"].get("type") != "oci" or not repo["spec"]["url"].startswith("oci://code.forgejo.org/"):
            errs.append("the Forgejo chart is published as an OCI artifact on code.forgejo.org only: HelmRepository type oci and an oci:// url")
        host = re.sub(r"^\w+://", "", repo["spec"]["url"]).split("/")[0]
        if f"matchName: {host}" not in egress_text:
            errs.append(f"Flux's egress policy does not admit {host}: the chart cannot be fetched")
    kdb, kfg = flux.get("forgejo-db"), flux.get("forgejo")
    if not kdb or not kfg:
        errs.append("the Flux Kustomizations forgejo-db and forgejo must both exist")
    else:
        if kdb["spec"].get("wait") is not True:
            errs.append("forgejo-db must be wait: true (the chart is installed against a database that exists)")
        subs = [s.get("name") for s in (kdb["spec"].get("postBuild") or {}).get("substituteFrom", [])]
        if "cluster-identity" not in subs:
            errs.append("forgejo-db must substitute from cluster-identity (the S3 destination of its ObjectStores)")
        if "forgejo-db" not in [d["name"] for d in kfg["spec"].get("dependsOn", [])]:
            errs.append("forgejo must depend on forgejo-db")
        if kdb["spec"]["path"] != "./apps/base/forgejo-db":
            errs.append("forgejo-db path must be ./apps/base/forgejo-db")
    for f in cnpg_files:
        if "forgejo" in f:
            errs.append(f"{f}: a forgejo object in the cnpg directory ties the forge's database to every profile that picks cnpg")
    return errs


# The Audit finding that stays: the chart's init containers and the forge write under / (never proven read-only here). A NEW
# failing rule, or this one on a container that used to pass, is a regression.
KNOWN_KYVERNO = {"require-ro-rootfs"}


def kyverno_failures(ky, pod):
    with tempfile.TemporaryDirectory() as t:
        f = Path(t) / "pod.yaml"
        f.write_text(yaml.safe_dump(pod))
        r = subprocess.run([ky, "apply", str(REPO / "apps/base/kyverno-policies"), "--resource", str(f)], capture_output=True, text=True)
    return set(re.findall(r"policy (\S+) -> resource \S+ failed:", r.stdout)), r.stdout


def rendered_pod():
    sys.path.insert(0, str(REPO / "scripts"))
    import ha_render
    objs = ha_render.kustomize(REPO / "apps/base/forgejo")
    repos = {d["metadata"]["name"]: d["spec"]["url"] for d in objs if d["kind"] == "HelmRepository"}
    hr = next(d for d in objs if d["kind"] == "HelmRelease")
    dep = next(d for d in ha_render.helm_release(hr, repos) if d["kind"] == "Deployment")
    t = dep["spec"]["template"]
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "forgejo-x", "namespace": dep["metadata"]["namespace"],
                                                              "labels": t["metadata"]["labels"]}, "spec": t["spec"]}


def kyverno_problems():
    ky = os.environ.get("KYVERNO") or shutil.which("kyverno")
    if not ky or not shutil.which("helm"):
        print("skip the Kyverno check: no Kyverno CLI or no helm (set KYVERNO)")
        return [], None
    pod = rendered_pod()
    got, _ = kyverno_failures(ky, pod)
    errs = [f"Kyverno policy {p} fails on the rendered Forgejo pod" for p in sorted(got - KNOWN_KYVERNO)]
    return errs, (ky, pod)


def load():
    es = [d for rel in ("apps/base/forgejo/externalsecrets.yaml", "apps/base/forgejo-db/externalsecret.yaml") for d in docs(rel)
          if d["kind"] == "ExternalSecret"]
    hr = next(d for d in docs("apps/base/forgejo/helmrelease.yaml") if d["kind"] == "HelmRelease")
    repos = {d["metadata"]["name"]: d for d in docs("apps/base/forgejo/helmrelease.yaml") if d["kind"] == "HelmRepository"}
    flux = {d["metadata"]["name"]: d for rel in ("apps/flux/base/36-forgejo-db.yaml", "apps/flux/base/37-forgejo.yaml") for d in docs(rel)}
    cnpg = [str(p.relative_to(REPO)) for p in (REPO / "apps/base/cnpg").glob("*forgejo*")]
    cnpg += [str(REPO / "apps/base/cnpg/kustomization.yaml") for _ in [0] if "forgejo" in (REPO / "apps/base/cnpg/kustomization.yaml").read_text()]
    route = next(d for d in docs("apps/base/forgejo/httproute.yaml") if d["kind"] == "HTTPRoute")
    gw = next(d for d in docs("apps/base/istio/networkpolicy.yaml") if d["metadata"]["name"] == "openaether-gateway")
    return ((REPO / CONFIGURE).read_text(), es, hr, repos, (REPO / "apps/base/flux/flux-egress-restricted.yaml").read_text(), flux, cnpg, route,
            gateway_egress_namespaces(gw), route_namespaces())


def selftest(args):
    """Each mutation of the real inputs must be caught; an unmutated copy must pass."""
    conf, es, hr, repos, egress, flux, cnpg, route, gw_ns, routed = args
    muts = {
        "a missing forgejo/db seed": dict(conf=re.sub(r".*kv_create forgejo/db D\n", "", conf)),
        "a missing forgejo/admin seed": dict(conf=re.sub(r".*kv_create forgejo/admin D\n", "", conf)),
        "a missing forgejo/oidc seed": dict(conf=re.sub(r".*kv_create forgejo/oidc D\n", "", conf)),
        "an https chart repository": dict(repos={k: dict(v, spec=dict(v["spec"], type="default", url="https://forgejo-contrib.codeberg.page/forgejo-helm/")) for k, v in repos.items()}),
        "a chart host Flux cannot reach": dict(egress=egress.replace("code.forgejo.org", "elsewhere.example")),
        "a forgejo object in the cnpg directory": dict(cnpg=["apps/base/cnpg/forgejo-db-cluster.yaml"]),
    }
    a = copy.deepcopy(es)
    for e in a:
        if e["metadata"]["name"] == "forgejo-admin":
            e["spec"]["target"].pop("template", None)
    muts["an admin Secret without username"] = dict(es=a)
    f = copy.deepcopy(flux); f["forgejo-db"]["spec"]["wait"] = False
    muts["forgejo-db without wait"] = dict(flux=f)
    f = copy.deepcopy(flux); f["forgejo"]["spec"]["dependsOn"] = [{"name": "cnpg"}]
    muts["forgejo not depending on forgejo-db"] = dict(flux=f)
    f = copy.deepcopy(flux); f["forgejo-db"]["spec"]["postBuild"] = {}
    muts["forgejo-db without the cluster-identity substitution"] = dict(flux=f)
    r = copy.deepcopy(route); r["spec"]["rules"][0]["backendRefs"] = [{"name": "forgejo-http", "port": 3000}]
    muts["/metrics routed to the backend"] = dict(route=r)
    r = copy.deepcopy(route); r["spec"]["rules"] = r["spec"]["rules"][1:]
    muts["no /metrics rule"] = dict(route=r)
    h = copy.deepcopy(hr); h["spec"]["values"]["gitea"]["config"].pop("service")
    muts["self-registration left open"] = dict(hr=h)
    muts["a routed namespace the Gateway may not reach"] = dict(gw_ns=gw_ns - {"services-git"})
    ok = not problems(*args)
    print(("ok   " if ok else "FAIL ") + "the real inputs pass")
    for name, m in muts.items():
        a2 = dict(conf=conf, es=es, hr=hr, repos=repos, egress=egress, flux=flux, cnpg=cnpg, route=route, gw_ns=gw_ns, routed=routed)
        a2.update(m)
        caught = bool(problems(a2["conf"], a2["es"], a2["hr"], a2["repos"], a2["egress"], a2["flux"], a2["cnpg"], a2["route"], a2["gw_ns"], a2["routed"]))
        print(("ok   " if caught else "FAIL ") + f"caught: {name}")
        ok = ok and caught
    return ok


def kyverno_selftest(ky, pod):
    ok = True
    for name, mut in {"no runAsNonRoot": lambda p: p["spec"]["securityContext"].pop("runAsNonRoot"),
                      "no limit on an init container": lambda p: p["spec"]["initContainers"][0]["resources"].pop("limits"),
                      "an image from another registry": lambda p: p["spec"]["containers"][0].update(image="docker.io/library/nginx:1")}.items():
        p = copy.deepcopy(pod)
        mut(p)
        caught = bool(kyverno_failures(ky, p)[0] - KNOWN_KYVERNO)
        print(("ok   " if caught else "FAIL ") + f"caught by Kyverno: {name}")
        ok = ok and caught
    return ok


def optin_inputs():
    profiles = {n: kustomization_names(n) for n in ("management", "management-forge", "workload")}
    catalog = yaml.safe_load((REPO / "apps/flux/bricks.yaml").read_text())
    hr = next(d for d in docs("apps/base/forgejo/helmrelease.yaml") if d["kind"] == "HelmRelease")
    jobs = [d for d in docs("apps/base/forgejo/recurringjob.yaml") if d["kind"] == "RecurringJob"]
    pvc = None
    if shutil.which("helm"):
        sys.path.insert(0, str(REPO / "scripts"))
        import ha_render
        objs = ha_render.kustomize(REPO / "apps/base/forgejo")
        repos = {d["metadata"]["name"]: d["spec"]["url"] for d in objs if d["kind"] == "HelmRepository"}
        rel = next(d for d in objs if d["kind"] == "HelmRelease")
        pvc = next(d for d in ha_render.helm_release(rel, repos) if d["kind"] == "PersistentVolumeClaim")["metadata"].get("labels", {})
    return profiles, catalog, hr, jobs, other_schedules(), pvc


def optin_selftest(inp):
    profiles, catalog, hr, jobs, crons, pvc = inp
    muts = {
        "the forge back in the default management profile": dict(profiles=dict(profiles, management=profiles["management"] | BRICK)),
        "the forge in the workload profile": dict(profiles=dict(profiles, workload=profiles["workload"] | {"forgejo"})),
        "an opt-in profile that renders nothing more": dict(profiles=dict(profiles, **{"management-forge": profiles["management"]})),
        "no forge alias": dict(catalog={"aliases": {}}),
        "a PVC without the backup group label": dict(hr=_without_label(hr)),
        "a rendered PVC without the label": dict(pvc_labels={}),
        "no RecurringJob": dict(jobs=[]),
        "a RecurringJob on another group": dict(jobs=[_job(jobs, groups=["other"])]),
        "a RecurringJob that is a snapshot, not a backup": dict(jobs=[_job(jobs, task="snapshot")]),
        "an hourly RecurringJob": dict(jobs=[_job(jobs, cron="55 * * * *")]),
        "a RecurringJob at the time of oa-backup-daily": dict(jobs=[_job(jobs, cron="37 3 * * *")]),
        "a RecurringJob with no retention": dict(jobs=[_job(jobs, retain=0)]),
    }
    ok = not optin_problems(*inp)
    print(("ok   " if ok else "FAIL ") + "the real opt-in inputs pass")
    for name, m in muts.items():
        a = dict(profiles=profiles, catalog=catalog, hr=hr, jobs=jobs, other_crons=crons, pvc_labels=pvc)
        a.update(m)
        caught = bool(optin_problems(**a))
        print(("ok   " if caught else "FAIL ") + f"caught: {name}")
        ok = ok and caught
    return ok


def _job(jobs, **spec):
    j = copy.deepcopy(jobs[0])
    j["spec"].update(spec)
    return j


def _without_label(hr):
    h = copy.deepcopy(hr)
    h["spec"]["values"]["persistence"].pop("labels")
    return h


def main():
    args = load()
    inp = optin_inputs()
    kerrs, kctx = kyverno_problems()
    if "--selftest" in sys.argv:
        ok = selftest(args)
        ok = optin_selftest(inp) and ok
        if kctx:
            ok = kyverno_selftest(*kctx) and ok
        sys.exit(0 if ok else 1)
    errs = problems(*args) + optin_problems(*inp) + kerrs
    for e in errs:
        print("FAIL " + e)
    print(f"{len(errs)} failed" if errs else "ok   the Forgejo brick is wired to its secrets, its chart source and its own database Kustomization")
    sys.exit(1 if errs else 0)


main()
