#!/usr/bin/env bash
# Run the audit-log pipeline of apps/base/observability/alloy-audit.yaml in a real Alloy (the chart's appVersion): it
# must read only lines appended after it starts (tail_from_end), label them log_type=k8s-audit and node, and leave the
# audit event untouched in the line. Needs docker and python3 with PyYAML. Rung: a real Alloy reading a file, no
# Kubernetes, no Loki (loki.echo prints what would be pushed), no Talos: the file is a fixture of the documented shape.
#   Usage: scripts/dev/test-alloy-audit.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="docker.io/grafana/alloy:v1.20.1"   # appVersion of the alloy chart 1.13.1 pinned in alloy-audit.yaml
command -v docker >/dev/null && command -v python3 >/dev/null || { echo "docker and python3 are required" >&2; exit 2; }
W="$(mktemp -d)"; NAME="alloyaudit$$"; PASS=0; FAIL=0
trap 'docker rm -f "$NAME" >/dev/null 2>&1; rm -rf "$W"' EXIT
ok()  { echo "  ok   $*"; PASS=$((PASS + 1)); }
bad() { echo "  FAIL $*"; FAIL=$((FAIL + 1)); }

python3 -I - "$ROOT/apps/base/observability/alloy-audit.yaml" "$W/config.alloy" <<'PY' || { echo "cannot build the test config" >&2; exit 2; }
import sys, yaml
for d in yaml.safe_load_all(open(sys.argv[1])):
    if d and d.get("kind") == "HelmRelease" and d["metadata"]["name"] == "alloy-audit":
        content = d["spec"]["values"]["alloy"]["configMap"]["content"]
# The shipped config with only its sink replaced: loki.write -> loki.echo.
start = content.index('loki.write "default"')
cfg = content[:start].replace("loki.write.default.receiver", "loki.echo.out.receiver") + 'loki.echo "out" {}\n'
open(sys.argv[2], "w").write(cfg)
PY
mkdir "$W/kube"
# A line written before Alloy starts: tail_from_end must skip it (it is the 1 GB backlog of a re-created pod).
echo '{"kind":"Event","stageTimestamp":"2026-01-01T00:00:00.000001Z","verb":"BACKLOG"}' > "$W/kube/kube-apiserver.log"
chmod -R a+rwX "$W"
docker run -d --name "$NAME" -e NODE_NAME=cp-test -v "$W/config.alloy:/etc/alloy/config.alloy:ro" -v "$W/kube:/var/log/audit/kube" \
  "$IMAGE" run --server.http.listen-addr=127.0.0.1:12345 --storage.path=/tmp/alloy /etc/alloy/config.alloy >/dev/null
for _ in $(seq 1 30); do docker logs "$NAME" 2>&1 | grep -q 'start tailing file' && break; sleep 1; done
sleep 2
cat >> "$W/kube/kube-apiserver.log" <<'LOGS'
{"kind":"Event","level":"Metadata","stage":"ResponseComplete","verb":"create","user":{"username":"system:admin"},"objectRef":{"resource":"pods","subresource":"exec","namespace":"default"},"stageTimestamp":"2026-10-16T08:30:00.123456Z"}
{"kind":"Event","level":"Metadata","stage":"ResponseComplete","verb":"get","user":{"username":"system:serviceaccount:default:x"},"objectRef":{"resource":"secrets"},"stageTimestamp":"2026-10-16T08:30:01.000001Z"}
not json at all, must still be shipped
LOGS
for _ in $(seq 1 30); do   # three lines are expected on stdout
  [ "$(docker logs "$NAME" 2>&1 | grep -c 'component_id=loki.echo.out')" -ge 3 ] && break
  sleep 1
done
docker logs "$NAME" > "$W/out.log" 2>&1
[ -n "${KEEP_OUT:-}" ] && cp "$W/out.log" "$KEEP_OUT"
grep 'component_id=loki.echo.out' "$W/out.log" > "$W/echo.log"

[ "$(wc -l < "$W/echo.log")" = 3 ] && ok "Alloy ran the shipped config and emitted the three appended lines" || bad "unexpected output: $(tail -5 "$W/out.log")"
! grep -q 'BACKLOG' "$W/echo.log" && ok "a line written before the start is not read (tail_from_end)" || bad "the backlog line was read"
[ "$(grep -cF 'log_type=\"k8s-audit\"' "$W/echo.log")" = 3 ] && ok "every line carries log_type=k8s-audit" || bad "log_type missing: $(cat "$W/echo.log")"
[ "$(grep -cF 'node=\"cp-test\"' "$W/echo.log")" = 3 ] && ok "every line carries the node name from NODE_NAME" || bad "node label missing"
grep -q 'pods' "$W/echo.log" && grep -q 'subresource' "$W/echo.log" && grep -q 'system:admin' "$W/echo.log" \
  && ok "the audit event stays whole in the line (user, verb, resource are LogQL fields, not labels)" || bad "the line was altered"
grep -q 'not json at all' "$W/echo.log" && ok "a line that is not JSON is still shipped" || bad "a non-JSON line was dropped"
! grep -E 'labels=' "$W/echo.log" | grep -qE 'verb=|username=|resource=' && ok "no high-cardinality label (verb, user, resource)" || bad "a high-cardinality label leaked"
! grep -iE 'level=(warn|error)' "$W/out.log" | grep -qi 'timestamp\|stage\|parse' && ok "no timestamp or JSON stage error in Alloy's log (the line is not valid JSON in one case, and must not raise one)" \
  || bad "a stage complained: $(grep -iE 'level=(warn|error)' "$W/out.log" | head -3)"
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
