#!/usr/bin/env python3
"""Fixture tests of scripts/check-externalsecret-namespaces.py: the check must pass on a
consistent set and fail, for the right reason, on each way the set can drift.
No cluster, no kubectl. Usage: python3 scripts/dev/test-check-externalsecret-namespaces.py
"""
import copy
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("chk", HERE.parent / "check-externalsecret-namespaces.py")
chk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chk)

NS = ["foundation-vault", "foundation-databases"]


def store(conds=None, name="openbao"):
    d = {"kind": "ClusterSecretStore", "metadata": {"name": name}, "spec": {}}
    if conds is not None:
        d["spec"]["conditions"] = conds
    return ("stores", d)


def es(ns, kind="ExternalSecret", name="x"):
    return ("brick", {"kind": kind, "metadata": {"name": name, "namespace": ns}})


def policy(excluded):
    return ("pol", {"kind": "ClusterPolicy", "metadata": {"name": chk.POLICY},
                    "spec": {"rules": [{"exclude": {"any": [{"resources": {"namespaces": excluded}}]}}]}})


GOOD = [store([{"namespaces": NS}]), es("foundation-vault"), es("foundation-databases", "PushSecret"), policy(NS)]
PASS, FAIL = 0, 0


def case(desc, docs, want_substr=None):
    global PASS, FAIL
    errs = chk.check(copy.deepcopy(docs))
    good = (not errs) if want_substr is None else any(want_substr in e for e in errs)
    PASS += good
    FAIL += not good
    print(("  ok   " if good else "  FAIL ") + desc + ("" if good else f" -> {errs}"))


case("consistent set passes", GOOD)
case("ExternalSecret in a namespace outside the list", GOOD + [es("tenant-a")], "tenant-a")
case("SecretStore without a namespace", GOOD + [es(None, "SecretStore")], "SecretStore")
case("ClusterExternalSecret", GOOD + [("b", {"kind": "ClusterExternalSecret", "metadata": {"name": "c"}})], "ClusterExternalSecret")
case("a second ClusterSecretStore", GOOD + [store([{"namespaces": NS}], "other")], "only openbao")
case("store without conditions", [store(), es("foundation-vault"), policy(NS)], "no spec.conditions")
case("store with a label selector", [store([{"namespaceSelector": {"matchLabels": {"a": "b"}}}]), policy(NS)], "namespaces")
case("store with an empty namespaces list", [store([{"namespaces": []}]), policy(NS)], "no namespace")
case("no store at all", [es("foundation-vault")], "no ClusterSecretStore")
case("policy excluding another set", [GOOD[0], GOOD[1], policy(NS + ["extra"])], "excludes")
case("policy missing", GOOD[:3], "no ClusterPolicy")

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL or not PASS else 0)
