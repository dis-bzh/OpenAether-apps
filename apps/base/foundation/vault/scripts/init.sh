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
#   3. with the root token held in memory for a few seconds: the bootstrap and break-glass policies, the
#      Kubernetes auth method, their roles. Then the root token is REVOKED. Everything else (configure.sh, and
#      every later run) happens under the bootstrap identity, whose token lives 15 minutes.
#
# Idempotent. Run again on an initialised cluster it only checks that the bootstrap identity still works.
#
# What it can NOT repair: a first run that dies between `sys/init` and the hand-over leaves an initialised OpenBao
# nobody can configure. On any exit the root token is revoked (and a token left by a killed run is revoked at the
# next start), so that window ends in a re-init (delete the three PVCs) on a cluster that holds nothing yet. The
# calls inside it are retried, and everything that can be checked BEFORE `sys/init` is (README, Interrupted first run).
. "$(dirname "$0")/lib.sh"

SEAL_SECRET=openbao-seal-key
SHARES_SECRET=openbao-recovery-shares
DEV_SECRET=openbao-recovery-dev
KEYS_CM=openbao-operator-keys
STATE_CM=openbao-init-state   # written once the cluster is initialised and handed over: "a seal key is expected to exist"
ROOT_FILE="$OA_TMP/root.hdr"
# On a raft cluster you initialise ONE node (it becomes the leader); the others join and unseal themselves.
INIT_ADDR="${OA_INIT_ADDR:-https://openbao-0.openbao.${VAULT_NS}.svc.cluster.local:8200}"

# ── the root token never outlives this process ──────────────────────────────────────────────────────────────
revoke_root() { # idempotent; safe on any exit
  [ -f "$ROOT_FILE" ] || return 0
  none | api POST /v1/auth/token/revoke-self "$ROOT_FILE"
  if ok; then log "  root token revoked"; else log "  ⚠ the root token could NOT be revoked (HTTP $(code)): revoke it now (README, Interrupted first run)"; fi
  rm -f "$ROOT_FILE"
}
on_exit() {
  local rc=$?
  [ "$rc" = 0 ] || [ ! -f "$ROOT_FILE" ] || log "  ✗ failed inside the root window: revoking the root token before leaving (README, Interrupted first run)"
  revoke_root
  rm -f "$OA_TMP/body" "$OA_TMP"/share-*
  exit "$rc"
}
trap on_exit EXIT

# A container killed while it held the root token (OOM, SIGKILL) restarts in the SAME pod, whose memory volume still has it.
revoke_stale_root() {
  [ -f "$ROOT_FILE" ] || return 0
  log "  a root token was left by a killed run: revoking it"
  BAO_ADDR="$INIT_ADDR" revoke_root
}

# ── the seal key ──────────────────────────────────────────────────────────────────────────────────────────────
ensure_seal_key() {
  if kctl get secret "$SEAL_SECRET" >/dev/null 2>&1; then
    log "  seal key present (not touched)"; return 0
  fi
  # A missing key on a cluster that is, or was, initialised is an accident or a restore, never a first run: a fresh random
  # key would not open the data, and the next pod restart would not unseal. Stop and say so.
  if kctl get configmap "$STATE_CM" >/dev/null 2>&1 || [ "$(cluster_state)" = true ]; then
    die "the Secret $SEAL_SECRET is missing but OpenBao has been initialised: it was deleted, or this is a restore. Put the escrowed key back (README, Disaster recovery); a new one would not open the data."
  fi
  ( umask 077; head -c 32 /dev/urandom > "$OA_TMP/seal.key" )
  # create, not apply: an existing Secret (a lost race, a restored one) is never overwritten.
  if kctl create secret generic "$SEAL_SECRET" --from-file=key="$OA_TMP/seal.key" >/dev/null 2>"$OA_TMP/err"; then
    log "  seal key created (Secret $SEAL_SECRET, 32 bytes; escrow it: README, section Day 0)"
  elif grep -q AlreadyExists "$OA_TMP/err"; then
    log "  seal key created by someone else meanwhile (kept)"
  else
    rm -f "$OA_TMP/seal.key"; die "could not create the seal key Secret: $(cat "$OA_TMP/err")"
  fi
  rm -f "$OA_TMP/seal.key"
}

