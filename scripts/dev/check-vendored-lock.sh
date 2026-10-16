#!/usr/bin/env bash
# Checks the sha256 of every vendored upstream manifest dump against apps/vendored-dumps.sha256.
# The lock records what the repository holds, so a change to a dump (a re-vendor, a hand edit, a bad merge) cannot go
# unnoticed in review: the lock moves in the same commit. It does NOT prove the dump equals the upstream release; that
# stays a human step (download, diff) when the dump is re-vendored.
#   scripts/dev/check-vendored-lock.sh            verify
#   scripts/dev/check-vendored-lock.sh --update   rewrite the lock after a deliberate re-vendor
set -euo pipefail
cd "$(dirname "$0")/../.."
LOCK="${VENDORED_LOCK:-apps/vendored-dumps.sha256}"
DUMPS=(
  apps/base/cnpg/cnpg-1.23.1.yaml
  apps/base/cluster-api-clusters/flux-gotk/gotk-components.yaml
  apps/base/external-secrets/install.yaml
  apps/base/kyverno/kyverno-1.19.1.yaml
  apps/base/orc/install.yaml
  apps/base/platform/gateway-api/standard-install.yaml
  apps/base/platform/local-path-provisioner/local-path-storage.yaml
)
if [[ "${1:-}" == "--update" ]]; then
  sha256sum "${DUMPS[@]}" > "$LOCK"
  echo "updated $LOCK"
  exit 0
fi
[[ -f "$LOCK" ]] || { echo "ERROR: $LOCK is missing" >&2; exit 1; }
rc=0
sha256sum --check --quiet "$LOCK" || rc=1
# A dump that is not in the lock is as bad as one that changed.
for f in "${DUMPS[@]}"; do
  grep -q " $f\$" "$LOCK" || { echo "ERROR: $f is not in $LOCK" >&2; rc=1; }
done
[[ $(wc -l < "$LOCK") -eq ${#DUMPS[@]} ]] || { echo "ERROR: $LOCK lists entries that are not vendored dumps" >&2; rc=1; }
[[ $rc -eq 0 ]] && echo "vendored dumps match $LOCK (${#DUMPS[@]} files)"
exit $rc
