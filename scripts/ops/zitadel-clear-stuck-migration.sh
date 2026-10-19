#!/usr/bin/env bash
# Clear a Zitadel setup migration that was STARTED and never finished (apps #133). A setup Job that dies half way leaves a
# `system.migration.started` event (eventstore.events2) and a `migration_started` row (eventstore.unique_constraints) for the step,
# with no `system.migration.done` (nor `repeatable.done`, which the repeatable steps write instead); every later setup logs "migration already started, will check again in 5 seconds" for ever.
#
# FIRST INSTALL ONLY: it refuses a database that holds any user event (restore from the CNPG backup instead of editing an event store
# that holds data), a step that has a `done` event, and a `started` event younger than --min-age (an attempt may still be running).
# Dry run by default: it lists what it would delete. --apply deletes exactly the two rows per stuck step, in one transaction.
#
#   scripts/ops/zitadel-clear-stuck-migration.sh [--apply] [--min-age SECONDS]    (default 600; the setup Job's own timeout is longer
#   than a healthy step, so 10 minutes without a `done` is stuck)
# Then: delete the failed Job and `flux reconcile helmrelease zitadel -n services-identity --reset --force` (docs/traps.md).
# It reaches the database through the CNPG primary of zitadel-db (kubectl exec) and refuses while the zitadel-setup Job has a running
# pod; PSQL_CMD replaces both (the test runs a real PostgreSQL that way). Rung: tested against a real Zitadel and PostgreSQL
# (scripts/dev/test-zitadel-migration-lock.sh); the kubectl path is not exercised by a test. Exit 0 nothing (left) to clear or done,
# 1 a step was found and not cleared (dry run), 2 usage or a tool is missing, 3 refused.
set -uo pipefail
APPLY=0; MIN_AGE=600
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --min-age) shift; MIN_AGE="${1:-}" ;;
    *) echo "usage: $0 [--apply] [--min-age SECONDS]" >&2; exit 2 ;;
  esac
  shift
done
case "$MIN_AGE" in ''|*[!0-9]*) echo "--min-age takes a number of seconds" >&2; exit 2 ;; esac

if [ -z "${PSQL_CMD:-}" ]; then
  command -v kubectl >/dev/null 2>&1 || { echo "kubectl is required (or set PSQL_CMD)" >&2; exit 2; }
  DB_NS="${DB_NAMESPACE:-foundation-databases}"; ID_NS="${IDENTITY_NAMESPACE:-services-identity}"
  active="$(kubectl -n "$ID_NS" get job zitadel-setup -o jsonpath='{.status.active}' 2>/dev/null || true)"
  if [ -n "$active" ] && [ "$active" != 0 ]; then
    echo "refused: the zitadel-setup Job has a running pod; wait for it to fail (an attempt writes the event again)" >&2; exit 3
  fi
  pod="$(kubectl -n "$DB_NS" get pod -l cnpg.io/cluster=zitadel-db,cnpg.io/instanceRole=primary -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  [ -n "$pod" ] || { echo "no CNPG primary pod of zitadel-db in $DB_NS" >&2; exit 2; }
  PSQL_CMD="kubectl -n $DB_NS exec -i $pod -c postgres -- psql -d zitadel"
fi
q() { echo "$1" | $PSQL_CMD -v ON_ERROR_STOP=1 -At -F '|'; }

# A finished first install already holds the bootstrap org, its one admin human and machine users; anything more is a used instance.
used="$(q "select (select count(*) from eventstore.events2 where event_type in ('user.human.added', 'user.human.registered'))
  || '/' || (select count(*) from eventstore.events2 where event_type = 'org.added');")" \
  || { echo "cannot query the zitadel database" >&2; exit 2; }
humans="${used%/*}"; orgs="${used#*/}"
if [ "$humans" -gt 1 ] || [ "$orgs" -gt 1 ]; then
  echo "refused: the database holds $humans human user(s) and $orgs organisation(s), more than the bootstrap ones; this recovery is for a FIRST install. Restore from the CNPG backup (docs/traps.md)." >&2
  exit 3
fi
stuck="$(q "select s.payload->>'name', extract(epoch from now() - s.created_at)::bigint
  from eventstore.events2 s
  where s.event_type = 'system.migration.started'
    and not exists (select 1 from eventstore.events2 d where d.event_type in ('system.migration.done', 'system.migration.repeatable.done')
                    and d.payload->>'name' = s.payload->>'name' and d.position >= s.position)
  order by s.position;")" || { echo "cannot query the zitadel database" >&2; exit 2; }
[ -n "$stuck" ] || { echo "no started-without-done migration: nothing to clear"; exit 0; }

rc=0
while IFS='|' read -r name age; do
  [ -n "$name" ] || continue
  case "$name" in *[!A-Za-z0-9_.]*) echo "refused: unexpected step name '$name'" >&2; exit 3 ;; esac
  if [ "$age" -lt "$MIN_AGE" ]; then
    echo "refused: step $name started ${age}s ago (< --min-age $MIN_AGE); an attempt may be running" >&2; exit 3
  fi
  if [ "$APPLY" = 1 ]; then
    q "begin;
       delete from eventstore.events2 where event_type = 'system.migration.started' and payload->>'name' = '$name'
         and position > (select coalesce(max(position), 0) from eventstore.events2
                         where event_type in ('system.migration.done', 'system.migration.repeatable.done') and payload->>'name' = '$name');
       delete from eventstore.unique_constraints where unique_type = 'migration_started' and unique_field = '$name';
       commit;" >/dev/null || { echo "delete failed for $name" >&2; exit 2; }
    echo "cleared: $name (started ${age}s ago)"
  else
    echo "would clear: $name (started ${age}s ago); re-run with --apply"; rc=1
  fi
done <<< "$stuck"
exit "$rc"
