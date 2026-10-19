#!/usr/bin/env bash
# The OpenBao bootstrap (apps/base/foundation/vault: configmap.yaml, scripts/init.sh, scripts/configure.sh, the policies) run
# against a REAL OpenBao in a container, with kubectl replaced by a stub that keeps Secrets and ConfigMaps in a directory.
#
# What it proves: the real entrypoint renders a configuration OpenBao accepts (static seal, audit device, TLS listener); init.sh
# initialises with the right mode, stores the seal key and the recovery shares, hands over to the bootstrap identity and
# REVOKES the root token; configure.sh creates the engines, policies, roles and application secrets; nothing secret reaches
# stdout/stderr; a second run changes nothing; the PGP-encrypted recovery shares decrypt and drive the documented
# break-glass. What it can NOT prove: the Kubernetes auth LOGIN (there is no kube-apiserver here to review a JWT), so the
# two scripts take a pre-made token at that one seam (OA_TEST_*); the login is exercised on a cluster.
#
# Needs docker, gpg, openssl, jq, curl. Without docker or gpg it says so and exits 0: a skip is NOT a pass.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
V="$ROOT/apps/base/foundation/vault"
IMAGE="${OPENBAO_TEST_IMAGE:-docker.io/openbao/openbao:2.7.1}"
PASS=0; FAIL=0
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; PASS=$((PASS + 1)); }
bad() { printf '  \033[31m✗\033[0m %s\n' "$*"; FAIL=$((FAIL + 1)); }

for b in docker gpg openssl jq curl python3; do
  command -v "$b" >/dev/null 2>&1 || { echo "↷ test-openbao-bootstrap not run: $b is missing (a skip is not a pass)"; exit 0; }
done
docker info >/dev/null 2>&1 || { echo "↷ test-openbao-bootstrap not run: no Docker daemon (a skip is not a pass)"; exit 0; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || docker pull -q "$IMAGE" >/dev/null 2>&1 || { echo "↷ test-openbao-bootstrap not run: cannot pull $IMAGE"; exit 0; }

T="$(mktemp -d)"; export GNUPGHOME="$T/gnupg"; mkdir -p "$GNUPGHOME"; chmod 700 "$GNUPGHOME"
cleanup() {
  [ -n "${OA_TEST_KEEP:-}" ] && { echo "kept: containers labelled oa-test=$$, files in $T"; return 0; }
  # By label, not from an array: the instances started inside $(...) are invisible to the parent shell.
  docker ps -aq --filter "label=oa-test=$$" | xargs -r docker rm -f >/dev/null 2>&1; rm -rf "$T"
}
trap cleanup EXIT

# --- fixtures: the real ConfigMap files, a TLS pair, a kubectl stub, PGP keys ----------------------------------------
python3 - "$V/configmap.yaml" "$T/conf" <<'PY'
import sys, os, re
src, out = sys.argv[1:3]
os.makedirs(out, exist_ok=True)
t = open(src).read()
data = t[t.index('data:\n'):]
for name in ('config.hcl.tpl', 'entrypoint.sh', 'cert-reload.sh'):
    m = re.search(r'^  ' + re.escape(name) + r': \|\n((?:    .*\n|\n)+)', data, re.M)
    body = ''.join(l[4:] if l.startswith('    ') else l for l in m.group(1).splitlines(True))
    open(os.path.join(out, name), 'w').write(body)
PY
mkdir -p "$T/tls" "$T/stub/bin"
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$T/tls/ca.key" -out "$T/tls/ca.crt" -subj "/CN=test-ca" -days 2 >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes -keyout "$T/tls/tls.key" -out "$T/tls/srv.csr" -subj "/CN=localhost" >/dev/null 2>&1
printf 'subjectAltName=DNS:localhost,IP:127.0.0.1\n' > "$T/tls/san.ext"
openssl x509 -req -in "$T/tls/srv.csr" -CA "$T/tls/ca.crt" -CAkey "$T/tls/ca.key" -CAcreateserial -out "$T/tls/tls.crt" -days 2 -extfile "$T/tls/san.ext" >/dev/null 2>&1
chmod -R a+rX "$T/tls"; chmod a+r "$T/tls/tls.key"
mkdir -p "$T/sa"; cp "$T/tls/ca.crt" "$T/sa/ca.crt"; echo "fake.jwt" > "$T/sa/token"

cat > "$T/stub/bin/kubectl" <<'STUB'
#!/usr/bin/env bash
# A stub of the five kubectl calls the scripts make; objects live in $STUB_DIR.
D="${STUB_DIR:?}"; mkdir -p "$D/secret" "$D/cm"
args=("$@"); [ "${args[0]:-}" = -n ] && args=("${args[@]:2}")
case "${args[0]} ${args[1]:-}" in
  "get secret")  [ -d "$D/secret/${args[2]}" ] ;;
  "get configmap")
     if [ -n "${STUB_CM_ERROR:-}" ]; then echo "Error from server (Forbidden): configmaps \"${args[2]}\" is forbidden" >&2; exit 1; fi
     [ -f "$D/cm/${args[2]}.json" ] && { cat "$D/cm/${args[2]}.json"; exit 0; }
     echo "Error from server (NotFound): configmaps \"${args[2]}\" not found" >&2; exit 1 ;;
  "create configmap")
     name="${args[2]}"
     [ -f "$D/cm/$name.json" ] && { echo "Error from server (AlreadyExists): configmaps \"$name\" already exists" >&2; exit 1; }
     printf '{"data":{}}' > "$D/cm/$name.json" ;;
  "create secret")
     name="${args[3]}"
     [ -d "$D/secret/$name" ] && { echo "Error from server (AlreadyExists): secrets \"$name\" already exists" >&2; exit 1; }
     mkdir -p "$D/secret/$name"
     for a in "${args[@]:4}"; do case "$a" in --from-file=*) kv="${a#--from-file=}"; cp "${kv#*=}" "$D/secret/$name/${kv%%=*}" ;; esac; done ;;
  "label secret"|"annotate secret") for a in "${args[@]:3}"; do case "$a" in --overwrite) ;; *) echo "$a" >> "$D/secret/${args[2]}.${args[0]}s" ;; esac; done ;;
  *) echo "kubectl stub: unsupported: ${args[*]}" >&2; exit 2 ;;
esac
STUB
chmod +x "$T/stub/bin/kubectl"
mkpgp() { # N — N test identities with an encryption subkey; public keys as base64 files pub1..pubN
  local n fpr
  for n in $(seq 1 "$1"); do
    gpg --batch --quiet --passphrase '' --quick-gen-key "ops$n@example.invalid" rsa2048 sign never 2>/dev/null
    fpr="$(gpg --list-keys --with-colons "ops$n@example.invalid" | awk -F: '/^fpr/{print $10; exit}')"
    gpg --batch --quiet --passphrase '' --quick-add-key "$fpr" rsa2048 encr never 2>/dev/null
    gpg --export "ops$n@example.invalid" | base64 -w0 > "$T/pub$n"
  done
}

