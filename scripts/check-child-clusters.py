#!/usr/bin/env python3
"""check-child-clusters.py - static checks for the CAPI child clusters of apps/clusters.

Covers every apps/clusters/*.yaml and *.yaml.example (the .example is invisible
to yamllint and to the kustomize build, which is how a Flux apiVersion that does
not exist survived in the on-ramp example). Rules:

  1. every Flux object uses an apiVersion the vendored Flux CRDs
     (flux-gotk/gotk-components.yaml) actually serve;
  2. no (kind, namespace, name) twice, across the shared helmrepository-*.yaml
     and the file under test, whatever the apiVersion;
  3. no unsubstituted ${...} in a metadata.name or a kubeConfig secret name: the
     capi-clusters Kustomization that reconciles these files has no postBuild;
  4. a child-gitops Kustomization sets CHILD_NAME, plus the CHILD_BACKUP_* its
     profile needs (cnpg: bucket + endpoint, storage-backup-target: target URL),
     the profile being resolved through pick.py's DAG;
  5. no public IPv4 literal, no UUID, and backup values that are placeholders
     (this repository is public);
  6. an *.example child keeps the Cilium values of edge-1.yaml (the same
     provider, proven end to end): the infra parity check only globs *.yaml.

Static only: it proves the files are coherent, not that a child comes up.

Usage: check-child-clusters.py [clusters-dir [flux-dir]]   (default apps/clusters, apps/flux)
"""

import ipaddress
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML missing: apt install python3-yaml (or pip install pyyaml)")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pick  # noqa: E402  (reuses the DAG loader instead of re-parsing profiles)

REPO = Path(__file__).resolve().parent.parent
GOTK = REPO / "apps/base/cluster-api-clusters/flux-gotk/gotk-components.yaml"
FLUX_GROUPS = ("source.toolkit.fluxcd.io", "kustomize.toolkit.fluxcd.io",
               "helm.toolkit.fluxcd.io", "notification.toolkit.fluxcd.io")
REFERENCE_CHILD = "edge-1.yaml"
ID = lambda d: (d.get("kind"), (d.get("metadata") or {}).get("namespace", ""),  # noqa: E731
                (d.get("metadata") or {}).get("name"))
IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.]|\.\d)")
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)


def served_versions():
    """{(group, kind): {served versions}} from the vendored Flux CRDs."""
    out = {}
    for d in yaml.safe_load_all(GOTK.read_text()):
        if not isinstance(d, dict) or d.get("kind") != "CustomResourceDefinition":
            continue
        spec = d["spec"]
        out[(spec["group"], spec["names"]["kind"])] = {
            v["name"] for v in spec["versions"] if v.get("served")}
    return out


def docs(path):
    return [d for d in yaml.safe_load_all(path.read_text()) if isinstance(d, dict)]


def child_files(cdir):
    return sorted(p for p in cdir.iterdir()
                  if p.name.endswith((".yaml", ".yaml.example")) and p.name != "kustomization.yaml")


def profile_bricks(profile, bricks, cat, flux_dir):
    """Bricks a profile ends up with: its '# Pick:' line, closed over dependsOn."""
    pf = flux_dir / profile / "kustomization.yaml"
    if not pf.is_file():
        return None
    m = pick.PICK_RE.search(pf.read_text())
    if not m:
        return None
    names = pick.resolve_names(m.group(1).split(), bricks, cat.get("aliases") or {})
    selected, _ = pick.pick(names, bricks, cat)
    return selected


