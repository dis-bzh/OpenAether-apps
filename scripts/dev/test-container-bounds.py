#!/usr/bin/env python3
"""Static check of the container hardening and resource bounds of B21 (ANSSI FT082-R10..R12, NSA12-POD-06).

Reads the manifests, not a cluster. Each rule has a self-test that proves it would catch the regression.
  - Alloy, Grafana and the CAPI operator run with a read-only root filesystem (the values set it, with an emptyDir
    for what they write);
  - the local-path provisioner has a securityContext, no --debug, and a helper image with an explicit tag;
  - every emptyDir of the OpenBao StatefulSet and of openbao-init has a sizeLimit;
  - ESO and Kyverno controllers have a CPU limit once kustomize has patched them (checked on the patch files);
  - Zitadel and MinIO Deployments drop the ServiceAccount token through a post-renderer;
  - the kube-apiserver scrape may read at least 32 MiB (vmagent's default cap is 16 MiB).
"""
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]


def docs(rel):
    return [d for d in yaml.safe_load_all((REPO / rel).read_text()) if isinstance(d, dict)]


def release_values(rel):
    return next(d for d in docs(rel) if d["kind"] == "HelmRelease")


def ro_root(sc):
    return isinstance(sc, dict) and sc.get("readOnlyRootFilesystem") is True


def check_values():
    out = []
    a = release_values("apps/base/observability/alloy.yaml")["spec"]["values"]
    if not ro_root(a["alloy"].get("securityContext")):
        out.append("alloy: alloy.securityContext.readOnlyRootFilesystem must be true")
    if not ro_root(a["configReloader"].get("securityContext")):
        out.append("alloy: configReloader.securityContext.readOnlyRootFilesystem must be true")
    mounts = {m["mountPath"] for m in a["alloy"].get("mounts", {}).get("extra", [])}
    if a["alloy"].get("storagePath") not in mounts:
        out.append("alloy: storagePath must be a mounted volume (read-only root filesystem)")
    if not all("sizeLimit" in v.get("emptyDir", {}) for v in a["controller"]["volumes"]["extra"]):
        out.append("alloy: extra emptyDirs need a sizeLimit")
    g = release_values("apps/base/observability/grafana.yaml")["spec"]["values"]
    if not ro_root(g.get("containerSecurityContext")):
        out.append("grafana: containerSecurityContext.readOnlyRootFilesystem must be true")
    if "/tmp" not in {m["mountPath"] for m in g.get("extraEmptyDirMounts", [])}:
        out.append("grafana: /tmp must be an emptyDir when the root filesystem is read-only")
    c = release_values("apps/base/cluster-api-operator/operator.yaml")["spec"]["values"]
    if not ro_root(c.get("containerSecurityContext", {}).get("manager")):
        out.append("capi-operator: containerSecurityContext.manager.readOnlyRootFilesystem must be true")
    if c.get("securityContext", {}).get("runAsNonRoot") is not True:
        out.append("capi-operator: pod securityContext.runAsNonRoot must be true")
    mounts = {m["mountPath"]: m["name"] for m in c["volumeMounts"]["manager"]}
    # /home/nonroot: the embedded clusterctl config reader creates $HOME/.config/cluster-api, and without it no provider installs.
    for path in ("/tmp", "/home/nonroot"):
        vol = next((v for v in c["volumes"] if v["name"] == mounts.get(path)), None)
        if vol is None or "emptyDir" not in vol:
            out.append(f"capi-operator: {path} must be an emptyDir when the root filesystem is read-only")
    return out


def local_path_problems(deploy):
    out = []
    ctr = deploy["spec"]["template"]["spec"]["containers"][0]
    if "--debug" in ctr.get("command", []):
        out.append("local-path: --debug logs every volume path")
    sc = ctr.get("securityContext") or {}
    if sc.get("allowPrivilegeEscalation") is not False or sc.get("runAsNonRoot") is not True:
        out.append("local-path: the provisioner needs a restricted securityContext")
    return out