run_bao() { # NAME PORT — (re)start the container for NAME on its existing data and seal directories; waits for the API
  # TLS_DIR replaces the shared certificate directory; BAO_ENV holds extra "-e VAR=value" words. The metrics listener
  # (8202) is published on PORT+1000.
  local name="$1" port="$2"
  docker rm -f "bao-test-$name" >/dev/null 2>&1
  docker run -d --label "oa-test=$$" --user "$(id -u):$(id -g)" --name "bao-test-$name" --hostname openbao-0 \
    -p "127.0.0.1:$port:8200" -p "127.0.0.1:$((port + 1000)):8202" ${BAO_ENV:-} \
    -e POD_NAMESPACE=foundation-vault \
    -v "$T/conf:/bao/config:ro" -v "$T/$name/seal:/bao/seal:ro" -v "${TLS_DIR:-$T/tls}:/bao/tls:ro" -v "$T/$name/data:/bao/data" \
    -v "$T/sa:/var/run/secrets/kubernetes.io/serviceaccount:ro" \
    --entrypoint /bin/sh "$IMAGE" /bao/config/entrypoint.sh >/dev/null 2>&1
  local _; for _ in $(seq 1 40); do
    curl -s --cacert "$T/tls/ca.crt" -o /dev/null "https://127.0.0.1:$port/v1/sys/health?uninitcode=200&sealedcode=200" && return 0; sleep 1
  done
  return 1
}
start_bao() { # NAME PORT [SEAL_KEY_FILE] — a fresh OpenBao with the REAL entrypoint and configuration template
  local name="$1" port="$2" key="${3:-}"
  mkdir -p "$T/$name/data" "$T/$name/seal"; chmod 777 "$T/$name/data"
  if [ -n "$key" ]; then cp "$key" "$T/$name/seal/key"; else head -c 32 /dev/urandom > "$T/$name/seal/key"; fi
  chmod a+r "$T/$name/seal/key"
  run_bao "$name" "$port"
}
wait_active_port() { # PORT — initialised, unsealed and active
  local _; for _ in $(seq 1 40); do
    [ "$(curl -s --cacert "$T/tls/ca.crt" -o /dev/null -w '%{http_code}' "https://127.0.0.1:$1/v1/sys/health")" = 200 ] && return 0; sleep 1
  done; return 1
}
hexkey() { # a PGP-encrypted share (base64) -> the key as `generate-root` takes it (the decrypted text IS the hex key)
  base64 -d | gpg --batch --quiet --decrypt 2>/dev/null | tr -d ' \n'
}

# The scripts run in the IMAGE the two Jobs run (read from the manifests, so the two can not drift): its bash, jq, curl
# and coreutils are the ones that matter, not the host's. OA_TEST_HOST_TOOLS=1 uses the host's instead (no image pull).
JOB_IMAGE="$(sed -n 's|^ *image: \(docker.io/alpine/k8s:[0-9.]*\)$|\1|p' "$V/openbao-init.yaml" "$V/bootstrap-roles-job.yaml" | sort -u)"
[ "$(wc -l <<<"$JOB_IMAGE")" = 1 ] && [ -n "$JOB_IMAGE" ] || { echo "✗ the two Jobs do not run one image: $JOB_IMAGE"; exit 1; }
if [ -z "${OA_TEST_HOST_TOOLS:-}" ]; then
  docker image inspect "$JOB_IMAGE" >/dev/null 2>&1 || docker pull -q "$JOB_IMAGE" >/dev/null 2>&1 || { echo "↷ test-openbao-bootstrap not run: cannot pull $JOB_IMAGE"; exit 0; }
fi
run_script() { # NAME PORT SCRIPT [extra env...] — runs one of the real scripts; output in $T/<name>/<script>.out
  local name="$1" port="$2" script="$3"; shift 3
  local envs=(STUB_DIR="$T/$name/k8s" PATH="$T/stub/bin:/usr/local/bin:/usr/bin:/bin:/sbin" OA_KUBECTL=kubectl OA_TMP="$T/$name/tmp"
              OA_SA_DIR="$T/sa" BAO_ADDR="https://127.0.0.1:$port" BAO_CACERT="$T/tls/ca.crt" OA_INIT_ADDR="https://127.0.0.1:$port"
              OA_NO_LEADER_REDIRECT=1 OA_POLICY_DIR="$V/scripts/policies" VAULT_NS=foundation-vault OA_POD_URLS="https://127.0.0.1:$port" OA_PROOF_TRIES=2 "$@")
  if [ -n "${OA_TEST_HOST_TOOLS:-}" ]; then
    env "${envs[@]}" bash "$V/scripts/$script" > "$T/$name/$script.out" 2>&1
  else
    local dargs=() e
    for e in "${envs[@]}"; do dargs+=(-e "$e"); done
    docker run --rm --network host --user "$(id -u):$(id -g)" -v "$T:$T" -v "$V:$V:ro" "${dargs[@]}" \
      --entrypoint bash "$JOB_IMAGE" "$V/scripts/$script" > "$T/$name/$script.out" 2>&1
  fi
}

echo "=== the real configuration template + entrypoint: OpenBao starts, static seal, audit device, TLS ==="
mkpgp 3
if start_bao dev 18211; then ok "OpenBao started with the rendered configuration"; else bad "OpenBao did not start: $(docker logs bao-test-dev 2>&1 | tail -5)"; fi
STATUS="$(curl -s --cacert "$T/tls/ca.crt" https://127.0.0.1:18211/v1/sys/seal-status)"
[ "$(jq -r .type <<<"$STATUS")" = static ] && ok "seal type is static" || bad "seal type: $(jq -r .type <<<"$STATUS")"
mkdir -p "$T/dev/tmp" "$T/dev/k8s"

echo "=== init.sh refuses to guess: a ConfigMap it can not read is not 'development' ==="
if run_script dev 18211 init.sh STUB_CM_ERROR=1; then bad "init.sh went ahead although it could not read openbao-operator-keys"; else ok "init.sh stops when reading the ConfigMap is forbidden"; fi
[ "$(curl -s --cacert "$T/tls/ca.crt" https://127.0.0.1:18211/v1/sys/init | jq -r .initialized)" = false ] && ok "…and OpenBao was not initialised" || bad "OpenBao was initialised in spite of it"

echo "=== production mode refuses, BEFORE sys/init, what would leave a half-initialised cluster ==="
start_bao pre 18218 && ok "OpenBao for the refusals started" || bad "pre instance did not start"
mkdir -p "$T/pre/tmp" "$T/pre/k8s/cm"
mkpgp_cm() { # THRESHOLD — a production ConfigMap with the three test keys
  jq -nc --rawfile a "$T/pub1" --rawfile b "$T/pub2" --rawfile c "$T/pub3" --arg t "$1" '{data:({"pgp-1":$a,"pgp-2":$b,"pgp-3":$c} + (if $t == "" then {} else {threshold:$t} end))}' > "$T/pre/k8s/cm/openbao-operator-keys.json"
}
not_initialised() { [ "$(curl -s --cacert "$T/tls/ca.crt" https://127.0.0.1:18218/v1/sys/init | jq -r .initialized)" = false ]; }
mkpgp_cm two
if run_script pre 18218 init.sh; then bad "init.sh accepted threshold 'two'"; else ok "a non-numeric threshold is refused"; fi
not_initialised && ok "…and OpenBao was not initialised (the guard used to error, not fail, and let sys/init run with an empty body)" || bad "OpenBao was initialised"
jq -nc --arg a "not base64 !!" --rawfile b "$T/pub2" --rawfile c "$T/pub3" '{data:{"pgp-1":$a,"pgp-2":$b,"pgp-3":$c}}' > "$T/pre/k8s/cm/openbao-operator-keys.json"
if run_script pre 18218 init.sh; then bad "init.sh accepted a key that is not base64"; else ok "a key that is not base64 is refused"; fi
not_initialised && ok "…and OpenBao was not initialised" || bad "OpenBao was initialised"
mkpgp_cm 2; mkdir -p "$T/pre/k8s/secret/openbao-recovery-shares"
if run_script pre 18218 init.sh; then bad "init.sh went ahead over a leftover shares Secret"; else ok "a leftover recovery-shares Secret stops the run"; fi
not_initialised && ok "…before OpenBao was initialised" || bad "OpenBao was initialised with its shares about to be swallowed"
rm -rf "$T/pre/k8s/secret/openbao-recovery-shares"

echo "=== a root token left by a killed run is revoked at the next start ==="
PRE_INIT="$(curl -s --cacert "$T/tls/ca.crt" -X PUT -d '{"recovery_shares":1,"recovery_threshold":1}' https://127.0.0.1:18218/v1/sys/init)"
wait_active_port 18218
printf 'X-Vault-Token: %s\n' "$(jq -r .root_token <<<"$PRE_INIT")" > "$T/pre/tmp/root.hdr"
run_script pre 18218 init.sh >/dev/null 2>&1 || true
grep -q "left by a killed run" "$T/pre/init.sh.out" && ok "init.sh notices the leftover" || bad "no 'left by a killed run' line"
[ ! -e "$T/pre/tmp/root.hdr" ] && ok "…removes the file" || bad "root.hdr is still there"
printf 'X-Vault-Token: %s\n' "$(jq -r .root_token <<<"$PRE_INIT")" > "$T/pre.hdr"
[ "$(curl -s --cacert "$T/tls/ca.crt" -H "@$T/pre.hdr" -o /dev/null -w '%{http_code}' https://127.0.0.1:18218/v1/auth/token/lookup-self)" = 403 ] \
  && ok "…and the token no longer works" || bad "the leftover root token is still valid"

echo "=== a failure inside the root window: the root token is revoked on the way out, not left in the pod ==="
start_bao fail 18217 && ok "OpenBao for the failing run started" || bad "fail instance did not start"
mkdir -p "$T/fail/tmp" "$T/fail/k8s"
if run_script fail 18217 init.sh OA_TEST_LOGIN_FAILS=1; then bad "init.sh succeeded although the bootstrap role could not log in"; else ok "init.sh fails when the hand-over is not proven"; fi
grep -q "the hand-over is not proven" "$T/fail/init.sh.out" && ok "…and says why" || bad "$(tail -3 "$T/fail/init.sh.out")"
[ ! -e "$T/fail/tmp/root.hdr" ] && ok "no root token file is left in the pod's memory volume" || bad "root.hdr is still there"
FAILREV=0; for _ in $(seq 1 10); do
  docker logs bao-test-fail 2>&1 | grep '"type":"response"' | grep '"path":"auth/token/revoke-self"' | grep -q '"token_policies":\["root"\]' && { FAILREV=1; break; }; sleep 1
done
[ "$FAILREV" = 1 ] && ok "the audit trail shows the root token revoked itself on the failure path" || bad "no root revocation in the audit trail after the failure"
grep -q "root token revoked" "$T/fail/init.sh.out" && ok "…and the log says so" || bad "no revocation line"

echo "=== init.sh, development mode: seal key, one recovery share, the root token revoked, nothing printed ==="
if run_script dev 18211 init.sh OA_TEST_MAKE_TOKEN_FILE="$T/dev/boot.hdr"; then ok "init.sh exits 0"; else bad "init.sh failed: $(tail -5 "$T/dev/init.sh.out")"; fi
[ "$(stat -c %s "$T/dev/k8s/secret/openbao-seal-key/key" 2>/dev/null)" = 32 ] && ok "Secret openbao-seal-key holds 32 bytes" || bad "no 32-byte seal key Secret"
[ -f "$T/dev/k8s/secret/openbao-recovery-dev/share-1" ] && ok "the development recovery share is stored" || bad "no development share Secret"
grep -q "openaether.io/dev-bootstrap=true" "$T/dev/k8s/secret/openbao-recovery-dev.labels" 2>/dev/null && ok "…and labelled as development" || bad "development Secret not labelled"
[ ! -d "$T/dev/k8s/secret/openbao-recovery-shares" ] && ok "no production shares Secret in development mode" || bad "production shares Secret exists in development mode"
grep -q "root token revoked" "$T/dev/init.sh.out" && ok "the root token was revoked" || bad "no revocation line"
# CI runners start OpenBao slowly: 15 s was a race (one red run on the 2026-10-15 wave-1 PR, green locally in a minute).
AUDIT=0; for _ in $(seq 1 90); do
  docker logs bao-test-dev 2>&1 | grep -q '"@message":"enabled audit backend".*"path":"stdout/","type":"file"' && { AUDIT=1; break; }; sleep 1
done
[ "$AUDIT" = 1 ] && ok "the audit device from the configuration is enabled" || bad "the audit device is not enabled"
# The audit trail is the witness: the revocation request was made by a ROOT token and was answered.
REVOKED=0; for _ in $(seq 1 10); do
  docker logs bao-test-dev 2>&1 | grep '"type":"response"' | grep '"path":"auth/token/revoke-self"' | grep -q '"token_policies":\["root"\]' && { REVOKED=1; break; }; sleep 1
done
[ "$REVOKED" = 1 ] && ok "the audit trail shows the root token revoking itself" || bad "no root revoke-self in the audit trail"
SHARE="$(cat "$T/dev/k8s/secret/openbao-recovery-dev/share-1" 2>/dev/null)"
if [ -n "$SHARE" ] && ! grep -qF "$SHARE" "$T/dev/init.sh.out"; then ok "the recovery share is not in the output"; else bad "the recovery share reached stdout"; fi
! grep -Eq '(^|[^A-Za-z0-9])(s|hvs|bao)\.[A-Za-z0-9]{16,}' "$T/dev/init.sh.out" && ok "no token-shaped string in the output" || bad "a token-shaped string is in the output"
SEAL_B64="$(base64 -w0 "$T/dev/k8s/secret/openbao-seal-key/key" 2>/dev/null)"
! grep -qF "$SEAL_B64" "$T/dev/init.sh.out" && ok "the seal key is not in the output" || bad "the seal key reached stdout"
BOOT="$T/dev/boot.hdr"
LEFT=""; for g in body root.hdr 'share-*'; do compgen -G "$T/dev/tmp/$g" >/dev/null && LEFT="$LEFT $g"; done
[ -z "$LEFT" ] && ok "no response body, root token or share file is left behind" || bad "left in the pod's memory volume:$LEFT"
[ -f "$T/dev/k8s/cm/openbao-init-state.json" ] && ok "the 'initialised' marker ConfigMap was written" || bad "no marker ConfigMap"
RB="$(curl -s --cacert "$T/tls/ca.crt" -H "@$BOOT" -X LIST https://127.0.0.1:18211/v1/auth/kubernetes/role | jq -r '.data.keys[]?' | tr '\n' ' ')"
grep -qw openbao-breakglass <<<"$RB" && ok "the break-glass role exists right after init.sh, before configure.sh ever ran" || bad "break-glass role missing after init ($RB)"
KC_CFG="$(curl -s --cacert "$T/tls/ca.crt" -H "@$BOOT" https://127.0.0.1:18211/v1/auth/kubernetes/config | jq -c '.data')"
[ "$(jq -r '.kubernetes_ca_cert' <<<"$KC_CFG")" = "" ] && [ "$(jq -r '.kubernetes_host' <<<"$KC_CFG")" = "https://kubernetes.default.svc.cluster.local" ] \
  && ok "the Kubernetes auth config pins no CA (a restore onto a rebuilt cluster keeps logging in)" || bad "k8s auth config: $KC_CFG"
echo "--- a second run changes nothing"
cp -r "$T/dev/k8s" "$T/dev/k8s.before"
if run_script dev 18211 init.sh OA_TEST_TOKEN_FILE="$BOOT"; then ok "second init.sh exits 0"; else bad "second init.sh failed: $(tail -3 "$T/dev/init.sh.out")"; fi
diff -r "$T/dev/k8s.before" "$T/dev/k8s" >/dev/null && ok "no Secret changed" || bad "a Secret changed on the second run"
grep -q "nothing to do" "$T/dev/init.sh.out" && ok "it says so" || bad "no 'nothing to do' line"
echo "--- a deleted seal key on an initialised cluster is never replaced by a fresh one"
mv "$T/dev/k8s/secret/openbao-seal-key" "$T/dev/seal-key.away"
if run_script dev 18211 init.sh OA_TEST_TOKEN_FILE="$BOOT"; then bad "init.sh went on without the seal key"; else ok "init.sh stops when the seal key is missing on an initialised cluster"; fi
grep -q "has been initialised" "$T/dev/init.sh.out" && ok "…and says why" || bad "$(tail -2 "$T/dev/init.sh.out")"
[ ! -e "$T/dev/k8s/secret/openbao-seal-key" ] && ok "…without creating a new key" || bad "a new seal key was created"
mv "$T/dev/seal-key.away" "$T/dev/k8s/secret/openbao-seal-key"

echo "=== configure.sh under the bootstrap identity: engines, policies, roles, application secrets ==="
if run_script dev 18211 configure.sh OA_TEST_TOKEN_FILE="$BOOT"; then ok "configure.sh exits 0"; else bad "configure.sh failed: $(tail -6 "$T/dev/configure.sh.out")"; fi
bao_get() { curl -s --cacert "$T/tls/ca.crt" -H "@$BOOT" "https://127.0.0.1:18211$1"; }
for p in openaether-bootstrap openaether-breakglass external-secrets-read cert-manager-pki backup-snapshot secret-seeder openaether-admin openaether-reader; do
  [ "$(bao_get "/v1/sys/policies/acl/$p" | jq -r '.data.name // empty')" = "$p" ] && ok "policy $p" || bad "policy $p missing"
done
ROLES="$(curl -s --cacert "$T/tls/ca.crt" -H "@$BOOT" -X LIST https://127.0.0.1:18211/v1/auth/kubernetes/role | jq -r '.data.keys[]?' | sort | tr '\n' ' ')"
for r in backup-snapshot cert-manager-issuer external-secrets-operator openaether-admin openaether-reader openbao-bootstrap openbao-breakglass secret-seeder; do
  grep -qw "$r" <<<"$ROLES" && ok "role $r" || bad "role $r missing ($ROLES)"
done
for kv in grafana/admin grafana/db minio/root longhorn/encryption zitadel/db zitadel/masterkey zitadel/admin backup/restic; do
  [ -n "$(bao_get "/v1/secret/data/$kv" | jq -r '.data.data | keys[0] // empty')" ] && ok "secret/$kv exists" || bad "secret/$kv missing"
done
CSR1="$(bao_get /v1/secret/data/pki/intermediate-csr | jq -r '.data.data.csr // empty')"
grep -q "BEGIN CERTIFICATE REQUEST" <<<"$CSR1" && ok "the intermediate CSR is stored in secret/pki/intermediate-csr (outlives the Job's log)" || bad "no CSR in secret/pki/intermediate-csr"
LEAK=0
for kv in grafana/admin grafana/db minio/root longhorn/encryption zitadel/db zitadel/masterkey zitadel/admin backup/restic; do
  while IFS= read -r val; do
    [ -n "$val" ] && grep -qF -- "$val" "$T/dev/configure.sh.out" && { LEAK=1; bad "the value of secret/$kv is in the output"; }
  done < <(bao_get "/v1/secret/data/$kv" | jq -r '.data.data | to_entries[] | select(.key != "username" and .key != "accessKey") | .value')
done
[ "$LEAK" = 0 ] && ok "no application secret value in the output"
[ "$(bao_get /v1/secret/data/zitadel/masterkey | jq -r '.data.data.masterkey | length')" = 32 ] && ok "the Zitadel master key is exactly 32 characters" || bad "master key length"
# Zitadel refuses a first-admin password that fails its policy (15+ characters, all four classes): the setup Job would
# fail and the identity layer with it. Checked here on what configure.sh really generated.
ADMINPW="$(bao_get /v1/secret/data/zitadel/admin | jq -r '.data.data.password')"
[ "${#ADMINPW}" -ge 15 ] && [[ "$ADMINPW" =~ [a-z] && "$ADMINPW" =~ [A-Z] && "$ADMINPW" =~ [0-9] && "$ADMINPW" =~ [^a-zA-Z0-9] ]] \
  && ok "the Zitadel first-admin password has 15+ characters and all four classes" || bad "the first-admin password would fail Zitadel's policy"
[ "$ADMINPW" != 'Password1!' ] && ok "...and is not upstream's default" || bad "the first-admin password is the public default"
RESTIC1="$(bao_get /v1/secret/data/backup/restic | jq -r '.data.data.password')"
echo "--- a second run changes no secret"
run_script dev 18211 configure.sh OA_TEST_TOKEN_FILE="$BOOT" && ok "second configure.sh exits 0" || bad "second configure.sh failed: $(tail -4 "$T/dev/configure.sh.out")"
[ "$(bao_get /v1/secret/data/backup/restic | jq -r '.data.data.password')" = "$RESTIC1" ] && ok "backup/restic is unchanged" || bad "backup/restic was rotated"
grep -q "already present" "$T/dev/configure.sh.out" && ok "it reports the secrets as already present" || bad "no 'already present'"
NKEYS="$(curl -s --cacert "$T/tls/ca.crt" -H "@$BOOT" -X LIST https://127.0.0.1:18211/v1/pki/keys | jq -r '.data.keys | length')"
[ "$NKEYS" = 1 ] && ok "two runs, one pending PKI key (no key per sync)" || bad "pki keys after two runs: $NKEYS"
grep -q "already generated" "$T/dev/configure.sh.out" && ok "the second run says the CSR is pending" || bad "no 'already generated' line"
[ "$(bao_get /v1/secret/data/pki/intermediate-csr | jq -r '.data.data.csr // empty')" = "$CSR1" ] && [ "$(bao_get /v1/secret/data/pki/intermediate-csr | jq -r '.data.metadata.version')" = 1 ] \
  && ok "the stored CSR is unchanged by the second run (still version 1)" || bad "the stored CSR changed or was rewritten"
echo "--- PKI: key types, role, revocation information, tidy (a throwaway EC root signs the intermediate, as the runbook does)"
bao_post() { local d="${2:-}"; [ -n "$d" ] || d='{}'; printf '%s' "$d" | curl -s --cacert "$T/tls/ca.crt" -H "@$BOOT" -X POST -d @- "https://127.0.0.1:18211$1"; }
P="$T/pki"; mkdir -p "$P"
openssl req -in <(printf '%s' "$CSR1") -noout -text 2>/dev/null | grep -q "secp384r1" && ok "the intermediate CSR is an ECDSA P-384 key" || bad "the intermediate CSR is not P-384"
openssl ecparam -genkey -name secp384r1 -noout -out "$P/root.key" 2>/dev/null
openssl req -x509 -new -key "$P/root.key" -sha384 -days 30 -subj "/CN=Throwaway Root" -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign -out "$P/root.crt"
printf '[v3]\nbasicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n' > "$P/ext.cnf"
printf '%s' "$CSR1" > "$P/int.csr"
openssl x509 -req -in "$P/int.csr" -CA "$P/root.crt" -CAkey "$P/root.key" -CAcreateserial -days 20 -sha384 -extfile "$P/ext.cnf" -extensions v3 -out "$P/int.crt" >/dev/null 2>&1
SET="$(bao_post /v1/pki/intermediate/set-signed "$(jq -nc --rawfile c "$P/int.crt" '{certificate:$c}')" | jq -r '.data.imported_issuers[0] // empty')"
[ -n "$SET" ] && ok "the intermediate signed by the offline root is imported" || bad "set-signed refused the intermediate"
ROLE="$(bao_get /v1/pki/roles/openaether | jq -c .data)"
for kv in key_type=ec key_bits=256 client_flag=false server_flag=true allow_wildcard_certificates=false allow_bare_domains=false allow_ip_sans=false; do
  [ "$(jq -r ".${kv%%=*}" <<<"$ROLE")" = "${kv#*=}" ] && ok "role openaether: $kv" || bad "role openaether: $kv ($ROLE)"
done
[ "$(jq -c .key_usage <<<"$ROLE")" = '["DigitalSignature"]' ] && ok "role openaether: key usage is DigitalSignature only" || bad "role key_usage: $(jq -c .key_usage <<<"$ROLE")"
mkcsr() { # NAME SAN [SUBJECT] [ec|rsa2048|rsa3072]
  local k=(-newkey ec -pkeyopt ec_paramgen_curve:prime256v1)
  case "${4:-ec}" in rsa2048) k=(-newkey rsa:2048) ;; rsa3072) k=(-newkey rsa:3072) ;; esac
  openssl req -new "${k[@]}" -nodes -keyout "$P/$1.key" -out "$P/$1.csr" -subj "${3:-/}" -addext "subjectAltName=$2" 2>/dev/null
}
sign() { bao_post /v1/pki/sign/openaether "$(jq -nc --rawfile c "$P/$1.csr" '{csr:$c,ttl:"24h"}')"; }
mkcsr gw "DNS:grafana.openaether.local,DNS:vault.openaether.local"
sign gw > "$P/gw.json"; jq -r '.data.certificate // empty' "$P/gw.json" > "$P/gw.crt"
TXT="$(openssl x509 -in "$P/gw.crt" -noout -text 2>/dev/null)"
grep -q "Public Key Algorithm: id-ecPublicKey" <<<"$TXT" && ok "the role issues an ECDSA leaf" || bad "no EC leaf: $(jq -c .errors "$P/gw.json")"
grep -q "TLS Web Server Authentication" <<<"$TXT" && ! grep -q "TLS Web Client Authentication" <<<"$TXT" && ok "…with serverAuth and no clientAuth" || bad "extended key usage is wrong"
grep -A1 "Key Usage" <<<"$TXT" | grep -v Extended | grep -q "Digital Signature" && ! grep -q "Key Encipherment\|Key Agreement" <<<"$TXT" && ok "…and Digital Signature as its only key usage" || bad "key usage is wrong"
grep -q "CRL Distribution Points" <<<"$TXT" && grep -q "svc.cluster.local:8200/v1/pki/crl" <<<"$TXT" && ok "…carrying the CRL distribution point" || bad "no CRL distribution point"
grep -q "OCSP - URI:https://openbao.foundation-vault.svc.cluster.local:8200/v1/pki/ocsp" <<<"$TXT" && grep -q "CA Issuers - URI:.*v1/pki/ca" <<<"$TXT" && ok "…and the OCSP and CA Issuers URLs of the in-cluster service" || bad "no OCSP/AIA URL"
openssl verify -CAfile "$P/root.crt" -untrusted "$P/int.crt" "$P/gw.crt" >/dev/null 2>&1 && ok "the chain verifies up to the throwaway root" || bad "chain does not verify"
refused() { # DESCRIPTION CSRNAME
  local r; r="$(sign "$2" | jq -r '.errors[0] // "ISSUED"')"; [ "$r" != ISSUED ] && ok "$1 is refused ($r)" || bad "$1 was issued"
}
mkcsr rsa "DNS:grafana.openaether.local" / rsa2048; refused "an RSA key" rsa
mkcsr wc "DNS:*.openaether.local";                refused "a wildcard name" wc
mkcsr bare "DNS:openaether.local";                refused "the bare domain" bare
mkcsr out "DNS:evil.example.com";                 refused "a name outside the domain" out
mkcsr ip "IP:10.0.0.1,DNS:grafana.openaether.local"; refused "an IP address SAN" ip
mkcsr cn "DNS:grafana.openaether.local" "/CN=openaether.local"; refused "a bare-domain common name" cn
U1="$(bao_get /v1/pki/config/urls | jq -c '.data | [.issuing_certificates, .crl_distribution_points, .ocsp_servers]')"
[ "$U1" = '[["https://openbao.foundation-vault.svc.cluster.local:8200/v1/pki/ca"],["https://openbao.foundation-vault.svc.cluster.local:8200/v1/pki/crl"],["https://openbao.foundation-vault.svc.cluster.local:8200/v1/pki/ocsp"]]' ] \
  && ok "config/urls points at the in-cluster service" || bad "config/urls: $U1"
