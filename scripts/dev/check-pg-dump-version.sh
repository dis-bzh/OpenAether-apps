#!/usr/bin/env bash
# Fail when a Postgres CLIENT image of the backup brick (pg_dump / pg_restore) has a
# lower major than a CNPG Cluster it must dump: pg_dump refuses a newer server, so
# the daily dump silently stops (zitadel-db went to 18 while cnpg-dump stayed on 16).
# Static check on the manifests, nothing runs on a cluster.
#   Usage: scripts/dev/check-pg-dump-version.sh [repo-root]
set -euo pipefail
ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
major() { sed -E 's/.*postgresql:([0-9]+).*/\1/'; }

servers="$(grep -rhE '^[[:space:]]*imageName:[[:space:]]*[^[:space:]]*postgresql:[0-9]+' "$ROOT/apps/base/cnpg" "$ROOT/apps/components" --include='*.yaml' | major | sort -un)"
clients="$(grep -rhE '^[[:space:]]*image:[[:space:]]*[^[:space:]]*postgresql:[0-9]+' "$ROOT/apps/base/backup" --include='*.yaml' | major | sort -un)"
# A vacuous pass would hide a renamed field or a moved brick.
[ -n "$servers" ] || { echo "ERROR: no Cluster imageName found under apps/base/cnpg or apps/components" >&2; exit 1; }
[ -n "$clients" ] || { echo "ERROR: no postgresql client image found under apps/base/backup" >&2; exit 1; }

top="$(tail -n 1 <<<"$servers")"
low="$(head -n 1 <<<"$clients")"
echo "Cluster majors: $(tr '\n' ' ' <<<"$servers")| client majors: $(tr '\n' ' ' <<<"$clients")"
if [ "$low" -lt "$top" ]; then
  echo "ERROR: a backup client image is on Postgres $low but a Cluster runs $top; pg_dump cannot dump a newer server." >&2
  exit 1
fi
echo "OK: every backup client image is >= the highest Cluster major ($top)"
