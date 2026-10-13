#!/usr/bin/env bash
# openbao-vault-bootstrap — the idempotent configuration of OpenBao, run by Flux on every sync.
#
# It logs in as the bootstrap identity (Kubernetes auth role `openbao-bootstrap`, ServiceAccount
# openbao-vault-bootstrap, 15 minute tokens) that init.sh created before it revoked the root token.
# It never sees a root token and prints no secret: the application secrets are generated here and written
# straight to the KV engine with check-and-set 0 (create only: a re-run can not rotate what exists), their
# values travelling through the environment of one jq process and then stdin of one curl, never on a command
# line and never on stdout. Operators read them back with the `openaether-admin` identity, which the audit
# device records; they are not in any log.
. "$(dirname "$0")/lib.sh"

HDR="$OA_TMP/boot.hdr"

login_bootstrap() { # wait for init.sh: its Job may still be waiting for the StatefulSet
  local i
  for i in $(seq 1 90); do
    if login_k8s openbao-bootstrap "$HDR"; then log "  logged in as openbao-bootstrap"; return 0; fi
    [ $((i % 6)) = 0 ] && log "  waiting for the bootstrap identity (openbao-init)... ${i}/90 (HTTP $(code))"
    sleep 10
  done
  die "the bootstrap identity never logged in. openbao-init did not complete: read its log, not this one."
}

mount_engine() { # PATH JSON
  printf '%s' "$2" | api POST "/v1/sys/mounts/$1" "$HDR" || true
  case "$(code)" in 200|204) log "  mounted $1" ;; 400) log "  $1 already mounted" ;; *) die "mount $1: HTTP $(code) $(body | jq -c '.errors // empty')" ;; esac
}

# One application secret, created if absent. VALUE_JSON is read from the environment variable named in $2,
# never passed as an argument. cas=0 makes "already there" a 400 we accept.
kv_create() { # PATH ENVVAR_HOLDING_DATA_JSON
  local path="$1" var="$2"
  VAR_NAME="$var" jq -nc '{options:{cas:0},data:($ENV[env.VAR_NAME]|fromjson)}' | api POST "/v1/secret/data/$path" "$HDR"
  case "$(code)" in
    200|204) log "  secret/$path: created" ;;
    400) if body | grep -q "check-and-set"; then log "  secret/$path: already present"; else die "secret/$path: HTTP 400 $(body | jq -c '.errors // empty')"; fi ;;
    *) die "secret/$path: HTTP $(code) $(body | jq -c '.errors // empty')" ;;
  esac
}
rand() { head -c "$1" /dev/urandom | base64 | tr -d '/+=\n' | head -c "$2"; }

pki() {
  log "════ PKI ════"
  mount_engine pki '{"type":"pki","config":{"max_lease_ttl":"87600h"}}'
  # The intermediate CA's CSR is PUBLIC data (a request to sign a public key): printing it is the runbook.
  none | api GET /v1/pki/cert/ca "$HDR"
  if [ -n "$(body | jq -r '.data.certificate // empty' 2>/dev/null)" ]; then
    log "  intermediate CA present (signed and imported)"
  elif none | api LIST /v1/pki/keys "$HDR"; ok && [ -n "$(body | jq -r '.data.keys[0] // empty')" ]; then
    # one pending key is enough: every sync would otherwise add another, and the CSR already printed stays valid
    log "  intermediate CSR already generated, waiting for the signed certificate (README: PKI intermediate)"
  else
    printf '{"common_name":"OpenAether Workload Intermediate CA","ttl":"17520h"}' | api POST /v1/pki/intermediate/generate/internal "$HDR"
    local csr; csr="$(body | jq -r '.data.csr // empty')"
    if [ -n "$csr" ]; then
      log "  >>> MANUAL STEP (pki-root-offline-runbook.md): sign this CSR with the offline root, then import it."
      log "  ----- BEGIN INTERMEDIATE CSR -----"; log "$csr"; log "  ----- END INTERMEDIATE CSR -----"
    else
      log "  CSR generation answered HTTP $(code): retried on the next sync"
    fi
  fi
  printf '%s' '{"allowed_domains":["openaether.local"],"allow_subdomains":true,"allow_bare_domains":true,"allow_localhost":false,"max_ttl":"2160h","key_type":"rsa","key_bits":2048,"server_flag":true,"client_flag":true}' \
    | api POST /v1/pki/roles/openaether "$HDR"
  ok || die "pki role: HTTP $(code)"
  log "  pki role openaether"
}

