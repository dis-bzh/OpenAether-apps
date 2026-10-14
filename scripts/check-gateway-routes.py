#!/usr/bin/env python3
"""check-gateway-routes.py - every HTTPRoute on the shared Gateway is registered, with what authenticates its callers.

The app load balancer in front of openaether-gateway has no source ACL (docs/public-exposure.md), so a route attached to that
Gateway is reachable by anyone who sets the Host header. This scans the manifests (no cluster, no kubectl) and fails when:
  - an HTTPRoute whose parentRef is the Gateway has no row in docs/public-exposure.yaml, or a row names no such route;
  - a row has no `authentication`, or says "none" while its route is served by default;
  - `default` disagrees with the brick's kustomization.yaml (is the route's file listed there?);
  - `private_admin` disagrees with the routes apps/flux/components/private-admin-routes deletes.

Usage: python3 scripts/check-gateway-routes.py            Dependency: PyYAML.
"""
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
GATEWAY = "openaether-gateway"
REGISTER = "docs/public-exposure.yaml"
COMPONENT = "apps/flux/components/private-admin-routes/kustomization.yaml"


def find_routes(root):
    """{ns/name: (file, listed in the sibling kustomization.yaml)} for every HTTPRoute attached to the Gateway."""
    found, errors = {}, []
    for f in sorted((root / "apps").rglob("*.yaml")):
        try:
            docs = list(yaml.safe_load_all(f.read_text()))
        except yaml.YAMLError:
            continue  # a template or a vendored file that is not plain YAML: it cannot define a route we build
        for d in docs:
            if not isinstance(d, dict) or d.get("kind") != "HTTPRoute":
                continue
            refs = (d.get("spec") or {}).get("parentRefs") or []
            if not any(r.get("name") == GATEWAY and r.get("kind", "Gateway") == "Gateway" for r in refs):
                continue
            md = d.get("metadata") or {}
            if not md.get("namespace"):
                errors.append(f"{f.relative_to(root)}: HTTPRoute/{md.get('name')} has no metadata.namespace")
                continue
            kz = f.parent / "kustomization.yaml"
            listed = kz.exists() and f.name in ((yaml.safe_load(kz.read_text()) or {}).get("resources") or [])
            found[f"{md['namespace']}/{md['name']}"] = (str(f.relative_to(root)), listed)
    return found, errors


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
    found, errors = find_routes(REPO)
    register = yaml.safe_load((REPO / REGISTER).read_text()) or {}
    errors += check(found, register, deleted_by_component((REPO / COMPONENT).read_text()))
    if errors:
        print(f"{len(errors)} problem(s):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)
    print(f"OK - {len(found)} HTTPRoute(s) on {GATEWAY}, all registered with their authentication.")


if __name__ == "__main__":
    main()
