#!/usr/bin/env bash
# The Zitadel configuration of apps/base/identity/zitadel, run through the REAL Zitadel (the pinned chart's appVersion
# in a container, against a real PostgreSQL), on what the manifests really produce: the HelmRelease values through
# the chart, and the ExternalSecret template filled in. It answers what a manifest check cannot:
#   - upstream's first admin (zitadel-admin / Password1!) really logs in WITHOUT the change, and does not WITH it;
#   - the first-admin password the bootstrap generates is accepted by the 15-character policy;
#   - the instance policies (complexity, lockout, MFA, no self-registration, token lifetimes) land on a new instance;
#   - on an EXISTING instance those settings change nothing, and scripts/zitadel-apply-policies.py applies them;
#   - the argon2id switch keeps bcrypt-hashed passwords working with the `bcrypt` verifier, and locks everybody
#     out without it (the trap), and re-hashes at the next login;
#   - 5 wrong passwords lock the account and the machine PAT unlocks it (the break-glass);
#   - the logs are JSON, the access log is on stdout, and no token reaches it.
# Needs docker (pulls ghcr.io/zitadel/zitadel and postgres), helm and the network (the chart), kubectl, curl, jq,
# python3 with PyYAML. Rung: a real Zitadel and a real Postgres in containers, no Kubernetes, no OpenBao, no Istio.
#   Usage: scripts/dev/test-zitadel-config.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
for bin in docker helm kubectl curl jq openssl python3; do command -v "$bin" >/dev/null || { echo "$bin is required" >&2; exit 2; }; done
W="$(mktemp -d)"; NET="zcfg$$"; PORT="${ZITADEL_TEST_PORT:-18080}"; A="http://localhost:$PORT"
PASS=0; FAIL=0
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; PASS=$((PASS + 1)); }
bad() { printf '  \033[31m✗\033[0m %s\n' "$*"; FAIL=$((FAIL + 1)); }
cleanup() { docker rm -f "$NET-z" "$NET-pg" >/dev/null 2>&1; docker network rm "$NET" >/dev/null 2>&1; rm -rf "$W"; }
trap cleanup EXIT
chmod 755 "$W"; mkdir -p "$W/out" "$W/login"; chmod 777 "$W/out"

# ── 1. what the manifests produce ────────────────────────────────────────────────────────────────────────────
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
        open(f"{w}/chart", "w").write(f'{s["chart"]} {s["version"]}\n{d["spec"]["chart"]["spec"]["sourceRef"]["name"]}\n')
    if d["kind"] == "HelmRepository":
        open(f"{w}/repo", "w").write(d["spec"]["url"])
    if d["kind"] == "ExternalSecret" and d["metadata"]["name"] == "zitadel-secret-config":
        open(f"{w}/es.tpl", "w").write(d["spec"]["target"]["template"]["data"]["config-yaml"])
PY
read -r CHART VERSION < <(head -1 "$W/chart"); REPO="$(cat "$W/repo")"
helm template zitadel "$CHART" --repo "$REPO" --version "$VERSION" -n services-identity -f "$W/values.yaml" > "$W/rendered.yaml" \
  && ok "the chart $CHART $VERSION renders with the shipped values" || { bad "helm template failed"; exit 1; }
APPVER="$(helm show chart "$CHART" --repo "$REPO" --version "$VERSION" | sed -n 's/^appVersion: *//p')"
IMAGE="ghcr.io/zitadel/zitadel:$APPVER"
GOOD_PW='Xk3pQ9vLm2Rt7WnB5cDf8GhJ4sAa1!'   # the shape configure.sh generates, a fixed value for the test
python3 -I - "$W" "$GOOD_PW" <<'PY'
import sys, yaml
w, pw = sys.argv[1:3]
for d in yaml.safe_load_all(open(f"{w}/rendered.yaml")):
    if d and d["kind"] == "ConfigMap" and "zitadel-config-yaml" in (d.get("data") or {}):
        cfg = d["data"]["zitadel-config-yaml"]
open(f"{w}/cfg.yaml", "w").write(cfg)
tpl = open(f"{w}/es.tpl").read()
open(f"{w}/secret.yaml", "w").write(tpl.replace("{{ .password }}", "dbpass").replace("{{ .adminpw }}", pw))
# What the manifests produced before this change: no policies, no first-admin block, no hasher, no log settings.
base = yaml.safe_load(cfg)
for k in ("DefaultInstance", "SystemDefaults", "Log", "LogStore"):
    base.pop(k, None)
