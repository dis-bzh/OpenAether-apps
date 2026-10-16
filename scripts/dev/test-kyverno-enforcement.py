#!/usr/bin/env python3
"""Test of the Kyverno enforcement switch (apps/base/kyverno-policies, docs/kyverno-enforcement.md).

Five policies can be flipped to Enforce one namespace at a time, by labelling the namespace
`kyverno.openaether.io/enforce-<policy>: "true"` (spec.validationFailureActionOverrides, the Kyverno 1.12 spelling). Checks:
every one of them stays Audit globally, carries exactly that one override, matches pods at CREATE only (so an update of a running
pod is never denied; the ephemeral-container rule is an UPDATE by nature and is the one exception), no namespace in the repo
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
FLIPPABLE = ["restrict-privileged", "restrict-capabilities", "restrict-capabilities-ephemeral", "require-security-context",
             "restrict-image-registries", "require-limits"]
# one namespace label flips both capability policies (the ephemeral rule needed its own policy: background scan is off for it)
LABEL_OF = {"restrict-capabilities-ephemeral": "restrict-capabilities"}
ALREADY_ENFORCE = {"disallow-root-user", "disallow-default-namespace"}
ALWAYS_AUDIT = {"require-ro-rootfs"}
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

for name in FLIPPABLE:
    spec = pol[name]["spec"]
    print(name)
    check("Audit globally", spec["validationFailureAction"] == "Audit", spec["validationFailureAction"])
    want = [{"action": "Enforce", "namespaceSelector": {"matchLabels": {PREFIX + LABEL_OF.get(name, name): "true"}}}]
    check("one override: Enforce where the namespace carries its label", spec.get("validationFailureActionOverrides") == want,
          spec.get("validationFailureActionOverrides"))
    if any(k == "Pod/ephemeralcontainers" for rule in spec["rules"] for blk in rule["match"]["any"] for k in blk["resources"]["kinds"]):
        check("a policy that matches a subresource has background scanning off (Kyverno 1.12.1 refuses it otherwise)", spec.get("background") is False, spec.get("background"))
    for rule in spec["rules"]:
        kinds = [k for blk in rule["match"]["any"] for k in blk["resources"]["kinds"]]
        ops = [blk["resources"].get("operations") for blk in rule["match"]["any"]]
        if kinds == ["Pod/ephemeralcontainers"]:
            check(f"{rule['name']}: the ephemeral-container subresource is matched explicitly", True)
        else:
            check(f"{rule['name']}: matches Pod at CREATE only", kinds == ["Pod"] and ops == [["CREATE"]], (kinds, ops))
        check(f"{rule['name']}: no Kyverno-1.13 `validate.failureActionOverrides` (the vendored 1.12.1 rejects it)",
              "failureActionOverrides" not in rule["validate"])

check("require-ro-rootfs stays Audit with no override (informational, no flip prepared)",
      all(pol[n]["spec"]["validationFailureAction"] == "Audit" and "validationFailureActionOverrides" not in pol[n]["spec"]
          for n in ALWAYS_AUDIT))
check("the two policies that already enforce are untouched",
      all(pol[n]["spec"]["validationFailureAction"] == "Enforce" and "validationFailureActionOverrides" not in pol[n]["spec"]
          for n in ALREADY_ENFORCE))
check("no other policy exists unlisted", set(pol) == set(FLIPPABLE) | ALREADY_ENFORCE | ALWAYS_AUDIT, sorted(pol))

# Default off: nothing shipped labels a namespace for Enforce (policies and docs excluded: they spell the label).
hits = [str(p.relative_to(REPO)) for p in (REPO / "apps").rglob("*.yaml")
        if "kyverno-policies" not in p.parts and PREFIX in p.read_text()]
check("no manifest under apps/ labels a namespace for Enforce (the shipped default is Audit everywhere)", not hits, hits)

doc = (REPO / "docs/kyverno-enforcement.md").read_text()
check("docs/kyverno-enforcement.md names every label", all(PREFIX + LABEL_OF.get(n, n) in doc for n in FLIPPABLE),
      [n for n in FLIPPABLE if PREFIX + n not in doc])

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
