#!/usr/bin/env bash
# PROBE for apps #119 (not a CI test): does the opt-in write-ahead log of `loki.write` keep log lines that a plain restart
# or an exhausted retry would lose? It runs the `loki.write "default"` block of apps/base/observability/alloy.yaml, with
# a `wal` block added, in the real Alloy of the pinned chart, against a stand-in for Loki that is DOWN, and prints a
# verdict per scenario:
#   restart : lines are read while the receiver is down, Alloy is killed, a new Alloy starts on the same storage path
#             with nothing left to read, the receiver comes up. Lines that arrive came from the WAL.
#   retries : the receiver stays down until the (shortened) retries are exhausted, then comes up with Alloy still running.
# Result on Alloy v1.20.1 (2026-10-19): restart = NO replay, retries = lines dropped ("no retries left, dropping data"),
# with and without the wal block. So the WAL is NOT enabled in alloy.yaml; re-run this on every Alloy bump and enable it
# when both say YES (then turn this into a test and add it to CI). Needs docker and python3 with PyYAML.
#   Usage: scripts/dev/probe-alloy-wal.sh [--require]     (--require: exit 1 unless both scenarios say YES)
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="${ALLOY_IMAGE:-docker.io/grafana/alloy:v1.20.1}"   # appVersion of the alloy chart 1.13.1 pinned in alloy.yaml
command -v docker >/dev/null && command -v python3 >/dev/null || { echo "docker and python3 are required" >&2; exit 2; }
W="$(mktemp -d)"; PORT=39321; RPID=""
trap 'docker rm -f walA walB >/dev/null 2>&1; [ -n "$RPID" ] && kill "$RPID" 2>/dev/null; rm -rf "$W"' EXIT
U="$(id -u):$(id -g)"

python3 -I - "$ROOT/apps/base/observability/alloy.yaml" "$W" "$PORT" <<'PY' || { echo "cannot build the test config" >&2; exit 2; }
import re, sys, yaml
for d in yaml.safe_load_all(open(sys.argv[1])):
    if d and d.get("kind") == "HelmRelease" and d["metadata"]["name"] == "alloy":
        content = d["spec"]["values"]["alloy"]["configMap"]["content"]
m = re.search(r'loki\.write "default" \{.*?\n\}\n', content, re.S)
if not m:
    sys.exit('no loki.write "default" block')
b = re.sub(r'url = "[^"]*"', f'url = "http://127.0.0.1:{sys.argv[3]}/loki/api/v1/push"', m.group(0))
wal = '''  wal {
    enabled            = true
    max_segment_age    = "2h"
    min_read_frequency = "250ms"
    max_read_frequency = "5s"
    drain_timeout      = "30s"
  }
'''
b = b.rstrip().rstrip("}") + wal + "}\n"
src = '''
local.file_match "in" {
  path_targets = [{"__path__" = "/in/app.log", "namespace" = "t"}]
}
loki.source.file "in" {
  targets    = local.file_match.in.targets
  forward_to = [loki.write.default.receiver]
}
'''
fast = re.sub(r"max_backoff_retries = \d+", "max_backoff_retries = 3", re.sub(r'max_backoff_period\s+= "[^"]*"', 'max_backoff_period = "300ms"', b))
open(f"{sys.argv[2]}/shipped.alloy", "w").write(b + src)
open(f"{sys.argv[2]}/fast.alloy", "w").write(fast + src)
open(f"{sys.argv[2]}/empty.alloy", "w").write(b)
PY
cat > "$W/recv.py" <<'PY'
import http.server, sys
out = sys.argv[1]
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        open(out, "ab").write(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        self.send_response(204); self.end_headers()
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[2])), H).serve_forever()
PY
run() { # <scenario> -> number of marker lines the receiver saw
  local sc="$1" cfg=shipped.alloy; [ "$sc" = retries ] && cfg=fast.alloy
  rm -rf "$W/data" "$W/in" "$W/got.bin"; mkdir -p "$W/data" "$W/in"; : > "$W/got.bin"; chmod -R a+rwX "$W/data" "$W/in" "$W/got.bin"
  echo "WALMARKER-$sc-1" > "$W/in/app.log"; echo "WALMARKER-$sc-2" >> "$W/in/app.log"
  [ "$sc" = control ] && { python3 -I "$W/recv.py" "$W/got.bin" "$PORT" & RPID=$!; sleep 1; }
  docker run -d --name walA --user "$U" --network host -v "$W/$cfg:/c.alloy:ro" -v "$W/in:/in:ro" -v "$W/data:/data" "$IMAGE" run \
    --server.http.listen-addr=127.0.0.1:12391 --storage.path=/data /c.alloy >/dev/null
  if [ "$sc" = control ]; then sleep 5
  elif [ "$sc" = restart ]; then
    sleep 10; docker kill walA >/dev/null; docker rm -f walA >/dev/null; rm -f "$W/in/app.log"
    docker run -d --name walB --user "$U" --network host -v "$W/empty.alloy:/c.alloy:ro" -v "$W/data:/data" "$IMAGE" run \
      --server.http.listen-addr=127.0.0.1:12392 --storage.path=/data /c.alloy >/dev/null
  else
    sleep 15
  fi
  [ "$sc" = control ] || { python3 -I "$W/recv.py" "$W/got.bin" "$PORT" & RPID=$!; }
  sleep 20
  docker rm -f walA walB >/dev/null 2>&1; kill "$RPID" 2>/dev/null; RPID=""
  grep -a -c "WALMARKER-$sc" "$W/got.bin"
}
RC=0
[ "$(run control)" -ge 1 ] || { echo "harness check failed: with the receiver up the lines did not arrive" >&2; exit 2; }
for sc in restart retries; do
  n="$(run "$sc")"
  if [ "$n" -ge 1 ]; then echo "$sc: YES, the WAL delivered the lines"; else echo "$sc: NO, the lines were lost"; RC=1; fi
done
[ "${1:-}" = "--require" ] && exit "$RC"
exit 0
