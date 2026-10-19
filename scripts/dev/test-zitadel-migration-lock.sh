#!/usr/bin/env bash
# A Zitadel setup run that dies half way leaves a migration the next runs wait on for ever (apps #133), and
# scripts/ops/zitadel-clear-stuck-migration.sh is the recovery. Run through the REAL Zitadel (the pinned chart's appVersion in a
# container) against a real PostgreSQL, on the configuration the manifests produce:
#   - a normal setup writes started + done for every step, and the script finds nothing to clear;
#   - a step left `started` with no `done` (the state the lab left) makes the next setup wait ("migration already started"),
#     reproduced with a real run and a real wait;
#   - the script lists it, changes nothing without --apply, and with --apply deletes exactly the two rows (the event and the
#     unique-constraint row), after which the same setup finishes and the instance answers;
#   - it refuses a recent `started` (an attempt may be running) and a database that holds more than the bootstrap admin.
# Needs docker (pulls ghcr.io/zitadel/zitadel and postgres), helm and the network (the chart), kubectl, jq, openssl, python3 with PyYAML.
# Rung: a real Zitadel and a real PostgreSQL in containers; the kubectl path of the script is not run (PSQL_CMD replaces it).
#   Usage: scripts/dev/test-zitadel-migration-lock.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
for bin in docker helm kubectl jq openssl python3; do command -v "$bin" >/dev/null || { echo "$bin is required" >&2; exit 2; }; done
W="$(mktemp -d)"; NET="zlock$$"; SCRIPT="$ROOT/scripts/ops/zitadel-clear-stuck-migration.sh"
PASS=0; FAIL=0
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; PASS=$((PASS + 1)); }
bad() { printf '  \033[31m✗\033[0m %s\n' "$*"; FAIL=$((FAIL + 1)); }
cleanup() { docker rm -f "$NET-z" "$NET-pg" >/dev/null 2>&1; docker network rm "$NET" >/dev/null 2>&1; rm -rf "$W"; }
trap cleanup EXIT
chmod 755 "$W"; mkdir -p "$W/out"; chmod 777 "$W/out"

kubectl kustomize "$ROOT/apps/base/identity/zitadel" > "$W/brick.yaml"
python3 -I - "$W" <<'PY'
import sys, yaml
w = sys.argv[1]
for d in yaml.safe_load_all(open(f"{w}/brick.yaml")):
    if not d:
        continue
    if d["kind"] == "HelmRelease":
        yaml.safe_dump(d["spec"]["values"], open(f"{w}/values.yaml", "w"))
        s = d["spec"]["chart"]["spec"]
        open(f"{w}/chart", "w").write(f'{s["chart"]} {s["version"]}\n')
    if d["kind"] == "HelmRepository":
        open(f"{w}/repo", "w").write(d["spec"]["url"])
    if d["kind"] == "ExternalSecret" and d["metadata"]["name"] == "zitadel-secret-config":
        open(f"{w}/es.tpl", "w").write(d["spec"]["target"]["template"]["data"]["config-yaml"])
PY
read -r CHART VERSION < "$W/chart"; REPO="$(cat "$W/repo")"
helm template zitadel "$CHART" --repo "$REPO" --version "$VERSION" -n services-identity -f "$W/values.yaml" > "$W/rendered.yaml" \
  && ok "the chart $CHART $VERSION renders with the shipped values" || { bad "helm template failed"; exit 1; }
APPVER="$(helm show chart "$CHART" --repo "$REPO" --version "$VERSION" | sed -n 's/^appVersion: *//p')"
IMAGE="ghcr.io/zitadel/zitadel:$APPVER"
python3 -I - "$W" <<'PY'
import sys, yaml
w = sys.argv[1]
for d in yaml.safe_load_all(open(f"{w}/rendered.yaml")):
    if d and d["kind"] == "ConfigMap" and "zitadel-config-yaml" in (d.get("data") or {}):
        cfg = d["data"]["zitadel-config-yaml"]
