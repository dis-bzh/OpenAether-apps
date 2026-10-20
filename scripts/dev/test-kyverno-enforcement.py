#!/usr/bin/env python3
"""Test of the Kyverno enforcement switch (apps/base/kyverno-policies, docs/kyverno-enforcement.md).

Five policies can be flipped to Enforce one namespace at a time, by labelling the namespace
`kyverno.openaether.io/enforce-<policy>: "true"` (`validate.failureActionOverrides` on every rule, rule-level `failureAction: Audit`; the spec-level spelling is no longer honoured by Kyverno 1.19.1, lab 15A). Checks:
every one of them stays Audit globally, carries exactly that one override on each rule, matches pods at CREATE only (so an update of a running
pod is never denied; the two ephemeral-container policies are an UPDATE by nature and are the exception: they must list CREATE and UPDATE,
because with no `operations` Kyverno registers CREATE only on the webhook and `kubectl debug` is never seen, lab 11), no namespace in the repo
carries such a label (the default is today's Audit everywhere), the two policies that already enforce are untouched, and the
document names every label. It does NOT run Kyverno: that the override denies a pod is in LAB-CHECKLIST (B29).
Rung: mocked/static.   Usage: python3 scripts/dev/test-kyverno-enforcement.py   (needs kubectl and PyYAML)
"""
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PREFIX = "kyverno.openaether.io/enforce-"
FLIPPABLE = ["restrict-privileged", "restrict-capabilities", "restrict-capabilities-ephemeral", "restrict-privileged-ephemeral",
             "require-security-context",
             "restrict-image-registries", "require-limits"]
# one namespace label flips a policy and its ephemeral twin (the ephemeral rule needed its own policy: background scan is off for it)
LABEL_OF = {"restrict-capabilities-ephemeral": "restrict-capabilities", "restrict-privileged-ephemeral": "restrict-privileged"}
ALREADY_ENFORCE = {"disallow-root-user", "disallow-default-namespace"}
ALWAYS_AUDIT = {"require-ro-rootfs"}
# Matches CNPG Clusters, not Pods; Enforce with failurePolicy Ignore (a Kyverno outage must not block the live Clusters).
CLUSTER_GUARD = {"restrict-restore-test-clusters"}
PASS = FAIL = 0


def check(desc, ok, detail=""):
    global PASS, FAIL
    PASS += bool(ok)
    FAIL += not ok
    print(("  ok   " if ok else "  FAIL ") + desc + ("" if ok else f" -> {detail}"))


r = subprocess.run(["kubectl", "kustomize", str(REPO / "apps/base/kyverno-policies")], capture_output=True, text=True)
if r.returncode:
    sys.exit(f"kubectl kustomize failed: {r.stderr}")
pol = {d["metadata"]["name"]: d for d in yaml.safe_load_all(r.stdout) if d}
# The Cluster guard lives in its own brick: Kyverno compiles a policy against the kinds it resolves when the policy is created,
# so it must be created after the CNPG CRDs exist (lab 20: created earlier it admitted an rt-x Cluster until it was touched).
r2 = subprocess.run(["kubectl", "kustomize", str(REPO / "apps/base/kyverno-policies-cnpg")], capture_output=True, text=True)
if r2.returncode:
    sys.exit(f"kubectl kustomize failed: {r2.stderr}")
guard = {d["metadata"]["name"]: d for d in yaml.safe_load_all(r2.stdout) if d}
check("the Cluster guard is NOT in the brick that Kyverno loads before the CNPG CRDs exist", not (CLUSTER_GUARD & set(pol)), sorted(pol))
pol.update(guard)
ks = {d["metadata"]["name"]: d for d in yaml.safe_load_all((REPO / "apps/flux/base/40-kyverno.yaml").read_text()) if d}
check("its Kustomization waits for cnpg and for kyverno-policies",
      {x["name"] for x in ks["kyverno-policies-cnpg"]["spec"]["dependsOn"]} == {"cnpg", "kyverno-policies"})