policies_and_roles() {
  log "════ policies, Kubernetes auth, roles ════"
  local p
  for p in openaether-bootstrap openaether-breakglass external-secrets-read cert-manager-pki backup-snapshot secret-seeder openaether-admin openaether-reader; do
    put_policy "$p" "$HDR"
  done
  configure_k8s_auth "$HDR"
  # The bootstrap role itself, kept in step with init.sh's (which created it): this is how an existing cluster gets a changed binding.
  put_k8s_role openbao-bootstrap         "$BOOTSTRAP_SAS"  "$VAULT_NS"               openaether-bootstrap  15m 30m "$HDR"
  put_k8s_role external-secrets-operator external-secrets  foundation-pki-management external-secrets-read 1h 4h  "$HDR"
  put_k8s_role cert-manager-issuer       cert-manager      cert-manager              cert-manager-pki      1h 4h  "$HDR"
  put_k8s_role backup-snapshot           openbao-snapshot  "$VAULT_NS"               backup-snapshot       30m 1h "$HDR"
  put_k8s_role secret-seeder             openbao-seeder    "$VAULT_NS"               secret-seeder         15m 30m "$HDR"
  put_k8s_role openaether-admin          openaether-admin  "$VAULT_NS"               openaether-admin      1h 8h  "$HDR"
  put_k8s_role openaether-reader         openaether-reader "$VAULT_NS"               openaether-reader     1h 8h  "$HDR"
  put_k8s_role openbao-breakglass        openbao-breakglass "$VAULT_NS"              openaether-breakglass 10m 15m "$HDR"
}

application_secrets() {
  log "════ application secrets (create only, never printed) ════"
  mount_engine secret '{"type":"kv","options":{"version":"2"}}'
  local d
  d="$(P="$(rand 32 32)" jq -nc '{username:"admin",password:$ENV.P}')";   D="$d" kv_create grafana/admin D
  d="$(P="$(rand 32 32)" jq -nc '{password:$ENV.P}')";                    D="$d" kv_create grafana/db D
  d="$(P="$(rand 32 32)" jq -nc '{accessKey:"minio",secretKey:$ENV.P}')"; D="$d" kv_create minio/root D
  d="$(P="$(rand 48 48)" jq -nc '{passphrase:$ENV.P}')";                  D="$d" kv_create longhorn/encryption D
  d="$(P="$(rand 32 32)" jq -nc '{username:"zitadel",password:$ENV.P}')"; D="$d" kv_create zitadel/db D
  # Zitadel's master key must be EXACTLY 32 characters.
  d="$(K="$(rand 32 32)" jq -nc '{masterkey:$ENV.K}')";                   D="$d" kv_create zitadel/masterkey D
  # The restic repositories' password. Without it the backups are undecryptable the day OpenBao is lost, so an
  # operator reads it ONCE with the openaether-admin identity and escrows it (README): it is not in any log.
  d="$(P="$(rand 48 48)" jq -nc '{password:$ENV.P}')";                    D="$d" kv_create backup/restic D
  d=""
}

main() {
  log "════ OpenBao configure ════"
  wait_api
  resolve_leader
  login_bootstrap
  pki
  policies_and_roles
  application_secrets
  rm -f "$HDR"
  log "✅ OpenBao configured"
}
main "$@"
