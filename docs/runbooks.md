# Runbooks

Every alert of `apps/base/observability*/` carries a `runbook_url` annotation that points at its heading below (or at
a README that already holds the procedure). `scripts/dev/test-vmrules.sh` fails when an alert has none, when the file
does not exist or when the anchor has no heading. Each entry is three lines: what to **check** first, what to **do**.
Commands assume `kubectl` access as an operator. Rung of this page: written, not drilled (see `docs/detection.md`).

## Backups

### BackupJobFailed

- Check: `kubectl -n <ns> logs job/<job>`; the restic check is part of the run, so a failed integrity check lands here too.
- Do: Fix the cause named in the log (repository password, bucket, S3 key), then `kubectl -n <ns> create job --from=cronjob/<cronjob> manual-$(date +%s)`.

### BackupCronJobStale

- Check: `kubectl -n <ns> get cronjob <cronjob> -o wide` (last schedule), then controller-manager health.
- Do: Find why the CronJob no longer fires (suspended, controller-manager down, schedule removed); trigger one run by hand once fixed.

### BackupCronJobSuspended

- Check: `kubectl -n <ns> get cronjob <cronjob> -o jsonpath='{.spec.suspend}'`.
- Do: Unsuspend it, or remove the suspension from git: Flux reverts a hand edit.

### RestoreTestStale

- Check: `kubectl -n <ns> get cronjob,job | grep restore-test`.
- Do: Run the restore test once by hand from its CronJob; if it fails, treat the last backup as unproven.

### BackupCronJobMissing

- Check: `kubectl get cronjob -A | grep -E 'openbao-snapshot|cnpg-dump'`.
- Do: Flux deleted or never created it: check the `backup-*` Kustomizations (`flux get ks`).

### CnpgWalArchiveFailing

- Check: `kubectl -n <ns> logs <cluster>-1 -c postgres | grep -i archive`; the S3 secret and bucket.
- Do: Restore the archive credentials or bucket; a stuck WAL fills the primary disk, so act within hours.

### CnpgBackupFailed

- Check: `kubectl -n <ns> get backup` and the failed Backup's `.status.error`.
- Do: Fix the S3 access, then create a new `Backup` object for the cluster.

### CnpgBackupStale

- Check: `kubectl -n <ns> get scheduledbackup,backup`.
- Do: Check the ScheduledBackup is not suspended and the last Backup completed; run one by hand.

### CnpgMetricsAbsent

- Check: `kubectl -n <ns> get pods -l cnpg.io/cluster`; the `cnpg` VMPodScrape; netpol to port 9187.
- Do: Restore the scrape; until then the CNPG backup and WAL alerts are blind.

## Cluster and nodes

### NodeUnreachable

- Check: `kubectl get nodes`; provider console for the machine state.
- Do: Reboot at the provider if the VM is up but silent; replace the node otherwise (`docs/disaster-recovery.md` in OpenAether-infra).

### NodeNotReady

- Check: `kubectl describe node <node>`; kubelet and containerd state.
- Do: Same as NodeUnreachable; drain first if the node still answers.

### NodeUnderPressure

- Check: `kubectl describe node <node>` (condition), `kubectl top node`.
- Do: Memory: find the biggest pods, raise the node size. Disk: see NodeFilesystemFillingUp. PID: find the forking pod.

### NodeFilesystemFillingUp

- Check: `kubectl debug node/<node>` is not needed: read the Grafana node dashboard; local-path volumes and logs live on /var.
- Do: Free space (old images, big local-path volumes) or enlarge the disk.

### NodeFilesystemFullIn24h

- Check: Same as NodeFilesystemFillingUp, and find what grew in the last day.
- Do: Stop the growth before the node reaches DiskPressure (pods get evicted).

### NodeClockSkew

- Check: Node exporter `node_timex_offset_seconds`; Talos time status `talosctl time`.
- Do: Check the NTP servers are reachable from the node (egress UDP 123).

### NodeClockNotSynchronised

- Check: `talosctl time -n <node>`.
- Do: Same as NodeClockSkew; informational until it persists.

### PodCrashLooping

- Check: `kubectl -n <ns> logs <pod> --previous`; `kubectl -n <ns> describe pod <pod>`.
- Do: Fix the cause in the log (config, secret, OOM: see ContainerOOMKilled).

### PersistentVolumeFillingUp

- Check: `kubectl -n <ns> exec <pod> -- df -h`.
- Do: Grow the PVC (`spec.resources.requests.storage`, Longhorn expands online) or purge data.

### KubeStateMetricsAbsent

- Check: `kubectl -n services-observability get pods -l app.kubernetes.io/name=kube-state-metrics`.
- Do: Restore kube-state-metrics: every `kube_*` alert is blind until then.

### Watchdog

- Check: It must always fire. Its absence is detected outside the cluster by the dead-man's-switch service.
- Do: If the external service pages you: see `docs/alert-delivery.md`, section Heartbeat.

## etcd

### EtcdNoLeader

- Check: `talosctl etcd status`; control-plane connectivity (port 2380).
- Do: Restore a quorum of members; do not restart all control planes at once.

### EtcdQuorumLost

- Check: `talosctl etcd members`; how many control planes are up.
- Do: Bring members back; only if two of three are lost for good, follow the etcd disaster-recovery procedure of OpenAether-infra.

### EtcdMemberDown

- Check: `talosctl -n <node> service etcd`.
- Do: Reboot the node; if the member stays out, remove and re-add it.

### EtcdLeaderFlapping

