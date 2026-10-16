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
| The OpenBao seal-key escrow not attested, or attested too long ago (also when the check has never succeeded) | `OpenBaoEscrowNotAttested` | `vmrule-backup.yaml` |
| Backups: a restore test or CronJob that no longer runs, WAL archive failing, base backup failed or stale | `RestoreTestStale`, `BackupCronJobMissing`, `CnpgWalArchiveFailing`, `CnpgBackupFailed`, `CnpgBackupStale` | `vmrule-backup.yaml` |

## Not covered (and why)

- **A silent disk, clock, policy-denial, Grafana-error or restore-test rule.** Not every family has an `*Absent` guard:
  `NodeFilesystem*` (a mount point Talos does not report as `/var` disables both disk rules without a sound),
  `NodeClock*`, `CiliumPolicyDeniedBurst`, `GrafanaServerErrors` and `RestoreTestStale` have none. `task check-alerts` on a running cluster
  is the check.

- **OpenBao authentication failures, root-token use, seal and raft state.** The audit log reaches Loki, but no Loki ruler
  evaluates it, and the OpenBao rules in `foundation/vault/prometheusrule.yaml` are not deployed (they use the
  Prometheus Operator CRDs, which this platform does not install); porting them is a separate change.
- **`kubectl exec`, new cluster-admin bindings, RBAC changes.** They need the API server audit log, which is not
  collected.
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
ignores background scans (they recount existing violations on every pass); the report above is where those are read.
Review it before switching any policy from Audit to Enforce.
