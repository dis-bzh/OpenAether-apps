#!/usr/bin/env bash
# Test of check-vendored-lock.sh: passes on the repository, fails on a changed hash and on a missing lock entry.
# Rung: mocked/static (the lock is copied, no dump is touched).
set -uo pipefail
cd "$(dirname "$0")/../.."
S=scripts/dev/check-vendored-lock.sh
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
P=0; F=0
t() { if [[ "$1" == "$2" ]]; then echo "  ok   $3"; P=$((P+1)); else echo "  FAIL $3 (got rc=$1, want $2)"; F=$((F+1)); fi; }
$S >/dev/null 2>&1; t $? 0 "the repository matches its lock"
sed '1s/^./0/' apps/vendored-dumps.sha256 > "$W/bad"; [[ "$(head -c1 apps/vendored-dumps.sha256)" == 0 ]] && sed -i '1s/^./1/' "$W/bad"
VENDORED_LOCK="$W/bad" $S >/dev/null 2>&1; t $? 1 "a changed hash fails"
sed '1d' apps/vendored-dumps.sha256 > "$W/short"
VENDORED_LOCK="$W/short" $S >/dev/null 2>&1; t $? 1 "a dump missing from the lock fails"
VENDORED_LOCK="$W/none" $S >/dev/null 2>&1; t $? 1 "a missing lock fails"
mkdir -p "$W/tree"; for f in apps/base/platform/local-path-provisioner/{kustomization,local-path-storage}.yaml apps/base/platform/gateway-api/{kustomization,standard-install}.yaml apps/base/cnpg/barman-cloud-plugin-0.15.1.yaml; do
  mkdir -p "$W/tree/$(dirname "$f")"; cp "$f" "$W/tree/$f"; done
VENDORED_TREE="$W/tree" $S >/dev/null 2>&1; t $? 0 "the version comments equal the dumps (#106)"
sed -i 's/vendored from v0.0.37/vendored from v0.0.36/' "$W/tree/apps/base/platform/local-path-provisioner/kustomization.yaml"
VENDORED_TREE="$W/tree" $S >/dev/null 2>&1; t $? 1 "a stale local-path-provisioner comment fails"
cp apps/base/platform/local-path-provisioner/kustomization.yaml "$W/tree/apps/base/platform/local-path-provisioner/"
sed -i 's/vendored from v1.2.1/vendored from v1.6.1/' "$W/tree/apps/base/platform/gateway-api/kustomization.yaml"
VENDORED_TREE="$W/tree" $S >/dev/null 2>&1; t $? 1 "a stale gateway-api comment fails"
sed -i 's#plugin-barman-cloud:v0.15.1#plugin-barman-cloud:v0.16.0#' "$W/tree/apps/base/cnpg/barman-cloud-plugin-0.15.1.yaml"
VENDORED_TREE="$W/tree" $S >/dev/null 2>&1; t $? 1 "a plugin dump whose image differs from its file name fails"
echo "$P passed, $F failed"; exit $((F > 0))