def check_file(path, shared, served, bricks, cat, flux_dir, errs):
    ds = docs(path)
    rel = path.name

    def err(msg):
        errs.append(f"{rel}: {msg}")

    # 1. served apiVersions
    for d in ds:
        api, kind = str(d.get("apiVersion", "")), d.get("kind")
        group, _, ver = api.partition("/")
        if group.endswith(FLUX_GROUPS) or group in FLUX_GROUPS:
            ok = served.get((group, kind))
            if ok is None:
                err(f"{kind} {group} is not in the vendored Flux CRDs")
            elif ver not in ok:
                err(f"{kind} uses {api}, the vendored Flux CRDs serve {sorted(ok)}")

    # 2. duplicates, shared helmrepositories included
    seen = {ID(d): "shared" for d in shared}
    for d in ds:
        if ID(d) in seen:
            err(f"{ID(d)[0]} {ID(d)[1]}/{ID(d)[2]} is already defined"
                + (" by a shared helmrepository-*.yaml" if seen[ID(d)] == "shared" else " in this file"))
        seen[ID(d)] = "self"

    # 3. unsubstituted names
    for d in ds:
        names = [(d.get("metadata") or {}).get("name", "")]
        names.append((((d.get("spec") or {}).get("kubeConfig") or {}).get("secretRef") or {}).get("name", ""))
        for n in names:
            if "${" in str(n):
                err(f"{d.get('kind')} name '{n}' keeps a ${{...}}: nothing substitutes it here")

    # 4. child identity and backup variables
    for d in ds:
        spec = d.get("spec") or {}
        if d.get("kind") != "Kustomization" or not str(spec.get("path", "")).endswith("child-gitops"):
            continue
        name = d["metadata"]["name"]
        sub = (spec.get("postBuild") or {}).get("substitute") or {}
        need = ["CHILD_NAME"]
        profile = sub.get("CHILD_PROFILE", "workload")  # child-gitops default
        picked = profile_bricks(profile, bricks, cat, flux_dir)
        if picked is None:
            err(f"{name}: CHILD_PROFILE '{profile}' has no {profile}/kustomization.yaml with a '# Pick:' line")
            picked = set()
        if "cnpg" in picked:
            need += ["CHILD_BACKUP_S3_BUCKET", "CHILD_BACKUP_S3_ENDPOINT"]
        if "storage-backup-target" in picked:
            need += ["CHILD_BACKUP_TARGET_URL"]
        for v in need:
            if not str(sub.get(v, "")).strip():
                err(f"{name}: postBuild.substitute lacks {v} (profile '{profile}' needs it)")
        for v in ("CHILD_BACKUP_S3_BUCKET", "CHILD_BACKUP_TARGET_URL"):
            if sub.get(v) and "<" not in str(sub[v]):
                err(f"{name}: {v} is not a <placeholder>: this repository is public")

    # 5. public data
    text = path.read_text()
    for m in IPV4.finditer(text):
        try:
            ip = ipaddress.ip_address(m.group(1))
        except ValueError:
            continue
        if ip.is_global:
            err(f"public IPv4 literal {m.group(1)}")
    for m in UUID.finditer(text):
        err(f"UUID literal {m.group(0)} (project/account id?)")


def cilium_values(path):
    for d in docs(path):
        if d.get("kind") == "HelmRelease" and d["metadata"]["name"].endswith("-cilium"):
            return d["spec"].get("values"), d["spec"]["chart"]["spec"].get("version")
    return None, None


def main():
    cdir = Path(sys.argv[1] if len(sys.argv) > 1 else REPO / "apps/clusters").resolve()
    flux_dir = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else REPO / "apps/flux"
    served = served_versions()
    bricks = pick.load_dag()
    cat = pick.load_catalog(bricks)
    shared = [d for p in sorted(cdir.glob("helmrepository-*.yaml")) for d in docs(p)]
    errs = []
    files = child_files(cdir)
    for p in files:
        if not p.name.startswith("helmrepository-"):
            check_file(p, shared, served, bricks, cat, flux_dir, errs)
    # enabled children together: the same object twice is a kustomize error only
    # when the apiVersion also matches, so look at (kind, ns, name) alone.
    owner = {}
    for p in files:
        if p.name.endswith(".yaml.example"):
            continue
        for d in docs(p):
            if ID(d) in owner and owner[ID(d)] != p.name and d.get("kind") != "Namespace":
                errs.append(f"{p.name}: {ID(d)} also defined in {owner[ID(d)]}")
            owner.setdefault(ID(d), p.name)
    # 6. examples keep the proven Cilium values
    ref = cdir / REFERENCE_CHILD
    if ref.is_file():
        want = cilium_values(ref)
        for p in files:
            if p.name.endswith(".yaml.example") and cilium_values(p) != want:
                errs.append(f"{p.name}: Cilium values/version differ from {REFERENCE_CHILD} "
                            "(a Talos child needs KubePrism, cgroup and WireGuard settings)")
    if errs:
        print("\n".join(errs), file=sys.stderr)
        print(f"\nFAIL: {len(errs)} problem(s) in {cdir}", file=sys.stderr)
        return 1
    print(f"OK: {len(files)} child file(s) coherent in {cdir.name}/ (static, not run on a cluster)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
