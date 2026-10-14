#!/usr/bin/env bash
# Static check of the OpenBao bootstrap Jobs (#34): every Job with a deadline has a ttl (a Failed Job without one is never
# re-created), the configure Job's deadline covers the sum of its wait loops, the ttl is in (0, 7200] and above the alert's `for`
# (the docs promise a re-run about 70 minutes later), and the alert names both Jobs.
# Usage: check-bootstrap-jobs.sh [REPO_ROOT]   (the self-test in --selftest mutates a copy and requires a failure)
set -uo pipefail
ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
if [ "${1:-}" = "--selftest" ]; then
  me="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "$0")"; src="$(cd "$(dirname "$me")/../.." && pwd)"
  t="$(mktemp -d)"; trap 'rm -rf "$t"' EXIT; rc=0
  mut() { rm -rf "$t/r"; mkdir -p "$t/r"; cp -r "$src/apps" "$src/scripts" "$t/r/"; }
  check() { if "$me" "$t/r" >/dev/null 2>&1; then echo "selftest: $1 NOT detected"; rc=1; else echo "selftest: $1 detected"; fi; }
  mut; sed -i '/ttlSecondsAfterFinished/d' "$t/r/apps/base/foundation/vault/bootstrap-roles-job.yaml"; check "missing ttl"
  mut; sed -i 's/activeDeadlineSeconds: 3600/activeDeadlineSeconds: 1800/' "$t/r/apps/base/foundation/vault/bootstrap-roles-job.yaml"; check "short deadline"
  mut; sed -i 's/openbao-init|//' "$t/r/apps/base/observability/vm-customresources/vmrule-bootstrap.yaml"; check "alert without openbao-init"
  mut; sed -i 's/ttlSecondsAfterFinished: 3600/ttlSecondsAfterFinished: 999999/' "$t/r/apps/base/foundation/vault/bootstrap-roles-job.yaml"; check "ttl above 2 hours"
  mut; sed -i 's/ttlSecondsAfterFinished: 3600/ttlSecondsAfterFinished: 300/' "$t/r/apps/base/foundation/vault/bootstrap-roles-job.yaml"; check "ttl below the alert's for"
  exit $rc
fi
command -v kubectl >/dev/null || { echo "kubectl is missing" >&2; exit 2; }
V="$ROOT/apps/base/foundation/vault"
R="$(mktemp)"; trap 'rm -f "$R"' EXIT
kubectl kustomize "$V" > "$R" || { echo "kustomize failed" >&2; exit 1; }
python3 -I - "$V" "$R" "$ROOT/apps/base/observability/vm-customresources/vmrule-bootstrap.yaml" <<'PY'
import re, sys, yaml
v, rendered, rule = sys.argv[1:4]
bad = []
def loop(path, fn):  # seconds spent by `seq 1 N` + `sleep S` inside function fn
    m = re.search(fn + r"\(\)[^\n]*\{.*?\n\}", open(path).read(), re.S)
    n, s = re.search(r"seq 1 (\d+)", m.group(0)), re.search(r"sleep (\d+)", m.group(0))
    return int(n.group(1)) * int(s.group(1))
waits = loop(v + "/scripts/lib.sh", "wait_api") + loop(v + "/scripts/configure.sh", "login_bootstrap")
jobs = {d["metadata"]["name"]: d["spec"] for d in yaml.safe_load_all(open(rendered)) if d and d["kind"] == "Job"}
if set(jobs) != {"openbao-init", "openbao-vault-bootstrap"}:
    bad.append("unexpected Jobs: %s" % sorted(jobs))
for n, sp in jobs.items():
    if "activeDeadlineSeconds" in sp and "ttlSecondsAfterFinished" not in sp:
        bad.append("%s has a deadline and no ttlSecondsAfterFinished" % n)
if jobs.get("openbao-vault-bootstrap", {}).get("activeDeadlineSeconds", 0) <= waits:
    bad.append("the configure Job deadline is not above its waits (%d s)" % waits)
rules = [r for g in yaml.safe_load(open(rule))["spec"]["groups"] for r in g["rules"]]
exprs = [r["expr"] for r in rules]
def secs(d):
    m = re.fullmatch(r"(\d+)([smh])", d)
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)] if m else 0
fors = [secs(str(r.get("for", ""))) for r in rules]
for n, sp in jobs.items():
    ttl = sp.get("ttlSecondsAfterFinished", 0)
    if not 0 < ttl <= 7200:
        bad.append("%s ttlSecondsAfterFinished %s is not in (0, 7200]" % (n, ttl))
    if fors and ttl <= max(fors):
        bad.append("%s ttl %s is not above the alert's for (%d s): the Job would vanish before the alert fires" % (n, ttl, max(fors)))
expr = " ".join(exprs)
for n in ("openbao-init", "openbao-vault-bootstrap"):
    if n not in expr:
        bad.append("the alert expression does not name %s" % n)
if 'condition="true"' not in expr or "kube_job_failed" not in expr:
    bad.append("the alert must use kube_job_failed{condition=\"true\"}")
for b in bad:
    print("FAIL:", b)
print("%d Jobs, waits %d s: %s" % (len(jobs), waits, "FAIL" if bad else "ok"))
sys.exit(1 if bad else 0)
PY
