#!/usr/bin/env bash
# Parse every VMRule of the observability brick (and of observability-openbao) with the real vmalert (-dryRun: PromQL/MetricsQL syntax, group and
# rule validity) and check the conventions the tooling relies on: unique alert names, a severity and a summary on
# each, alert rules only (OpenAether-infra's scripts/ops/check-alert-metrics.py reads `alert:` and would fail on a
# `record:`). It also runs scripts/dev/vmrules-unittest.yaml (synthetic series, vmalert-tool) for the alerts whose
# logic is more than a threshold. It does NOT check that a metric exists: that needs a cluster (`task check-alerts`).
# Needs kubectl and python3 with PyYAML. vmalert and vmalert-tool come from $VMALERT / $VMALERT_TOOL, else the pinned
# release is downloaded and its sha256 checked. Rung: mocked/static.
#   Usage: scripts/dev/test-vmrules.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VM_VERSION="v1.154.0"
VM_SHA256="0c6b01fba8ca3149a017ad7713ae65cd2222ba6a64f1e1f8787692cbb5d953c9"   # vmutils-linux-amd64 tarball
for bin in kubectl python3; do command -v "$bin" >/dev/null || { echo "$bin is required" >&2; exit 2; }; done
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

VMALERT="${VMALERT:-}"; VMALERT_TOOL="${VMALERT_TOOL:-}"
if [[ -z "$VMALERT" || -z "$VMALERT_TOOL" ]]; then
  command -v curl >/dev/null || { echo "curl is required (or set VMALERT)" >&2; exit 2; }
  curl -fsSL -o "$WORK/vmutils.tgz" \
    "https://github.com/VictoriaMetrics/VictoriaMetrics/releases/download/$VM_VERSION/vmutils-linux-amd64-$VM_VERSION.tar.gz"
  echo "$VM_SHA256  $WORK/vmutils.tgz" | sha256sum -c - >/dev/null || { echo "vmutils checksum mismatch" >&2; exit 2; }
  tar -xzf "$WORK/vmutils.tgz" -C "$WORK" vmalert-prod vmalert-tool-prod
  VMALERT="${VMALERT:-$WORK/vmalert-prod}"; VMALERT_TOOL="${VMALERT_TOOL:-$WORK/vmalert-tool-prod}"
fi

# The VMRules of the observability brick, and those of the companions that need a CRD or a namespace of their own.
{ kubectl kustomize "$ROOT/apps/base/observability/vm-customresources"; echo "---"
  kubectl kustomize "$ROOT/apps/base/observability-openbao"; echo "---"
  kubectl kustomize "$ROOT/apps/base/observability-alert-probe"; echo "---"
  kubectl kustomize "$ROOT/apps/base/observability-audit-sink"; } > "$WORK/built.yaml"
python3 -I - "$WORK" "$ROOT" <<'PY'
import os, re, sys, yaml
work, root = sys.argv[1:3]
RB_PREFIX = "https://github.com/dis-bzh/OpenAether-apps/blob/main/"
def anchors(path):  # GitHub heading slugs of a Markdown file
    out = set()
    for line in open(path, encoding="utf-8"):
        m = re.match(r"#{1,6}\s+(.*?)\s*#*\s*$", line)
        if m:
            out.add(re.sub(r"[^\w\- ]", "", m.group(1).lower().replace("`", "")).replace(" ", "-"))
    return out
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
            url = (r.get("annotations") or {}).get("runbook_url", "")
            path, _, frag = url.partition("#")
            ok = path.startswith(RB_PREFIX) and os.path.isfile(os.path.join(root, path[len(RB_PREFIX):]))
            ok = ok and (not frag or frag in anchors(os.path.join(root, path[len(RB_PREFIX):])))
            check(f"{a}: runbook_url names a file of this repo and a heading that exists", ok)
check("at least 40 alert rules are loaded", len(seen) >= 40)
yaml.safe_dump({"groups": groups}, open(f"{work}/rules.yaml", "w"))
print(f"{fails} failed")
sys.exit(1 if fails else 0)
PY
"$VMALERT" -dryRun -rule="$WORK/rules.yaml" -datasource.url=http://127.0.0.1:1 >"$WORK/vmalert.out" 2>&1 \
  || { grep -E "fatal|invalid|error" "$WORK/vmalert.out" >&2; echo "vmalert -dryRun rejected the rules" >&2; exit 1; }
echo "ok   vmalert -dryRun accepts all $(grep -c 'alert:' "$WORK/rules.yaml") rules"

# Behaviour of the rules that are more than a threshold: only the groups the test file names are loaded.
python3 -I - "$WORK" "$ROOT/scripts/dev/vmrules-unittest.yaml" <<'PY'
import sys, yaml
work, tests = sys.argv[1:3]
cases = [r for t in yaml.safe_load(open(tests))["tests"] for r in t["alert_rule_test"]]
wanted = {r["alertname"] for r in cases}
# An alert whose cases never state exp_annotations is tested on firing and labels only (annotations are checked
# statically above: summary and runbook_url); the harness compares annotations exactly, so they are dropped there.
checked = {r["alertname"] for r in cases if any("exp_annotations" in e for e in r.get("exp_alerts") or [])}
groups = []
for g in yaml.safe_load(open(f"{work}/rules.yaml"))["groups"]:
    g["rules"] = [r for r in g["rules"] if r.get("alert") in wanted]
    for r in g["rules"]:
        if r["alert"] not in checked:
            r.pop("annotations", None)
    if g["rules"]:
        groups.append(g)
if {r["alert"] for g in groups for r in g["rules"]} != wanted:
    sys.exit("vmrules-unittest.yaml names an alert that no VMRule defines")
yaml.safe_dump({"groups": groups}, open(f"{work}/rules-under-test.yaml", "w"))
PY
cp "$ROOT/scripts/dev/vmrules-unittest.yaml" "$WORK/vmrules-unittest.yaml"
( cd "$WORK" && "$VMALERT_TOOL" unittest --disableAlertgroupLabel --files vmrules-unittest.yaml >unittest.out 2>&1 ) \
  || { cat "$WORK/unittest.out" >&2; echo "vmalert-tool unittest failed" >&2; exit 1; }
echo "ok   vmalert-tool unittest: $(grep -c '^  - name:' "$ROOT/scripts/dev/vmrules-unittest.yaml") cases pass"
echo "0 failed"