- Check: `talosctl etcd status`; network latency and disk fsync (see NodeIoPressure).
- Do: Find the slow disk or network between control planes.

## Platform

### FluxReconciliationStalled

- Check: `flux get all -A | grep -v True`; `kubectl -n flux-system logs deploy/<controller>`.
- Do: Fix the manifest error or the dependency named in the message.

### FluxObjectNotReady

- Check: `flux get ks,hr -A`; `kubectl describe` the object.
- Do: Fix the cause in the status message; a Stalled HelmRelease needs `flux suspend`/`resume`.

### FluxObjectSuspended

- Check: `flux get ks,hr -A | grep -i suspended`.
- Do: Resume it, or remove the suspension from git. The `alloy-audit` HelmRelease is excluded by design.

### FluxSourceNotVerified

- Check: `kubectl -n flux-system describe gitrepository openaether`.
- Do: The commit is not signed by an allowed key: sign it, or fix the keys of the verification (`docs/signed-commits.md`).

### FluxStateMetricsAbsent

- Check: The Flux-only kube-state-metrics release and its scrape.
- Do: Restore them; Flux failure alerts are blind meanwhile (the Flux Alert to Alertmanager still works).

### CiliumPolicyImportFailed

- Check: `kubectl -n kube-system logs ds/cilium | grep -i policy`.
- Do: Fix the rejected CiliumNetworkPolicy named in the log.

### CiliumEndpointRegenerationFailing

- Check: `cilium-dbg status` in the agent pod.
- Do: Restart the agent on that node if it persists.

### CiliumAgentMissing

- Check: `kubectl -n kube-system get ds cilium`.
- Do: Fix the DaemonSet (image pull, node taint); a node without an agent has no networking.

### CiliumPolicyDeniedBurst

- Check: `hubble`-less: agent metric `cilium_drop_count_total{reason="Policy denied"}`, then the agent log.
- Do: Find the denied flow; add the missing policy rule, or ignore a known probe.

### CertificateExpiringSoon

- Check: `kubectl get certificate -A`; `kubectl describe certificate <name>`.
- Do: See why cert-manager did not renew (issuer not Ready, challenge failing).

### CertificateNotReady

- Check: `kubectl describe certificate <name>`; the CertificateRequest and Order.
- Do: Fix the issuer or the challenge.

### KyvernoPolicyViolation

- Check: `kubectl get polr -A` (see `docs/detection.md`).
- Do: Fix the offending object, or decide the policy is wrong.

### KyvernoAdmissionDown

- Check: `kubectl -n kyverno get pods`.
- Do: Restore the admission controller; an Enforce policy with a dead webhook can block deployments.

### KyvernoControllerNotReady

- Check: `kubectl -n kyverno get deploy`.
- Do: Restore the replica (resources, node).

### KyvernoMetricsAbsent

- Check: The `kyverno` VMServiceScrape.
- Do: Restore the scrape.

## Logging

### AlloyNotRunningEverywhere

- Check: `kubectl -n services-observability get ds alloy`.
- Do: Fix the nodes where it does not run (taints, resources).

### AlloyAbsent

- Check: `kubectl -n services-observability get ds alloy`; the `alloy` HelmRelease.
- Do: Restore it: no pod log is collected meanwhile.

### AlloyDroppingLogs

- Check: `kubectl -n services-observability logs ds/alloy | grep -i drop`; Loki health.
- Do: Loki unreachable longer than the buffer: restore Loki; lines already dropped are lost.

### AlloyNotShipping

- Check: Alloy log and Loki readiness.
- Do: Fix the connection to Loki (netpol, Loki replicas).

### AlloyMetricsAbsent

- Check: The `alloy` VMPodScrape.
- Do: Restore the scrape.

### LokiPushErrors

- Check: `kubectl -n services-observability logs sts/loki`; the object store.
- Do: Fix the store credentials or the disk.

### LokiReplicaNotReady

- Check: `kubectl -n services-observability get pods -l app.kubernetes.io/name=loki`.
- Do: Fix the pod (PVC, memory).

### LokiDiscardingLogs

- Check: The `reason` label: rate_limited, line_too_long, too_far_behind...
- Do: Raise the limit or fix the noisy source.

### LokiMetricsAbsent

- Check: The `loki` VMPodScrape.
- Do: Restore the scrape.

### LokiIngestionBurst

- Check: Which namespace logs more: Grafana Explore, `sum by (namespace)(rate({}[5m]))`.
- Do: Fix a log-spamming pod before retention costs grow.

### GrafanaServerErrors

- Check: `kubectl -n services-observability logs deploy/grafana`; the grafana-db.
- Do: Fix the database or the datasource.

### K8sAuditCollectorNotRunning

- Check: `kubectl -n services-observability-audit get ds alloy-audit`.
- Do: Opt-in component: restore it or remove the component.

### K8sAuditLogSilent

- Check: The audit file on the control plane and the collector log.
- Do: Restore the collector.

### K8sAuditLogNeverRead

- Check: The hostPath and capabilities of the collector (lab check needed).
- Do: See `docs/detection.md`; read the collector log.

## Security (OpenBao)

### OpenBaoRaftNoFailureTolerance

- Check: `bao operator raft list-peers`.
- Do: Bring the missing replica back; one more loss means no quorum.

### OpenBaoRaftApplyLag

- Check: Disk latency of the node (see NodeIoPressure) and raft logs.
- Do: Move or fix the slow volume.

### OpenBaoMetricsDown

- Check: The OpenBao VMServiceScrape (TLS) and the metrics listener.
- Do: Restore the scrape; the seal alerts are blind meanwhile.
