#!/usr/bin/env bash
# Shared helpers of the OpenBao bootstrap Jobs (init.sh, configure.sh).
#
# THE RULE THAT SHAPES EVERYTHING HERE: a secret is never printed, never put on a
# command line, never written to a disk-backed path. Pod logs are shipped to Loki
# (Alloy collects every namespace), so a value that reaches stdout is stored for
# the retention of the log store and readable by anyone who can read logs. Tokens
# travel in a header FILE on a memory-backed volume (curl -H @file), request
# bodies that carry a secret travel on stdin (curl -d @-), and `log` is the only
# thing that writes to stdout.
#
# Everything environment-specific is overridable (OA_*), so the same code runs
# unchanged against a real OpenBao in a container in scripts/dev/test-openbao-bootstrap.sh.
set -euo pipefail

: "${VAULT_NS:=foundation-vault}"
: "${BAO_ADDR:=https://openbao.${VAULT_NS}.svc.cluster.local:8200}"
: "${BAO_CACERT:=/bao/tls/ca.crt}"
: "${OA_TMP:=/tmp}"                                  # a memory-backed emptyDir in the Jobs
: "${OA_KUBECTL:=kubectl}"
: "${OA_SA_DIR:=/var/run/secrets/kubernetes.io/serviceaccount}"
: "${OA_POLICY_DIR:=/scripts}"

log() { printf '%s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }
kctl() { "$OA_KUBECTL" -n "$VAULT_NS" "$@"; }

# curl with the cluster-internal CA; the response body goes to $OA_TMP/body, the
# HTTP status to $OA_TMP/code. `api METHOD PATH [TOKEN_FILE]` reads a JSON body from stdin
# when stdin is not a terminal and not empty (callers pass `</dev/null` for none).
CURL_CA=()
[ -n "${BAO_CACERT:-}" ] && [ -f "$BAO_CACERT" ] && CURL_CA=(--cacert "$BAO_CACERT")

api() { # METHOD PATH [TOKEN_FILE] — body on stdin
  local method="$1" path="$2" tokfile="${3:-}"
  local args=(-s "${CURL_CA[@]}" -o "$OA_TMP/body" -w '%{http_code}' -X "$method" --data-binary @-)
  [ -n "$tokfile" ] && args+=(-H "@$tokfile")
  curl "${args[@]}" "${BAO_ADDR}${path}" > "$OA_TMP/code" || echo 000 > "$OA_TMP/code"
}
code() { cat "$OA_TMP/code" 2>/dev/null || echo 000; }
body() { cat "$OA_TMP/body" 2>/dev/null || true; }
ok() { case "$(code)" in 200|201|202|204) return 0 ;; *) return 1 ;; esac; }
none() { printf ''; }

# A token as a curl header file (0600, memory-backed). Never a command-line argument.
header_file() { # TOKEN OUTFILE
  ( umask 077; printf 'X-Vault-Token: %s\n' "$1" > "$2" )
}

wait_api() { # up to 15 minutes; any HTTP answer counts, sealed and uninitialised included
  local i c
  for i in $(seq 1 180); do
    c="$(curl -s "${CURL_CA[@]}" -o /dev/null -w '%{http_code}' \
      "${BAO_ADDR}/v1/sys/health?uninitcode=200&sealedcode=200&standbyok=true" || true)"
    if [ -n "$c" ] && [ "$c" != "000" ]; then log "  API answers (health=$c)"; return 0; fi
    [ $((i % 6)) = 0 ] && log "  waiting for the API... ${i}/180"
    sleep 5
  done
  die "the OpenBao API never answered (is the StatefulSet running, is the seal key Secret present?)"
}

