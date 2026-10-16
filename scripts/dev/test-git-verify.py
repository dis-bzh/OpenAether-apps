#!/usr/bin/env python3
"""Test of apps/flux/components/git-verify (opt-in: Flux verifies the OpenPGP signature of the HEAD commit).

Renders apps/flux/base, apps/flux/local and a profile with and without the component. Checks: default unchanged (no
`spec.verify` anywhere), the component adds exactly `verify: {mode: HEAD, secretRef: git-signers}` to the GitRepository
and keeps url/ref/interval/timeout (so ${GIT_REF} stays the injection point), and touches no other object.
It does NOT run Flux: that source-controller accepts it, rejects an unsigned commit and keeps the last artifact is in
LAB-CHECKLIST (B27). Rung: mocked/static.
Usage: python3 scripts/dev/test-git-verify.py   (needs kubectl and PyYAML)
"""
import copy
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
COMPONENT = REPO / "apps/flux/components/git-verify"
PASS = FAIL = 0


def check(desc, ok, detail=""):
    global PASS, FAIL
    PASS += bool(ok)
    FAIL += not ok
    print(("  ok   " if ok else "  FAIL ") + desc + ("" if ok else f" -> {detail}"))


def render(resource, components=()):
    with tempfile.TemporaryDirectory() as work:
        rel = lambda p: os.path.relpath(p, work)  # noqa: E731
        k = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": [rel(resource)]}
        if components:
            k["components"] = [rel(p) for p in components]
        (Path(work) / "kustomization.yaml").write_text(yaml.safe_dump(k))
        r = subprocess.run(["kubectl", "kustomize", "--load-restrictor=LoadRestrictionsNone", work],
                           capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"kubectl kustomize {resource} failed: {r.stderr}")
    return [d for d in yaml.safe_load_all(r.stdout) if d]


def gitrepos(docs):
    return [d for d in docs if d["kind"] == "GitRepository"]


for name in ("base", "workload", "management"):
    path = REPO / "apps/flux" / name
    if not (path / "kustomization.yaml").exists():
        continue
    print(name)
    off, on = render(path), render(path, [COMPONENT])
    g_off, g_on = gitrepos(off), gitrepos(on)
    check("one GitRepository, default has no spec.verify", len(g_off) == 1 and "verify" not in g_off[0]["spec"])
    if len(g_on) != 1:
        check("component keeps one GitRepository", False, len(g_on))
        continue
    spec = g_on[0]["spec"]
    check("verify is mode HEAD with the git-signers Secret", spec.get("verify") == {"mode": "HEAD",
          "secretRef": {"name": "git-signers"}}, spec.get("verify"))
    want = copy.deepcopy(g_off[0])
    want["spec"]["verify"] = spec.get("verify")
    check("only spec.verify differs (url, ref, interval, timeout kept)", g_on[0] == want)
    check("ref still the substituted ${GIT_REF} default", "GIT_REF" in str(spec["ref"]["name"]), spec["ref"])
    others = lambda docs: [d for d in docs if d["kind"] != "GitRepository"]  # noqa: E731
    check("no other object changes", others(off) == others(on))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
