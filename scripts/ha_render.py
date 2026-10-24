"""Shared by check-ha.py: render the pods the platform bricks create (kustomize output plus every HelmRelease through
`helm template` and its post-renderer patches). Needs kubectl, helm (network for the charts) and PyYAML."""
import subprocess
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent


def load_docs(text):
    out = []
    for d in yaml.safe_load_all(text):
        if isinstance(d, dict):
            if d.get("kind") == "List":
                out.extend(i for i in d.get("items", []) if isinstance(i, dict))
            else:
                out.append(d)
    return out


def kustomize(path):
    r = subprocess.run(["kubectl", "kustomize", str(path)], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"{path}: {r.stderr.strip()[-300:]}")
    return load_docs(r.stdout)


def helm_release(hr, repos):
    """The objects one HelmRelease creates: chart + values, then the post-renderer kustomize patches."""
    spec = hr["spec"]["chart"]["spec"]
    name, ns = hr["metadata"]["name"], hr["metadata"].get("namespace", "default")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        values = hr["spec"].get("values", {}) or {}
        for vf in hr["spec"].get("valuesFrom", []):  # a Secret read at install time: a placeholder is enough to render
            if vf.get("targetPath"):
                node = values
                *head, last = vf["targetPath"].split(".")
                for k in head:
                    node = node.setdefault(k, {})
                node.setdefault(last, "placeholder")
        (tmp / "values.yaml").write_text(yaml.safe_dump(values))
        cmd = ["helm", "template", hr["spec"].get("releaseName", name), spec["chart"], "--repo", repos[spec["sourceRef"]["name"]],
               "--version", str(spec["version"]), "-n", ns, "-f", str(tmp / "values.yaml"), "--include-crds=false"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            raise SystemExit(f"helm template {name}: {r.stderr.strip()[-300:]}")
        docs = load_docs(r.stdout)
        patches = [p for pr in hr["spec"].get("postRenderers", []) for p in pr.get("kustomize", {}).get("patches", [])]
        if patches:
            (tmp / "all.yaml").write_text(yaml.safe_dump_all(docs))
            (tmp / "kustomization.yaml").write_text(yaml.safe_dump({"resources": ["all.yaml"], "patches": patches}))
            docs = kustomize(tmp)
    for d in docs:
        d.setdefault("metadata", {}).setdefault("namespace", ns)
    return docs


def render_platform(bricks_paths):
    """Every object of every brick path, HelmReleases expanded. Returns (objects, helm_failed)."""
    objs = []
    for p in bricks_paths:
        objs.extend(kustomize(REPO / p.lstrip("./")))
    repos = {d["metadata"]["name"]: d["spec"]["url"] for d in objs if d.get("kind") == "HelmRepository"}
    seen, extra = set(), []
    for d in objs:
        if d.get("kind") == "HelmRelease" and (d["metadata"].get("namespace"), d["metadata"]["name"]) not in seen:
            seen.add((d["metadata"].get("namespace"), d["metadata"]["name"]))
            extra.extend(helm_release(d, repos))
    return objs + extra
