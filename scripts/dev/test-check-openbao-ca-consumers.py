#!/usr/bin/env python3
"""Fixture tests of scripts/check-openbao-ca-consumers.py: passes on a consistent set, fails for the right reason on each drift.
No cluster, no kubectl. Usage: python3 scripts/dev/test-check-openbao-ca-consumers.py
"""
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("chk", HERE.parent / "check-openbao-ca-consumers.py")
chk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chk)


def cert(ns, issuer="openbao-ca", kind="ClusterIssuer", k="Certificate"):
    return ("b", {"kind": k, "metadata": {"name": "c", "namespace": ns}, "spec": {"issuerRef": {"name": issuer, "kind": kind}}})


def policy(excluded, failure="Ignore", ops=("CREATE", "UPDATE")):
    return ("p", {"kind": "ClusterPolicy", "metadata": {"name": chk.POLICY}, "spec": {
        **({"failurePolicy": failure} if failure else {}),
        "rules": [{"name": "r", "match": {"any": [{"resources": {"operations": list(ops)}}]},
                   "exclude": {"any": [{"resources": {"namespaces": excluded}}]}}]}})


NS = ["a", "b"]
GOOD = [cert("a"), cert("b", k="CertificateRequest"), cert("z", issuer="other"), cert("y", kind="Issuer"), policy(NS)]
cases = [
    ("consistent set passes", GOOD, None),
    ("a consumer outside the policy is flagged", GOOD + [cert("c")], "excludes ['a', 'b']"),
    ("an excluded namespace with no consumer is flagged", GOOD[:1] + [policy(NS)], "excludes ['a', 'b']"),
    ("missing policy is flagged", GOOD[:-1], "no ClusterPolicy"),
    ("failurePolicy Fail is flagged", GOOD[:-1] + [policy(NS, failure="Fail")], "failurePolicy: Ignore"),
    ("DELETE matching is flagged", GOOD[:-1] + [policy(NS, ops=("CREATE", "DELETE"))], "CREATE/UPDATE only"),
]
bad = 0
for name, docs, want in cases:
    errs = chk.check(docs)
    ok = (not errs) if want is None else any(want in e for e in errs)
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f" -> {errs}"))
    bad += not ok
sys.exit(1 if bad else 0)
