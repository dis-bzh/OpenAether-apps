# Detection: what raises an alert, what does not

The rules live in `apps/base/observability/vm-customresources/vmrule-*.yaml` and are evaluated by VMAlert; Alertmanager
routes them to Slack (`vmalert.yaml`). Rung of everything below: written and parsed (`scripts/dev/test-vmrules.sh`; the
logic of `OpenBaoEscrowNotAttested` and `KyvernoMetricsAbsent` is also run on synthetic series, `scripts/dev/vmrules-unittest.yaml`),
**not yet fired on a cluster** (what 0.4.0 M1 added, and what a lab must still see: `docs/monitoring-0.4.md`; every alert links to `docs/runbooks.md`); the thresholds are starting values to tune during the first week. `task check-alerts` (in
OpenAether-infra) asks a running cluster which metrics the rules read have no data.

## Covered

| Scenario | Alert | File |
|---|---|---|
| Alloy missing on a node, absent, dropping or not shipping logs | `AlloyNotRunningEverywhere`, `AlloyAbsent`, `AlloyDroppingLogs`, `AlloyNotShipping` | `vmrule-logging.yaml` |
| Loki refusing pushes, a replica down, lines discarded, an ingestion burst | `LokiPushErrors`, `LokiReplicaNotReady`, `LokiDiscardingLogs`, `LokiIngestionBurst` | `vmrule-logging.yaml` |
| A rule blind because its metrics vanished (Kyverno's only when Kyverno is deployed) | `AlloyMetricsAbsent`, `LokiMetricsAbsent`, `KyvernoMetricsAbsent`, `FluxStateMetricsAbsent`, `CnpgMetricsAbsent` | several |
| A new object breaks a Kyverno policy (Audit or Enforce) | `KyvernoPolicyViolation` | `vmrule-security.yaml` |
| The Kyverno webhook is down | `KyvernoAdmissionDown`, `KyvernoControllerNotReady` | `vmrule-security.yaml` |
| Flux object not Ready, or suspended for a day (the HelmRelease `alloy-audit`, suspended by design, is excluded); a commit whose signature Flux rejected | `FluxObjectNotReady`, `FluxObjectSuspended`, `FluxSourceNotVerified` | `vmrule-security.yaml` |
| Flux itself reports an `error` event (reconciliation failure, rejected signature), whatever the metrics say | Alert `flux-errors` and Provider `alertmanager` (`flux-system`), routed by Alertmanager like the rules | `apps/base/flux/notification-alertmanager.yaml` |
| Network policy denials in a loop (no Hubble) | `CiliumPolicyDeniedBurst` | `vmrule-platform.yaml` |
| `/var` (EPHEMERAL, local-path volumes) filling | `NodeFilesystemFillingUp`, `NodeFilesystemFullIn24h` | `vmrule-cluster.yaml` |
| A CNPG instance pod left Completed after a node reboot without a drain (CNPG 1.23.1 never restarts it) | `CnpgInstancePodSucceeded` | `vmrule-platform.yaml` |
| Node clock drift | `NodeClockSkew`, `NodeClockNotSynchronised` | `vmrule-cluster.yaml` |
| An OpenBao pod sealed, no active node, raft without failure tolerance or lagging in applying entries, its metrics not scraped | `OpenBaoWorkloadSealed`, `OpenBaoNoActiveNode`, `OpenBaoRaftNoFailureTolerance`, `OpenBaoRaftApplyLag`, `OpenBaoMetricsDown` | `observability-openbao/vmrule.yaml` |
| The OpenBao seal-key escrow not attested, or attested too long ago (also when the check has never succeeded) | `OpenBaoEscrowNotAttested` | `vmrule-backup.yaml` |
| Backups: a restore test or CronJob that no longer runs, WAL archive failing, base backup failed or stale | `RestoreTestStale`, `BackupCronJobMissing`, `CnpgWalArchiveFailing`, `CnpgBackupFailed`, `CnpgBackupStale` | `vmrule-backup.yaml` |
| The last SUCCESS of a backup or restore test too old (the schedule alone is not enough); the verification alerts on the M2 verifier's series | `BackupNotSucceededRecently`, `RestoreTestNotSucceeding`, `BackupVerificationStale`, `BackupVerificationFailed` | `vmrule-backup-signals.yaml` |
| A restore test that failed (canary chain, copy too old, tables empty), no pass for days, never reported, a slow recovery, a scratch Cluster left behind, the canary writer stopped | `RestoreTestFailed`, `RestoreTestNoRecentPass`, `RestoreTestNeverReported`, `RestoreRecoverySlow`, `RestoreScratchLeftover`, `CnpgCanaryNotWriting` | `vmrule-restore-test.yaml` |
| The copies of the backups (0.4.0 M2): a copy, etcd snapshot or verifier Job failed; the CNPG archive not copied to the replica store for an hour; no etcd snapshot for 36 h; the verifier not completed for 36 h; a store failing verification | `BackupCopyJobFailed`, `CnpgStoreReplicationStale`, `EtcdSnapshotStale`, `BackupVerifierStale`, `BackupStoreVerificationFailed` | `vmrule-backup-copies.yaml` |
| CNPG replica lag, replica not streaming, PostgreSQL not answering, an instance gone from the exporters, WAL files piling | `CnpgReplicationLagHigh`, `CnpgReplicaNotStreaming`, `CnpgInstanceDown`, `CnpgInstanceMissing`, `CnpgWalFilesPiling` | `vmrule-database.yaml` |
| The alerting chain itself: VMAlert to Alertmanager, rule evaluation, alert state, config reload, Alertmanager absent | `VMAlertNotifierErrors`, `VMAlertRuleEvaluationErrors`, `VMAlertRemoteWriteErrors`, `AlertmanagerConfigReloadFailed`, `AlertmanagerAbsent` | `vmrule-alerting.yaml` |
| The scheduled delivery test, notifications failing, the test not acknowledged (opt-in component, `docs/alert-delivery.md`) | `AlertDeliveryProbe`, `AlertDeliveryFailing`, `AlertDeliveryNotAcknowledged` | `observability-alert-probe/vmrule.yaml` |
| Customer path (RED) at the gateway: 5xx error-budget burn (fast, slow), p99 latency, fewer than 2 replicas; its metrics missing | `GatewayErrorBudgetBurnFast`, `GatewayErrorBudgetBurnSlow`, `GatewayLatencyP99High`, `GatewayReplicasLow`, `GatewayMetricsAbsent` | `vmrule-gateway.yaml` |
| Resources (USE): OOM kill, CPU throttling, I/O and memory pressure, disk busy or slow, memory low, network errors, Pending pods, a PDB allowing no disruption, requests near capacity | `ContainerOOMKilled`, `ContainerCpuThrottled`, `NodeIoPressure`, `NodeMemoryPressureStall`, `NodeDiskBusy`, `NodeDiskLatencyHigh`, `NodeMemoryLow`, `NodeNetworkErrors`, `PodPendingTooLong`, `PdbBlocksDisruption`, `ClusterCpuRequestsHigh`, `ClusterMemoryRequestsHigh` | `vmrule-resources.yaml` |
| Longhorn volume degraded or faulted, node not ready, disk filling, backup in error, its metrics missing | `LonghornVolumeDegraded`, `LonghornVolumeFaulted`, `LonghornNodeNotReady`, `LonghornDiskFilling`, `LonghornBackupFailed`, `LonghornMetricsAbsent` | `vmrule-storage.yaml` |
| Loki compactor no longer applying retention (the sweep runs; that chunks vanish is for a lab, #127) | `LokiRetentionNotSwept` | `vmrule-retention.yaml` |

## Not covered (and why)

- **`task check-alerts` reports three metrics without series on a healthy cluster** (lab 11, Scaleway). `kube_job_failed` is emitted
  by kube-state-metrics only for a Job that has a Failed condition, so "no series" is the quiet state of `OpenBaoBootstrapJobFailed`,
  not a blind rule (the alert keeps `kube_job_failed`: `kube_job_status_failed` would fire on every retry of a Job still inside its
  backoff limit). `kubelet_volume_stats_available_bytes` and `_capacity_bytes` come from the kubelet only for CSI volumes that
  are mounted (Longhorn): the Loki and VictoriaMetrics volumes are local-path (hostPath), so **`PersistentVolumeFillingUp` cannot
  fire for them**; `NodeFilesystemFillingUp` on `/var` is their guard.

- **A silent disk, clock, policy-denial, Grafana-error or restore-test rule.** Not every family has an `*Absent` guard:
  `NodeFilesystem*` (a mount point Talos does not report as `/var` disables both disk rules without a sound),
  `NodeClock*`, `CiliumPolicyDeniedBurst`, `GrafanaServerErrors` and `RestoreTestStale` have none. `task check-alerts` on a running cluster
  is the check.

- **OpenBao authentication failures and root-token use.** The audit trail reaches Loki, labelled `log_type="audit"`, but no Loki
  ruler evaluates it, so nothing alerts on it; read it in Grafana Explore (`{log_type="audit"} | json | response_error != ""`,
  not tried on a cluster).
- **`kubectl exec`, new cluster-admin bindings, RBAC changes.** The API server audit log (Talos's default Metadata level)
  reaches Loki as `{log_type="k8s-audit"}` once `alloy-audit` is started (off by default: opt-in component
  `apps/flux/components/audit-log-collector`; control planes only; read it in Explore with
  `{log_type="k8s-audit"} | json | verb="create" | objectRef_subresource="exec"`, not tried on a cluster). No rule reads it
  (no Loki ruler); `K8sAuditLogSilent` only says the file stopped arriving. The audit policy is Talos's default: it does not
  record request bodies, and nothing here changes it.
- **An empty log stream for one namespace.** Needs a Loki ruler or an Alloy `stage.metrics`; only "nothing at all is
  shipped" is detected.
- **Zitadel.** Scraped (`/debug/metrics`), but no rule reads it: its metric names are not checked against a running
  instance.
- **Who was denied by a network policy.** Hubble is off; the counter says how much, on which node, not by whom.
- **Etcd snapshots.** Taken outside the cluster (OpenAether-infra): nothing here sees a missing one.
- **A dead gateway or load balancer seen from outside.** The RED rules read the gateway's own metrics; no blackbox probe
  exists. Only the heartbeat covers a dead cluster.
- **Data written by the delivery test to a muted channel.** The receiver answering 2xx is the only acknowledgement.
- **Alloy keeps log batches in memory only (#119, #80).** Its write-ahead log was tried with the real Alloy of the pinned chart
  and does not replay after a hard restart (`scripts/dev/probe-alloy-wal.sh`): not enabled.
- **Metrics no cluster has shown.** `docs/monitoring-0.4.md` lists them; `scripts/dev/test-alert-metrics.py` checks offline that
  every metric an alert reads is catalogued with a scrape that exists.

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
