#!/usr/bin/env bash
# openbao-init — the ONE-SHOT part of the OpenBao bootstrap, and the only part that ever holds the root token.
#
#   1. the static seal key: created once as the Secret `openbao-seal-key` (32 random bytes), never overwritten,
#      never printed. The StatefulSet mounts it and OpenBao unseals itself with it on every start: there is
#      no unsealer, no Shamir share and no root token stored in the cluster.
#   2. `sys/init` with RECOVERY shares. Production (the ConfigMap `openbao-operator-keys` exists): the shares
#      come back encrypted to the operators' PGP public keys and are kept as such in `openbao-recovery-shares`
#      (useless without the operators' private keys). Development (no such ConfigMap): ONE plaintext share in
#      `openbao-recovery-dev`, labelled as such: the cluster works, and says it is not production.
#   3. with the root token held in memory for a few seconds: the bootstrap policy, the Kubernetes auth method,
#      the bootstrap role. Then the root token is REVOKED. Everything else (configure.sh, and every later
#      run) happens under the bootstrap identity, whose token lives 15 minutes.
#
# Idempotent. Run again on an initialised cluster it only checks that the bootstrap identity still works.
#
# What it can NOT repair: a first run that dies between `sys/init` and the revocation leaves an initialised
# OpenBao nobody can configure. On ANY exit the root token is revoked (never left in the pod's memory), so that window
# ends in a re-init (delete the three PVCs) on a cluster that holds nothing yet, or `generate-root` with the recovery
# shares (README). The calls inside it are retried, because that is the cheap way to not need either.
. "$(dirname "$0")/lib.sh"

SEAL_SECRET=openbao-seal-key
SHARES_SECRET=openbao-recovery-shares
DEV_SECRET=openbao-recovery-dev
KEYS_CM=openbao-operator-keys
# On a raft cluster you initialise ONE node (it becomes the leader); the others join and unseal themselves.
INIT_ADDR="${OA_INIT_ADDR:-https://openbao-0.openbao.${VAULT_NS}.svc.cluster.local:8200}"

ensure_seal_key() {
  if kctl get secret "$SEAL_SECRET" >/dev/null 2>&1; then
    log "  seal key present (not touched)"; return 0
  fi
  ( umask 077; head -c 32 /dev/urandom > "$OA_TMP/seal.key" )
  # create, not apply: an existing Secret (a lost race, a restored one) is never overwritten.
  if kctl create secret generic "$SEAL_SECRET" --from-file=key="$OA_TMP/seal.key" >/dev/null 2>"$OA_TMP/err"; then
    log "  seal key created (Secret $SEAL_SECRET, 32 bytes; escrow it: README, section Escrow)"
  elif grep -q AlreadyExists "$OA_TMP/err"; then
    log "  seal key created by someone else meanwhile (kept)"
  else
    rm -f "$OA_TMP/seal.key"; die "could not create the seal key Secret: $(cat "$OA_TMP/err")"
  fi
  rm -f "$OA_TMP/seal.key"
}

# The operators' PGP public keys (base64, one per entry) and the threshold, from the ConfigMap, or "dev".
resolve_mode() {
  MODE=dev; PGP_JSON='[]'; THRESHOLD=1; SHARES=1
  # Only "not found" means development. Any other failure (RBAC, API down) must stop here: a production
  # cluster silently initialised in development mode would keep a plaintext recovery share.
  if ! kctl get configmap "$KEYS_CM" -o json > "$OA_TMP/cm.json" 2>"$OA_TMP/err"; then
    grep -q NotFound "$OA_TMP/err" || die "cannot read the ConfigMap $KEYS_CM: $(cat "$OA_TMP/err")"
    rm -f "$OA_TMP/cm.json"
  fi
  if [ -s "$OA_TMP/cm.json" ]; then
    PGP_JSON="$(jq -c '[.data // {} | to_entries[] | select(.key | startswith("pgp-")) | .value | gsub("\\s";"")] | map(select(length>0))' "$OA_TMP/cm.json")"
    SHARES="$(jq 'length' <<<"$PGP_JSON")"
    THRESHOLD="$(jq -r '.data.threshold // empty' "$OA_TMP/cm.json")"
    [ -n "$THRESHOLD" ] || THRESHOLD=$(( SHARES / 2 + 1 ))
    if [ "$SHARES" -lt 3 ] || [ "$THRESHOLD" -lt 2 ] || [ "$THRESHOLD" -gt "$SHARES" ]; then
      die "ConfigMap $KEYS_CM: need at least 3 keys named pgp-*, and 2 <= threshold <= number of keys (got $SHARES keys, threshold $THRESHOLD)"
    fi
    MODE=prod
  fi
  log "  mode: $MODE ($SHARES recovery share(s), threshold $THRESHOLD)"
}

wait_active() { # the node we initialised unseals itself with the static key, then wins the election
  local i c
  for i in $(seq 1 60); do
    c="$(curl -s "${CURL_CA[@]}" -o /dev/null -w '%{http_code}' "${BAO_ADDR}/v1/sys/health" || true)"
    [ "$c" = 200 ] && { log "  active and unsealed"; return 0; }
    sleep 5
  done
  die "the initialised node did not become active and unsealed in 5 minutes (health=$c): wrong seal key, or the Secret $SEAL_SECRET is not mounted"
}

