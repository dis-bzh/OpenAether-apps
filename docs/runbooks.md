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

## Alerting chain

### VMAlertNotifierErrors

- Check: `kubectl -n services-observability get pods -l app.kubernetes.io/name=vmalertmanager`; vmalert log; the vmalert CiliumNetworkPolicy.
- Do: restore Alertmanager or the policy. Meanwhile nothing leaves the cluster, the heartbeat included.

### VMAlertRuleEvaluationErrors

- Check: `kubectl -n services-observability logs deploy/vmalert-vmalert | grep -i error`; vmselect health.
- Do: fix the datasource or the expression named in the log.

### VMAlertRemoteWriteErrors

- Check: vmalert log, vminsert health.
- Do: restore vminsert; until then a vmalert restart resets all `for:` timers.

### AlertmanagerConfigReloadFailed

- Check: `kubectl -n services-observability logs vmalertmanager-alertmanager-0 -c config-reloader`.
- Do: fix the `configRawYaml` of `vmalert.yaml` in git (`docs/alert-delivery.md`, Routing); the old config stays in force.

### AlertmanagerAbsent

- Check: `kubectl -n services-observability get vmalertmanager,pods`; the secrets `alertmanager-slack` and `alertmanager-deadmansswitch` (the pod does not start without them).
- Do: seed the two secrets in OpenBao or fix the pod; read the Alertmanager init container log.

### AlertDeliveryProbe

- Check: not a fault. It fires 10 minutes at 06:00 and 18:00 UTC and reaches the chat once, quietly.
- Do: nothing. If it stops arriving, read AlertDeliveryNotAcknowledged and AlertDeliveryFailing.

### AlertDeliveryFailing

- Check: Alertmanager `alertmanager_notifications_failed_total{integration}` and its log (`kubectl -n services-observability logs vmalertmanager-alertmanager-0 -c alertmanager | grep -i 'notify\|error'`).
- Do: `slack`: renew the webhook in OpenBao (`secret/observability/alertmanager-slack`); `webhook`: the heartbeat URL or its host in the egress policy.

### AlertDeliveryNotAcknowledged

- Check: as AlertDeliveryFailing, and whether the chat channel or webhook still exists (a deleted webhook answers 4xx).
- Do: fix the receiver. The heartbeat is stopped meanwhile on purpose; it resumes when this resolves.

## Gateway (RED)

### GatewayErrorBudgetBurnFast

- Check: Grafana, requests by `response_code` and `destination_service_name`; `kubectl -n services-gateway get pods`; the backend named by the 5xx.
- Do: Fix or roll back the failing backend; if the gateway itself fails (503 with no backend), see its pods and the HTTPRoutes.

### GatewayErrorBudgetBurnSlow

- Check: As GatewayErrorBudgetBurnFast, over a longer window.
- Do: Find the intermittently failing backend or route; watch the ratio fall.

### GatewayLatencyP99High

- Check: Grafana, latency by `destination_service_name`; node pressure (NodeIoPressure, NodeCpuThrottled) and the backend's own saturation.
- Do: Find the slow backend or the saturated node; the threshold is a starting value to tune.

### GatewayReplicasLow

- Check: `kubectl -n services-gateway get pods -o wide` (are both on one node? the spread policy), HPA events.
- Do: Free capacity so the second replica schedules; check the spread Kyverno policy.

### GatewayMetricsAbsent

- Check: VMAgent targets page for `istio-gateway`; the `openaether-gateway` CiliumNetworkPolicy (port 15090) and vmagent egress.
- Do: Restore the scrape. UNVERIFIED path: the first lab confirms the port and metric names.

## Resources (USE)

### ContainerOOMKilled

- Check: `kubectl -n <ns> describe pod <pod>` (Last State), Grafana memory vs limit.
- Do: Raise the limit (and the request) in git, or fix the leak; the chart values carry measured figures.

### ContainerCpuThrottled

- Check: Grafana CPU usage vs limit for the container.
- Do: Raise the CPU limit or remove it where the chart allows; informational by design.

### NodeIoPressure

- Check: `kubectl debug node` is not needed: Grafana node disk panels, NodeDiskBusy and NodeDiskLatencyHigh on the same node; which pods write (Longhorn rebuild, Loki, etcd).
- Do: Move or throttle the writer, use a faster disk class (sizing in the infra docs); never run an I/O drill on a node that carries etcd.

### NodeMemoryPressureStall

- Check: `kubectl top pods -A --sort-by=memory`; ContainerOOMKilled on the node.
- Do: Reduce requests on the node or add memory.