CRLC="$(bao_get /v1/pki/config/crl | jq -c '.data | [.auto_rebuild, .enable_delta, .expiry]')"
[ "$CRLC" = '[true,true,"72h"]' ] && ok "the CRL is rebuilt automatically (delta CRLs on, 72h)" || bad "config/crl: $CRLC"
TIDY="$(bao_get /v1/pki/config/auto-tidy | jq -c '.data | [.enabled, .tidy_cert_store, .tidy_revoked_certs]')"
[ "$TIDY" = '[true,true,true]' ] && ok "auto-tidy is enabled for the cert store and revoked certificates" || bad "config/auto-tidy: $TIDY"
SERIAL="$(jq -r '.data.serial_number' "$P/gw.json")"
REVOKED_AT="$(bao_post /v1/pki/revoke "$(jq -nc --arg s "$SERIAL" '{serial_number:$s}')" | jq -r '.data.revocation_time // .errors[0] // "?"')"
bao_get /v1/pki/crl/rotate >/dev/null
curl -s --cacert "$T/tls/ca.crt" https://127.0.0.1:18211/v1/pki/crl/pem > "$P/crl.pem"
SN_HEX="$(tr -d ':' <<<"$SERIAL" | tr a-f A-F)"
openssl crl -in "$P/crl.pem" -noout -text 2>/dev/null | grep -i "Serial Number" | tr -d ' :' | grep -qi "$SN_HEX" && ok "a revoked leaf is on the CRL, served without a token" || bad "the revoked serial $SN_HEX is not in the CRL (revoke answered: $REVOKED_AT; $(openssl crl -in "$P/crl.pem" -noout -text 2>&1 | grep -i 'serial' | head -3 | tr '\n' ' '))"
openssl crl -in "$P/crl.pem" -noout -CAfile "$P/int.crt" >/dev/null 2>&1 && ok "…and the CRL is signed by the intermediate" || bad "CRL signature does not verify"
echo "--- PKI: a second run changes nothing; the default issuer only follows imports on request; the RSA-3072 variant"
run_script dev 18211 configure.sh OA_TEST_TOKEN_FILE="$BOOT" && ok "configure.sh on a mount with a signed intermediate exits 0" || bad "configure.sh failed: $(tail -4 "$T/dev/configure.sh.out")"
grep -q "intermediate CA present" "$T/dev/configure.sh.out" && ok "…and finds it present" || bad "no 'intermediate CA present'"
[ "$(bao_get /v1/pki/config/issuers | jq -r '.data.default_follows_latest_issuer')" = false ] && ok "the default issuer does not follow imports by default" || bad "default_follows_latest_issuer is on"
run_script dev 18211 configure.sh OA_TEST_TOKEN_FILE="$BOOT" OA_PKI_DEFAULT_FOLLOWS_LATEST=true && ok "configure.sh with OA_PKI_DEFAULT_FOLLOWS_LATEST=true exits 0" || bad "configure.sh failed: $(tail -4 "$T/dev/configure.sh.out")"
[ "$(bao_get /v1/pki/config/issuers | jq -r '.data.default_follows_latest_issuer')" = true ] && ok "…and the default issuer follows the latest import" || bad "default_follows_latest_issuer is still off"
run_script dev 18211 configure.sh OA_TEST_TOKEN_FILE="$BOOT" OA_PKI_KEY_TYPE=rsa && ok "configure.sh with OA_PKI_KEY_TYPE=rsa exits 0" || bad "configure.sh failed: $(tail -4 "$T/dev/configure.sh.out")"
mkcsr r3 "DNS:grafana.openaether.local" / rsa3072; [ "$(sign r3 | jq -r '.errors[0] // "ISSUED"')" = ISSUED ] && ok "the RSA variant signs an RSA-3072 key" || bad "RSA-3072 refused"
mkcsr r2 "DNS:grafana.openaether.local" / rsa2048; refused "an RSA-2048 key under the RSA variant" r2
[ "$(bao_get /v1/pki/roles/openaether | jq -r '.data.use_pss')" = true ] && ok "…with PSS signatures" || bad "use_pss is off"
if run_script dev 18211 configure.sh OA_TEST_TOKEN_FILE="$BOOT" OA_PKI_KEY_TYPE=dsa; then bad "an unknown key type was accepted"; else grep -q "must be ec or rsa" "$T/dev/configure.sh.out" && ok "an unknown OA_PKI_KEY_TYPE stops configure.sh with a message" || bad "no message"; fi
run_script dev 18211 configure.sh OA_TEST_TOKEN_FILE="$BOOT" && [ "$(bao_get /v1/pki/roles/openaether | jq -r '.data.key_type')" = ec ] && ok "back to the default: the role is EC again" || bad "the role did not return to ec"
echo "--- the seeding identity can create and test, never overwrite or read a value"
printf '{"policies":["secret-seeder"],"ttl":"10m"}' | curl -s --cacert "$T/tls/ca.crt" -H "@$BOOT" -X POST -d @- https://127.0.0.1:18211/v1/auth/token/create > "$T/dev/seeder.json"
SEEDER_CODE="$(jq -r '.errors[0] // "created"' "$T/dev/seeder.json")"
echo "    (bootstrap identity creating a token: $SEEDER_CODE)"
[ "$SEEDER_CODE" != created ] && ok "the bootstrap identity can not mint tokens" || bad "the bootstrap identity minted a token"

