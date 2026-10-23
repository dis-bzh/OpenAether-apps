#!/usr/bin/env python3
"""CycloneDX 1.5 inventory of what the DAG deploys (ANSSI SBOM25-1/-2/-3, SSDLC CICD-AIP-01b).

Built from the RENDERED output of every DAG path (scripts/dev/render-dag.py), because that is what Flux applies:
  - every Helm chart a HelmRelease installs (name, version, repository),
  - every container image named in the rendered manifests (Pods, Deployments, CronJobs, the vendored dumps...),
  - with --helm, the images the charts themselves render (`helm template` with the HelmRelease's values; needs
    helm and the network; a chart that does not render is listed on stderr and fails the run unless --allow-chart-failures).

An inventory, not a vulnerability report: the weekly workflow (sbom.yml) scans the images with Trivy. What it
cannot see: the layers of an image (only references are read here), anything a controller creates at run time
(Cluster API machines, the CNPG-managed pods), and the child clusters' own profile.

Usage: gen-sbom.py --rendered <dir> [--helm] [--version <tag>] [--timestamp <iso8601>] [--output <file>] [--images]
  --images prints only the distinct image references, one per line (the scan matrix).
Dependencies: PyYAML; helm for --helm.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import yaml

# A real reference: [registry/]path[:tag][@sha256:...] with no template left in it.
IMAGE_OK = re.compile(r"^[a-z0-9][\w.\-]*(?::[0-9]+)?(?:/[\w.\-]+)*(?::[\w.\-]+)?(?:@sha256:[0-9a-f]{64})?$", re.I)


def load_docs(rendered):
    docs = []
    for f in sorted(Path(rendered).glob("*.yaml")):
        docs += [d for d in yaml.safe_load_all(f.read_text()) if isinstance(d, dict)]
    return docs


def walk_images(node, out):
    """Every string under a key named `image`, and every {repository, tag} pair: Pod specs and chart values."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("image", "imageName") and isinstance(v, str):  # imageName: a CNPG Cluster's Postgres image
                out.add(v)
            elif k == "image" and isinstance(v, dict):
                ref = ref_from_values(v)
                if ref:
                    out.add(ref)
            else:
                walk_images(v, out)
    elif isinstance(node, list):
        for v in node:
            walk_images(v, out)


def ref_from_values(d):
    repo = d.get("repository")
    if not isinstance(repo, str) or not repo:
        return None
    reg = d.get("registry")
    ref = f"{reg}/{repo}" if isinstance(reg, str) and reg and not repo.startswith(reg) else repo
    tag = d.get("tag")
    if tag:
        ref += f":{tag}"
    return ref


def valid(ref):
    return bool(ref) and "{{" not in ref and "${" not in ref and IMAGE_OK.match(ref)


def split_image(ref):
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    tag = None
    if ":" in ref.rsplit("/", 1)[-1]:
        ref, tag = ref.rsplit(":", 1)
    return ref, tag, digest


def image_component(ref):
    name, tag, digest = split_image(ref)
    purl = f"pkg:oci/{name.rsplit('/', 1)[-1]}"
    purl += f"@{quote(digest, safe='')}" if digest else (f"@{quote(tag, safe='')}" if tag else "")
    qual = [f"repository_url={quote(name, safe='')}"] + ([f"tag={quote(tag, safe='')}"] if tag and digest else [])
    purl += "?" + "&".join(qual)
    comp = {"type": "container", "bom-ref": purl, "name": name, "version": tag or digest or "unversioned", "purl": purl}
    if digest and digest.startswith("sha256:"):
        comp["hashes"] = [{"alg": "SHA-256", "content": digest[len("sha256:"):]}]
    return comp


def charts(docs):
    """[(release, namespace, chart, version, repo url, values)] of the HelmReleases, repo urls resolved."""
    repos = {(d["metadata"].get("namespace"), d["metadata"]["name"]): d["spec"].get("url", "")
             for d in docs if d.get("kind") == "HelmRepository"}
    out = []
    for d in docs:
        if d.get("kind") != "HelmRelease":
            continue
        spec = ((d.get("spec") or {}).get("chart") or {}).get("spec") or {}
        if not spec.get("chart"):
            continue
        ref = spec.get("sourceRef") or {}
        url = repos.get((ref.get("namespace") or d["metadata"].get("namespace"), ref.get("name")), "")
        values = standins(d["spec"].get("values") or {}, d["spec"].get("valuesFrom") or [])
        out.append((d["metadata"]["name"], d["metadata"].get("namespace") or "default", spec["chart"],
                    str(spec.get("version", "")), url, values, ref.get("name", "")))
    return out


def standins(values, values_from):
    """values plus a placeholder at every valuesFrom targetPath: Flux injects the real ones from a Secret at run
    time, and a chart that dereferences them (Loki's bucket names) does not render without something there."""
    out = json.loads(json.dumps(values))
    for vf in values_from:
        path = vf.get("targetPath")
        if not path:
            continue
        node = out
        keys = path.split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
            if not isinstance(node, dict):
                break
        else:
            node.setdefault(keys[-1], "placeholder")
    return out


