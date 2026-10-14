#!/usr/bin/env bash
# The "local Docker" rung for the apps: a Talos-in-Docker cluster (brought up by OpenAether-infra's own `task local-up`),
# Flux controllers, then a CONSERVATIVE subset of the bricks reconciled by Flux from GitHub at one commit.
# The workflow .github/workflows/local-docker.yml calls the subcommands in order; `plan` needs no cluster.
#
#   plan            render apps/flux/base, keep the selected Kustomizations, check the set is closed under dependsOn
#   flux            flux install + apply the GitRepository (pinned commit) and the selected Kustomizations
#   prepare-mode    prod mode only: create the ConfigMap of (throwaway) operator PGP keys the init Job looks for
#   wait            bounded waits: every selected Kustomization Ready, then the two OpenBao Jobs Complete
#   assert          read-only assertions (nodes, Cilium, Kustomizations, OpenBao end state, nothing secret in logs)
#   collect         get/describe/log dump into $LANE_OUT, then a leak scan that deletes any log file holding a secret
#
# What it proves and what it does not is written in docs/local-docker.md. It never touches a cloud.
# ok/bad always return 0, so "A && ok || bad" is a true if-then-else here.
# shellcheck disable=SC2015
set -uo pipefail

ROOT="${LANE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"  # LANE_ROOT: the tests mutate a copy
OUT="${LANE_OUT:-$ROOT/local-docker-out}"
# Bricks that can really run on Docker (see docs/local-docker.md). Closed under dependsOn: `plan` fails otherwise.
DEFAULT_SET="namespaces platform-priority-classes platform-gateway-api platform-local-path-provisioner \
external-secrets external-secrets-stores cert-manager vault-ca foundation-vault cert-manager-issuers kyverno kyverno-policies"
SET="${LANE_SET:-$DEFAULT_SET}"
MODE="${LANE_MODE:-dev}"
WAIT_TOTAL="${LANE_WAIT_SECONDS:-1500}"
VAULT_NS=foundation-vault

PASS=0; FAIL=0
ok()  { printf '  ok   %s\n' "$*"; PASS=$((PASS + 1)); }
bad() { printf '  FAIL %s\n' "$*"; FAIL=$((FAIL + 1)); }
die() { printf 'error: %s\n' "$*" >&2; exit 2; }

case "$MODE" in dev|prod) ;; *) die "LANE_MODE must be dev or prod (got '$MODE')" ;; esac

# --- plan ---------------------------------------------------------------------------------------------------------
cmd_plan() {
  command -v kubectl >/dev/null || die "kubectl is required"
  mkdir -p "$OUT"
  local rendered
  rendered="$(kubectl kustomize "$ROOT/apps/flux/base")" || die "kubectl kustomize apps/flux/base failed"
  RENDER="$rendered" SET="$SET" COMMIT="${LANE_COMMIT:-}" BRANCH="${LANE_BRANCH:-}" DEST="$OUT/flux-objects.yaml" \
    python3 -I - <<'PY' || exit 1
import os, sys
import yaml

want = os.environ["SET"].split()
docs = [d for d in yaml.safe_load_all(os.environ["RENDER"]) if d]
ks = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Kustomization"
      and d["apiVersion"].startswith("kustomize.toolkit.fluxcd.io")}
repo = [d for d in docs if d["kind"] == "GitRepository"]
errors = []
if len(repo) != 1:
    errors.append(f"expected exactly one GitRepository in apps/flux/base, found {len(repo)}")
for n in want:
    if n not in ks:
        errors.append(f"unknown Kustomization '{n}' (not in apps/flux/base)")
if not want:
    errors.append("empty set")
for n in want:
    if n not in ks:
        continue
    spec = ks[n]["spec"]
    for dep in spec.get("dependsOn", []):
        if dep["name"] not in want:
            errors.append(f"'{n}' dependsOn '{dep['name']}', which is not in the set (the set must be closed)")
    # cluster-identity is created by the cloud bootstrap manifest only; nothing here creates it.
    if spec.get("postBuild", {}).get("substituteFrom"):
        errors.append(f"'{n}' substitutes from a ConfigMap/Secret the lane does not create")
