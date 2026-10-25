#!/usr/bin/env bash
# openbao-vault-bootstrap — the idempotent configuration of OpenBao, run by a Job that Flux re-creates (script change, or ttl plus one sync).
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
  case "$(code)" in
    200|204) log "  mounted $1" ;;
    400) if body | grep -q "already in use"; then log "  $1 already mounted"; else die "mount $1: HTTP 400 $(body | jq -c '.errors // empty')"; fi ;;
    *) die "mount $1: HTTP $(code) $(body | jq -c '.errors // empty')" ;;
  esac
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

# PKI key types (ANSSI IGC-BP-03, NT35-R8). ec = ECDSA P-384 intermediate and P-256 leaves; rsa = the documented legacy variant,
# RSA-3072 with PSS signatures, for clients that cannot do ECDSA (then set the same on the Certificate: README, "PKI").
# The role only constrains the key of the CSR it signs, so an existing RSA intermediate keeps signing EC leaves.
: "${OA_PKI_KEY_TYPE:=ec}"
# Off by default: a newly imported intermediate is NOT the default issuer (the role keeps signing with the old one) until
# `pki/config/issuers default=<id>` is written, which the admin identity cannot do. On, the latest import becomes the default.
: "${OA_PKI_DEFAULT_FOLLOWS_LATEST:=false}"

pki() {
  log "════ PKI ════"
  local int_params leaf_params
  case "$OA_PKI_KEY_TYPE" in
    ec)  int_params='"key_type":"ec","key_bits":384';  leaf_params='"key_type":"ec","key_bits":256,"use_pss":false' ;;
    rsa) int_params='"key_type":"rsa","key_bits":3072'; leaf_params='"key_type":"rsa","key_bits":3072,"use_pss":true' ;;
    *) die "OA_PKI_KEY_TYPE must be ec or rsa, got '$OA_PKI_KEY_TYPE'" ;;
  esac
  mount_engine secret '{"type":"kv","options":{"version":"2"}}' # before the CSR, which is stored there
  mount_engine pki '{"type":"pki","config":{"max_lease_ttl":"87600h"}}'
  # The intermediate CA's CSR is PUBLIC data (a request to sign a public key): printing it is the runbook.
  none | api GET /v1/pki/cert/ca "$HDR"
  if [ -n "$(body | jq -r '.data.certificate // empty' 2>/dev/null)" ]; then
    log "  intermediate CA present (signed and imported)"
  elif none | api LIST /v1/pki/keys "$HDR"; ok && [ -n "$(body | jq -r '.data.keys[0] // empty')" ]; then
    # one pending key is enough: every sync would otherwise add another, and the CSR already printed stays valid
    log "  intermediate CSR already generated, waiting for the signed certificate (README: PKI intermediate)"
  else
    printf '{"common_name":"OpenAether Workload Intermediate CA","ttl":"17520h",%s}' "$int_params" | api POST /v1/pki/intermediate/generate/internal "$HDR"
    local csr; csr="$(body | jq -r '.data.csr // empty')"
    if [ -n "$csr" ]; then
      log "  >>> MANUAL STEP (pki-root-offline-runbook.md): sign this CSR with the offline root, then import it."
      log "  ----- BEGIN INTERMEDIATE CSR -----"; log "$csr"; log "  ----- END INTERMEDIATE CSR -----"
      # The Job and its log are deleted after ttlSecondsAfterFinished, the offline signing takes longer: keep the (public) CSR in KV.
      local d; d="$(C="$csr" jq -nc '{csr:$ENV.C}')"; D="$d" kv_create pki/intermediate-csr D
    else
      log "  CSR generation answered HTTP $(code): retried on the next sync"
    fi
  fi
  # Server leaves only: no clientAuth, no key encipherment, no wildcard, no bare domain, no IP, no CN required (the Certificate
  # lists its names as SANs). Names are checked against the CSR's SANs (the sign endpoint ignores `alt_names` with use_csr_sans).
  printf '{"allowed_domains":["openaether.local"],"allow_subdomains":true,"allow_bare_domains":false,"allow_wildcard_certificates":false,"allow_ip_sans":false,"allow_localhost":false,"require_cn":false,"max_ttl":"2160h",%s,"server_flag":true,"client_flag":false,"key_usage":["DigitalSignature"],"ext_key_usage":[]}' "$leaf_params" \
    | api POST /v1/pki/roles/openaether "$HDR"
  ok || die "pki role: HTTP $(code)"
  log "  pki role openaether ($OA_PKI_KEY_TYPE)"
  pki_revocation
}