open(f"{w}/cfg.yaml", "w").write(cfg)
tpl = open(f"{w}/es.tpl").read()
open(f"{w}/secret.yaml", "w").write(tpl.replace("{{ .password }}", "dbpass").replace("{{ .adminpw }}", 'Xk3pQ9vLm2Rt7WnB5cDf8GhJ4sAa1!'))
PY
cat > "$W/override.yaml" <<YAML
ExternalDomain: localhost
ExternalPort: 8080
ExternalSecure: false
Database:
  Postgres:
    Host: $NET-pg
    Port: 5432
    Database: zitadel
    User: {Username: zitadel, Password: dbpass, SSL: {Mode: disable}}
    Admin: {Username: postgres, Password: postgres, SSL: {Mode: disable}}
SystemAPIUsers:
  login-client:
    Path: /secrets/login-client/tls.crt
YAML
mkdir -p "$W/login"; openssl req -x509 -newkey rsa:2048 -nodes -keyout "$W/login/key.pem" -out "$W/login/tls.crt" -subj "/CN=login-client" -days 2 2>/dev/null
chmod 755 "$W/login"; chmod 644 "$W/login"/*

docker network create "$NET" >/dev/null
docker run -d --name "$NET-pg" --network "$NET" -e POSTGRES_PASSWORD=postgres \
  postgres:17-alpine@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24 >/dev/null
for _ in $(seq 40); do docker exec "$NET-pg" pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done
sql() { docker exec "$NET-pg" psql -U postgres -d zitadel -Atc "$1"; }
export PSQL_CMD="docker exec -i $NET-pg psql -U postgres -d zitadel"
# One setup run (the chart's setup Job: init is separate, `start-from-init` does both for the first run). TIMEOUT seconds at most.
zsetup() { # init|setup TIMEOUT ... (the container is removed after the timeout: killing the docker client leaves it running)
  timeout -s KILL "$2" docker run --rm --name "$NET-z" --network "$NET" -v "$W:/w" -v "$W/login:/secrets/login-client:ro" \
    -e ZITADEL_MASTERKEY=MasterkeyNeedsToHave32Characters -e ZITADEL_FIRSTINSTANCE_MACHINEKEYPATH=/w/out/sa.json \
    -e ZITADEL_FIRSTINSTANCE_PATPATH=/w/out/pat "$IMAGE" "$1" ${3:+--masterkeyFromEnv} --config /w/cfg.yaml --config /w/secret.yaml \
    --config /w/override.yaml ${3:+--steps /w/cfg.yaml --steps /w/secret.yaml --steps /w/override.yaml} >"$W/$1.log" 2>&1
  local rc=$?; docker rm -f "$NET-z" >/dev/null 2>&1; return $rc
}
echo "=== a first setup on an empty database"
zsetup init 120 && zsetup setup 240 1 && ok "init + setup finish" || { bad "init/setup failed"; tail -5 "$W/setup.log"; exit 1; }

echo "=== nothing stuck: the script finds nothing"
out="$("$SCRIPT" --apply 2>&1)"; rc=$?
[ "$rc" = 0 ] && echo "$out" | grep -q "nothing to clear" && ok "a finished install: nothing to clear (exit 0)" || bad "clean database: rc=$rc $out"
[ "$(sql "select count(*) from eventstore.events2 where event_type like 'system.migration.%'")" -gt 4 ] && ok "(fixture) the install wrote its migration events" || bad "no migration events"

echo "=== a step left started with no done (the lab state), and a real setup waiting on it"
STEP="$(sql "select payload->>'name' from eventstore.events2 where event_type='system.migration.done' order by position desc limit 1")"
# The last step is the one a Job killed half way would have left: its done event is the thing that never got written.
sql "delete from eventstore.events2 where event_type='system.migration.done' and payload->>'name'='$STEP'" >/dev/null
sql "delete from eventstore.unique_constraints where unique_type='migration_done' and unique_field='$STEP'" >/dev/null   # written with done
sql "select count(*) from eventstore.events2 where event_type='system.migration.started' and payload->>'name'='$STEP'" | grep -qx 1 \
  && ok "(fixture) step $STEP has a started event and no done" || bad "fixture for $STEP"
zsetup setup 45 1; rc=$?
if [ "$rc" = 137 ] && grep -q "already started" "$W/setup.log"; then ok "a new setup waits on $STEP for ever ('migration already started'), as in the lab"
else bad "the setup did not wait (rc=$rc)"; tail -3 "$W/setup.log"; fi
docker rm -f "$NET-z" >/dev/null 2>&1

echo "=== the recovery script"
out="$("$SCRIPT" 2>&1)"; rc=$?
[ "$rc" = 3 ] && echo "$out" | grep -q "an attempt may be running" && ok "a recent started event is refused (exit 3)" || bad "recent not refused: rc=$rc $out"
sql "update eventstore.events2 set created_at = now() - interval '1 hour' where event_type='system.migration.started' and payload->>'name'='$STEP'" >/dev/null
out="$("$SCRIPT" 2>&1)"; rc=$?
[ "$rc" = 1 ] && echo "$out" | grep -q "would clear: $STEP" && ok "dry run names the step and exits 1" || bad "dry run: rc=$rc $out"
[ "$(sql "select count(*) from eventstore.events2 where event_type='system.migration.started' and payload->>'name'='$STEP'")" = 1 ] \
  && ok "the dry run changed nothing" || bad "the dry run deleted"
sql "insert into eventstore.events2 select instance_id, 'user', 'u2', 'user.human.added', 1, 1, now(), '{}', 'x', 'x', 0, 0 from eventstore.events2 where event_type='user.human.added' limit 1" >/dev/null
out="$("$SCRIPT" --apply 2>&1)"; rc=$?
[ "$rc" = 3 ] && echo "$out" | grep -q "holds 2 human" && ok "a database with a second human user is refused (exit 3)" || bad "users not refused: rc=$rc $out"
sql "delete from eventstore.events2 where aggregate_id='u2'" >/dev/null
out="$("$SCRIPT" --apply 2>&1)"; rc=$?
[ "$rc" = 0 ] && echo "$out" | grep -q "cleared: $STEP" && ok "--apply clears the step (exit 0)" || bad "apply: rc=$rc $out"
[ "$(sql "select count(*) from eventstore.events2 where payload->>'name'='$STEP'")" = 0 ] \
  && [ "$(sql "select count(*) from eventstore.unique_constraints where unique_type='migration_started' and unique_field='$STEP'")" = 0 ] \
  && ok "exactly the event and the unique-constraint row of that step are gone" || bad "rows left"
[ "$(sql "select count(*) from eventstore.events2 where event_type='system.migration.done'")" -gt 0 ] && ok "the other steps are untouched" || bad "other steps deleted"
zsetup setup 240 1 && ok "the same setup now finishes ($STEP runs again)" || { bad "setup still blocked"; grep "$STEP" "$W/setup.log" | tail -4 | cut -c1-300
  sql "select event_type, payload, created_at from eventstore.events2 where payload->>'name'='$STEP'"
  sql "select unique_type, unique_field from eventstore.unique_constraints where unique_field like '%$STEP%'"; }
[ "$(sql "select count(*) from eventstore.events2 where event_type='system.migration.done' and payload->>'name'='$STEP'")" = 1 ] \
  && ok "$STEP has its done event" || bad "$STEP not done"
"$SCRIPT" >/dev/null 2>&1 && ok "the script is idempotent: nothing left to clear" || bad "script finds something after the recovery"

echo; echo "$PASS passed, $FAIL failed"; [ "$FAIL" = 0 ]
