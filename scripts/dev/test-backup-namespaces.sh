#!/usr/bin/env bash
# The Flux Kustomizations of the backup bricks carry no targetNamespace, so every namespaced object they render must name its
# own namespace. A generated ConfigMap does not unless the generator says so (lab 19: `backup-databases` stayed NotReady with
# "ConfigMap/backup-verify-scripts-... namespace not specified"). Static: no cluster.
set -euo pipefail
cd "$(dirname "$0")/../.."
rc=0
for d in backup/databases backup/etcd backup/openbao backup/databases-identity backup/openbao-identity; do
  bad="$(kubectl kustomize "apps/base/$d" | python3 -I -c '
import sys, yaml
CLUSTER = {"Namespace","ClusterRole","ClusterRoleBinding","CustomResourceDefinition","StorageClass","PersistentVolume",
           "ClusterPolicy","ClusterSecretStore","ClusterIssuer","ValidatingWebhookConfiguration","MutatingWebhookConfiguration","PriorityClass"}
for d in yaml.safe_load_all(sys.stdin):
    if d and d["kind"] not in CLUSTER and not d["metadata"].get("namespace"):
        print(d["kind"] + "/" + d["metadata"]["name"])')"
  if [[ -z "$bad" ]]; then echo "ok   $d: every namespaced object names its namespace"
  else echo "FAIL $d: no namespace on: $(tr '\n' ' ' <<<"$bad")"; rc=1; fi
done
exit $rc
