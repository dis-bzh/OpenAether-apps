#!/usr/bin/env bash
# Mutation test of scripts/check-dependson-contracts.py (#38): the checker passes on the tree, and must fail, naming the
# right Kustomization, when an edge it guards is removed. pick.py --validate/--check do NOT catch these.
set -euo pipefail
repo="$(cd "$(dirname "$0")/../.." && pwd)"
fails=0

mutant() {  # mutant <dir>: fresh copy of the tree, small enough for a tmpfs
  rm -rf "$1" && mkdir -p "$1" && cp -r "$repo/apps" "$repo/scripts" "$1/"
}
expect() {  # expect <label> <dir> <rc> [expected line]...
  local label=$1 dir=$2 want=$3 out rc=0; shift 3
  out="$(python3 -I "$dir/scripts/check-dependson-contracts.py" 2>&1)" || rc=$?
  [ "$rc" -eq "$want" ] || { echo "FAIL $label: exit $rc, want $want"; echo "$out"; fails=$((fails+1)); return; }
  for l in "$@"; do
    grep -qF "$l" <<<"$out" || { echo "FAIL $label: output lacks '$l'"; echo "$out"; fails=$((fails+1)); return; }
  done
  echo "ok   $label"
}

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
t="$tmp/t"

expect "unmodified tree passes" "$repo" 0

mutant "$t"
sed -i '/- name: external-secrets-stores/d' "$t"/apps/flux/base/{20-cnpg,21-storage,30-observability}.yaml
# identity only reaches the store through cnpg: it must be flagged too (transitive, as a failure case)
expect "store edge removed from cnpg, storage, observability" "$t" 1 \
  "MISSING cnpg external-secrets-stores" "MISSING storage external-secrets-stores" \
  "MISSING observability external-secrets-stores" "MISSING identity external-secrets-stores"

mutant "$t"
sed -i '/- name: external-secrets-stores/d' "$t"/apps/flux/base/20-cnpg.yaml
expect "identity flagged when only the cnpg edge is gone" "$t" 1 "MISSING identity external-secrets-stores"

mutant "$t"
cp "$t/apps/base/services-gateway/openbao-destinationrule.yaml" "$t/apps/base/foundation/vault/dr-mutant.yaml"
sed -i '/^resources:/a\  - dr-mutant.yaml' "$t/apps/base/foundation/vault/kustomization.yaml"
expect "DestinationRule back in foundation-vault" "$t" 1 "MISSING foundation-vault istio"

mutant "$t"
sed -i '/- name: istio  *#/d' "$t"/apps/flux/base/14c-services-gateway.yaml
expect "istio edge removed from services-gateway" "$t" 1 "MISSING services-gateway istio"

[ "$fails" -eq 0 ] && echo "all passed" || { echo "$fails failed"; exit 1; }