for name in FLIPPABLE:
    spec = pol[name]["spec"]
    print(name)
    check("Audit globally", spec["validationFailureAction"] == "Audit", spec["validationFailureAction"])
    sel = {"matchLabels": {PREFIX + LABEL_OF.get(name, name): "true"}}
    want = [{"action": "Enforce", "namespaceSelector": sel}]
    if name in LABEL_OF:
        # Kyverno 1.19.1 ignores a validationFailureActionOverrides namespaceSelector on the ephemeralcontainers subresource
        # (admitted with a warning in a labelled namespace; lab 12). Two rules do it: Audit outside the labelled namespaces,
        # Enforce inside, the namespaceSelector sitting in `match` / `exclude`.
        rules = spec["rules"]
        enf = [r for r in rules if r["validate"].get("failureAction") == "Enforce"]
        aud = [r for r in rules if r["validate"].get("failureAction") == "Audit"]
        check("ephemeral policy: no spec-level override (it is not honoured on the subresource)",
              "validationFailureActionOverrides" not in spec, spec.get("validationFailureActionOverrides"))
        check("ephemeral policy: one Enforce rule whose match carries the namespace label",
              len(enf) == 1 and all(blk["resources"].get("namespaceSelector") == sel for blk in enf[0]["match"]["any"]), enf)
        check("ephemeral policy: one Audit rule that excludes the labelled namespaces",
              len(aud) == 1 and any(blk["resources"].get("namespaceSelector") == sel for blk in aud[0]["exclude"]["any"]), aud)
    else:
        # Kyverno 1.19.1 does not apply the spec-level validationFailureActionOverrides namespaceSelector at all: a hostNetwork pod
        # was admitted with a warning in a labelled namespace (lab 15A, OVH). The rule-level spelling (1.13+) denies it.
        check("no spec-level override (Kyverno 1.19.1 ignores it)", "validationFailureActionOverrides" not in spec,
              spec.get("validationFailureActionOverrides"))
        check("every rule: failureAction Audit + one override, Enforce where the namespace carries its label",
              all(r["validate"].get("failureAction") == "Audit" and r["validate"].get("failureActionOverrides") == want for r in spec["rules"]),
              [(r["name"], r["validate"].get("failureAction"), r["validate"].get("failureActionOverrides")) for r in spec["rules"]])
    if any(k == "Pod/ephemeralcontainers" for rule in spec["rules"] for blk in rule["match"]["any"] for k in blk["resources"]["kinds"]):
        check("a policy that matches a subresource has background scanning off (Kyverno 1.12.1 refuses it otherwise)", spec.get("background") is False, spec.get("background"))
    for rule in spec["rules"]:
        kinds = [k for blk in rule["match"]["any"] for k in blk["resources"]["kinds"]]
        ops = [blk["resources"].get("operations") for blk in rule["match"]["any"]]
        if kinds == ["Pod/ephemeralcontainers"]:
            check(f"{rule['name']}: the ephemeral-container subresource is matched at CREATE and UPDATE (kubectl debug is an UPDATE)",
                  ops == [["CREATE", "UPDATE"]], ops)
        else:
            check(f"{rule['name']}: matches Pod at CREATE only", kinds == ["Pod"] and ops == [["CREATE"]], (kinds, ops))
        if name in LABEL_OF:
            check(f"{rule['name']}: no `validate.failureActionOverrides` (the ephemeral policies use two rules, see above)",
                  "failureActionOverrides" not in rule["validate"])

check("require-ro-rootfs stays Audit with no override (informational, no flip prepared)",
      all(pol[n]["spec"]["validationFailureAction"] == "Audit" and "validationFailureActionOverrides" not in pol[n]["spec"]
          for n in ALWAYS_AUDIT))
check("the two policies that already enforce are untouched",
      all(pol[n]["spec"]["validationFailureAction"] == "Enforce" and "validationFailureActionOverrides" not in pol[n]["spec"]
          for n in ALREADY_ENFORCE))
# spread-gateway-pods is a mutation (no validate rule, so nothing to flip): it must stay one.
MUTATIONS = {"spread-gateway-pods"}
check("the mutation policies have no validate rule",
      all("validate" not in r and "mutate" in r for n in MUTATIONS for r in pol[n]["spec"]["rules"]))
check("the Cluster guard enforces, ignores a Kyverno outage, and matches Clusters only",
      all(pol[n]["spec"]["validationFailureAction"] == "Enforce" and pol[n]["spec"].get("failurePolicy") == "Ignore"
          and all(k == "postgresql.cnpg.io/v1/Cluster" for r in pol[n]["spec"]["rules"] for b in r["match"]["any"] for k in b["resources"]["kinds"])
          for n in CLUSTER_GUARD))
check("no other policy exists unlisted", set(pol) - MUTATIONS == set(FLIPPABLE) | ALREADY_ENFORCE | ALWAYS_AUDIT | CLUSTER_GUARD, sorted(pol))

# Default off: nothing shipped labels a namespace for Enforce (policies and docs excluded: they spell the label).
hits = [str(p.relative_to(REPO)) for p in (REPO / "apps").rglob("*.yaml")
        if "kyverno-policies" not in p.parts and PREFIX in p.read_text()]
check("no manifest under apps/ labels a namespace for Enforce (the shipped default is Audit everywhere)", not hits, hits)

doc = (REPO / "docs/kyverno-enforcement.md").read_text()
check("docs/kyverno-enforcement.md names every label", all(PREFIX + LABEL_OF.get(n, n) in doc for n in FLIPPABLE),
      [n for n in FLIPPABLE if PREFIX + n not in doc])

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