echo "=== production mode: PGP-encrypted recovery shares that decrypt, and the documented break-glass ==="
start_bao prod 18212 && ok "second OpenBao started" || bad "second OpenBao did not start"
mkdir -p "$T/prod/tmp" "$T/prod/k8s/cm"
jq -nc --rawfile a "$T/pub1" --rawfile b "$T/pub2" --rawfile c "$T/pub3" '{data:{"pgp-1":$a,"pgp-2":$b,"pgp-3":$c,threshold:"2"}}' > "$T/prod/k8s/cm/openbao-operator-keys.json"
if run_script prod 18212 init.sh OA_TEST_MAKE_TOKEN_FILE="$T/prod/boot.hdr"; then ok "init.sh (production) exits 0"; else bad "init.sh (production) failed: $(tail -6 "$T/prod/init.sh.out")"; fi
[ -f "$T/prod/k8s/secret/openbao-recovery-shares/share-3" ] && ok "three recovery shares stored" || bad "recovery shares missing"
[ ! -d "$T/prod/k8s/secret/openbao-recovery-dev" ] && ok "no development Secret in production mode" || bad "development Secret in production mode"
DEC1="$(hexkey < "$T/prod/k8s/secret/openbao-recovery-shares/share-1")"
[ "${#DEC1}" -ge 32 ] && ok "share 1 decrypts with operator 1's private key" || bad "share 1 does not decrypt"
grep -q "mode: prod" "$T/prod/init.sh.out" && ok "the log says production mode" || bad "mode line"
if run_script prod 18212 configure.sh OA_TEST_TOKEN_FILE="$T/prod/boot.hdr"; then ok "configure.sh (production) exits 0"; else bad "configure.sh (production) failed"; fi
CA="$T/tls/ca.crt"
# the break-glass identity is the Kubernetes role; here its token is minted directly (no apiserver) from the same policy
# file by a throwaway bootstrap of its own: a third OpenBao, initialised by hand.
start_bao bg 18213 || bad "break-glass OpenBao did not start"
BGI="$(curl -s --cacert "$CA" -X POST -d "$(jq -nc --rawfile a "$T/pub1" --rawfile b "$T/pub2" --rawfile c "$T/pub3" '{recovery_shares:3,recovery_threshold:2,recovery_pgp_keys:[$a,$b,$c]}')" https://127.0.0.1:18213/v1/sys/init)"
BGROOT="$(jq -r .root_token <<<"$BGI")"; printf 'X-Vault-Token: %s\n' "$BGROOT" > "$T/bg.root"
wait_active_port 18213 || bad "break-glass OpenBao never became active"
echo "=== the policies, as an enforcement: what each identity can and can not do (a fresh instance, root still held) ==="
CAB="$T/tls/ca.crt"; U="https://127.0.0.1:18213"
rq() { # TOKEN_HEADER_FILE METHOD PATH [JSON] -> HTTP status
  local f="$1" m="$2" p="$3" d="${4:-}"
  if [ -n "$d" ]; then printf '%s' "$d" | curl -s --cacert "$CAB" -H "@$f" -X "$m" -d @- -o /dev/null -w '%{http_code}' "$U$p"
  else curl -s --cacert "$CAB" -H "@$f" -X "$m" -o /dev/null -w '%{http_code}' "$U$p"; fi
}
printf '{"type":"kv","options":{"version":"2"}}' | curl -s --cacert "$CAB" -H "@$T/bg.root" -X POST -d @- -o /dev/null "$U/v1/sys/mounts/secret"
printf '{"type":"pki"}' | curl -s --cacert "$CAB" -H "@$T/bg.root" -X POST -d @- -o /dev/null "$U/v1/sys/mounts/pki"
printf '{"data":{"csr":"x"}}' | curl -s --cacert "$CAB" -H "@$T/bg.root" -X POST -d @- -o /dev/null "$U/v1/secret/data/pki/intermediate-csr"
for pol in openaether-bootstrap openaether-breakglass external-secrets-read cert-manager-pki backup-snapshot secret-seeder openaether-admin openaether-reader; do
  jq -nc --rawfile p "$V/scripts/policies/$pol.hcl" '{policy:$p}' | curl -s --cacert "$CAB" -H "@$T/bg.root" -X PUT -d @- -o /dev/null "$U/v1/sys/policies/acl/$pol"
  printf '{"policies":["%s"],"ttl":"30m","no_parent":true}' "$pol" | curl -s --cacert "$CAB" -H "@$T/bg.root" -X POST -d @- "$U/v1/auth/token/create" \
    | jq -r '.auth.client_token' | sed 's/^/X-Vault-Token: /' > "$T/tok-$pol"