# Revocation information (ANSSI NT35-R33, IGC-BP-06): the leaves carry the CRL/OCSP/AIA URLs of the in-cluster service, the CRL is
# rebuilt on its own, and expired certificates leave the store. No client in the platform checks revocation today (README, "PKI").
pki_revocation() {
  local u="https://openbao.${VAULT_NS}.svc.cluster.local:8200/v1/pki" r
  for r in \
    "config/urls|$(U="$u" jq -nc '{issuing_certificates:[$ENV.U+"/ca"],crl_distribution_points:[$ENV.U+"/crl"],ocsp_servers:[$ENV.U+"/ocsp"]}')" \
    'config/crl|{"auto_rebuild":true,"enable_delta":true,"expiry":"72h","delta_rebuild_interval":"15m","auto_rebuild_grace_period":"12h"}' \
    'config/auto-tidy|{"enabled":true,"interval_duration":"12h","safety_buffer":"72h","tidy_cert_store":true,"tidy_revoked_certs":true,"tidy_revoked_cert_issuer_associations":true}'; do
    printf '%s' "${r#*|}" | api POST "/v1/pki/${r%%|*}" "$HDR"
    ok || die "pki ${r%%|*}: HTTP $(code) $(body | jq -c '.errors // empty')"
  done
  log "  pki revocation URLs, CRL rebuild and auto-tidy set"
  if [ "$OA_PKI_DEFAULT_FOLLOWS_LATEST" = true ]; then
    none | api GET /v1/pki/config/issuers "$HDR"
    local def; def="$(body | jq -r '.data.default // empty')"
    if [ -n "$def" ]; then
      D="$def" jq -nc '{default:$ENV.D,default_follows_latest_issuer:true}' | api POST /v1/pki/config/issuers "$HDR"
      ok || die "pki config/issuers: HTTP $(code) $(body | jq -c '.errors // empty')"
      log "  the default issuer follows the latest import"
    fi
  fi
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
  # The first human admin of Zitadel (FirstInstance), instead of upstream's public Password1!. `rand` is
  # alphanumeric, so the suffix guarantees the four character classes of Zitadel's password policy (a missing
  # class makes the setup Job fail, and with it the whole identity layer). Forced to change at the first login.
  d="$(P="$(rand 28 28)Aa1!" jq -nc '{username:"zitadel-admin",password:$ENV.P}')"; D="$d" kv_create zitadel/admin D
  # Signs the Zitadel login UI's session cookie (ZITADEL_SESSION_COOKIE_SECRET, 32 characters at least; v4.19.5).
  d="$(P="$(rand 48 48)" jq -nc '{secret:$ENV.P}')";                      D="$d" kv_create zitadel/session-cookie D
  # The restic repositories' password. Without it the backups are undecryptable the day OpenBao is lost, so an
  # operator reads it ONCE with the openaether-admin identity and escrows it (README): it is not in any log.
  d="$(P="$(rand 48 48)" jq -nc '{password:$ENV.P}')";                    D="$d" kv_create backup/restic D
  # The Forgejo git forge (apps/base/forgejo, apps/base/forgejo-db): the database role's password (the CNPG bootstrap Secret
  # and the app's twin both read it), the initial admin's password (Forgejo creates the account ONCE and asks for a change
  # at the first login, passwordMode initialOnlyRequireReset; `rand` is alphanumeric, the suffix gives the chart's admin
  # password the symbols and digits it may require), and a PLACEHOLDER for the Zitadel OIDC client: the auth source ships
  # disabled, nothing reads these two values until an operator writes the real client id and secret over them (kv_create
  # never overwrites). The placeholder is a fixed, public string on purpose: it protects nothing and must not look like it does.
  d="$(P="$(rand 32 32)" jq -nc '{password:$ENV.P}')";                    D="$d" kv_create forgejo/db D
  d="$(P="$(rand 28 28)Aa1!" jq -nc '{password:$ENV.P}')";                D="$d" kv_create forgejo/admin D
  d='{"key":"oidc-not-configured","secret":"oidc-not-configured"}';       D="$d" kv_create forgejo/oidc D
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
