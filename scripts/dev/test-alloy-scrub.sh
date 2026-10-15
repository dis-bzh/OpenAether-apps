#!/usr/bin/env bash
# Run the `loki.process "scrub"` block of apps/base/observability/alloy.yaml in a real Alloy (the chart's appVersion)
# and check that a token in a query string (an OIDC logout's id_token_hint) is blanked in Zitadel's access log, that the
# parameter and the rest of the line stay, and that other namespaces and lines are not touched; and that the OpenBao
# audit lines (and only those) get the label log_type=audit. Needs docker and python3
# with PyYAML. Rung: a real Alloy reading a file, no Kubernetes, no Loki (loki.echo prints what would be pushed).
#   Usage: scripts/dev/test-alloy-scrub.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="docker.io/grafana/alloy:v1.6.1"   # appVersion of the alloy chart 0.11.0 pinned in alloy.yaml
command -v docker >/dev/null && command -v python3 >/dev/null || { echo "docker and python3 are required" >&2; exit 2; }
W="$(mktemp -d)"; NAME="alloyscrub$$"; PASS=0; FAIL=0
trap 'docker rm -f "$NAME" >/dev/null 2>&1; rm -rf "$W"' EXIT
ok()  { echo "  ok   $*"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL $*"; FAIL=$((FAIL + 1)); }

python3 -I - "$ROOT/apps/base/observability/alloy.yaml" "$W/config.alloy" <<'PY' || { echo "cannot build the test config" >&2; exit 2; }
import re, sys, yaml
for d in yaml.safe_load_all(open(sys.argv[1])):
    if d and d.get("kind") == "HelmRelease" and d["metadata"]["name"] == "alloy":
        content = d["spec"]["values"]["alloy"]["configMap"]["content"]
block = re.search(r'loki\.process "scrub" \{.*?\n\}\n', content, re.S)
if not block:
    sys.exit('no loki.process "scrub" block')
# The shipped block, with its output redirected to loki.echo and a file as the source (labels set as the relabeling would).
cfg = block.group(0).replace("loki.write.default.receiver", "loki.echo.out.receiver")
cfg += '''
loki.echo "out" {}
local.file_match "in" {
  path_targets = [
    {"__path__" = "/in/z.log", "namespace" = "services-identity"},
    {"__path__" = "/in/o.log", "namespace" = "other"},
    {"__path__" = "/in/a.log", "namespace" = "foundation-vault", "container" = "openbao"},
    {"__path__" = "/in/b.log", "namespace" = "foundation-vault", "container" = "cert-reload"},
    {"__path__" = "/in/c.log", "namespace" = "other", "container" = "openbao"},
  ]
}
loki.source.file "in" {
  targets    = local.file_match.in.targets
  forward_to = [loki.process.scrub.receiver]
}
'''
open(sys.argv[2], "w").write(cfg)
PY
mkdir "$W/in"
T='eyJhbGciOiJSUzI1NiJ9.CANARYPAYLOAD.CANARYSIG'
# The shape Zitadel v4.14 really logs (captured by test-zitadel-config.sh): the record is a JSON string inside the JSON line,
# so a quote is \" and the & of a query string is \\u0026.
cat > "$W/in/z.log" <<'LOGS'
{"level":"info","msg":"log record emitted","record":"{\"requestUrl\":\"/oidc/v1/end_session?id_token_hint=TOKEN\\u0026post_logout_redirect_uri=https://grafana.example.invalid\",\"responseStatus\":200}"}
{"level":"info","msg":"log record emitted","record":"{\"requestUrl\":\"/oauth/v2/token?client_secret=SECRETVALUE\",\"responseStatus\":400}"}
{"level":"info","msg":"log record emitted","record":"{\"requestUrl\":\"/ui/console/healthz\",\"responseStatus\":200}"}
LOGS
sed -i "s/TOKEN/$T/" "$W/in/z.log"
printf '%s\n' '{"requestUrl":"/x?id_token_hint='"$T"'"}' > "$W/in/o.log"
# OpenBao: the audit device and the server log share stdout, both JSON. Only the first line below is audit.
cat > "$W/in/a.log" <<'LOGS'
{"time":"2026-10-15T15:28:54.231377246Z","type":"request","auth":{"token_type":"default"},"request":{"operation":"read","path":"sys/mounts"}}
{"@level":"info","@message":"post-unseal setup complete","@module":"core","@timestamp":"2026-10-15T15:28:29.842673Z","type":"request"}
LOGS
printf '%s\n' '{"time":"2026-10-15T15:28:54.231377246Z","type":"response","request":{"path":"x"}}' > "$W/in/b.log"
printf '%s\n' '{"time":"2026-10-15T15:28:54.231377246Z","type":"response","request":{"path":"x"}}' > "$W/in/c.log"
chmod -R a+rX "$W"
docker run -d --name "$NAME" -v "$W/config.alloy:/etc/alloy/config.alloy:ro" -v "$W/in:/in:ro" "$IMAGE" run --server.http.listen-addr=127.0.0.1:12345 \
  --storage.path=/tmp/alloy /etc/alloy/config.alloy >/dev/null
for _ in $(seq 1 40); do   # seven lines are expected on stdout
  [ "$(docker logs "$NAME" 2>&1 | grep -c 'component_id=loki.echo.out')" -ge 7 ] && break
  sleep 1
done
docker logs "$NAME" > "$W/out.log" 2>&1
[ -n "${KEEP_OUT:-}" ] && cp "$W/out.log" "$KEEP_OUT"
grep 'component_id=loki.echo.out' "$W/out.log" > "$W/echo.log"
grep 'in/z\.log' "$W/echo.log" > "$W/zitadel.log"; grep 'in/o\.log' "$W/echo.log" > "$W/other.log"

[ "$(wc -l < "$W/zitadel.log")" = 3 ] && ok "Alloy ran the shipped block and emitted the three Zitadel lines" || bad "unexpected output: $(tail -3 "$W/out.log")"
! grep -q 'CANARYPAYLOAD' "$W/zitadel.log" && ok "the id_token_hint value is gone from the Zitadel lines" || bad "the token is still in a Zitadel line"
grep -q 'id_token_hint=REDACTED' "$W/zitadel.log" && grep -q 'post_logout_redirect_uri=https://grafana.example.invalid' "$W/zitadel.log" \
  && ok "the parameter name and the rest of the URL stay, only the value is REDACTED" || bad "the URL was not rewritten as expected"
! grep -q 'SECRETVALUE' "$W/zitadel.log" && grep -q 'client_secret=REDACTED' "$W/zitadel.log" && ok "client_secret in a query string is blanked too" || bad "client_secret value kept"
grep -q 'ui/console/healthz' "$W/zitadel.log" && ok "a line without a token is unchanged" || bad "the untouched line is missing"
grep -q 'CANARYPAYLOAD' "$W/other.log" && ok "another namespace is not touched (the selector is services-identity)" || bad "another namespace was rewritten"
grep 'in/a\.log' "$W/echo.log" > "$W/openbao.log"
[ "$(wc -l < "$W/openbao.log")" = 2 ] && [ "$(grep -c 'log_type=[^,]*audit' "$W/openbao.log")" = 1 ] && grep 'log_type=[^,]*audit' "$W/openbao.log" | grep -q 'sys/mounts' \
  && ok "of two OpenBao lines, only the audit one carries log_type=audit" || bad "OpenBao labelling: $(cat "$W/openbao.log")"
! grep 'in/b\.log\|in/c\.log' "$W/echo.log" | grep -q 'log_type' && ok "an audit-shaped line of another container or namespace is not labelled" || bad "a line outside foundation-vault/openbao got log_type"
! grep 'in/z\.log\|in/o\.log' "$W/echo.log" | grep -q 'log_type' && ok "the Zitadel lines get no log_type" || bad "log_type leaked onto another stream"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