# ── production or development ─────────────────────────────────────────────────────────────────────────────────
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
    # Integers first: `[ two -lt 2 ]` is an ERROR, not false, and a guard that errors lets the run through.
    [[ "$THRESHOLD" =~ ^[0-9]+$ ]] || die "ConfigMap $KEYS_CM: threshold must be a whole number (got '$THRESHOLD')"
    if [ "$SHARES" -lt 3 ] || [ "$THRESHOLD" -lt 2 ] || [ "$THRESHOLD" -gt "$SHARES" ]; then
      die "ConfigMap $KEYS_CM: need at least 3 keys named pgp-*, and 2 <= threshold <= number of keys (got $SHARES keys, threshold $THRESHOLD)"
    fi
    local k
    while IFS= read -r k; do
      printf '%s' "$k" | base64 -d >/dev/null 2>&1 || die "ConfigMap $KEYS_CM: an entry is not base64 (export each key with: gpg --export <id> | base64 -w0)"
    done < <(jq -r '.[]' <<<"$PGP_JSON")
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

# ── sys/init ──────────────────────────────────────────────────────────────────────────────────────────────────
store_shares() { # NAME --from-file=…; create only: an existing Secret is a failure, not a success
  kctl create secret generic "$@" >/dev/null 2>"$OA_TMP/err" || die "could not store the recovery shares: $(cat "$OA_TMP/err")"
}

do_init() { # leaves ROOT_FILE (a header file holding the root token)
  resolve_mode
  local name body_json i=0 s root args=()
  if [ "$MODE" = prod ]; then name="$SHARES_SECRET"; else name="$DEV_SECRET"; fi
  # Everything that can be checked BEFORE the point of no return is checked here. A stale Secret would swallow the new shares.
  if kctl get secret "$name" >/dev/null 2>&1; then
    die "the Secret $name already exists: it would hide the new recovery shares. Delete it if it is a leftover, or restore from it (README, Disaster recovery)."
  fi
  body_json="$(jq -nc --argjson s "$SHARES" --argjson t "$THRESHOLD" --argjson k "$PGP_JSON" \
    'if ($k|length) > 0 then {recovery_shares:$s,recovery_threshold:$t,recovery_pgp_keys:$k} else {recovery_shares:$s,recovery_threshold:$t} end')"
  [ -n "$body_json" ] || die "could not build the sys/init request; nothing was initialised"

  BAO_ADDR="$INIT_ADDR"
  printf '%s' "$body_json" | api POST /v1/sys/init ""
  if ! ok; then
    rm -f "$OA_TMP/body"
    if [ "$(cluster_state)" = true ]; then
      die "sys/init answered HTTP $(code) and OpenBao now reports itself initialised, with no root token and no shares: re-init (README, Interrupted first run). The usual cause is a PGP key without a usable encryption subkey (gpg --list-keys must show an [E])."
    fi
    die "sys/init: HTTP $(code)"
  fi
  root="$(body | jq -r '.root_token // empty')"
  [ -n "$root" ] || { rm -f "$OA_TMP/body"; die "sys/init answered without a root token: re-init (README, Interrupted first run)"; }
  header_file "$root" "$ROOT_FILE"; root=""
  # The recovery shares: encrypted to the operators (prod) or the single development share (dev).
  while IFS= read -r s; do
    i=$((i+1)); printf '%s' "$s" > "$OA_TMP/share-$i"; args+=(--from-file="share-$i=$OA_TMP/share-$i")
  done < <(body | jq -r '.recovery_keys_base64[]')
  rm -f "$OA_TMP/body"      # the response held the root token and the shares: gone as soon as they are parsed
  [ "$i" -ge 1 ] || die "sys/init answered without recovery shares: re-init (README, Interrupted first run)"
  retry3 store_shares "$name" "${args[@]}"
  rm -f "$OA_TMP"/share-*
  if [ "$MODE" = dev ]; then
    kctl label secret "$name" openaether.io/dev-bootstrap=true --overwrite >/dev/null
    kctl annotate secret "$name" openaether.io/warning="DEVELOPMENT ONLY: a plaintext recovery share. A development cluster can not be turned into a production one: re-initialise it with the ConfigMap $KEYS_CM (operators' PGP keys) in place before it holds anything that matters." --overwrite >/dev/null
    log "  ⚠ DEVELOPMENT bootstrap: the single recovery share is in Secret $name (plaintext). Not for production."
  else
    log "  recovery shares encrypted to $SHARES operator keys, in Secret $name (threshold $THRESHOLD)"
  fi
  wait_active
}

