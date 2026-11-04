#!/usr/bin/env bash
# Two checks on the management profile's Kustomizations (REPO_ROOT overrides the tree):
#  1. the ones with neither `wait` nor `healthChecks` must match the list kept in apps/flux/README.md (#50): "Ready" on
#     those means "applied", and the doc must say so;
#  2. what a gate is worth (lab 32, N1): no `wait: true` next to `healthChecks` (the controller ignores them), and `cnpg`
#     carries exactly the healthCheckExprs entry that holds it until both platform Clusters are Ready, with timeout >= 15m.
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

# What a gate is worth (lab 32, N1): the why is in apps/flux/README.md ("The cnpg gate"). The pin below is the exact string,
# not a pattern: a pattern let `... || true`, `!has(...) && ...`, `has(...) || exists(...)` and an added `failed: "true"` through.
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
    CANON = "has(status.conditions) && status.conditions.exists(c, c.type == 'Ready' && c.status == 'True')"
    exprs = [e for e in cnpg.get("healthCheckExprs") or []
             if str(e.get("apiVersion", "")).split("/")[0] == "postgresql.cnpg.io" and e.get("kind") == "Cluster"]
    if not cnpg.get("wait"):
        errors.append("cnpg has no wait: true")
    if len(exprs) != 1:
        errors.append(f"cnpg needs exactly one healthCheckExprs entry for postgresql.cnpg.io Cluster, found {len(exprs)}")
    else:
        e = exprs[0]
        if e.get("apiVersion") != "postgresql.cnpg.io/v1":
            errors.append(f"cnpg Cluster entry apiVersion must be postgresql.cnpg.io/v1: {e.get('apiVersion')!r}")
        if e.get("current") != CANON:
            errors.append(f"cnpg Cluster 'current' must be exactly {CANON!r} (Ready condition True, nothing else): {e.get('current')!r}")
        extra = sorted(set(e) - {"apiVersion", "kind", "current"})
        if extra:
            errors.append(f"cnpg Cluster entry must set only apiVersion, kind and current (an inProgress or failed would change the verdict): {extra}")
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", str(cnpg.get("timeout", "")))
    secs = (int(m[1] or 0) * 3600 + int(m[2] or 0) * 60 + int(m[3] or 0)) if m else 0
    if secs < 900:
        # The figures are in apps/flux/README.md ("The cnpg gate"): under them a slow first install is a failed retry loop.
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