done
allowed() { # DESCRIPTION ACTUAL — the policy lets the request through (the engine may still answer with an error of its own)
  [ "$2" != 403 ] && [ "$2" != 000 ] && ok "$1 (not denied: $2)" || bad "$1: denied ($2)"
}
expect() { # DESCRIPTION EXPECTED_CODE_REGEX ACTUAL
  [[ "$3" =~ ^($2)$ ]] && ok "$1 ($3)" || bad "$1: expected $2, got $3"
}
KV='{"data":{"k":"v"}}'; KVCAS='{"options":{"cas":0},"data":{"k":"v"}}'
echo "--- secret-seeder: create once, never overwrite, never read a value"
expect "seeder creates backup/s3-primary"        '200|204' "$(rq "$T/tok-secret-seeder" POST /v1/secret/data/backup/s3-primary "$KVCAS")"
expect "seeder can not overwrite it"             '403'     "$(rq "$T/tok-secret-seeder" POST /v1/secret/data/backup/s3-primary "$KV")"
expect "seeder can not read the value"           '403'     "$(rq "$T/tok-secret-seeder" GET  /v1/secret/data/backup/s3-primary)"
expect "seeder can test that it exists"          '200'     "$(rq "$T/tok-secret-seeder" GET  /v1/secret/metadata/backup/s3-primary)"
expect "seeder can not write elsewhere"          '403'     "$(rq "$T/tok-secret-seeder" POST /v1/secret/data/grafana/admin "$KVCAS")"
echo "--- openaether-admin: secrets yes, structure no"
expect "admin reads a secret"                    '200'     "$(rq "$T/tok-openaether-admin" GET  /v1/secret/data/backup/s3-primary)"
expect "admin writes a secret"                   '200|204' "$(rq "$T/tok-openaether-admin" POST /v1/secret/data/grafana/admin "$KV")"
expect "admin reads the stored CSR"               '200'     "$(rq "$T/tok-openaether-admin" GET  /v1/secret/data/pki/intermediate-csr)"
expect "admin can not overwrite the stored CSR"   '403'     "$(rq "$T/tok-openaether-admin" POST /v1/secret/data/pki/intermediate-csr "$KV")"
expect "admin can not delete the stored CSR"      '403'     "$(rq "$T/tok-openaether-admin" DELETE /v1/secret/data/pki/intermediate-csr)"
expect "admin can not destroy its versions"       '403'     "$(rq "$T/tok-openaether-admin" POST /v1/secret/destroy/pki/intermediate-csr '{"versions":[1]}')"
expect "admin can not wipe its metadata"          '403'     "$(rq "$T/tok-openaether-admin" DELETE /v1/secret/metadata/pki/intermediate-csr)"
expect "admin can not soft-delete a version (the delete endpoint)" '403' "$(rq "$T/tok-openaether-admin" POST /v1/secret/delete/pki/intermediate-csr '{"versions":[1]}')"
expect "admin can not undelete a version"         '403'     "$(rq "$T/tok-openaether-admin" POST /v1/secret/undelete/pki/intermediate-csr '{"versions":[1]}')"
# A raw HTTP path with a trailing slash is served by the same handlers; the CLI strips it, so only curl can send it (review of 4405794).
expect "admin can not delete the stored CSR with a trailing slash"  '403' "$(rq "$T/tok-openaether-admin" DELETE /v1/secret/data/pki/intermediate-csr/)"
expect "admin can not wipe its metadata with a trailing slash"      '403' "$(rq "$T/tok-openaether-admin" DELETE /v1/secret/metadata/pki/intermediate-csr/)"
[ "$(curl -s --cacert "$CAB" -H "@$T/tok-openaether-admin" "$U/v1/secret/data/pki/intermediate-csr" | jq -r '[.data.metadata.version, .data.data.csr] | @tsv')" = "$(printf '1\tx')" ] \
  && ok "the stored CSR is still version 1 and readable after every attempt" || bad "the stored CSR changed or vanished during the attempts above"