if errors:
    print("plan: FAIL")
    for e in errors:
        print("  -", e)
    sys.exit(1)

commit, branch = os.environ["COMMIT"], os.environ["BRANCH"]
g = repo[0]
# Flux does no substitution in what kubectl applies, so the literal ${GIT_REF:=...} must not reach the cluster.
g["spec"]["ref"] = {"branch": branch or "main"}
if commit:
    g["spec"]["ref"]["commit"] = commit
out = [g] + [ks[n] for n in want]
with open(os.environ["DEST"], "w") as f:
    yaml.safe_dump_all(out, f, sort_keys=False)
print(f"plan: ok, {len(want)} Kustomizations: {' '.join(want)}")
print(f"plan: GitRepository ref {g['spec']['ref']}")
PY
}

# --- flux ---------------------------------------------------------------------------------------------------------
cmd_flux() {
  [ -s "$OUT/flux-objects.yaml" ] || die "run 'plan' first"
  kubectl get nodes >/dev/null || die "no cluster reachable through KUBECONFIG"
  flux install || die "flux install failed"
  kubectl -n flux-system wait --for=condition=Available deployment --all --timeout=300s || die "Flux controllers not Available"
  kubectl apply -f "$OUT/flux-objects.yaml" || die "applying the Flux objects failed"
}

# --- prepare-mode -------------------------------------------------------------------------------------------------
cmd_prepare_mode() {
  [ "$MODE" = prod ] || { echo "mode dev: no ConfigMap openbao-operator-keys (the init Job must choose development)"; return 0; }
  command -v gpg >/dev/null || die "gpg is required for prod mode"
  local i end=$((SECONDS + 600))
  until kubectl get namespace "$VAULT_NS" >/dev/null 2>&1; do
    [ "$SECONDS" -lt "$end" ] || die "namespace $VAULT_NS never appeared (namespaces Kustomization)"
    sleep 5
  done
  local gh args=()
  gh="$(mktemp -d)"; chmod 700 "$gh"
  for i in 1 2 3; do
    GNUPGHOME="$gh" gpg --batch --quiet --passphrase '' --quick-generate-key "lane-throwaway-$i" default default never 2>/dev/null \
      || { rm -rf "$gh"; die "gpg key generation failed"; }
    args+=("--from-literal=pgp-$i=$(GNUPGHOME="$gh" gpg --batch --export "lane-throwaway-$i" | base64 -w0)")
  done
  # Public keys only; the private halves die with $gh, so the shares cannot be decrypted: this proves the
  # production path of the init Job, not the break-glass.
  rm -rf "$gh"
  kubectl -n "$VAULT_NS" create configmap openbao-operator-keys --from-literal=threshold=2 "${args[@]}" \
    || die "could not create openbao-operator-keys"
}

# --- wait ---------------------------------------------------------------------------------------------------------
left() { echo $(( DEADLINE - SECONDS )); }

