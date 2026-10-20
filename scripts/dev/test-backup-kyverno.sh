#!/usr/bin/env bash
# The workloads of the backup bricks (the dumps, the store replication, the verifier and its exporter, the etcd snapshot) and
# of the CNPG brick (operator, Barman Cloud plugin) pass every Kyverno policy of apps/base/kyverno-policies with the Kyverno CLI
# (1.19.x, $KYVERNO or PATH): the image registries, the security context per container, the resource limits. Static: no cluster.
set -euo pipefail
cd "$(dirname "$0")/../.."
ky="${KYVERNO:-$(command -v kyverno || true)}"
[[ -x "$ky" ]] || { echo "kyverno CLI not found (PATH or \$KYVERNO)" >&2; exit 2; }
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
rc=0
for d in backup/databases backup/etcd backup/openbao cnpg storage-restore-test; do
  kubectl kustomize "apps/base/$d" | python3 -I -c '
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.stdin) if d and d["kind"] in ("Deployment", "CronJob", "Job", "Pod")]
yaml.safe_dump_all(docs, open(sys.argv[1], "w"))' "$W/wl.yaml"
  out="$("$ky" apply apps/base/kyverno-policies/ --resource "$W/wl.yaml" 2>&1)" || true
  line="$(grep -E '^pass: [0-9]+, fail: [0-9]+' <<<"$out" | tail -1)"
  if [[ "$line" == *"fail: 0"* && "$line" == *"error: 0"* && "$line" != "" ]]; then echo "ok   $d: $line"
  else echo "FAIL $d: ${line:-no verdict}"; grep -E 'failed:' <<<"$out" | head -3; rc=1; fi
done
exit $rc
