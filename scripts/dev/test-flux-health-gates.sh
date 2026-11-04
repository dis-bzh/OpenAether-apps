#!/usr/bin/env bash
# The Kustomizations of the management profile that have neither `wait` nor `healthChecks` must match the list kept in
# apps/flux/README.md (#50): "Ready" on those means "applied", and the doc must say so. REPO_ROOT overrides the tree.
set -euo pipefail
root="${REPO_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
rendered="$(kubectl kustomize "$root/apps/flux/management")"

RENDER="$rendered" README="$root/apps/flux/README.md" python3 -I - <<'PY'
import os, re, sys
import yaml

docs = [d for d in yaml.safe_load_all(os.environ["RENDER"])
        if d and d.get("kind") == "Kustomization" and d["apiVersion"].startswith("kustomize.toolkit.fluxcd.io")]
actual = {d["metadata"]["name"] for d in docs
          if not d["spec"].get("wait") and not d["spec"].get("healthChecks")}
text = open(os.environ["README"]).read()
m = re.search(r"<!-- health-gates:begin.*?-->\n(.*?)<!-- health-gates:end -->", text, re.S)
if not m:
    sys.exit("FAIL apps/flux/README.md has no health-gates:begin/end block")
listed = set(re.findall(r"^- (\S+)$", m.group(1), re.M))
if actual != listed:
    print(f"FAIL without a health gate, not listed in the README: {sorted(actual - listed)}")
    print(f"FAIL listed in the README but gated or gone: {sorted(listed - actual)}")
    sys.exit(1)
print(f"ok: {len(actual)} of {len(docs)} Kustomizations have no health gate, as apps/flux/README.md lists")
PY

# What a gate is worth (lab 32, finding N1). Two facts read in kustomize-controller v1.9.6 and v1.10.1:
#  - with `wait: true` the controller IGNORES `spec.healthChecks` (checkHealth reads them only when wait is false), so an entry
#    there next to `wait: true` gates nothing and reads as if it did;
#  - kstatus calls a custom resource that has no Ready condition "Current". A CNPG Cluster gets its Ready condition with its
#    first phase, so a Cluster the operator has not yet touched passes `wait: true`: `cnpg` was Ready after 47 s with both
#    platform databases still empty and released its dependents 9 minutes early. `healthCheckExprs` replaces the verdict for
#    that kind. `current` must be an `exists` over the conditions: the usual `filter(...).all(...)` is true on a list with
#    no Ready condition, which is the very hole.
RENDER="$rendered" python3 -I - <<'PY'
import os, re, sys
import yaml

docs = [d for d in yaml.safe_load_all(os.environ["RENDER"])
        if d and d.get("kind") == "Kustomization" and d["apiVersion"].startswith("kustomize.toolkit.fluxcd.io")]
by_name = {d["metadata"]["name"]: d["spec"] for d in docs}
errors = []

dead = sorted(n for n, s in by_name.items() if s.get("wait") and s.get("healthChecks"))
if dead:
    errors.append(f"wait: true and healthChecks together (the controller ignores healthChecks): {dead}")

cnpg = by_name.get("cnpg")
if cnpg is None:
    errors.append("no Kustomization named cnpg in the management render")
else:
    exprs = [e for e in cnpg.get("healthCheckExprs") or []
             if str(e.get("apiVersion", "")).split("/")[0] == "postgresql.cnpg.io" and e.get("kind") == "Cluster"]
    cur = exprs[0].get("current", "") if len(exprs) == 1 else ""
    if not cnpg.get("wait"):
        errors.append("cnpg has no wait: true")
    if len(exprs) != 1:
        errors.append(f"cnpg needs exactly one healthCheckExprs entry for postgresql.cnpg.io Cluster, found {len(exprs)}")
    elif not (".exists(" in cur and re.search(r"['\"]Ready['\"]", cur) and re.search(r"['\"]True['\"]", cur)
              and ".all(" not in cur):
        errors.append(f"cnpg Cluster 'current' must be an exists() on the Ready condition being True, not all(): {cur!r}")
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", str(cnpg.get("timeout", "")))
    secs = (int(m[1] or 0) * 3600 + int(m[2] or 0) * 60 + int(m[3] or 0)) if m else 0
    if secs < 900:
        # The gate now spans the databases: 5:52-6:16 (Scaleway) and 7:19 (OVH) after the Kustomization starts, 10:02 when
        # its dependents competed for the nodes. A timeout under that turns a slow first install into a failed retry loop.
        errors.append(f"cnpg timeout {cnpg.get('timeout')!r} is under 15m: it now covers both databases coming up")
if errors:
    print("\n".join("FAIL " + e for e in errors))
    sys.exit(1)
print("ok: no wait+healthChecks pair; cnpg gates both Clusters on their Ready condition (timeout >= 15m)")
PY

# Comments claiming a Job is recreated on every sync were wrong (a finished Job is reported unchanged).
if grep -rniE 'recreated[^.]{0,30}on every|(deleted|recreated)[^.]{0,30}every (flux )?sync' \
     "$root/apps/base/foundation/vault" "$root/apps/flux/base/11-foundation-vault.yaml" >/dev/null; then
  echo "FAIL a Job comment still says it is recreated on every sync:"
  grep -rniE 'recreated[^.]{0,30}on every|(deleted|recreated)[^.]{0,30}every (flux )?sync' \
    "$root/apps/base/foundation/vault" "$root/apps/flux/base/11-foundation-vault.yaml"
  exit 1
fi
echo "ok: no Job comment claims a recreation on every sync"
