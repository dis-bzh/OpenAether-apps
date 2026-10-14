#!/usr/bin/env bash
# Static checks of the local Docker lane (no cluster, no Docker): the set it deploys is closed under dependsOn, the Git source it
# applies carries a commit and no unsubstituted ${GIT_REF}, mutated sets are refused, and the workflow keeps the properties the
# lane relies on (pinned actions, minimal permissions, a timeout, an always-run teardown, a paths filter, no secret-bearing
# trigger). Needs kubectl and PyYAML. What it can not tell: whether the lane works on a runner.
# ok/bad always return 0, so "A && ok || bad" is a true if-then-else here.
# shellcheck disable=SC2015
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LANE="$ROOT/scripts/dev/local-docker-lane.sh"
WF="$ROOT/.github/workflows/local-docker.yml"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
FAIL=0
ok()  { printf '  ok   %s\n' "$*"; }
bad() { printf '  FAIL %s\n' "$*"; FAIL=$((FAIL + 1)); }

plan() { LANE_OUT="$T/out" LANE_COMMIT=0123456789abcdef0123456789abcdef01234567 LANE_BRANCH=test-branch LANE_SET="$1" "$LANE" plan 2>&1; }

default_out="$(plan "")" && ok "default set plans" || bad "default set does not plan: $default_out"
grep -q 'commit: 0123456789abcdef' "$T/out/flux-objects.yaml" && ok "GitRepository pinned to a commit" || bad "no commit in the GitRepository"
grep -q 'GIT_REF' "$T/out/flux-objects.yaml" && bad "unsubstituted GIT_REF reaches the cluster" || ok "no GIT_REF placeholder applied"

# Mutations: each must be refused.
refuse() {
  if plan "$2" >"$T/m.out"; then bad "$1: accepted"; else ok "$1: refused ($(grep -m1 -- '- ' "$T/m.out" | sed 's/^ *- //' | cut -c1-70))"; fi
}
refuse "OpenBao without cert-manager" "namespaces platform-priority-classes platform-gateway-api platform-local-path-provisioner foundation-vault"
refuse "unknown brick" "namespaces no-such-brick"
# A copy of the DAG where the first brick substitutes from a ConfigMap the lane never creates.
mkdir -p "$T/mut/apps"; cp -r "$ROOT/apps/flux" "$T/mut/apps/flux"; cp -r "$ROOT/apps/base" "$T/mut/apps/base" 2>/dev/null
sed -i '0,/prune: true/s//prune: true\n  postBuild:\n    substituteFrom:\n      - { kind: ConfigMap, name: cluster-identity }/' "$T/mut/apps/flux/base/00-namespaces.yaml"
if LANE_ROOT="$T/mut" LANE_OUT="$T/mout" LANE_SET="namespaces" "$LANE" plan >"$T/m.out" 2>&1; then
  bad "substituteFrom on a selected brick: accepted"; else ok "substituteFrom on a selected brick: refused"; fi

# The leak scan must catch what it claims to catch (positive controls), and keep a clean log. A stub kubectl: no Secret to read.
mkdir -p "$T/bin" "$T/scan/logs"; printf '#!/bin/sh\nexit 1\n' >"$T/bin/kubectl"; chmod +x "$T/bin/kubectl"
printf 'starting\nall good\nreconciled in 3s\n' >"$T/scan/logs/clean.log"
printf 'token s.%s here\n' "$(printf 'A1b2C3d4E5f6G7h8I9j0K1l2')" >"$T/scan/logs/openbao-service.log"
printf '{"client_token":"b.%s"}\n' "$(printf 'A1b2C3d4E5f6G7h8I9j0K1l2')" >"$T/scan/logs/openbao-batch.log"
printf 'hvs.%s\n' "$(printf 'A1b2C3d4E5f6G7h8I9j0K1l2M3n4')" >"$T/scan/logs/vault.log"
# The PEM marker is assembled here so that no key header sits in this file (secret scanners flag it).
printf -- '-----BEGIN %s KEY-----\nabc\n' "OPENSSH PRIVATE" >"$T/scan/logs/ssh.log"
printf -- '-----BEGIN %s KEY-----\nabc\n' "ENCRYPTED PRIVATE" >"$T/scan/logs/pkcs8.log"
PATH="$T/bin:$PATH" LANE_OUT="$T/scan" "$LANE" scan >"$T/scan.out" 2>&1
[ "$(grep -c . "$T/scan/leak-scan.txt")" = 6 ] && grep -qx 'hits=5' "$T/scan/leak-scan.txt" \
  && ok "leak scan: OpenBao service/batch tokens, a Vault token and two PEM private keys are caught" || bad "leak scan misses a planted secret: $(tr '\n' ' ' <"$T/scan/leak-scan.txt")"
