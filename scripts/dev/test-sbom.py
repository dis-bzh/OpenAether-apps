#!/usr/bin/env python3
"""gen-sbom.py on a fixture (each extractor seen to find its input and to skip the lookalike) and, when kubectl is
there, on the real tree (a floor per component kind: an inventory that silently lists nothing is the defect this
guards against). No network (the --helm path is exercised by sbom.yml). Usage: python3 scripts/dev/test-sbom.py
"""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("gen", HERE / "gen-sbom.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

FIXTURE = textwrap.dedent("""\
    apiVersion: apps/v1
    kind: Deployment
    metadata: {name: web}
    spec:
      template:
        spec:
          initContainers:
            - {name: i, image: "docker.io/acme/init:1.0"}
          containers:
            - {name: c, image: "ghcr.io/acme/web:v2@sha256:%s"}
            - {name: t, image: "{{ .Values.image }}"}
            - {name: e, image: "${IMAGE}"}
    ---
    apiVersion: postgresql.cnpg.io/v1
    kind: Cluster
    metadata: {name: db}
    spec: {imageName: "ghcr.io/cloudnative-pg/postgresql:18.4"}
    ---
    apiVersion: apiextensions.k8s.io/v1
    kind: CustomResourceDefinition
    metadata: {name: crd}
    spec:
      versions:
        - schema:
            openAPIV3Schema:
              properties:
                image: {type: string, description: "the image to run"}
    ---
    apiVersion: source.toolkit.fluxcd.io/v1
    kind: HelmRepository
    metadata: {name: acme, namespace: flux-system}
    spec: {url: "https://charts.example.invalid"}
    ---
    apiVersion: helm.toolkit.fluxcd.io/v2
    kind: HelmRelease
    metadata: {name: widget, namespace: apps}
    spec:
      chart:
        spec:
          chart: widget
          version: "3.4.5"
          sourceRef: {kind: HelmRepository, name: acme, namespace: flux-system}
      valuesFrom:
        - {kind: Secret, name: s, valuesKey: bucket, targetPath: store.bucket}
      values:
        image: {registry: quay.io, repository: acme/widget, tag: "9.9"}
""" % ("a" * 64))
PASS = FAIL = 0


def check(name, ok):
    global PASS, FAIL
    print(("ok   " if ok else "FAIL ") + name)
    PASS += ok
    FAIL += not ok


with tempfile.TemporaryDirectory() as d:
    (Path(d) / "x.yaml").write_text(FIXTURE)
    bom, failures = gen.build(gen.load_docs(d), "t", "2026-01-01T00:00:00Z", False, False)
    comps = {c["bom-ref"]: c for c in bom["components"]}
    names = {(c["type"], c["name"], c["version"]) for c in comps.values()}
    check("an init container image is found", ("container", "docker.io/acme/init", "1.0") in names)
    check("a digest-pinned image keeps its tag and carries the sha256",
          any(c["name"] == "ghcr.io/acme/web" and c["version"] == "v2" and c.get("hashes") for c in comps.values()))
    check("a CNPG imageName is found", ("container", "ghcr.io/cloudnative-pg/postgresql", "18.4") in names)
    check("a chart's values {registry, repository, tag} become an image", ("container", "quay.io/acme/widget", "9.9") in names)
    check("a templated or substituted reference is skipped", not any("{{" in c["name"] or "$" in c["name"] for c in comps.values()))
    check("a CRD schema property named image is not an image", len([c for c in comps.values() if c["type"] == "container"]) == 4)
    check("a HelmRelease is a chart component with its repository",
          ("application", "widget", "3.4.5") in names and any("repository_url=https%3A%2F%2Fcharts.example.invalid" in r for r in comps))
    check("no render failure without --helm", failures == [])
    rel = [x for x in gen.charts(gen.load_docs(d))][0]
    check("a valuesFrom target gets a placeholder so the chart can render", rel[5]["store"]["bucket"] == "placeholder")
    again, _ = gen.build(gen.load_docs(d), "t", "2026-01-01T00:00:00Z", False, False)
    check("the output is deterministic", json.dumps(bom, sort_keys=True) == json.dumps(again, sort_keys=True))

with tempfile.TemporaryDirectory() as d:
    r = subprocess.run([sys.executable, str(HERE / "gen-sbom.py"), "--rendered", d], capture_output=True, text=True)
    check("an empty tree is an error, not an empty inventory", r.returncode == 1)

# check-image-pins.py: the floating ones fail, the tag-only ones are counted.
spec2 = importlib.util.spec_from_file_location("pins", HERE.parent / "check-image-pins.py")
pins = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(pins)
pod = lambda *imgs: {"kind": "Pod", "metadata": {"name": "p"}, "spec": {"containers": [{"image": i} for i in imgs]}}
errs, tag_only = pins.audit([pod("docker.io/acme/a", "docker.io/acme/b:latest", "docker.io/acme/c:1.0",
                                 "docker.io/acme/d:1.0@sha256:" + "b" * 64)])
check("an image with no tag is an error", any("acme/a" in e and "floats" in e for e in errs))
check("a moving tag is an error", any("acme/b" in e and "moves" in e for e in errs))
check("a tag-only image is counted, a digest-pinned one is not", tag_only == ["docker.io/acme/c:1.0"])

if shutil.which("kubectl"):
    with tempfile.TemporaryDirectory() as d:
        subprocess.run([sys.executable, str(HERE / "render-dag.py"), d], check=True, capture_output=True)
        bom, _ = gen.build(gen.load_docs(d), "t", "2026-01-01T00:00:00Z", False, False)
        n = {t: len([c for c in bom["components"] if c["type"] == t]) for t in ("container", "application")}
        check(f"the real tree lists at least 10 images and 10 charts (got {n})", n["container"] >= 10 and n["application"] >= 10)
else:
    print("skip real tree: kubectl is missing")

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