root_window() { # with the root token: the minimum that lets the bootstrap and break-glass identities exist, then revoke the root
  local tok="$ROOT_FILE" test_hdr="$OA_TMP/boot.hdr" i proved=0
  log "  root window: bootstrap and break-glass policies, Kubernetes auth, their roles"
  retry3 put_policy openaether-bootstrap "$tok"
  retry3 put_policy openaether-breakglass "$tok"
  retry3 configure_k8s_auth "$tok"
  retry3 put_k8s_role openbao-bootstrap "$BOOTSTRAP_SAS" "$VAULT_NS" openaether-bootstrap 15m 30m "$tok"
  # The break-glass exists from the first second: it is the way back when anything later fails.
  retry3 put_k8s_role openbao-breakglass openbao-breakglass "$VAULT_NS" openaether-breakglass 10m 15m "$tok"
  if [ -n "${OA_TEST_MAKE_TOKEN_FILE:-}" ]; then   # harness only: there is no kube-apiserver to review a JWT
    printf '{"policies":["openaether-bootstrap"],"ttl":"1h","no_parent":true}' | api POST /v1/auth/token/create "$tok"   # an orphan: a child would die with the root
    ok || die "test token: HTTP $(code)"
    header_file "$(body | jq -r .auth.client_token)" "$OA_TEST_MAKE_TOKEN_FILE"
    rm -f "$OA_TMP/body"
    export OA_TEST_TOKEN_FILE="$OA_TEST_MAKE_TOKEN_FILE"
  fi
  # Prove the hand-over works, FROM THIS JOB'S OWN identity, BEFORE the root disappears. The TokenReview behind a login can
  # take a moment to be served right after the auth method was configured: three minutes of attempts.
  for i in $(seq 1 "${OA_PROOF_TRIES:-36}"); do
    if login_k8s openbao-bootstrap "$test_hdr"; then proved=1; break; fi
    sleep 5
  done
  [ "$proved" = 1 ] || die "the bootstrap role cannot log in (HTTP $(code)): the hand-over is not proven, so the root token is revoked and the run fails"
  log "  bootstrap identity logs in"
  rm -f "$test_hdr" "$OA_TMP/body"
  revoke_root
}

mark_initialised() { kctl create configmap "$STATE_CM" --from-literal=initialised="$(date -u +%FT%TZ)" >/dev/null 2>&1 || true; }

main() {
  local st="unknown" i
  log "════ OpenBao init ════"
  revoke_stale_root
  ensure_seal_key
  wait_api
  for i in $(seq 1 12); do st="$(cluster_state)"; [ "$st" != unknown ] && break; sleep 5; done
  [ "$st" != unknown ] || die "could not tell whether OpenBao is initialised (a pod did not answer for a minute): nothing was changed"
  if [ "$st" = true ]; then
    log "  already initialised"
    resolve_leader
    local hdr="$OA_TMP/boot.hdr"
    if login_k8s openbao-bootstrap "$hdr"; then
      rm -f "$hdr" "$OA_TMP/body"; mark_initialised; log "  bootstrap identity logs in: nothing to do"; return 0
    fi
    die "OpenBao is initialised but the bootstrap identity cannot log in (the first run was interrupted after sys/init?). The root token is gone by design: re-init on an empty cluster (delete the data-openbao-* PVCs), or the break-glass if the cluster holds data (README)."
  fi
  do_init
  root_window
  mark_initialised
  log "✅ init complete"
}
main "$@"