[ -e "$T/scan/logs/clean.log" ] && [ ! -e "$T/scan/logs/openbao-service.log" ] && ok "leak scan: the clean log is kept, the leaking ones are deleted" || bad "leak scan kept a leaking log or dropped a clean one"

# The workflow.
python3 -I - "$WF" <<'PY' || FAIL=$((FAIL + 1))
import re, sys
import yaml
wf = yaml.safe_load(open(sys.argv[1])); text = open(sys.argv[1]).read()
on = wf.get(True) or wf.get("on")
errs = []
if "workflow_dispatch" not in on: errs.append("no workflow_dispatch")
paths = (on.get("pull_request") or {}).get("paths") or []
if ".github/workflows/local-docker.yml" not in paths or "scripts/dev/local-docker-lane.sh" not in paths:
    errs.append("pull_request paths filter must list the workflow and the script")
if "pull_request_target" in on: errs.append("pull_request_target is forbidden")
if wf.get("permissions") != {"contents": "read"}: errs.append("top-level permissions must be exactly contents: read")
if not wf.get("concurrency"): errs.append("no concurrency group")
job = wf["jobs"]["lane"]
if not job.get("timeout-minutes"): errs.append("no job timeout")
if "permissions" in job and job["permissions"] != {"contents": "read"}: errs.append("job widens permissions")
for u in re.findall(r"uses:\s*(\S+)", text):
    if not re.search(r"@[0-9a-f]{40}$", u): errs.append(f"action not pinned to a sha: {u}")
steps = job["steps"]
down = [s for s in steps if "local-down" in s.get("run", "")]
if not down or str(down[-1].get("if", "")).strip() != "always()" or steps[-1] is not down[-1]:
    errs.append("the last step must be the teardown, with if: always()")
col = [s for s in steps if s.get("run", "").count("local-docker-lane.sh") and " collect" in s["run"]]
if not col or str(col[0].get("if", "")).strip() != "always()": errs.append("collect must run if: always()")
runs = "\n".join(s.get("run", "") for s in steps)
if "inputs." in runs: errs.append("an input is interpolated into a run script")
if re.search(r"github\.(head_ref|event\.)", runs): errs.append("an event-controlled value is interpolated into a run script")
if "secrets." in text: errs.append("the lane must not use secrets.* (it runs on pull requests)")
order = [i for k in ("local-docker-lane.sh wait", "local-docker-lane.sh collect", "local-docker-lane.sh assert")
         for i, s in enumerate(steps) if k in s.get("run", "")]
if len(order) != 3 or order != sorted(order): errs.append("the wait, collect and assert steps must all exist, in that order (assert reads the leak-scan result)")
print("workflow:", "FAIL " + "; ".join(errs) if errs else "ok")
sys.exit(1 if errs else 0)
PY

# The false promise must stay gone.
if grep -rn 'drives the whole order' "$ROOT/README.md" "$ROOT/README.fr.md" "$ROOT/apps/flux/local" >/dev/null; then
  bad "'task local-test drives the whole order' is back"; else ok "no 'drives the whole order' claim"; fi
command -v shellcheck >/dev/null && { shellcheck "$LANE" "${BASH_SOURCE[0]}" && ok "shellcheck" || bad "shellcheck"; }

[ "$FAIL" -eq 0 ] && echo "0 failed" || echo "$FAIL failed"
[ "$FAIL" -eq 0 ]
