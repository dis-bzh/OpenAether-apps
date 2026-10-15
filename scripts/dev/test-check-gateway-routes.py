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

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