expect "admin reads a policy"                    '200'     "$(rq "$T/tok-openaether-admin" GET  /v1/sys/policies/acl/secret-seeder)"
expect "admin can not write a policy"            '403'     "$(rq "$T/tok-openaether-admin" PUT  /v1/sys/policies/acl/evil '{"policy":"path \"*\" { capabilities = [\"sudo\",\"create\",\"read\",\"update\",\"delete\",\"list\"] }"}')"
expect "admin can not mount an engine"           '403'     "$(rq "$T/tok-openaether-admin" POST /v1/sys/mounts/evil '{"type":"kv"}')"
expect "admin can not write a PKI role (allow_any_name would issue for anything)" '403' "$(rq "$T/tok-openaether-admin" POST /v1/pki/roles/evil '{"allow_any_name":true}')"
expect "admin can not touch the PKI root"        '403'     "$(rq "$T/tok-openaether-admin" DELETE /v1/pki/root)"
allowed "admin may import a signed intermediate"          "$(rq "$T/tok-openaether-admin" POST /v1/pki/intermediate/set-signed '{"certificate":"x"}')"
expect "admin can list the PKI roles"            '200|404' "$(rq "$T/tok-openaether-admin" LIST /v1/pki/roles)"
expect "admin can not mint a token"              '403'     "$(rq "$T/tok-openaether-admin" POST /v1/auth/token/create '{"policies":["root"]}')"
expect "admin can not write an auth role"        '403'     "$(rq "$T/tok-openaether-admin" POST /v1/auth/kubernetes/role/evil '{"bound_service_account_names":"*"}')"
expect "admin can not seal"                      '403'     "$(rq "$T/tok-openaether-admin" PUT  /v1/sys/seal)"
expect "admin can not read raw storage"          '403'     "$(rq "$T/tok-openaether-admin" GET  /v1/sys/raw/core/keyring)"
expect "admin can not start a root generation"   '403'     "$(rq "$T/tok-openaether-admin" PUT  /v1/sys/generate-root-token/attempt '{}')"
echo "--- the others"
expect "reader reads a secret"                   '200'     "$(rq "$T/tok-openaether-reader" GET  /v1/secret/data/grafana/admin)"
expect "reader can not write"                    '403'     "$(rq "$T/tok-openaether-reader" POST /v1/secret/data/grafana/admin "$KV")"
expect "ESO reads a secret"                      '200'     "$(rq "$T/tok-external-secrets-read" GET  /v1/secret/data/grafana/admin)"
expect "ESO can not write"                       '403'     "$(rq "$T/tok-external-secrets-read" POST /v1/secret/data/grafana/admin "$KV")"
expect "break-glass can not read a secret"       '403'     "$(rq "$T/tok-openaether-breakglass" GET  /v1/secret/data/grafana/admin)"
expect "bootstrap can not seal"                  '403'     "$(rq "$T/tok-openaether-bootstrap" PUT  /v1/sys/seal)"
expect "bootstrap can not mint a token"          '403'     "$(rq "$T/tok-openaether-bootstrap" POST /v1/auth/token/create '{"policies":["root"]}')"
expect "bootstrap can not read raw storage"      '403'     "$(rq "$T/tok-openaether-bootstrap" GET  /v1/sys/raw/core/keyring)"
expect "bootstrap can not disable the audit"     '403'     "$(rq "$T/tok-openaether-bootstrap" DELETE /v1/sys/audit/stdout)"
allowed "cert-manager may sign"                  "$(rq "$T/tok-cert-manager-pki" POST /v1/pki/sign/openaether '{"common_name":"x.openaether.local"}')"
expect "cert-manager can not read a secret"      '403'     "$(rq "$T/tok-cert-manager-pki" GET  /v1/secret/data/grafana/admin)"
allowed "snapshot may take the snapshot"         "$(rq "$T/tok-backup-snapshot" GET  /v1/sys/storage/raft/snapshot)"
expect "snapshot can not read a secret"          '403'     "$(rq "$T/tok-backup-snapshot" GET  /v1/secret/data/grafana/admin)"