base["FirstInstance"]["Org"].pop("Human", None)
yaml.safe_dump(base, open(f"{w}/cfg-baseline.yaml", "w"))
noverif = yaml.safe_load(cfg)
noverif["SystemDefaults"]["PasswordHasher"].pop("Verifiers")
yaml.safe_dump(noverif, open(f"{w}/cfg-noverif.yaml", "w"))
yaml.safe_dump({"Database": {"Postgres": {"User": {"Password": "dbpass"}, "Admin": {"Password": "dbpass"}}}},
               open(f"{w}/secret-nopw.yaml", "w"))
PY
cat > "$W/override.yaml" <<YAML
ExternalDomain: localhost
ExternalPort: $PORT
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
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$W/login/key.pem" -out "$W/login/tls.crt" -subj "/CN=login-client" -days 2 2>/dev/null
chmod 755 "$W/login"; chmod 644 "$W/login"/*

# ── helpers ──────────────────────────────────────────────────────────────────────────────────────────────────
docker network create "$NET" >/dev/null
pg_up() {
  docker rm -f "$NET-pg" >/dev/null 2>&1
  # PostgreSQL 17.11, by digest: a test fixture, not what the platform runs (CNPG's own image).
  docker run -d --name "$NET-pg" --network "$NET" -e POSTGRES_PASSWORD=postgres \
    postgres:17-alpine@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24 >/dev/null
  for _ in $(seq 40); do docker exec "$NET-pg" pg_isready -U postgres >/dev/null 2>&1 && return 0; sleep 1; done; return 1
}
zit() { # start|start-from-init CONFIG SECRET [docker args]
  local cmd="$1" cfg="$2" sec="$3"; shift 3
  docker rm -f "$NET-z" >/dev/null 2>&1
  local steps=(); [ "$cmd" = start-from-init ] && steps=(--steps "/w/$cfg" --steps "/w/$sec" --steps /w/override.yaml)
  docker run -d --name "$NET-z" --network "$NET" -p "$PORT:8080" -v "$W:/w" -v "$W/login:/secrets/login-client:ro" \
    -e ZITADEL_MASTERKEY=MasterkeyNeedsToHave32Characters -e ZITADEL_FIRSTINSTANCE_MACHINEKEYPATH=/w/out/sa.json \
    -e ZITADEL_FIRSTINSTANCE_PATPATH=/w/out/pat "$@" "$IMAGE" "$cmd" --masterkeyFromEnv --tlsMode disabled \
    --config "/w/$cfg" --config "/w/$sec" --config /w/override.yaml "${steps[@]}" >/dev/null
  for _ in $(seq 120); do curl -sf "$A/debug/healthz" >/dev/null 2>&1 && return 0; sleep 2; done
  docker logs "$NET-z" 2>&1 | tail -15; return 1
}
# The chart re-runs the setup Job on every upgrade: against an initialised instance it must succeed and change nothing.
setup_again() { # CONFIG SECRET
  docker run --rm --network "$NET" -v "$W:/w" -v "$W/login:/secrets/login-client:ro" -e ZITADEL_MASTERKEY=MasterkeyNeedsToHave32Characters \
    -e ZITADEL_FIRSTINSTANCE_MACHINEKEYPATH=/tmp/sa.json -e ZITADEL_FIRSTINSTANCE_PATPATH=/tmp/pat "$IMAGE" setup --masterkeyFromEnv \
    --config "/w/$1" --config "/w/$2" --config /w/override.yaml --steps "/w/$1" --steps "/w/$2" --steps /w/override.yaml >"$W/setup.log" 2>&1
}
pat() { cat "$W/out/pat"; }
api() { curl -s -H "Authorization: Bearer $(pat)" -H 'Content-Type: application/json' "$@"; }
# login NAME PASSWORD -> prints the HTTP status of a password check through the session API (retries a starting pod)
login() {
  local code _
  for _ in $(seq 30); do
    code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$A/v2/sessions" -H "Authorization: Bearer $(pat)" \
      -H 'Content-Type: application/json' -d "{\"checks\":{\"user\":{\"loginName\":\"$1\"},\"password\":{\"password\":\"$2\"}}}")"
    [ "$code" = 503 ] || break; sleep 2
  done; echo "$code"
}
ADMIN='zitadel-admin@zitadel.localhost'
policy() { api "$A/admin/v1/policies/$1" | jq -r ".policy.$2 // \"unset\""; }
sql() { docker exec "$NET-pg" psql -U postgres -d zitadel -Atc "$1"; }

# ── 2. upstream's defaults: the finding, reproduced ──────────────────────────────────────────────────────────
echo "=== an instance created from the manifests as they were (no first-admin password, no policies)"
pg_up && zit start-from-init cfg-baseline.yaml secret-nopw.yaml && ok "Zitadel $APPVER is up on an empty database" || { bad "no start"; exit 1; }
[ "$(login "$ADMIN" 'Password1!')" = 201 ] && ok "upstream's public Password1! logs in as the IAM_OWNER admin (the gap, reproduced)" || bad "Password1! did not log in: the test no longer shows the gap"
[ "$(policy password/complexity minLength)" = 8 ] && ok "the password policy is upstream's 8 characters" || bad "complexity is not the upstream default"
[ "$(policy login allowRegister)" = true ] && ok "self-registration is open by default" || bad "allowRegister default changed"

echo "=== the same instance, restarted with the shipped values (an EXISTING instance)"
zit start cfg-noverif.yaml secret-nopw.yaml && ok "restart with argon2id and NO bcrypt verifier" || bad "no start (noverif)"
[ "$(login "$ADMIN" 'Password1!')" != 201 ] && ok "...nobody can log in: the bcrypt verifier is what keeps existing passwords working" || bad "login worked without the verifier"
zit start cfg.yaml secret-nopw.yaml && ok "restart with the shipped values" || bad "no start (shipped)"
[ "$(login "$ADMIN" 'Password1!')" = 201 ] && ok "the existing bcrypt password still logs in" || bad "the existing admin cannot log in after the hasher switch"
setup_again cfg.yaml secret.yaml && [ "$(login "$ADMIN" 'Password1!')" = 201 ] \
  && ok "the setup Job re-run on this instance with the first-admin password in its config succeeds and leaves the password alone" \
  || bad "setup on an initialised instance: $(tail -3 "$W/setup.log")"
[ "$(sql "select count(*) from eventstore.events2 where event_type='user.human.password.hash.updated' and payload->>'encodedHash' like '\$argon2id\$v=19\$m=32768,%'")" -ge 1 ] \
  && ok "...and its hash was rewritten as argon2id (m=32768)" || bad "no argon2id re-hash"
[ "$(policy password/complexity minLength)" = 8 ] && [ "$(policy login allowRegister)" = true ] \
  && ok "DefaultInstance.* changed NOTHING on the existing instance (it is read once, at creation)" || bad "an existing instance picked up DefaultInstance"
export ZITADEL_PAT="$(pat)"
python3 "$ROOT/scripts/zitadel-apply-policies.py" --url "$A" > "$W/dry.out"; rc=$?
{ [ "$rc" = 0 ] && grep -q 'PasswordComplexityPolicy.minLength: 8 -> 15' "$W/dry.out" && grep -q 'dry run' "$W/dry.out" && [ "$(policy password/complexity minLength)" = 8 ]; } \
  && ok "zitadel-apply-policies.py: the dry run lists the differences and changes nothing" || bad "dry run: $(cat "$W/dry.out")"
python3 "$ROOT/scripts/zitadel-apply-policies.py" --url "$A" --apply > "$W/apply.out"; rc=$?
{ [ "$rc" = 0 ] && [ "$(policy password/complexity minLength)" = 15 ] && [ "$(policy login allowRegister)" = unset ] \
  && [ "$(policy login forceMfa)" = true ] && [ "$(policy lockout maxPasswordAttempts)" = 5 ] \
  && [ "$(api "$A/admin/v1/settings/oidc" | jq -r .settings.accessTokenLifetime)" = 3600s ]; } \
  && ok "...--apply sets complexity 15, lockout 5, MFA forced, registration closed, 1 h access tokens" || bad "apply: $(cat "$W/apply.out")"
python3 "$ROOT/scripts/zitadel-apply-policies.py" --url "$A" --apply > "$W/again.out"
[ "$(grep -c 'already as wanted' "$W/again.out")" = 4 ] && ok "...and a second run changes nothing" || bad "not idempotent: $(cat "$W/again.out")"
[ "$(policy login allowUsernamePassword)" = true ] && ok "...the login policy's other fields are kept (a PUT replaces the whole policy)" || bad "allowUsernamePassword was dropped"

echo "=== lockout and the break-glass"
for _ in 1 2 3 4 5; do login "$ADMIN" 'wrong-wrong-wrong-1A!' >/dev/null; done
[ "$(login "$ADMIN" 'Password1!')" != 201 ] && ok "5 wrong passwords lock the account, even for the right one" || bad "no lockout"
UID_="$(api -X POST "$A/v2/users" -d '{"filters":[{"userNameFilter":{"userName":"zitadel-admin","method":"TEXT_FILTER_METHOD_EQUALS"}}]}' | jq -r '.result[0].userId')"
api -X POST "$A/v2/users/$UID_/unlock" -d '{}' >/dev/null
[ "$(login "$ADMIN" 'Password1!')" = 201 ] && ok "the iam-admin machine PAT unlocks it (docs/zitadel-hardening.md)" || bad "unlock failed"

# ── 3. a NEW instance from the shipped manifests ─────────────────────────────────────────────────────────────
echo "=== a new instance from the shipped values and the ExternalSecret template (what a fresh cluster gets)"
pg_up && zit start-from-init cfg.yaml secret.yaml && ok "a new instance is created with the shipped values" || { bad "no start (fresh)"; exit 1; }
[ "$(login "$ADMIN" 'Password1!')" != 201 ] && ok "Password1! is refused" || bad "Password1! still works on a new instance"
[ "$(login "$ADMIN" "$GOOD_PW")" = 201 ] && ok "the generated first-admin password (15+ characters, four classes) is accepted and logs in" || bad "the generated password is refused: the setup would fail on a cluster"
[ "$(sql "select (payload->>'changeRequired')::text from eventstore.events2 where event_type='user.human.added' limit 1")" = true ] \
  && ok "the first login must change the password" || bad "PasswordChangeRequired is not set"
[ "$(policy password/complexity minLength)" = 15 ] && [ "$(policy lockout maxPasswordAttempts)" = 5 ] && [ "$(policy lockout maxOtpAttempts)" = 5 ] \
  && ok "the instance has the 15-character policy and the lockout at 5" || bad "policies did not land"
[ "$(policy login forceMfa)" = true ] && [ "$(policy login allowRegister)" = unset ] && [ "$(policy login allowExternalIdp)" = unset ] \
  && ok "MFA is forced; self-registration and external IdPs are off" || bad "login policy did not land"
[ "$(policy login passwordCheckLifetime)" = 43200s ] && [ "$(api "$A/admin/v1/settings/oidc" | jq -r .settings.refreshTokenExpiration)" = 2592000s ] \
  && ok "check lifetime 12 h, refresh token 30 days" || bad "lifetimes did not land"
export ZITADEL_PAT="$(pat)"
python3 "$ROOT/scripts/zitadel-apply-policies.py" --url "$A" > "$W/fresh.out"
[ "$(grep -c 'already as wanted' "$W/fresh.out")" = 4 ] && ok "zitadel-apply-policies.py finds nothing to change: the script and the Helm values agree" || bad "script and values disagree: $(cat "$W/fresh.out")"
docker logs "$NET-z" > "$W/z.log" 2>&1   # the container that created the instance and served the checks above
zit start cfg.yaml secret.yaml && [ "$(login "$ADMIN" "$GOOD_PW")" = 201 ] \
  && ok "the Deployment's config (the same secret file, first-admin password included) starts and the password is unchanged" \
  || bad "start with the first-admin password in the secret config failed"
setup_again cfg.yaml secret.yaml && [ "$(login "$ADMIN" "$GOOD_PW")" = 201 ] && ok "a second setup run (the next Helm upgrade) succeeds and keeps the password" \
  || bad "the second setup run failed: $(tail -3 "$W/setup.log")"
# An OIDC logout carries the ID token in the query string (id_token_hint): see whether the access log keeps it.
curl -s -o /dev/null "$A/oidc/v1/end_session?id_token_hint=CANARY.id-token.value&post_logout_redirect_uri=http%3A%2F%2Fx.invalid"
docker logs "$NET-z" >> "$W/z.log" 2>&1
[ "$(grep -c '^{' "$W/z.log")" -gt 20 ] && ! grep -v '^{' "$W/z.log" | grep -q 'level=' && ok "the logs are JSON" || bad "the logs are not JSON"
grep -q 'log record emitted' "$W/z.log" && grep -q '"requestUrl' "$W/z.log" && ok "the HTTP access log is on stdout" || bad "no access log on stdout"
# Known and handled downstream: the access log keeps the request URL, query string included, so Alloy blanks such values
# before they reach Loki (alloy.yaml, scripts/dev/test-alloy-scrub.sh). If a Zitadel upgrade stops logging it, the scrub is redundant.
if grep -qF 'CANARY.id-token.value' "$W/z.log"; then ok "...the access log keeps an id_token_hint query string (Alloy's loki.process scrub blanks it)"
else ok "...Zitadel no longer logs the id_token_hint query string (Alloy's scrub is then redundant)"; fi
[ -z "${ZITADEL_TEST_KEEP_LOG:-}" ] || cp "$W/z.log" "$ZITADEL_TEST_KEEP_LOG"
if grep -qF "$(pat)" "$W/z.log" || grep -qF "$GOOD_PW" "$W/z.log"; then bad "a token or the admin password is in the logs"; else ok "...and neither the machine PAT nor the admin password is in them"; fi

echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