def chart_component(chart, version, url):
    v = quote(version.lstrip("v"), safe="")
    purl = f"pkg:helm/{chart}@{v}" + (f"?repository_url={quote(url, safe='')}" if url else "")
    return {"type": "application", "bom-ref": purl, "name": chart, "version": version, "purl": purl,
            "description": "Helm chart installed by a HelmRelease"}


def is_test_hook(doc):
    """A `helm.sh/hook: test` resource only exists when someone runs `helm test`, which Flux does not (no spec.test)."""
    ann = (doc.get("metadata") or {}).get("annotations") or {}
    return "test" in [h.strip() for h in str(ann.get("helm.sh/hook", "")).split(",")]


def helm_images(rel, ns, chart, version, url, values):
    """Images a chart renders with the release's values. Raises RuntimeError with helm's message on failure."""
    with tempfile.TemporaryDirectory() as d:
        vf = Path(d) / "values.yaml"
        vf.write_text(yaml.safe_dump(values))
        if url.startswith("oci://"):
            cmd = ["helm", "template", rel, f"{url.rstrip('/')}/{chart}", "--version", version]
        else:
            cmd = ["helm", "template", rel, chart, "--repo", url, "--version", version]
        cmd += ["--namespace", ns, "-f", str(vf), "--include-crds"]
        res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip().splitlines()[-1] if res.stderr.strip() else "helm template failed")
    found = set()
    for doc in yaml.safe_load_all(res.stdout):
        if isinstance(doc, dict) and not is_test_hook(doc):
            walk_images(doc, found)
    return found


def build(docs, version, timestamp, use_helm, allow_failures):
    comps = {}

    def add(c, where):
        known = comps.setdefault(c["bom-ref"], c)
        props = known.setdefault("properties", [])
        p = {"name": "oa:where", "value": where}
        if p not in props:
            props.append(p)

    refs = {}
    for d in docs:
        found = set()
        walk_images(d, found)
        for ref in found:
            if valid(ref):
                refs.setdefault(ref, set()).add(f"{d.get('kind')}/{(d.get('metadata') or {}).get('name')}")
    failures = []
    for rel, ns, chart, ver, url, values, _ in charts(docs):
        add(chart_component(chart, ver, url), f"HelmRelease/{rel}")
        found = set()
        walk_images(values, found)
        if use_helm:
            try:
                found |= helm_images(rel, ns, chart, ver, url, values)
            except RuntimeError as e:
                failures.append(f"{rel} ({chart} {ver}): {e}")
        for ref in found:
            if valid(ref):
                refs.setdefault(ref, set()).add(f"chart {chart}")
    for ref, where in refs.items():
        for w in sorted(where):
            add(image_component(ref), w)
    for c in comps.values():
        c["properties"] = sorted(c.get("properties", []), key=lambda p: p["value"])
    ordered = sorted(comps.values(), key=lambda c: (c["type"], c["bom-ref"]))
    bom = {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
        "metadata": {
            "timestamp": timestamp,
            "tools": {"components": [{"type": "application", "name": "gen-sbom.py", "version": "1"}]},
            "component": {"type": "application", "bom-ref": "openaether-apps", "name": "OpenAether-apps",
                          "version": version},
        },
        "components": ordered,
    }
    return bom, failures


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rendered", required=True, help="directory written by render-dag.py")
    ap.add_argument("--helm", action="store_true", help="also render each chart with helm template")
    ap.add_argument("--allow-chart-failures", action="store_true")
    ap.add_argument("--version", default="unreleased", help="the tag or commit this inventory describes")
    ap.add_argument("--timestamp", help="ISO 8601; default SOURCE_DATE_EPOCH, else now")
    ap.add_argument("--output")
    ap.add_argument("--images", action="store_true", help="print the distinct image references only")
    a = ap.parse_args(argv)
    ts = a.timestamp
    if not ts:
        epoch = os.environ.get("SOURCE_DATE_EPOCH")
        ts = (datetime.fromtimestamp(int(epoch), timezone.utc) if epoch
              else datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bom, failures = build(load_docs(a.rendered), a.version, ts, a.helm, a.allow_chart_failures)
    for f in failures:
        print(f"chart did not render: {f}", file=sys.stderr)
    if failures and not a.allow_chart_failures:
        return 1
    kinds = {c["type"] for c in bom["components"]}
    if not {"container", "application"} <= kinds:
        print("the inventory has no image or no chart: the extractors are broken, not the repository", file=sys.stderr)
        return 1
    if a.images:
        for c in bom["components"]:
            if c["type"] == "container":
                print(c["name"] + (f":{c['version']}" if c["version"] != "unversioned" and "hashes" not in c
                                  else ""))
        return 0
    text = json.dumps(bom, indent=2, sort_keys=True) + "\n"
    if a.output:
        Path(a.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
