# Detection: what raises an alert, what does not

The rules live in `apps/base/observability/vm-customresources/vmrule-*.yaml` and are evaluated by VMAlert; Alertmanager
routes them to Slack (`vmalert.yaml`). Rung of everything below: written and parsed (`scripts/dev/test-vmrules.sh`; the
logic of `OpenBaoEscrowNotAttested` and `KyvernoMetricsAbsent` is also run on synthetic series, `scripts/dev/vmrules-unittest.yaml`),
**not yet fired on a cluster**; the thresholds are starting values to tune during the first week. `task check-alerts` (in
OpenAether-infra) asks a running cluster which metrics the rules read have no data.

## Covered

| Scenario | Alert | File |
|---|---|---|
| Alloy missing on a node, absent, dropping or not shipping logs | `AlloyNotRunningEverywhere`, `AlloyAbsent`, `AlloyDroppingLogs`, `AlloyNotShipping` | `vmrule-logging.yaml` |
| Loki refusing pushes, a replica down, lines discarded, an ingestion burst | `LokiPushErrors`, `LokiReplicaNotReady`, `LokiDiscardingLogs`, `LokiIngestionBurst` | `vmrule-logging.yaml` |
| A rule blind because its metrics vanished (Kyverno's only when Kyverno is deployed) | `AlloyMetricsAbsent`, `LokiMetricsAbsent`, `KyvernoMetricsAbsent`, `FluxStateMetricsAbsent`, `CnpgMetricsAbsent` | several |
| A new object breaks a Kyverno policy (Audit or Enforce) | `KyvernoPolicyViolation` | `vmrule-security.yaml` |
| The Kyverno webhook is down | `KyvernoAdmissionDown`, `KyvernoControllerNotReady` | `vmrule-security.yaml` |
| Flux object not Ready, or suspended for a day | `FluxObjectNotReady`, `FluxObjectSuspended` | `vmrule-security.yaml` |
| Network policy denials in a loop (no Hubble) | `CiliumPolicyDeniedBurst` | `vmrule-platform.yaml` |
| `/var` (EPHEMERAL, local-path volumes) filling | `NodeFilesystemFillingUp`, `NodeFilesystemFullIn24h` | `vmrule-cluster.yaml` |
| Node clock drift | `NodeClockSkew`, `NodeClockNotSynchronised` | `vmrule-cluster.yaml` |
| An OpenBao pod sealed, no active node, raft without failure tolerance or lagging in applying entries, its metrics not scraped | `OpenBaoWorkloadSealed`, `OpenBaoNoActiveNode`, `OpenBaoRaftNoFailureTolerance`, `OpenBaoRaftApplyLag`, `OpenBaoMetricsDown` | `observability-openbao/vmrule.yaml` |
| The OpenBao seal-key escrow not attested, or attested too long ago (also when the check has never succeeded) | `OpenBaoEscrowNotAttested` | `vmrule-backup.yaml` |
| Backups: a restore test or CronJob that no longer runs, WAL archive failing, base backup failed or stale | `RestoreTestStale`, `BackupCronJobMissing`, `CnpgWalArchiveFailing`, `CnpgBackupFailed`, `CnpgBackupStale` | `vmrule-backup.yaml` |

## Not covered (and why)

- **A silent disk, clock, policy-denial, Grafana-error or restore-test rule.** Not every family has an `*Absent` guard:
  `NodeFilesystem*` (a mount point Talos does not report as `/var` disables both disk rules without a sound),
  `NodeClock*`, `CiliumPolicyDeniedBurst`, `GrafanaServerErrors` and `RestoreTestStale` have none. `task check-alerts` on a running cluster
  is the check.

- **OpenBao authentication failures and root-token use.** The audit trail reaches Loki, labelled `log_type="audit"`, but no Loki
  ruler evaluates it, so nothing alerts on it; read it in Grafana Explore (`{log_type="audit"} | json | response_error != ""`,
  not tried on a cluster).
- **`kubectl exec`, new cluster-admin bindings, RBAC changes.** The API server audit log (Talos's default Metadata level)
  reaches Loki as `{log_type="k8s-audit"}` since `alloy-audit` (control planes only; read it in Explore with
  `{log_type="k8s-audit"} | json | verb="create" | objectRef_subresource="exec"`, not tried on a cluster). No rule reads it
  (no Loki ruler); `K8sAuditLogSilent` only says the file stopped arriving. The audit policy is Talos's default: it does not
  record request bodies, and nothing here changes it.
- **An empty log stream for one namespace.** Needs a Loki ruler or an Alloy `stage.metrics`; only "nothing at all is
  shipped" is detected.
- **Zitadel.** Scraped (`/debug/metrics`), but no rule reads it: its metric names are not checked against a running
  instance.
- **Who was denied by a network policy.** Hubble is off; the counter says how much, on which node, not by whom.
- **Longhorn backups and etcd snapshots.** No Longhorn metrics are scraped; the etcd snapshots are taken outside the
  cluster (OpenAether-infra).

## Reviewing policy violations

Kyverno records every Audit violation in a policy report, whether or not an alert fired:

    kubectl get cpol                          # the policies and their mode (Audit / Enforce)
    kubectl get polr -A                       # one report per namespace: pass / fail counts
    kubectl get polr -A -o json | jq -r '.items[] | .metadata.namespace as $n | .results[]? | select(.result=="fail")
        | [$n, .policy, .rule, (.resources[0].kind + "/" + .resources[0].name)] | @tsv'

`KyvernoPolicyViolation` ignores `require-security-context` until its known violations (vendored charts) are fixed, and
`require-ro-rootfs` and `require-limits` (informational by design), and ignores background scans (they recount existing
violations on every pass); the report above is where those are read.
Review it before switching any policy from Audit to Enforce.

## Log formats and levels

Set here, so that a detection rule can rely on the shape of a line (ANSSI PA-012 R1, R6 and R23 in part). Alloy ships every pod's
stdout to Loki and adds `namespace, pod, container, app, component`; only the OpenBao audit lines (`audit`) and the API server audit log (`k8s-audit`, from `alloy-audit`, plus `node`) get a `log_type`.

| Component | Format | Level | Where |
|---|---|---|---|
| OpenBao (server log and audit trail, one stdout) | JSON | info (explicit) | `foundation/vault/configmap.yaml`; audit = `log_type="audit"` |
| Flux controllers of the CAPI children (`cluster-api-clusters/flux-gotk`) | JSON | info | `--log-level`, `--log-encoding`; the management cluster's own Flux is installed by OpenAether-infra |
| External Secrets | upstream default | info | `--loglevel=info` |
| istio-cni | upstream default | info | `logLevel: info` |
| Grafana | JSON | chart default | `log.console.format: json`, requests logged (`router_logging`) |
| Zitadel | JSON | upstream default | access log scrubbed of tokens by Alloy |
| Kyverno | text | upstream default | `--loggingFormat=text` of the vendored manifest, not changed here |
| local-path-provisioner | text | default (no `--debug`) | `platform/local-path-provisioner` |

Nothing else is set explicitly, and none of this was read back from a running cluster. The Loki label `cluster` is the constant
`management`: only that cluster ships logs today.