do_init() { # sets ROOT_FILE (a header file holding the root token)
  resolve_mode
  BAO_ADDR="$INIT_ADDR"
  jq -nc --argjson s "$SHARES" --argjson t "$THRESHOLD" --argjson k "$PGP_JSON" \
    'if ($k|length) > 0 then {recovery_shares:$s,recovery_threshold:$t,recovery_pgp_keys:$k} else {recovery_shares:$s,recovery_threshold:$t} end' \
    | api POST /v1/sys/init ""
  ok || die "sys/init: HTTP $(code) $(body | jq -c '.errors // empty' 2>/dev/null)"
  local root; root="$(body | jq -r '.root_token // empty')"
  [ -n "$root" ] || die "sys/init answered without a root token"
  header_file "$root" "$OA_TMP/root.hdr"; ROOT_FILE="$OA_TMP/root.hdr"; root=""

  # The recovery shares: encrypted to the operators (prod) or the single development share (dev).
  local i=0 args=() name
  if [ "$MODE" = prod ]; then name="$SHARES_SECRET"; else name="$DEV_SECRET"; fi
  while IFS= read -r s; do
    i=$((i+1)); ( umask 077; printf '%s' "$s" > "$OA_TMP/share-$i" ); args+=(--from-file="share-$i=$OA_TMP/share-$i")
  done < <(body | jq -r '.recovery_keys_base64[]')
  kctl create secret generic "$name" "${args[@]}" >/dev/null 2>"$OA_TMP/err" \
    || { grep -q AlreadyExists "$OA_TMP/err" || die "could not store the recovery shares: $(cat "$OA_TMP/err")"; }
  if [ "$MODE" = dev ]; then
    kctl label secret "$name" openaether.io/dev-bootstrap=true --overwrite >/dev/null
    kctl annotate secret "$name" openaether.io/warning="DEVELOPMENT ONLY: a plaintext recovery share. Provide the ConfigMap $KEYS_CM (operators' PGP keys) before this cluster holds anything that matters." --overwrite >/dev/null
    log "  ⚠ DEVELOPMENT bootstrap: the single recovery share is in Secret $name (plaintext). Not for production."
  else
    log "  recovery shares encrypted to $SHARES operator keys, in Secret $name (threshold $THRESHOLD)"
  fi
  rm -f "$OA_TMP"/share-* "$OA_TMP/body"
  wait_active
}

ROOT_FILE=""
revoke_root() { # idempotent; safe on any exit
  [ -n "$ROOT_FILE" ] && [ -f "$ROOT_FILE" ] || return 0
  none | api POST /v1/auth/token/revoke-self "$ROOT_FILE"
  if ok; then log "  root token revoked"; else log "  ⚠ the root token could NOT be revoked (HTTP $(code)): revoke it now (README, Interrupted first run)"; fi
  rm -f "$ROOT_FILE"
  ROOT_FILE=""
}
on_exit() {
  local rc=$?
  if [ "$rc" != 0 ] && [ -n "$ROOT_FILE" ] && [ -f "$ROOT_FILE" ]; then
    log "  ✗ failed inside the root window: revoking the root token before leaving (the cluster stays initialised; README, Interrupted first run)"
  fi
  revoke_root
  exit "$rc"
}
trap on_exit EXIT

root_window() { # with the root token: the minimum that lets the bootstrap identity take over, then revoke the root
  local tok="$ROOT_FILE" test_hdr="$OA_TMP/boot.hdr" i proved=0
  log "  root window: bootstrap policy, Kubernetes auth, bootstrap role"
  retry3 put_policy openaether-bootstrap "$tok"
  retry3 configure_k8s_auth "$tok"
  retry3 put_k8s_role openbao-bootstrap "$BOOTSTRAP_SAS" "$VAULT_NS" openaether-bootstrap 15m 30m "$tok"
  if [ -n "${OA_TEST_MAKE_TOKEN_FILE:-}" ]; then   # harness only: there is no kube-apiserver to review a JWT
    printf '{"policies":["openaether-bootstrap"],"ttl":"1h","no_parent":true}' | api POST /v1/auth/token/create "$tok"   # an orphan: a child would die with the root
    ok || die "test token: HTTP $(code)"
    header_file "$(body | jq -r .auth.client_token)" "$OA_TEST_MAKE_TOKEN_FILE"
    export OA_TEST_TOKEN_FILE="$OA_TEST_MAKE_TOKEN_FILE"
  fi
  # Prove the hand-over works, FROM THIS JOB'S OWN identity, BEFORE the root disappears. The TokenReview behind a login can
  # take a moment to be served right after the auth method was configured, hence the retries.
  for i in $(seq 1 12); do
    if login_k8s openbao-bootstrap "$test_hdr"; then proved=1; break; fi
    sleep 5
  done
  [ "$proved" = 1 ] || die "the bootstrap role cannot log in (HTTP $(code)): the hand-over is not proven, so the root token is revoked and the run fails"
  log "  bootstrap identity logs in"
  rm -f "$test_hdr"
  revoke_root
}

main() {
  log "════ OpenBao init ════"
  ensure_seal_key
  wait_api
  if [ "$(initialized)" = true ]; then
    log "  already initialised"
    resolve_leader
    local hdr="$OA_TMP/boot.hdr"
    if login_k8s openbao-bootstrap "$hdr"; then
      rm -f "$hdr"; log "  bootstrap identity logs in: nothing to do"; return 0
    fi
    die "OpenBao is initialised but the bootstrap identity cannot log in (the first run was interrupted after sys/init?). The root token is gone by design: re-init on an empty cluster (delete the data-openbao-* PVCs) or generate-root with the recovery shares (README)."
  fi
  do_init
  root_window
  log "✅ init complete"
}
main "$@"
