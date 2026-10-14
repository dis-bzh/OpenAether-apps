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

# Comments claiming a Job is recreated on every sync were wrong (a finished Job is reported unchanged).
if grep -rniE 'recreated[^.]{0,30}on every|(deleted|recreated)[^.]{0,30}every (flux )?sync' \
     "$root/apps/base/foundation/vault" "$root/apps/flux/base/11-foundation-vault.yaml" >/dev/null; then
  echo "FAIL a Job comment still says it is recreated on every sync:"
  grep -rniE 'recreated[^.]{0,30}on every|(deleted|recreated)[^.]{0,30}every (flux )?sync' \
    "$root/apps/base/foundation/vault" "$root/apps/flux/base/11-foundation-vault.yaml"
  exit 1
fi
echo "ok: no Job comment claims a recreation on every sync"
