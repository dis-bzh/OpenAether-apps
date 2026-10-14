#!/usr/bin/env python3
"""Pins the Kyverno cleanup CronJob fix (#48): static checks on the render, then the rendered script is run under bash
with a stub kubectl. The old patch downloaded kubectl at every run and could not fail; both must stay impossible.
Needs kubectl and PyYAML. A self-test mutates the script and requires the checks to fail."""
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[2])
NAMES = ("kyverno-cleanup-admission-reports", "kyverno-cleanup-cluster-admission-reports")
IMAGE = re.compile(r"^docker\.io/alpine/k8s:\d+\.\d+\.\d+$")
FORBIDDEN = re.compile(r"\b(wget|curl|apk)\b|dl\.k8s\.io")


def rendered():
    out = subprocess.run(["kubectl", "kustomize", str(REPO / "apps/base/kyverno")],
                         capture_output=True, text=True, check=True).stdout
    jobs = {d["metadata"]["name"]: d for d in yaml.safe_load_all(out) if d and d.get("kind") == "CronJob"}
    return {n: jobs[n] for n in NAMES if n in jobs}


def static_errors(name, script, pod):
    c, errs = pod["containers"][0], []
    if not IMAGE.match(c["image"]):
        errs.append(f"{name}: image {c['image']} is not docker.io/alpine/k8s:<semver>")
    if c["command"][:2] != ["/bin/bash", "-c"]:
        errs.append(f"{name}: command is not /bin/bash -c")
    if not script.startswith("set -euo pipefail"):
        errs.append(f"{name}: script does not start with set -euo pipefail")
    if FORBIDDEN.search(script):
        errs.append(f"{name}: script downloads something (wget/curl/apk/dl.k8s.io)")
    sc = c.get("securityContext", {})
    if sc.get("runAsNonRoot") is not True or not isinstance(sc.get("runAsUser"), int):
        errs.append(f"{name}: needs runAsNonRoot true and a numeric runAsUser")
    home = {e["name"]: e.get("value") for e in c.get("env", [])}.get("HOME")
    mounts = {m["mountPath"]: m["name"] for m in c.get("volumeMounts", [])}
    empty = {v["name"] for v in pod.get("volumes", []) if "emptyDir" in v}
    if not home or mounts.get(home) not in empty:
        errs.append(f"{name}: HOME {home} is not on a mounted emptyDir")
    return errs


def run_script(script, count=None, fail=False):
    """Run the script with a stub kubectl; returns (rc, stdout, calls)."""
    with tempfile.TemporaryDirectory() as d:
        calls = Path(d, "calls")
        stub = Path(d, "kubectl")
        stub.write_text('#!/bin/bash\necho "$*" >> "$CALLS"\n[ "$1" = get ] || exit 0\n'
                        '[ -z "$FAIL" ] || exit 1\nseq 1 "$COUNT"\n')
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        env = {"PATH": f"{d}:/usr/bin:/bin", "CALLS": str(calls), "COUNT": str(count or 0),
               "FAIL": "1" if fail else ""}
        p = subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True)
        return p.returncode, p.stdout, calls.read_text() if calls.exists() else ""


def behaviour_errors(name, script):
    errs = []
    rc, _, _ = run_script(script, fail=True)
    if rc == 0:
        errs.append(f"{name}: a failing kubectl get does not fail the script")
    rc, out, calls = run_script(script, count=5)
    if rc != 0 or "(5) reports found, no clean up needed" not in out or "delete" in calls:
        errs.append(f"{name}: 5 reports should log 'no clean up needed' and delete nothing")
    rc, out, calls = run_script(script, count=10001)
    if rc != 0 or "too many reports found (10001)" not in out or "delete" not in calls:
        errs.append(f"{name}: 10001 reports should take the delete branch")
    return errs


def check(script_of, pod_of):
    errs = []
    jobs = rendered()
    for n in NAMES:
        if n not in jobs:
            errs.append(f"{n}: CronJob missing from the kyverno render")
            continue
        pod = jobs[n]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        script = script_of(pod["containers"][0]["command"][-1])
        errs += static_errors(n, script, pod) + behaviour_errors(n, script)
    return errs


def main():
    errs = check(lambda s: s, None)
    # The checks must be able to fail: each mutation below has to be caught.
    mutations = {
        "pipefail removed": lambda s: s.replace("set -euo pipefail", "set -eu"),
        "download re-added": lambda s: "wget -q https://dl.k8s.io/kubectl && " + s,
        "failure swallowed": lambda s: s.replace("set -euo pipefail", "set -u").replace("| wc -l", "| wc -l || true"),
    }
    for label, mutate in mutations.items():
        if not check(mutate, None):
            errs.append(f"self-test: mutation '{label}' was not detected")
    if errs:
        print("\n".join("FAIL " + e for e in errs))
        return 1
    print("ok: both cleanup CronJobs render the fixed script and behave under a stub kubectl; 3 mutations caught")
    return 0


sys.exit(main())
