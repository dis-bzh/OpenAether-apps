#!/bin/sh
# Stage 1 of the CNPG restore test: the live side of the comparison, taken BEFORE anything is restored. Read-only (the
# owner role reads its own tables). A database that cannot be read leaves no file: restore_cnpg.py then reports the
# dataset as failed instead of comparing against nothing.
set -u
export PGPORT="${PGPORT:-5432}" PGCONNECT_TIMEOUT=10
for spec in "zitadel-db|${ZITADEL_DB_HOST:-zitadel-db-rw.foundation-databases.svc.cluster.local}|zitadel|/creds/zitadel" \
            "grafana-db|${GRAFANA_DB_HOST:-grafana-db-rw.foundation-databases.svc.cluster.local}|grafana|/creds/grafana"; do
  IFS='|' read -r name host db creds <<EOS
$spec
EOS
  PGHOST="$host" PGDATABASE="$db" PGUSER="$(cat "$creds/username")" PGPASSWORD="$(cat "$creds/password")"
  export PGHOST PGDATABASE PGUSER PGPASSWORD
  if psql -X -At -F '	' -f /scripts/snapshot.sql >"/work/$name.src.tmp" 2>"/work/$name.src.err"; then
    mv "/work/$name.src.tmp" "/work/$name.src"
  else
    echo "ERROR: cannot read the live $name: $(head -c 300 "/work/$name.src.err")"
    rm -f "/work/$name.src.tmp"
  fi
done
exit 0
