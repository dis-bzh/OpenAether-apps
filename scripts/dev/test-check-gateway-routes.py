#!/usr/bin/env python3
"""Fixture tests of scripts/check-gateway-routes.py: it passes on a consistent set and fails, for the right reason, on each way
the register can drift. No cluster, no kubectl. Usage: python3 scripts/dev/test-check-gateway-routes.py
"""
import copy
import importlib.util
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("chk", HERE.parent / "check-gateway-routes.py")
chk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chk)

FOUND = {"ns-a/one": ("apps/base/a/route.yaml", True), "ns-b/two": ("apps/base/b/route.yaml", False)}
REG = {"routes": [
    {"route": "ns-a/one", "authentication": "a login", "default": True, "private_admin": True},
    {"route": "ns-b/two", "authentication": "none: opt-in", "default": False, "private_admin": False},
]}
DELETED = {"ns-a/one"}
PASS = FAIL = 0


def case(desc, found, reg, deleted, want=None):
    global PASS, FAIL
    errs = chk.check(copy.deepcopy(found), copy.deepcopy(reg), set(deleted))
    good = (not errs) if want is None else any(want in e for e in errs)
    PASS += good
    FAIL += not good
    print(("  ok   " if good else "  FAIL ") + desc + ("" if good else f" -> {errs}"))


def mut(fn):
    r = copy.deepcopy(REG)
    fn(r["routes"])
    return r


case("a consistent set passes", FOUND, REG, DELETED)
case("a route with no row", {**FOUND, "ns-c/three": ("x.yaml", True)}, REG, DELETED, "ns-c/three")
case("a row for a route that does not exist", FOUND, mut(lambda r: r.append({"route": "ns-d/gone", "authentication": "x"})), DELETED, "ns-d/gone")
case("a row without authentication", FOUND, mut(lambda r: r[0].update(authentication="  ")), DELETED, "no `authentication`")
case("none authentication on a route served by default", FOUND, mut(lambda r: r[0].update(authentication="None at all")), DELETED, "states no authentication")
case("default=true but the file is not listed", FOUND, mut(lambda r: r[1].update(default=True)), DELETED, "not listed")
case("default=false but the file is listed", FOUND, mut(lambda r: r[0].update(default=False)), DELETED, "is listed in its kustomization")
case("private_admin says yes, the component does not delete it", FOUND, REG, set(), "private_admin=True")
case("the component deletes a route the register calls public", FOUND, mut(lambda r: r[0].update(private_admin=False)), DELETED, "private_admin=False")
case("the component deletes a route no manifest defines", FOUND, REG, DELETED | {"ns-z/ghost"}, "ns-z/ghost")
case("a route listed twice", FOUND, mut(lambda r: r.append(dict(r[0]))), DELETED, "listed twice")

# find_routes on a scratch tree: only routes whose parentRef is the Gateway count; the sibling kustomization decides `listed`.
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "apps/base/x").mkdir(parents=True)
    route = lambda name, parent: (  # noqa: E731
        "apiVersion: gateway.networking.k8s.io/v1\nkind: HTTPRoute\nmetadata: {name: %s, namespace: n}\n"
        "spec: {parentRefs: [{name: %s}]}\n" % (name, parent))
    (root / "apps/base/x/mine.yaml").write_text(route("mine", chk.GATEWAY))
    (root / "apps/base/x/other.yaml").write_text(route("other", "another-gateway"))
    (root / "apps/base/x/unlisted.yaml").write_text(route("unlisted", chk.GATEWAY))
    (root / "apps/base/x/template.yaml").write_text("a: {{ not yaml")
    (root / "apps/base/x/kustomization.yaml").write_text("resources: [mine.yaml, other.yaml]\n")
    found, errs = chk.find_routes(root)
    ok = (set(found) == {"n/mine", "n/unlisted"} and found["n/mine"][1] and not found["n/unlisted"][1] and not errs)
    PASS += ok
    FAIL += not ok
    print(("  ok   " if ok else "  FAIL ") + f"find_routes keeps the Gateway's routes and reads the kustomization ({found}, {errs})")
    (root / "apps/base/x/nons.yaml").write_text(route("nons", chk.GATEWAY).replace(", namespace: n", ""))
    _, errs = chk.find_routes(root)
    ok = any("no metadata.namespace" in e for e in errs)
    PASS += ok
    FAIL += not ok
    print(("  ok   " if ok else "  FAIL ") + "a route without a namespace is reported")