echo "=== production mode (continued): break-glass ==="
jq -nc --rawfile p "$V/scripts/policies/openaether-breakglass.hcl" '{policy:$p}' | curl -s --cacert "$CA" -H "@$T/bg.root" -X PUT -d @- https://127.0.0.1:18213/v1/sys/policies/acl/openaether-breakglass -o /dev/null
printf '{"policies":["openaether-breakglass"],"ttl":"10m","no_parent":true}' | curl -s --cacert "$CA" -H "@$T/bg.root" -X POST -d @- https://127.0.0.1:18213/v1/auth/token/create | jq -r '.auth.client_token' | sed 's/^/X-Vault-Token: /' > "$T/bg.tok"
curl -s --cacert "$CA" -H "@$T/bg.root" -X POST https://127.0.0.1:18213/v1/auth/token/revoke-self -o /dev/null
BGTOK="$(sed 's/^X-Vault-Token: //' "$T/bg.tok")"
BAO_RUN() { docker run --rm --network host -e BAO_ADDR=https://127.0.0.1:18213 -e BAO_CACERT=/ca.crt -e BAO_TOKEN="$BGTOK" -v "$CA:/ca.crt:ro" --entrypoint bao "$IMAGE" "$@"; }
OTP="$(BAO_RUN operator generate-root -generate-otp 2>/dev/null)"
INITOUT="$(BAO_RUN operator generate-root -init -otp="$OTP" -format=json 2>&1)"
NONCE="$(jq -r .nonce <<<"$INITOUT" 2>/dev/null)"
OUT=""
for n in 1 2; do
  KEY="$(jq -r ".recovery_keys_base64[$((n-1))]" <<<"$BGI" | hexkey)"
  OUT="$(BAO_RUN operator generate-root -nonce="$NONCE" -format=json "$KEY" 2>&1)"
done
ENC="$(jq -r .encoded_token <<<"$OUT" 2>/dev/null)"
NEWROOT="$(BAO_RUN operator generate-root -decode="$ENC" -otp="$OTP" 2>"$T/decode.err")"
[ -n "$NEWROOT" ] && ok "break-glass produced a root token from 2 decrypted shares" || bad "break-glass failed: decode: $(head -3 "$T/decode.err" | tr "\n" " ") otp=${#OTP} enc=${#ENC}"
CODE="$(curl -s --cacert "$CA" -H "X-Vault-Token: $NEWROOT" -o /dev/null -w '%{http_code}' https://127.0.0.1:18213/v1/sys/mounts)"
[ "$CODE" = 200 ] && ok "…and that root token works" || bad "the regenerated root token answers HTTP $CODE"

echo "=== disaster recovery: a raft snapshot restored into a NEW cluster needs the escrowed seal key ==="
DRCA="$T/tls/ca.crt"
printf '{"data":{"k":"precious"}}' | curl -s --cacert "$DRCA" -H "X-Vault-Token: $NEWROOT" -X POST -d @- -o /dev/null https://127.0.0.1:18213/v1/secret/data/dr/canary
curl -s --cacert "$DRCA" -H "X-Vault-Token: $NEWROOT" -o "$T/snap.raft" -w '%{http_code}' https://127.0.0.1:18213/v1/sys/storage/raft/snapshot | grep -q 200 && [ -s "$T/snap.raft" ] && ok "snapshot taken from the source cluster" || bad "no snapshot"
restore_into() { # NAME PORT KEY_FILE -> the HTTP status of the forced restore; the target is a freshly initialised cluster
  start_bao "$1" "$2" "$3" || return 1
  local init root
  init="$(curl -s --cacert "$DRCA" -X PUT -d '{"recovery_shares":1,"recovery_threshold":1}' "https://127.0.0.1:$2/v1/sys/init")"
  root="$(jq -r .root_token <<<"$init")"; wait_active_port "$2" || return 1
  printf 'X-Vault-Token: %s\n' "$root" > "$T/$1.root"
  curl -s --cacert "$DRCA" -H "@$T/$1.root" -X POST --data-binary "@$T/snap.raft" -o /dev/null -w '%{http_code}' "https://127.0.0.1:$2/v1/sys/storage/raft/snapshot-force"
}
CODE="$(restore_into dr 18214 "$T/bg/seal/key")"; sleep 5
[ "$CODE" = 204 ] && ok "forced restore into a fresh cluster with the SAME seal key (204)" || bad "restore with the escrowed key answered $CODE"
wait_active_port 18214 && ok "the restored cluster is unsealed and active" || bad "the restored cluster is not active"
GOT="$(curl -s --cacert "$DRCA" -H "X-Vault-Token: $NEWROOT" https://127.0.0.1:18214/v1/secret/data/dr/canary | jq -r '.data.data.k // empty')"
[ "$GOT" = precious ] && ok "the secret is back, read with a token that only existed in the source cluster" || bad "the secret did not come back (got '$GOT')"
REQ="$(curl -s --cacert "$DRCA" -H "X-Vault-Token: $NEWROOT" https://127.0.0.1:18214/v1/sys/generate-root-token/attempt | jq -r '.data.required // empty')"
[ "$REQ" = 2 ] && ok "the recovery configuration is the SOURCE's (2 shares required, not the target's 1)" || bad "recovery threshold after restore: '$REQ'"
head -c 32 /dev/urandom > "$T/other.key"
CODE2="$(restore_into drbad 18215 "$T/other.key")"; sleep 5
GOT2="$(curl -s --cacert "$DRCA" -H "X-Vault-Token: $NEWROOT" https://127.0.0.1:18215/v1/secret/data/dr/canary | jq -r '.data.data.k // empty')"
[ "$GOT2" != precious ] && ok "with ANOTHER seal key the data does not come back (restore answered $CODE2): the key is the data" || bad "the data came back with the wrong key"

echo "=== seal key rotation: new key + previous key, then the previous key dropped ==="
RA="https://127.0.0.1:18216"
start_bao rot 18216 && ok "OpenBao with key 1" || bad "rot did not start"
ROTINIT="$(curl -s --cacert "$DRCA" -X PUT -d '{"recovery_shares":1,"recovery_threshold":1}' "$RA/v1/sys/init")"
printf 'X-Vault-Token: %s\n' "$(jq -r .root_token <<<"$ROTINIT")" > "$T/rot.root"; wait_active_port 18216
printf '{"type":"kv","options":{"version":"2"}}' | curl -s --cacert "$DRCA" -H "@$T/rot.root" -X POST -d @- -o /dev/null "$RA/v1/sys/mounts/secret"
printf '{"data":{"k":"survives"}}' | curl -s --cacert "$DRCA" -H "@$T/rot.root" -X POST -d @- -o /dev/null "$RA/v1/secret/data/x"
head -c 32 /dev/urandom > "$T/rot/seal/key.new"
mv "$T/rot/seal/key" "$T/rot/seal/previous-key"; mv "$T/rot/seal/key.new" "$T/rot/seal/key"; chmod a+r "$T/rot/seal/"*
run_bao rot 18216; wait_active_port 18216 && ok "restart with key 2 + previous-key 1: unsealed" || bad "no unseal with key 2 + previous key"
GOT="$(curl -s --cacert "$DRCA" -H "@$T/rot.root" "$RA/v1/secret/data/x" | jq -r '.data.data.k // empty')"
[ "$GOT" = survives ] && ok "the data is intact after the rotation" || bad "data after rotation: '$GOT'"
sleep 3; rm -f "$T/rot/seal/previous-key"
run_bao rot 18216; wait_active_port 18216 && ok "restart with key 2 only: unsealed (the previous key is no longer needed)" || bad "no unseal with key 2 alone"

echo "=== TLS 1.3 only, the metrics listener, and the certificate reload (the real template, entrypoint and cert-reload.sh) ==="
TLSD="$T/tls-rl"; mkdir -p "$TLSD"; cp "$T/tls/tls.crt" "$T/tls/tls.key" "$T/tls/ca.crt" "$TLSD/"; chmod a+r "$TLSD"/*
export TLS_DIR="$TLSD"
start_bao tls 18221 && ok "OpenBao started with the TLS directory of the test" || bad "tls instance did not start"
AP="127.0.0.1:18221"; MP="127.0.0.1:19221"; CA="$T/tls/ca.crt"
serial() { echo | openssl s_client -connect "$1" -CAfile "$CA" 2>/dev/null | openssl x509 -noout -serial 2>/dev/null; }
# Captured into variables: under pipefail a `grep -q` that exits early makes the producer die of SIGPIPE and the test flap.
hs() { echo | openssl s_client -connect "$@" 2>&1; }
refused() { local o; o="$(hs "$@")"; grep -q -E 'alert (protocol version|handshake failure)|alert number (40|70)|no protocols available|unsupported protocol' <<<"$o"; }
accepted() { local o; o="$(hs "$@")"; grep -q 'Verify return code: 0' <<<"$o"; }
accepted "$AP" -tls1_3 -CAfile "$CA" && ok "TLS 1.3 is accepted and the certificate verifies" || bad "TLS 1.3 handshake failed"
refused "$AP" -tls1_2 && ok "the API listener refuses TLS 1.2" || bad "TLS 1.2 was not refused on 8200"
refused "$MP" -tls1_2 && ok "the metrics listener refuses TLS 1.2" || bad "TLS 1.2 was not refused on 8202"
for g in X25519 secp384r1 secp256r1; do
  accepted "$AP" -tls1_3 -groups "$g" -CAfile "$CA" && ok "key exchange $g is accepted" || bad "key exchange $g was refused"
done
refused "$AP" -tls1_3 -groups secp521r1 && ok "a curve outside tls_key_exchange_preferences (secp521r1) is refused" || bad "secp521r1 was accepted"
if curl -s --cacert "$CA" --tls-max 1.2 -o /dev/null "https://$AP/v1/sys/health"; then bad "curl limited to TLS 1.2 got an answer"; else ok "a TLS 1.2 client (curl --tls-max 1.2) gets no answer"; fi
MCODE() { curl -s --cacert "$CA" -o "${2:-/dev/null}" -w '%{http_code}' "https://$1/v1/sys/metrics?format=prometheus"; }
[ "$(MCODE "$AP")" != 200 ] && ok "the API listener does not serve the metrics without a token" || bad "8200 served the metrics unauthenticated"
[ "$(MCODE "$MP" "$T/metrics.sealed")" = 200 ] && ok "the metrics listener serves them without a token, SEALED and uninitialised" || bad "no metrics from a sealed pod"
grep -E '^vault_core_unsealed(\{[^}]*\})? 0$' "$T/metrics.sealed" >/dev/null && ok "a sealed pod reports vault_core_unsealed 0 (the alert reads this)" || bad "sealed pod: $(grep core_unsealed "$T/metrics.sealed")"
INIT="$(curl -s --cacert "$CA" -X PUT -d '{"recovery_shares":1,"recovery_threshold":1}' "https://$AP/v1/sys/init")"
wait_active_port 18221 && ok "initialised and unsealed" || bad "tls instance not active"
sleep 12   # the metrics loop emits the raft and autopilot gauges every ~10 s
MCODE "$MP" "$T/metrics.active" >/dev/null
python3 -I - "$V/../../observability-openbao/vmrule.yaml" "$T/metrics.active" <<'PY' && ok "every vault_* metric the OpenBao alerts read is served by this OpenBao" || bad "an alert reads a metric this OpenBao does not serve"
import re, sys, yaml
served = set(re.findall(r'^(vault_[A-Za-z0-9_]+)', open(sys.argv[2]).read(), re.M))
names = set()
for d in yaml.safe_load_all(open(sys.argv[1])):
    for g in d["spec"]["groups"]:
        for r in g["rules"]:
            names |= set(re.findall(r'\b(vault_[A-Za-z0-9_]+)', r["expr"]))
missing = sorted(names - served)
print("  rules read:", ", ".join(sorted(names)), "| missing:", missing or "none")
sys.exit(1 if missing or not names else 0)
PY
grep -q '^vault_core_unsealed{cluster="vault-cluster-[^"]*"} 1$' "$T/metrics.active" && ok "an unsealed pod reports vault_core_unsealed 1 under its cluster name" || bad "unsealed gauge missing"
docker exec "bao-test-tls" sh -c 'netstat -tln' 2>/dev/null | grep -q '127.0.0.1:8203' && ! docker exec "bao-test-tls" sh -c 'netstat -tln' | grep -E '(:::|0\.0\.0\.0:)8203' >/dev/null \
  && ok "the metrics listener's cluster port is on the loopback only" || bad "port 8203 is not confined to the loopback"

# Certificate reload: a new pair is written (as kubelet does for a renewed Secret) and the sidecar signals the server.
BEFORE="$(serial "$AP")"
docker run -d --label "oa-test=$$" --name bao-test-tls-reload --pid container:bao-test-tls --user "$(id -u):$(id -g)" -e OA_RELOAD_INTERVAL=1 \
  -v "$T/conf:/bao/config:ro" -v "$TLSD:/bao/tls:ro" --entrypoint /bin/sh "$IMAGE" /bao/config/cert-reload.sh >/dev/null
sleep 3
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout "$TLSD/new.key" -out "$T/new.csr" -subj "/CN=localhost" >/dev/null 2>&1
openssl x509 -req -in "$T/new.csr" -CA "$T/tls/ca.crt" -CAkey "$T/tls/ca.key" -set_serial 4242 -out "$TLSD/new.crt" -days 2 -extfile "$T/tls/san.ext" >/dev/null 2>&1
mv "$TLSD/new.key" "$TLSD/tls.key"; mv "$TLSD/new.crt" "$TLSD/tls.crt"; chmod a+r "$TLSD"/*
AFTER="$BEFORE"; for _ in $(seq 1 20); do AFTER="$(serial "$AP")"; [ "$AFTER" != "$BEFORE" ] && break; sleep 1; done
[ "$AFTER" != "$BEFORE" ] && ok "the API listener serves the renewed certificate without a restart ($BEFORE -> $AFTER)" || bad "certificate not reloaded"
[ "$(serial "$MP")" = "$AFTER" ] && ok "so does the metrics listener" || bad "the metrics listener kept the old certificate"
RELOADLOG="$(docker logs bao-test-tls-reload 2>&1)"; grep -q 'SIGHUP sent to bao' <<<"$RELOADLOG" && ok "the sidecar logged the signal" || bad "no signal in the sidecar log: $(tail -2 <<<"$RELOADLOG")"
[ "$(docker inspect -f '{{.State.Running}}' bao-test-tls)" = true ] && ok "the server survived the SIGHUP" || bad "the server stopped"
curl -s --cacert "$CA" -H "X-Vault-Token: nope" -o /dev/null "https://$AP/v1/sys/mounts"
sleep 1; TLSLOG="$(docker logs bao-test-tls 2>&1)"
grep -q '"type":"request"' <<<"$TLSLOG" && ok "the audit device still writes to stdout after the reload" || bad "no audit line after the reload"
grep -q -E '^\{"time":"[^"]+","type":"(request|response)"' <<<"$TLSLOG" && ok "audit lines start with {\"time\",\"type\" (the Alloy log_type selector keys on it)" || bad "unexpected audit line shape"
TLSLOG="$(docker logs bao-test-tls 2>&1)"
grep -q -E '^\{"@level"' <<<"$TLSLOG" && ok "the server log is JSON too (log_format = json)" || bad "server log is not JSON"
unset TLS_DIR

echo "=== OA_TLS_MIN_VERSION: tls12 is the documented way back; anything else is refused ==="
BAO_ENV="-e OA_TLS_MIN_VERSION=tls12" start_bao old 18222 && ok "OpenBao started with OA_TLS_MIN_VERSION=tls12" || bad "old instance did not start"
accepted 127.0.0.1:18222 -tls1_2 -CAfile "$CA" && ok "…and then accepts TLS 1.2" || bad "TLS 1.2 refused although tls12 was asked for"
if docker run --rm --user "$(id -u):$(id -g)" -e OA_TLS_MIN_VERSION=tls11 -e POD_NAMESPACE=x -v "$T/conf:/bao/config:ro" -v "$T/tls:/bao/seal:ro" \
     --entrypoint /bin/sh "$IMAGE" /bao/config/entrypoint.sh >"$T/badtls.out" 2>&1; then bad "tls11 was accepted"; else grep -q 'must be tls12 or tls13' "$T/badtls.out" && ok "an unknown value stops the entrypoint with a message" || bad "no message: $(cat "$T/badtls.out")"; fi

printf '%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$PASS" -gt 0 ] && [ "$FAIL" -eq 0 ]