cmd_wait() {
  DEADLINE=$((SECONDS + WAIT_TOTAL))
  local n rc=0 t
  for n in $SET; do
    t="$(left)"; [ "$t" -gt 5 ] || { bad "Kustomization $n: total wait budget (${WAIT_TOTAL}s) spent"; rc=1; continue; }
    if kubectl -n flux-system wait "kustomization/$n" --for=condition=Ready --timeout="${t}s" >/dev/null 2>&1; then
      ok "Kustomization $n Ready"
    else
      bad "Kustomization $n not Ready within the budget"; rc=1
    fi
  done
  local j
  for j in openbao-init openbao-vault-bootstrap; do
    # foundation-vault has wait:false, so its Jobs appear after the Kustomization is "Ready".
    until kubectl -n "$VAULT_NS" get "job/$j" >/dev/null 2>&1; do
      [ "$(left)" -gt 5 ] || break
      sleep 5
    done
    t="$(left)"; [ "$t" -gt 5 ] || t=5
    if kubectl -n "$VAULT_NS" wait "job/$j" --for=condition=Complete --timeout="${t}s" >/dev/null 2>&1; then
      ok "Job $j Complete"
    else
      bad "Job $j not Complete within the budget"; rc=1
    fi
  done
  t="$(left)"; [ "$t" -gt 5 ] || t=5
  if kubectl -n "$VAULT_NS" wait pod -l app=openbao --for=condition=Ready --timeout="${t}s" >/dev/null 2>&1; then
    ok "OpenBao pods Ready"
  else
    bad "OpenBao pods not Ready within the budget"; rc=1
  fi
  return "$rc"
}

# --- assert -------------------------------------------------------------------------------------------------------
cmd_assert() {
  local n want_nodes="${LANE_NODES:-2}" ready desired
  ready="$(kubectl get nodes --no-headers 2>/dev/null | awk '$2 ~ /^Ready/ {c++} END {print c+0}')"
  [ "$ready" = "$want_nodes" ] && ok "$ready nodes Ready" || bad "nodes Ready: $ready, expected $want_nodes"

  ready="$(kubectl -n kube-system get ds cilium -o jsonpath='{.status.numberReady}' 2>/dev/null)"
  desired="$(kubectl -n kube-system get ds cilium -o jsonpath='{.status.desiredNumberScheduled}' 2>/dev/null)"
  if [ -n "$ready" ] && [ "$ready" = "$desired" ] && [ "$ready" -gt 0 ]; then ok "Cilium $ready/$desired"; else bad "Cilium ${ready:-?}/${desired:-?}"; fi

  for n in $SET; do
    [ "$(kubectl -n flux-system get "kustomization/$n" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" = True ] \
      && ok "Kustomization $n Ready=True" || bad "Kustomization $n Ready is not True"
  done

  local log secs
  log="$(kubectl -n "$VAULT_NS" logs job/openbao-init 2>&1)"
  grep -q "mode: $MODE " <<<"$log" && ok "init Job chose mode $MODE" || bad "init Job log does not say 'mode: $MODE'"
  grep -q 'init complete' <<<"$log" && ok "init Job reached its end ('init complete')" || bad "init Job log lacks 'init complete'"
  kubectl -n "$VAULT_NS" get secret openbao-seal-key >/dev/null 2>&1 && ok "Secret openbao-seal-key exists" || bad "Secret openbao-seal-key missing"
  secs="$(kubectl -n "$VAULT_NS" get secret -o name 2>/dev/null)"
  if [ "$MODE" = dev ]; then
    grep -q 'secret/openbao-recovery-dev$' <<<"$secs" && ! grep -q 'secret/openbao-recovery-shares$' <<<"$secs" \
      && ok "dev: only the development recovery Secret exists" || bad "dev: recovery Secrets are not as documented"
  else
    grep -q 'secret/openbao-recovery-shares$' <<<"$secs" && ! grep -q 'secret/openbao-recovery-dev$' <<<"$secs" \
      && ok "prod: only the encrypted recovery shares Secret exists" || bad "prod: recovery Secrets are not as documented"
  fi

  if [ -f "$OUT/leak-scan.txt" ] && grep -q '^hits=0$' "$OUT/leak-scan.txt"; then ok "no secret value in the collected pod logs"
  else bad "leak scan missing or found secrets (see $OUT/leak-scan.txt; offending files were deleted)"; fi

  printf '%s passed, %s failed\n' "$PASS" "$FAIL"
  [ "$FAIL" -eq 0 ]
}

