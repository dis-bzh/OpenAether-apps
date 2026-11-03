#!/usr/bin/env python3
"""check-gateway-routes.py - every HTTPRoute on the shared Gateway is registered, with what authenticates its callers.

The app load balancer in front of openaether-gateway has no source ACL by default (docs/public-exposure.md), so a route attached
to that Gateway is reachable by anyone who sets the Host header. This scans the manifests (no cluster, no kubectl) and fails when:
  - an HTTPRoute whose parentRef is the Gateway has no row in docs/public-exposure.yaml, or a row names no such route;
  - a row has no `authentication`, or says "none" while its route is served by default;
  - `default` disagrees with the brick's kustomization.yaml (is the route's file listed there?);
  - `private_admin` disagrees with the routes apps/flux/components/private-admin-routes deletes;
  - a metrics endpoint (METRICS_PATHS) is not refused at the Gateway: no listed DENY AuthorizationPolicy covers it for every
    caller and host (the routes that would forward it are named), or the policy also denies ordinary paths; or a URL scrape
    (VMStaticScrape, VMProbe) names a Gateway host, where the deny would cut it. Pod and service scrapes never pass the Gateway.

Usage: python3 scripts/check-gateway-routes.py            Dependency: PyYAML.
"""
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
GATEWAY = "openaether-gateway"
REGISTER = "docs/public-exposure.yaml"
COMPONENT = "apps/flux/components/private-admin-routes/kustomization.yaml"
GATEWAY_NS = "services-gateway"
POLICY_BRICK = "apps/base/services-gateway"
# What the services behind the Gateway publish without a login and what vmagent reads pod to pod: /metrics (Grafana, Forgejo,
# Loki) and /debug/metrics (Zitadel, VMPodScrape zitadel). Each is denied exact and as a prefix.
METRICS_PATHS = ("/metrics", "/debug/metrics")
BENIGN_PATHS = ("/", "/login", "/api/health", "/ui/v2/login", "/.well-known/openid-configuration", "/metricsx")
KINDS = ("HTTPRoute", "AuthorizationPolicy", "VMStaticScrape", "VMProbe")


def load_docs(root):
    """[(file, doc)] for the kinds this script reads, in one pass over apps/ (the text filter skips the big vendored dumps)."""
    out = []
    for f in sorted((root / "apps").rglob("*.yaml")):
        text = f.read_text()
        if not any(k in text for k in KINDS):
            continue
        try:
            docs = list(yaml.safe_load_all(text))
        except yaml.YAMLError:
            continue  # a template or a vendored file that is not plain YAML: it cannot define an object we build
        out += [(f, d) for d in docs if isinstance(d, dict)]
    return out


def gateway_routes(docs):
    """[(file, HTTPRoute)] for every HTTPRoute whose parentRef is the Gateway."""
    return [(f, d) for f, d in docs if d.get("kind") == "HTTPRoute" and any(
        r.get("name") == GATEWAY and r.get("kind", "Gateway") == "Gateway" for r in (d.get("spec") or {}).get("parentRefs") or [])]


def find_routes(root, docs=None):
    """{ns/name: (file, listed in the sibling kustomization.yaml)} for every HTTPRoute attached to the Gateway."""
    found, errors = {}, []
    for f, d in gateway_routes(load_docs(root) if docs is None else docs):
        md = d.get("metadata") or {}
        if not md.get("namespace"):
            errors.append(f"{f.relative_to(root)}: HTTPRoute/{md.get('name')} has no metadata.namespace")
            continue
        kz = f.parent / "kustomization.yaml"
        listed = kz.exists() and f.name in ((yaml.safe_load(kz.read_text()) or {}).get("resources") or [])
        found[f"{md['namespace']}/{md['name']}"] = (str(f.relative_to(root)), listed)
    return found, errors


def policies(root, docs):
    """The AuthorizationPolicies of the Gateway's brick that its kustomization.yaml lists (an unlisted file is never applied)."""
    kz = root / POLICY_BRICK / "kustomization.yaml"
    listed = set((yaml.safe_load(kz.read_text()) or {}).get("resources") or []) if kz.exists() else set()
    return [d for f, d in docs if d.get("kind") == "AuthorizationPolicy" and f.parent == root / POLICY_BRICK and f.name in listed]


def istio_path(pattern, path):
    """Istio's `operation.paths` match: exact, `x*` prefix, `*x` suffix, `*` any. No normalisation, no query string."""
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        return path.startswith(pattern[:-1])
    return path.endswith(pattern[1:]) if pattern.startswith("*") else path == pattern


def gateway_denies(pols, path):
    """True when a DENY policy applied to the Gateway refuses EVERY request for `path`, whoever sends it.

    A rule narrowed by `from` or `when` leaves some callers through; an operation narrowed by host, port or method leaves some
    requests through; a policy outside the Gateway's namespace or aimed at another target is inert. A rule with no `to` denies all.
    """
    for p in pols:
        s = p.get("spec") or {}
        aimed = any(t.get("kind") == "Gateway" and t.get("name") == GATEWAY and t.get("group") == "gateway.networking.k8s.io"
                    for t in s.get("targetRefs") or [])
        if s.get("action") != "DENY" or not aimed or (p.get("metadata") or {}).get("namespace") != GATEWAY_NS:
            continue
        for rule in s.get("rules") or []:
            if rule.get("from") or rule.get("when"):
                continue
            ops = [t.get("operation") or {} for t in rule.get("to") or []]
            if not ops or any(set(op) == {"paths"} and any(istio_path(x, path) for x in op["paths"]) for op in ops):
                return True
    return False