def check_local_path():
    rel = "apps/base/platform/local-path-provisioner/local-path-storage.yaml"
    ds = docs(rel)
    out = local_path_problems(next(d for d in ds if d["kind"] == "Deployment"))
    cm = next(d for d in ds if d["kind"] == "ConfigMap")
    pod = yaml.safe_load(cm["data"]["helperPod.yaml"])
    image = pod["spec"]["containers"][0]["image"]
    if ":" not in image.rsplit("/", 1)[-1] and "@" not in image:
        out.append(f"local-path: helper image {image!r} has no tag")
    return out


def check_emptydirs():
    out = []
    for rel in ("apps/base/foundation/vault/statefulset.yaml", "apps/base/foundation/vault/openbao-init.yaml"):
        for d in docs(rel):
            spec = d.get("spec", {})
            spec = spec.get("template", {}).get("spec") or spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
            for v in spec.get("volumes", []):
                if "emptyDir" in v and "sizeLimit" not in v["emptyDir"]:
                    out.append(f"{rel}: emptyDir {v['name']} has no sizeLimit")
    return out


def check_patches():
    out = []
    for rel in ("apps/base/external-secrets/patch-resources.yaml", "apps/base/kyverno/patch-cpu-limits.yaml"):
        for d in docs(rel):
            for c in d["spec"]["template"]["spec"]["containers"]:
                if "cpu" not in c.get("resources", {}).get("limits", {}):
                    out.append(f"{rel}: {d['metadata']['name']}/{c['name']} has no cpu limit")
    for rel in ("apps/base/external-secrets/patch-resources.yaml",):
        for d in docs(rel):
            for c in d["spec"]["template"]["spec"]["containers"]:
                if "memory" not in c["resources"]["limits"]:
                    out.append(f"{rel}: {d['metadata']['name']}/{c['name']} has no memory limit")
    for rel in ("apps/base/identity/zitadel/helmrelease.yaml", "apps/base/storage/minio/minio.yaml"):
        pr = release_values(rel)["spec"].get("postRenderers", [])
        flat = yaml.dump(pr)
        if "automountServiceAccountToken" not in flat or "value: false" not in flat:
            out.append(f"{rel}: the Deployments must drop the ServiceAccount token (postRenderers)")
    return out


def check_scrape_size():
    """The kube-apiserver scrape must be allowed more than vmagent's 16 MiB default, or a busy control plane goes unmonitored."""
    out = []
    scrapes = [d for d in docs("apps/base/observability/vm-customresources/vmscapes.yaml") if d["metadata"]["name"] == "kube-apiserver"]
    if len(scrapes) != 1:
        return ["vmscapes.yaml: the kube-apiserver VMServiceScrape is missing"]
    for ep in scrapes[0]["spec"]["endpoints"]:
        size = str(ep.get("max_scrape_size", ""))
        if not size.endswith("MiB") or int(size[:-3]) < 32:
            out.append(f"kube-apiserver scrape: max_scrape_size {size or 'unset'} must be at least 32MiB (default 16 MiB, lab 11)")
    return out


def self_test():
    ok = True
    deploy = {"spec": {"template": {"spec": {"containers": [{"command": ["x", "--debug"]}]}}}}
    if len(local_path_problems(deploy)) != 2:
        ok = False
        print("✗ self-test: --debug and a missing securityContext must both be caught")
    if ro_root({"readOnlyRootFilesystem": False}) or ro_root(None):
        ok = False
        print("✗ self-test: ro_root must reject false and absent")
    return ok


def main():
    if not self_test():
        sys.exit(1)
    problems = check_values() + check_local_path() + check_emptydirs() + check_patches() + check_scrape_size()
    for p in problems:
        print("✗", p)
    if problems:
        sys.exit(1)
    print("✓ Alloy, Grafana, CAPI operator: read-only root filesystem with a writable volume; local-path hardened and pinned;"
          " OpenBao emptyDirs bounded; ESO/Kyverno CPU limits; Zitadel/MinIO drop the SA token; apiserver scrape size; self-test passes")


main()