# --- collect ------------------------------------------------------------------------------------------------------
cmd_collect() {
  mkdir -p "$OUT/logs" "$OUT/describe"
  kubectl version >"$OUT/version.txt" 2>&1
  kubectl get nodes -o wide >"$OUT/nodes.txt" 2>&1
  kubectl get pods -A -o wide >"$OUT/pods.txt" 2>&1
  kubectl get kustomization,gitrepository,helmrelease,helmrepository -A -o wide >"$OUT/flux.txt" 2>&1
  kubectl get events -A --sort-by=.lastTimestamp >"$OUT/events.txt" 2>&1
  kubectl get jobs,statefulset,pvc -A >"$OUT/workloads.txt" 2>&1
  # Names only: a Secret's content never goes into an artifact.
  kubectl get secret -A -o name >"$OUT/secret-names.txt" 2>&1
  kubectl describe kustomization,gitrepository,helmrelease -A >"$OUT/describe/flux.txt" 2>&1
  kubectl describe jobs,statefulset,pods -n "$VAULT_NS" >"$OUT/describe/$VAULT_NS.txt" 2>&1
  kubectl describe nodes >"$OUT/describe/nodes.txt" 2>&1
  local ns pod
  while read -r ns pod; do
    [ -n "$pod" ] || continue
    kubectl -n "$ns" logs "$pod" --all-containers --tail=1000 >"$OUT/logs/$ns.$pod.log" 2>&1
    kubectl -n "$ns" logs "$pod" --all-containers --previous --tail=300 >"$OUT/logs/$ns.$pod.previous.log" 2>/dev/null \
      || rm -f "$OUT/logs/$ns.$pod.previous.log"
  done < <(kubectl get pods -A --no-headers -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name 2>/dev/null)
  scan_logs
}

# Known secret VALUES (seal key, recovery shares) plus the shapes of a token (Vault `hvs.`, an OpenBao service token `s.` + 24 characters, an OpenBao batch token `b.` + a long body) and of a
# PEM private key. Counts and names only are printed. `scan` runs it alone on an existing $OUT (used by the static test).
scan_logs() {
  local known="$OUT/.known-secrets" hits=0 f
  : >"$known"; chmod 600 "$known"
  local s
  for s in openbao-seal-key openbao-recovery-dev openbao-recovery-shares; do
    kubectl -n "$VAULT_NS" get secret "$s" -o json 2>/dev/null | python3 -I -c '
import base64, json, sys
try:
    d = json.load(sys.stdin).get("data", {})
except ValueError:
    sys.exit(0)
for v in d.values():
    for cand in (v, base64.b64decode(v).decode("latin-1")):
        if len(cand) >= 16 and "\n" not in cand:
            print(cand)
' >>"$known"
  done
  : >"$OUT/leak-scan.txt"
  for f in "$OUT"/logs/*.log; do
    [ -e "$f" ] || continue
    if { [ -s "$known" ] && grep -qF -f "$known" "$f"; } || grep -qE '(^|[^A-Za-z0-9_./-])(hvs\.[A-Za-z0-9_-]{20,}|s\.[A-Za-z0-9]{24}|b\.[A-Za-z0-9_-]{40,})([^A-Za-z0-9_-]|$)|BEGIN ([A-Z]+ )*PRIVATE KEY' "$f"; then
      hits=$((hits + 1)); echo "leak: $(basename "$f")" >>"$OUT/leak-scan.txt"
      rm -f "$f"
    fi
  done
  rm -f "$known"
  echo "hits=$hits" >>"$OUT/leak-scan.txt"
  echo "leak scan: $hits log file(s) held a secret and were deleted from the artifact"
}

case "${1:-}" in
  plan) cmd_plan ;;
  flux) cmd_flux ;;
  prepare-mode) cmd_prepare_mode ;;
  wait) cmd_wait ;;
  assert) cmd_assert ;;
  collect) cmd_collect ;;
  scan) scan_logs ;;
  *) echo "usage: $0 plan|flux|prepare-mode|wait|assert|collect|scan" >&2; exit 2 ;;
esac
