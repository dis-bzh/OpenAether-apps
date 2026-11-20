#!/usr/bin/env python3
"""check-openbao-ca-consumers.py — the Audit policy names exactly the namespaces that use the openbao-ca ClusterIssuer.

A Certificate or CertificateRequest that references ClusterIssuer/openbao-ca from a namespace the policy
`restrict-openbao-ca-issuer` does not exclude would be reported on every cluster; a namespace excluded without
a consumer is a hole for nothing (apps #73). Also keeps `failurePolicy: Ignore` and CREATE/UPDATE-only matching.

Usage: python3 scripts/check-openbao-ca-consumers.py      Dependency: kubectl, PyYAML.
"""
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_s = importlib.util.spec_from_file_location("esns", HERE / "check-externalsecret-namespaces.py")
esns = importlib.util.module_from_spec(_s)
_s.loader.exec_module(esns)

ISSUER, POLICY = "openbao-ca", "restrict-openbao-ca-issuer"
KINDS = {"Certificate", "CertificateRequest"}


def check(docs):
    docs = [(p, d) for p, d in docs if isinstance(d, dict) and d.get("kind")]
    errors, used = [], set()
    for p, d in docs:
        ref = (d.get("spec") or {}).get("issuerRef") or {}
        if d["kind"] in KINDS and ref.get("name") == ISSUER and ref.get("kind") == "ClusterIssuer":
            used.add(d["metadata"].get("namespace"))
    pols = [(p, d) for p, d in docs if d["kind"] == "ClusterPolicy" and d["metadata"]["name"] == POLICY]
    if not pols:
        return [f"no ClusterPolicy/{POLICY} rendered"]
    p, pol = pols[0]
    excluded = set()
    for rule in pol["spec"]["rules"]:
        for e in (rule.get("exclude") or {}).get("any", []):
            excluded.update(e.get("resources", {}).get("namespaces", []))
        for m in (rule.get("match") or {}).get("any", []):
            if set(m.get("resources", {}).get("operations") or ["DELETE"]) - {"CREATE", "UPDATE"}:
                errors.append(f"{p}: rule {rule.get('name')} must match CREATE/UPDATE only (request.object is empty on DELETE)")
    if excluded != used:
        errors.append(f"{p}: {POLICY} excludes {sorted(excluded)} but the consumers of {ISSUER} are {sorted(x or '' for x in used)}")
    if pol["spec"].get("failurePolicy") != "Ignore":
        errors.append(f"{p}: {POLICY} needs failurePolicy: Ignore (an Audit policy must not block writes when Kyverno is down)")
    return errors


def main():
    from pick import load_dag
    paths = sorted({b.path for b in load_dag().values() if b.path} | esns.flux_paths())
    docs, errors = [], []
    for path in paths:
        objs, err = esns.render(path)
        if err:
            errors.append(f"{path}: build failed: {err.strip()}")
            continue
        docs += [(path, o) for o in objs or []]
    errors += check(docs)
    if errors:
        print(f"{len(errors)} problem(s):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)
    print(f"OK — the namespaces that use ClusterIssuer/{ISSUER} are exactly the ones {POLICY} allows.")


if __name__ == "__main__":
    main()