# --- the metrics endpoints are refused at the Gateway (Grafana /metrics answered 200 to anyone on the public Gateway) ---
def policy(paths=("/metrics", "/metrics/*", "/debug/metrics", "/debug/metrics/*"), action="DENY", target=chk.GATEWAY,
           ns=chk.GATEWAY_NS, rule=None, group="gateway.networking.k8s.io"):
    return {"kind": "AuthorizationPolicy", "metadata": {"name": "deny-metrics", "namespace": ns}, "spec": {
        "action": action, "targetRefs": [{"group": group, "kind": "Gateway", "name": target}],
        "rules": [rule if rule is not None else {"to": [{"operation": {"paths": list(paths)}}]}]}}


def route(name, rules):
    return (Path(f"{name}.yaml"), {"kind": "HTTPRoute", "metadata": {"name": name, "namespace": "ns"}, "spec": {"rules": rules}})


def backend(path, kind="PathPrefix", name="app"):
    return {"matches": [{"path": {"type": kind, "value": path}}], "backendRefs": [{"name": name, "port": 80}]}


ROUTES = [route("grafana", [backend("/", name="grafana")]),
          route("forgejo", [{"matches": [{"path": {"type": "PathPrefix", "value": "/metrics"}}]}, backend("/", name="forgejo-http")])]


def mcase(desc, pols, want=None, routes=ROUTES):
    global PASS, FAIL
    errs = chk.metrics_problems(pols, routes)
    good = (not errs) if want is None else any(want in e for e in errs)
    PASS += good
    FAIL += not good
    print(("  ok   " if good else "  FAIL ") + desc + ("" if good else f" -> {errs}"))


mcase("a DENY on the Gateway for /metrics and /debug/metrics, exact and prefix, passes", [policy()])
mcase("no policy: every metrics path is open, and the routes that forward it are named", [], "routes that forward /metrics: ns/grafana")
mcase("the forge route's own backendless /metrics rule is not an answer for the Gateway", [], "routes that forward /debug/metrics: ns/grafana, ns/forgejo")
mcase("no policy: /debug/metrics (Zitadel) is reported too", [], "refuses /debug/metrics")
mcase("the prefix form is missing: /metrics/x stays open", [policy(("/metrics", "/debug/metrics", "/debug/metrics/*"))], "/metrics/x")
mcase("the exact form is missing: /metrics stays open", [policy(("/metrics/*", "/debug/metrics", "/debug/metrics/*"))], "refuses /metrics; routes")
mcase("Zitadel's /debug/metrics is missing", [policy(("/metrics", "/metrics/*"))], "/debug/metrics")
mcase("an ALLOW policy is not a deny", [policy(action="ALLOW")], "refuses /metrics")
mcase("a policy aimed at another Gateway is inert", [policy(target="another-gateway")], "refuses /metrics")
mcase("a policy aimed at something that is not a Gateway group is inert", [policy(group="")], "refuses /metrics")
mcase("a policy in another namespace is inert", [policy(ns="istio-system")], "refuses /metrics")
mcase("a rule narrowed by `from` leaves callers through",
      [policy(rule={"from": [{"source": {"notPrincipals": ["cluster.local/ns/x/sa/y"]}}], "to": [{"operation": {"paths": ["/metrics", "/metrics/*", "/debug/metrics", "/debug/metrics/*"]}}]})], "refuses /metrics")
mcase("a rule narrowed by `when` leaves requests through",
      [policy(rule={"when": [{"key": "request.headers[x]", "values": ["1"]}], "to": [{"operation": {"paths": ["/metrics", "/metrics/*", "/debug/metrics", "/debug/metrics/*"]}}]})], "refuses /metrics")
mcase("an operation narrowed by host leaves the other hosts open",
      [policy(rule={"to": [{"operation": {"hosts": ["grafana.openaether.local"], "paths": ["/metrics", "/metrics/*", "/debug/metrics", "/debug/metrics/*"]}}]})], "refuses /metrics")