def forwards(route, path):
    """The backends of the HTTPRoute rule that answers `path` (Exact, then the longest PathPrefix), or [] when it has none."""
    best, backends = (-1, 0), []
    for rule in (route.get("spec") or {}).get("rules") or []:
        for m in rule.get("matches") or [{}]:
            kind, val = (m.get("path") or {}).get("type", "PathPrefix"), (m.get("path") or {}).get("value", "/")
            if kind == "Exact":
                hit, score = path == val, (2, len(val))
            else:  # PathPrefix matches whole path elements; a RegularExpression is not read, it is not used here
                hit, score = val == "/" or path == val or path.startswith(val.rstrip("/") + "/"), (1, len(val))
            if hit and score > best:
                redirect = any(f.get("type") == "RequestRedirect" for f in rule.get("filters") or [])
                best, backends = score, ([] if redirect else [b.get("name") for b in rule.get("backendRefs") or []])
    return backends


def metrics_problems(pols, routes):
    """`routes` is [(file, HTTPRoute)]. Every metrics path is refused at the Gateway, and ordinary paths are not."""
    errors = []
    for base in METRICS_PATHS:
        open_ = [p for p in (base, base + "/", base + "/x") if not gateway_denies(pols, p)]
        if open_:
            via = [f"{d['metadata'].get('namespace')}/{d['metadata']['name']}" for _, d in routes if forwards(d, base)]
            errors.append(f"{POLICY_BRICK}: no listed DENY AuthorizationPolicy on {GATEWAY} (ns {GATEWAY_NS}) refuses "
                          f"{', '.join(open_)}" + (f"; routes that forward {base}: {', '.join(via)}" if via else ""))
    errors += [f"{POLICY_BRICK}: a DENY policy on {GATEWAY} also refuses {p}, which the services serve"
               for p in BENIGN_PATHS if gateway_denies(pols, p)]
    return errors


def scrape_problems(docs, hosts):
    """A URL-based scrape of a Gateway host would be cut by the deny; the pod and service scrapes do not pass the Gateway."""
    return [f"{f}: {d['kind']}/{d['metadata']['name']} reads a Gateway host ({h}): the metrics deny would cut it, scrape the pod instead"
            for f, d in docs if d.get("kind") in ("VMStaticScrape", "VMProbe") for h in hosts if h and h in yaml.safe_dump(d)]


def deleted_by_component(text):
    """{ns/name} of the HTTPRoutes the component's nested Flux patches delete."""
    out = set()
    for p in (yaml.safe_load(text) or {}).get("patches", []):
        for inner in (yaml.safe_load(p["patch"]).get("spec") or {}).get("patches", []):
            d = yaml.safe_load(inner["patch"])
            if d.get("kind") == "HTTPRoute" and d.get("$patch") == "delete":
                out.add(f"{d['metadata']['namespace']}/{d['metadata']['name']}")
    return out


def check(found, register, deleted):
    errors = []
    rows = {}
    for r in register.get("routes") or []:
        if r.get("route") in rows:
            errors.append(f"{REGISTER}: {r['route']} is listed twice")
        rows[r.get("route")] = r
    for route, (f, listed) in found.items():
        if route not in rows:
            errors.append(f"{f}: HTTPRoute {route} is attached to {GATEWAY} but has no row in {REGISTER}")
    for route, r in rows.items():
        if route not in found:
            errors.append(f"{REGISTER}: {route} is listed but no manifest defines it on {GATEWAY}")
            continue
        auth = str(r.get("authentication") or "").strip()
        if not auth:
            errors.append(f"{REGISTER}: {route} has no `authentication`")
        if r.get("default") and auth.lower().startswith("none"):
            errors.append(f"{REGISTER}: {route} is served by default and states no authentication")
        if bool(r.get("default")) != found[route][1]:
            errors.append(f"{REGISTER}: {route} says default={r.get('default')} but its file is "
                          f"{'' if found[route][1] else 'not '}listed in its kustomization.yaml")
        if bool(r.get("private_admin")) != (route in deleted):
            errors.append(f"{REGISTER}: {route} says private_admin={r.get('private_admin')} but the component "
                          f"{'deletes' if route in deleted else 'does not delete'} it")
    for route in deleted - set(found):
        errors.append(f"{COMPONENT}: deletes {route}, which no manifest defines")
    return errors


def main():
    docs = load_docs(REPO)
    found, errors = find_routes(REPO, docs)
    register = yaml.safe_load((REPO / REGISTER).read_text()) or {}
    errors += check(found, register, deleted_by_component((REPO / COMPONENT).read_text()))
    errors += metrics_problems(policies(REPO, docs), gateway_routes(docs))
    errors += scrape_problems(docs, [r.get("host") for r in register.get("routes") or []])
    if errors:
        print(f"{len(errors)} problem(s):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)
    print(f"OK - {len(found)} HTTPRoute(s) on {GATEWAY}, all registered with their authentication; "
          f"{', '.join(METRICS_PATHS)} refused at the Gateway.")


if __name__ == "__main__":
    main()