# Raft writes go through the active node: follow /sys/leader and point BAO_ADDR at it.
resolve_leader() {
  [ -z "${OA_NO_LEADER_REDIRECT:-}" ] || return 0     # harness: a single node, whose advertised address is not reachable from outside
  local i leader
  for i in $(seq 1 36); do
    leader="$(curl -s "${CURL_CA[@]}" "${BAO_ADDR}/v1/sys/leader" | jq -r '.leader_address // empty' 2>/dev/null || true)"
    if [ -n "$leader" ] && [ "$leader" != "null" ]; then
      BAO_ADDR="${leader%/}"; log "  active node: $BAO_ADDR"; return 0
    fi
    sleep 5
  done
  die "no raft leader after 3 minutes"
}

initialized() { curl -s "${CURL_CA[@]}" "${BAO_ADDR}/v1/sys/init" | jq -r '.initialized // false' 2>/dev/null || echo false; }

# Log in with the Job's own ServiceAccount token (Kubernetes auth). Writes the client
# token header to $2. In the test harness a pre-made token file replaces it (OA_TEST_TOKEN_FILE):
# there is no kube-apiserver to review a JWT there; the login path itself is exercised on a cluster.
login_k8s() { # ROLE TOKEN_HEADER_FILE
  local role="$1" out="$2" jwt
  if [ -n "${OA_TEST_TOKEN_FILE:-}" ]; then cp "$OA_TEST_TOKEN_FILE" "$out"; return 0; fi
  [ -r "$OA_SA_DIR/token" ] || return 1
  jwt="$(cat "$OA_SA_DIR/token")"
  jq -nc --arg r "$role" --arg j "$jwt" '{role:$r,jwt:$j}' | api POST /v1/auth/kubernetes/login ""
  ok || return 1
  local tok; tok="$(body | jq -r '.auth.client_token // empty')"
  [ -n "$tok" ] || return 1
  header_file "$tok" "$out"
}

# Write a policy from $OA_POLICY_DIR/<name>.hcl (the file is the single source of truth).
put_policy() { # NAME TOKEN_FILE
  local name="$1" tok="$2" f="$OA_POLICY_DIR/$1.hcl"
  [ -f "$f" ] || die "missing policy file $f"
  jq -nc --rawfile p "$f" '{policy:$p}' | api PUT "/v1/sys/policies/acl/$name" "$tok"
  ok || die "policy $name: HTTP $(code) $(body | jq -c '.errors // empty' 2>/dev/null)"
  log "  policy $name"
}

# Kubernetes-auth role: ROLE SA NAMESPACE POLICIES TTL MAX_TTL TOKEN_FILE
put_k8s_role() {
  local role="$1" sa="$2" ns="$3" pol="$4" ttl="$5" maxttl="$6" tok="$7"
  jq -nc --arg sa "$sa" --arg ns "$ns" --arg p "$pol" --arg t "$ttl" --arg m "$maxttl" \
    '{bound_service_account_names:$sa,bound_service_account_namespaces:$ns,token_policies:$p,token_ttl:$t,token_max_ttl:$m}' \
    | api POST "/v1/auth/kubernetes/role/$role" "$tok"
  ok || die "k8s role $role: HTTP $(code) $(body | jq -c '.errors // empty' 2>/dev/null)"
  log "  role $role"
}

configure_k8s_auth() { # TOKEN_FILE — enable + configure the Kubernetes auth method (idempotent)
  local tok="$1" ca
  printf '{"type":"kubernetes"}' | api POST /v1/sys/auth/kubernetes "$tok" || true
  case "$(code)" in 200|204|400) : ;; *) die "enable auth/kubernetes: HTTP $(code)" ;; esac   # 400 = already mounted
  ca="$(cat "$OA_SA_DIR/ca.crt")"
  jq -nc --arg ca "$ca" '{kubernetes_host:"https://kubernetes.default.svc.cluster.local",kubernetes_ca_cert:$ca,disable_iss_validation:true}' \
    | api POST /v1/auth/kubernetes/config "$tok"
  ok || die "auth/kubernetes/config: HTTP $(code) $(body | jq -c '.errors // empty' 2>/dev/null)"
  log "  auth/kubernetes configured"
}
