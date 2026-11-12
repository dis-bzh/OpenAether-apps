#!/usr/bin/env bash
# Mutation test of scripts/check-dependson-contracts.py (#38): the checker passes on the tree, and must fail, naming the
# right Kustomization, when an edge it guards is removed. pick.py --validate/--check do NOT catch these.
#
# The five cases share nothing: each one has its own copy of the tree under its own directory and its own output file,
# so they run at the same time (the checker builds every brick, about 15 s each) and their messages are printed in the
# order of the list below, after all of them have finished. Any failing case fails the script.
set -euo pipefail
repo="$(cd "$(dirname "$0")/../.." && pwd)"

mutant() {  # mutant <dir>: fresh copy of the tree, small enough for a tmpfs
  rm -rf "$1" && mkdir -p "$1" && cp -r "$repo/apps" "$repo/scripts" "$1/"
}
expect() {  # expect <label> <dir> <rc> [expected line]...: prints ok or FAIL, returns 1 on FAIL
  local label=$1 dir=$2 want=$3 out rc=0; shift 3
  # -B: no .pyc written, so no case changes the tree another case is copying (cp -r of scripts/ in mutant)
  out="$(python3 -I -B "$dir/scripts/check-dependson-contracts.py" 2>&1)" || rc=$?
  [ "$rc" -eq "$want" ] || { echo "FAIL $label: exit $rc, want $want"; echo "$out"; return 1; }
  for l in "$@"; do
    grep -qF "$l" <<<"$out" || { echo "FAIL $label: output lacks '$l'"; echo "$out"; return 1; }
  done
  echo "ok   $label"
}

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

case_unmodified() {
  expect "unmodified tree passes" "$repo" 0
}
case_stores() {
  local t="$tmp/stores"
  mutant "$t"
  sed -i '/- name: external-secrets-stores/d' "$t"/apps/flux/base/{20-cnpg,21-storage,30-observability}.yaml
  # identity only reaches the store through cnpg: it must be flagged too (transitive, as a failure case)
  expect "store edge removed from cnpg, storage, observability" "$t" 1 \
    "MISSING cnpg external-secrets-stores" "MISSING storage external-secrets-stores" \
    "MISSING observability external-secrets-stores" "MISSING identity external-secrets-stores"
}
case_identity() {
  local t="$tmp/identity"
  mutant "$t"
  sed -i '/- name: external-secrets-stores/d' "$t"/apps/flux/base/20-cnpg.yaml
  expect "identity flagged when only the cnpg edge is gone" "$t" 1 "MISSING identity external-secrets-stores"
}
case_destinationrule() {
  local t="$tmp/destinationrule"
  mutant "$t"
  cp "$t/apps/base/services-gateway/openbao-destinationrule.yaml" "$t/apps/base/foundation/vault/dr-mutant.yaml"
  sed -i '/^resources:/a\  - dr-mutant.yaml' "$t/apps/base/foundation/vault/kustomization.yaml"
  expect "DestinationRule back in foundation-vault" "$t" 1 "MISSING foundation-vault istio"
}
case_istio() {
  local t="$tmp/istio"
  mutant "$t"
  sed -i '/- name: istio  *#/d' "$t"/apps/flux/base/14c-services-gateway.yaml
  expect "istio edge removed from services-gateway" "$t" 1 "MISSING services-gateway istio"
}

cases=(case_unmodified case_stores case_identity case_destinationrule case_istio)
pids=()
for c in "${cases[@]}"; do
  ( "$c" ) >"$tmp/$c.out" 2>&1 &
  pids+=("$!")
done

fails=0
for i in "${!cases[@]}"; do
  c="${cases[$i]}"; rc=0
  wait "${pids[$i]}" || rc=$?
  cat "$tmp/$c.out"
  if [ "$rc" -ne 0 ]; then
    grep -q '^FAIL ' "$tmp/$c.out" || echo "FAIL $c: exit $rc before its verdict"
    fails=$((fails+1))
  fi
done

[ "$fails" -eq 0 ] && echo "all passed" || { echo "$fails failed"; exit 1; }