### NodeDiskBusy

- Check: As NodeIoPressure.
- Do: As NodeIoPressure.

### NodeDiskLatencyHigh

- Check: As NodeIoPressure; etcd fsync latency on control planes.
- Do: As NodeIoPressure.

### NodeMemoryLow

- Check: `kubectl top node`, the biggest pods on it.
- Do: Reduce memory use or enlarge the node.

### NodeNetworkErrors

- Check: Provider status, `talosctl dmesg` on the node, MTU settings.
- Do: Replace or reboot the node if it persists.

### PodPendingTooLong

- Check: `kubectl -n <ns> describe pod <pod>` (Events): Insufficient cpu/memory, volume node affinity, image pull.
- Do: Free or add capacity (see ClusterCpuRequestsHigh), fix the affinity or the image.

### PdbBlocksDisruption

- Check: `kubectl -n <ns> get pdb,pods -o wide`: are replicas missing or unready?
- Do: Bring the missing replica back; do not delete the PDB.

### ClusterCpuRequestsHigh

- Check: `kubectl describe nodes | grep -A8 'Allocated resources'`.
- Do: Add nodes or lower requests; the sizing notes are in the infra docs.

### ClusterMemoryRequestsHigh

- Check: As ClusterCpuRequestsHigh.
- Do: As ClusterCpuRequestsHigh.

## Storage (Longhorn)

### LonghornVolumeDegraded

- Check: `kubectl -n longhorn-system get volumes.longhorn.io,replicas.longhorn.io`; the Longhorn UI is not exposed by default.
- Do: Wait for a running rebuild; if none, check the node and disk of the missing replica (LonghornNodeNotReady).

### LonghornVolumeFaulted

- Check: `kubectl -n longhorn-system get volumes.longhorn.io <volume> -o yaml`; restore from backup if replicas are lost.
- Do: Salvage a replica (Longhorn docs) or restore the volume from its backup.

### LonghornNodeNotReady

- Check: `kubectl -n longhorn-system get nodes.longhorn.io`; the manager pod on that node.
- Do: Fix the node (NodeNotReady) or the manager pod.

### LonghornDiskFilling

- Check: `kubectl -n longhorn-system get nodes.longhorn.io -o yaml` (disk status).
- Do: Add disk space or remove unused volumes; do not resize while degraded.

### LonghornBackupFailed

- Check: `kubectl -n longhorn-system get backups.longhorn.io`; the backup target secret and bucket.
- Do: Fix the target (credentials, bucket); the next scheduled backup retries.

### LonghornMetricsAbsent

- Check: VMAgent targets for `longhorn-manager`; the longhorn CiliumNetworkPolicy (port 9500 from vmagent).
- Do: Restore the scrape. UNVERIFIED path until a lab.

## Backup signals

### BackupNotSucceededRecently

- Check: `kubectl -n <ns> get jobs --sort-by=.status.startTime` and the logs of the latest; BackupJobFailed usually fired first.
- Do: Fix the cause named in the log and trigger one run by hand: the RPO of 24 h is already exceeded.

### RestoreTestNotSucceeding

- Check: Latest restore-test Job logs.
- Do: Fix and run the test by hand; treat the backups as unverified meanwhile.

### BackupVerificationStale

- Check: The verifier's CronJob and logs (from M2).
- Do: Fix the verifier or the backup it reports on.

### BackupVerificationFailed

- Check: The verifier's logs (from M2): which object or checksum failed.
- Do: Treat the backup as corrupt: run a fresh backup and verify it.

## Databases (CNPG)

### CnpgReplicationLagHigh

- Check: `kubectl cnpg status <cluster> -n foundation-databases` (replication section); replica node I/O (NodeIoPressure).
- Do: Find the slow replica node or disk; a replica that cannot catch up is re-cloned by deleting its pod and PVC.

### CnpgReplicaNotStreaming

- Check: `kubectl cnpg status <cluster> -n foundation-databases`; the replica log.
- Do: Fix the network or primary; re-clone the replica if it cannot reconnect.

### CnpgInstanceDown

- Check: `kubectl -n foundation-databases logs <pod> -c postgres`.
- Do: Restart the pod if it is a replica; for a primary prefer a switchover to a healthy replica first.

### CnpgWalFilesPiling

- Check: `kubectl cnpg status`; archive log; replication slots.
- Do: Fix the archive (see CnpgWalArchiveFailing) or the lagging slot; grow the volume if time is short.