mcase("an operation narrowed by port leaves the other port open",
      [policy(rule={"to": [{"operation": {"ports": ["443"], "paths": ["/metrics", "/metrics/*", "/debug/metrics", "/debug/metrics/*"]}}]})], "refuses /metrics")
mcase("a DENY with an empty rule refuses every request: caught as too wide", [policy(rule={})], "also refuses /,")
mcase("a DENY on /* is too wide", [policy(("/*",))], "also refuses /login")
mcase("a prefix /metrics* would refuse /metricsx: too wide", [policy(("/metrics*", "/debug/metrics*"))], "also refuses /metricsx")

# the HTTPRoute matcher: Exact, then the longest prefix; whole path elements only; a rule without backend or a redirect forwards nothing
zit = {"spec": {"rules": [backend("/", name="server"), backend("/ui/v2/login", name="login"), backend("/ui/v2/login/_next/image", name="server2")]}}
ok = (chk.forwards(zit, "/ui/v2/login/x") == ["login"] and chk.forwards(zit, "/ui/v2/login/_next/image") == ["server2"]
      and chk.forwards(zit, "/ui/v2/loginx") == ["server"] and chk.forwards(zit, "/metrics") == ["server"])
PASS += ok
FAIL += not ok
print(("  ok   " if ok else "  FAIL ") + "forwards() picks the longest prefix and matches whole path elements")
forge = ROUTES[1][1]
ok = (chk.forwards(forge, "/metrics") == [] and chk.forwards(forge, "/metrics/x") == [] and chk.forwards(forge, "/metricsx") == ["forgejo-http"]
      and chk.forwards({"spec": {"rules": [backend("/metrics", "Exact")]}}, "/metrics/x") == [])
PASS += ok
FAIL += not ok
print(("  ok   " if ok else "  FAIL ") + "forwards(): a rule with no backend forwards nothing, Exact does not match a sub-path")
redir = {"spec": {"rules": [{"filters": [{"type": "RequestRedirect", "requestRedirect": {"scheme": "https"}}]}]}}
rewrite = {"spec": {"rules": [{"filters": [{"type": "URLRewrite", "urlRewrite": {"hostname": "x"}}], "backendRefs": [{"name": "app"}]}]}}
ok = chk.forwards(redir, "/metrics") == [] and chk.forwards(rewrite, "/metrics") == ["app"]
PASS += ok
FAIL += not ok
print(("  ok   " if ok else "  FAIL ") + "forwards(): a redirect forwards nothing, a rewrite still does")

# policies(): only the files its brick's kustomization.yaml lists count
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    brick = root / chk.POLICY_BRICK
    brick.mkdir(parents=True)
    (root / "apps/base/other").mkdir(parents=True)
    docs = [(brick / "listed.yaml", policy()), (brick / "unlisted.yaml", policy()), (root / "apps/base/other/p.yaml", policy())]
    (brick / "kustomization.yaml").write_text("resources: [listed.yaml]\n")
    got = chk.policies(root, docs)
    ok = len(got) == 1
    PASS += ok
    FAIL += not ok
    print(("  ok   " if ok else "  FAIL ") + f"policies() keeps the listed file of the Gateway's brick only ({len(got)} kept)")
    (brick / "kustomization.yaml").write_text("resources: []\n")
    ok = chk.policies(root, docs) == []
    PASS += ok
    FAIL += not ok
    print(("  ok   " if ok else "  FAIL ") + "policies(): a policy file that is not listed is never applied, so it counts for nothing")

# the in-cluster scrapes are pod to pod; a URL-based scrape of a Gateway host would be cut
hosts = ["grafana.openaether.local"]
static = (Path("s.yaml"), {"kind": "VMStaticScrape", "metadata": {"name": "g"}, "spec": {"targetEndpoints": [{"targets": ["grafana.openaether.local:443"]}]}})
pod = (Path("p.yaml"), {"kind": "VMPodScrape", "metadata": {"name": "grafana"}, "spec": {"podMetricsEndpoints": [{"port": "grafana", "path": "/metrics"}]}})
ok = len(chk.scrape_problems([static, pod], hosts)) == 1 and not chk.scrape_problems([pod], hosts)
PASS += ok
FAIL += not ok
print(("  ok   " if ok else "  FAIL ") + "a VMStaticScrape of a Gateway host is reported, a VMPodScrape is not")

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
