#!/bin/sh
# One canary row per database: a counter and a hash chained to the previous row (sha = sha256(prev_sha | counter | time)).
# A restored copy proves itself without the source: the counters are contiguous and every hash follows from its
# predecessor, so a missing page, a rewound table or a flipped byte breaks the chain (restore_cnpg.py reads it back).
# The table is the application owner's own, in `public`; 14 days of rows are kept (a row every 5 minutes).
set -eu
export PGPORT="${PGPORT:-5432}" PGCONNECT_TIMEOUT=10
rc=0
write() { # <host> <dbname> <credsdir>
  PGHOST="$1" PGDATABASE="$2" PGUSER="$(cat "$3/username")" PGPASSWORD="$(cat "$3/password")"
  export PGHOST PGDATABASE PGUSER PGPASSWORD
  psql -X -q -v ON_ERROR_STOP=1 <<'SQL' || return 1
BEGIN;
CREATE TABLE IF NOT EXISTS oa_canary (counter bigint PRIMARY KEY, written_at timestamptz NOT NULL, sha text NOT NULL);
SELECT pg_advisory_xact_lock(7461);
WITH prev AS (SELECT counter, sha FROM oa_canary ORDER BY counter DESC LIMIT 1),
     nxt AS (SELECT coalesce((SELECT counter FROM prev), 0) + 1 AS c, clock_timestamp() AS t,
                    coalesce((SELECT sha FROM prev), '') AS p)
INSERT INTO oa_canary
SELECT c, t, encode(sha256(convert_to(p || '|' || c || '|' || to_char(t AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'), 'UTF8')), 'hex') FROM nxt;
DELETE FROM oa_canary WHERE counter < (SELECT max(counter) FROM oa_canary) - 4032;
COMMIT;
SQL
}
write "${ZITADEL_DB_HOST:-zitadel-db-rw.foundation-databases.svc.cluster.local}" zitadel /creds/zitadel || { echo "ERROR: canary write to zitadel-db failed"; rc=1; }
write "${GRAFANA_DB_HOST:-grafana-db-rw.foundation-databases.svc.cluster.local}" grafana /creds/grafana || { echo "ERROR: canary write to grafana-db failed"; rc=1; }
[ "$rc" -eq 0 ] && echo "canary rows written"
exit "$rc"
