#!/usr/bin/env python3
"""Named-identity RBAC brick (B26, static rung: reads manifests, proves nothing about a cluster).

The group bindings must stay least-surprise: only Groups, none prefixed `system:`, only cluster-admin/view, and the Flux
Kustomization that applies them is in the baseline of every generated profile.
"""
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
failures = 0


def check(name, ok):
    global failures
    print(("ok   " if ok else "FAIL ") + name)
    failures += 0 if ok else 1


built = subprocess.run(["kubectl", "kustomize", str(REPO / "apps/base/platform/rbac")], capture_output=True, text=True, check=True).stdout
docs = [d for d in yaml.safe_load_all(built) if d]
check("brick renders only ClusterRoleBindings", docs and all(d["kind"] == "ClusterRoleBinding" for d in docs))
by_name = {d["metadata"]["name"]: d for d in docs}
check("oa-admins binds oa:admins to cluster-admin",
      by_name.get("oa-admins", {}).get("roleRef", {}).get("name") == "cluster-admin"
      and [s["name"] for s in by_name["oa-admins"]["subjects"]] == ["oa:admins"])
check("oa-readers binds oa:readers to view",
      by_name.get("oa-readers", {}).get("roleRef", {}).get("name") == "view"
      and [s["name"] for s in by_name["oa-readers"]["subjects"]] == ["oa:readers"])
subjects = [s for d in docs for s in d["subjects"]]
check("every subject is a Group", all(s["kind"] == "Group" for s in subjects))
check("no group name starts with system:", not any(s["name"].startswith("system:") for s in subjects))
check("no binding grants a role other than cluster-admin or view", {d["roleRef"]["name"] for d in docs} <= {"cluster-admin", "view"})

flux = [d for d in yaml.safe_load_all((REPO / "apps/flux/base/01-platform.yaml").read_text()) if d]
ks = next((d for d in flux if d["metadata"]["name"] == "platform-rbac"), None)
check("platform-rbac Kustomization points at the brick", ks is not None and ks["spec"]["path"] == "./apps/base/platform/rbac")
check("platform-rbac depends only on namespaces", ks is not None and [d["name"] for d in ks["spec"]["dependsOn"]] == ["namespaces"])
cat = yaml.safe_load((REPO / "apps/flux/bricks.yaml").read_text())
check("platform-rbac is in the profile baseline", "platform-rbac" in cat["baseline"])

sys.exit(1 if failures else 0)
