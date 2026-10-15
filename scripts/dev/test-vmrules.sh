#!/usr/bin/env bash
# Parse every VMRule of the observability brick with the real vmalert (-dryRun: PromQL/MetricsQL syntax, group and
# rule validity) and check the conventions the tooling relies on: unique alert names, a severity and a summary on
# each, alert rules only (OpenAether-infra's scripts/ops/check-alert-metrics.py reads `alert:` and would fail on a
# `record:`). It does NOT check that a metric exists: that needs a cluster (`task check-alerts`).
# Needs kubectl and python3 with PyYAML. vmalert comes from $VMALERT, else the pinned release is downloaded and
# its sha256 checked. Rung: mocked/static.
#   Usage: scripts/dev/test-vmrules.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VM_VERSION="v1.154.0"
VM_SHA256="0c6b01fba8ca3149a017ad7713ae65cd2222ba6a64f1e1f8787692cbb5d953c9"   # vmutils-linux-amd64 tarball
for bin in kubectl python3; do command -v "$bin" >/dev/null || { echo "$bin is required" >&2; exit 2; }; done
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

VMALERT="${VMALERT:-}"
if [[ -z "$VMALERT" ]]; then
  command -v curl >/dev/null || { echo "curl is required (or set VMALERT)" >&2; exit 2; }
  curl -fsSL -o "$WORK/vmutils.tgz" \
    "https://github.com/VictoriaMetrics/VictoriaMetrics/releases/download/$VM_VERSION/vmutils-linux-amd64-$VM_VERSION.tar.gz"
  echo "$VM_SHA256  $WORK/vmutils.tgz" | sha256sum -c - >/dev/null || { echo "vmutils checksum mismatch" >&2; exit 2; }
  tar -xzf "$WORK/vmutils.tgz" -C "$WORK" vmalert-prod
  VMALERT="$WORK/vmalert-prod"
fi

kubectl kustomize "$ROOT/apps/base/observability/vm-customresources" > "$WORK/built.yaml"
python3 -I - "$WORK" <<'PY'
import sys, yaml
work = sys.argv[1]
groups, fails, seen = [], 0, {}
def check(label, ok):
    global fails
    print(("ok   " if ok else "FAIL ") + label)
    fails += 0 if ok else 1
for d in yaml.safe_load_all(open(f"{work}/built.yaml")):
    if not d or d.get("kind") != "VMRule":
        continue
    name = d["metadata"]["name"]
    for g in d["spec"]["groups"]:
        groups.append(g)
        for r in g["rules"]:
            a = r.get("alert")
            check(f"{name}: a rule of {g['name']} is an alert, not a recording rule", bool(a) and "record" not in r)
            if not a:
                continue
            check(f"{a}: unique alert name", a not in seen); seen[a] = name
            check(f"{a}: severity label in none/info/warning/critical",
                  (r.get("labels") or {}).get("severity") in ("none", "info", "warning", "critical"))
            check(f"{a}: summary annotation", bool((r.get("annotations") or {}).get("summary")))
check("at least 40 alert rules are loaded", len(seen) >= 40)
yaml.safe_dump({"groups": groups}, open(f"{work}/rules.yaml", "w"))
print(f"{fails} failed")
sys.exit(1 if fails else 0)
PY
"$VMALERT" -dryRun -rule="$WORK/rules.yaml" -datasource.url=http://127.0.0.1:1 >"$WORK/vmalert.out" 2>&1 \
  || { grep -E "fatal|invalid|error" "$WORK/vmalert.out" >&2; echo "vmalert -dryRun rejected the rules" >&2; exit 1; }
echo "ok   vmalert -dryRun accepts all $(grep -c 'alert:' "$WORK/rules.yaml") rules"
echo "0 failed"
