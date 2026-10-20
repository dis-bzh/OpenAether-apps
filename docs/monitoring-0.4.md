# Monitoring in 0.4.0, milestone M1: what it adds, what it does not, what a lab must see

Rung of everything below: **mocked/static** (vmalert unit tests on synthetic series, render and policy checks, a real Alloy in
a container for the WAL probe). Nothing here has fired on a cluster. Not a compliance claim. Owner decisions used: 99.9 % customer
availability, and one heartbeat service plus one chat receiver as the only out-of-cluster dependencies.

## What M1 adds

| Area | What | Where |
|---|---|---|
| Runbooks | a `runbook_url` on every alert, one heading per alert; a test fails otherwise | `docs/runbooks.md`, `test-vmrules.sh` |
| Delivery | rules on the alerting chain itself (always on, nothing external); an opt-in scheduled delivery test with its "not acknowledged" alert | `vmrule-alerting.yaml`, component `alert-delivery-probe`, `docs/alert-delivery.md` |
| Heartbeat | Watchdog to the external dead-man's-switch (already wired); it now also stops on purpose when the chat leg is proven broken | `vmalert.yaml` |
| RED | gateway error-budget burn (fast, slow), p99, replicas, blind guard; scrape of the gateway Envoy | `vmrule-gateway.yaml`, `vmscapes.yaml` |
| USE | OOM kills, CPU throttling, node I/O and memory pressure (PSI), disk busy and latency, network errors, Pending pods, PDB allowing no disruption, CPU and memory requests near capacity | `vmrule-resources.yaml` |
| Storage | Longhorn volume degraded or faulted, node not ready, disk filling, backup in error, blind guard; scrape of the managers | `vmrule-storage.yaml`, `vmscapes.yaml` |
| Databases | CNPG replica lag, replica not streaming, PostgreSQL not answering, WAL files piling | `vmrule-database.yaml` |
| Backups | alerts on the last SUCCESS (the old ones watch the last schedule); the verification alerts on the series the M2 verifier exports | `vmrule-backup-signals.yaml` |
| Logs | Loki compactor not applying retention (#127, signal only) | `vmrule-retention.yaml` |
| Blind rules | every metric an alert reads is catalogued with its scrape and its status; UNVERIFIED ones must be listed here | `test-alert-metrics.py`, `alert-metrics.yaml` |

## What it deliberately does not do

- **No new always-on external dependency.** The default install still alerts inside the cluster (Alertmanager, vmalert,
  Grafana). The delivery test needs a real chat webhook and heartbeat URL (the existing seed keys) and is an opt-in component,
  off in every profile: with the seed placeholders it would only raise `AlertDeliveryFailing` for ever.
- **A different chat or heartbeat host needs one edit of the `vmalertmanager` CiliumNetworkPolicy** (FQDN rules cannot read
  a Secret). Known manual step; only `hooks.slack.com` and `hc-ping.com` are allowed today.
- **Alloy WAL (#119) is not enabled.** `scripts/dev/probe-alloy-wal.sh` ran the shipped `loki.write` block with a `wal` block in
  the real Alloy of the pinned chart (v1.20.1): after a hard restart the unsent lines were NOT replayed, and with retries
  exhausted they were dropped ("no retries left, dropping data"), with and without the WAL. Enabling it would cost disk and an
  eviction risk for no gain. Re-run the probe on every Alloy bump; enable when both scenarios say YES.
- **No external blackbox probe** of the customer path (the RED rules read the gateway's own metrics, so they cannot see a dead
  gateway or a broken load balancer; only the heartbeat covers a dead cluster).
- **No second log sink** (infra #318) and **no Loki ruler** (audit-log alerts stay in Explore): not an M1 item.
- **Verifier series not yet read on a cluster.** The M2 verifier exports `oa_backup_verify_ok{dataset}`,
  `oa_backup_verify_last_success_timestamp_seconds{dataset}` and `oa_backup_verify_store_ok{dataset,store}`; they are
  catalogued as `existing` because the code ships here, but only a lab proves them (criterion 12).
- **#127 is not closed:** the alert says the sweep runs, not that chunks disappear (a lab with a short retention proves that).
- **Etcd, certificates, Flux, Cilium, OpenBao rules** were already there; M1 only adds their runbooks.
- Thresholds are starting values to tune during the first week of a soak.

## UNVERIFIED: series and paths no cluster has shown (after lab 19)

`longhorn_backup_state` (the other 41 were read on a cluster in lab 19, see below).

Read in lab 19 (Scaleway, Longhorn 1.12.1, Talos 1.14.2): all the other series above exist (`task check-alerts`-style query of each name),
the scrapes `istio-gateway` (15090) and `longhorn-manager` (9500) are up, the PSI series exist on all six nodes (Talos exposes them),
`istio_requests_total` carries `response_code` and `reporter` (`source` only, on the gateway), the `alertmanager_notifications_*` counters
count per integration. Two shapes differed from the assumption and were corrected: `longhorn_volume_robustness` is one-hot (a series per
`state`, not 1/2/3), and the verifier's exporter could not be scraped until vmagent's egress allowed 9101. Still UNVERIFIED: the Longhorn
backup series (`longhorn_backup_state`: no backup target in the lab).

## What a short lab must observe (pass criteria)

Run on one cloud, throwaway objects in their own namespace, never on a node that carries etcd. Record time to each event.

1. **Series.** `task check-alerts` lists no metric without series except `kube_job_failed`, `kubelet_volume_stats_*`; with the M2 verifier
   deployed, `oa_backup_verify_*` has series too (criterion 12). Each UNVERIFIED metric above found: flip it to `existing` in `alert-metrics.yaml`; not found: fix or drop the rule.
2. **Scrapes.** `up == 1` for every gateway pod (`istio-gateway`) and every Longhorn manager; the PSI series exist on all nodes (else record that Talos lacks PSI).
3. **Runbooks.** After merge, the `runbook_url` of three fired alerts opens the right heading.
4. **Alerting chain.** Scale the Alertmanager to 0: `AlertmanagerAbsent` firing in vmalert after about 10 min and the heartbeat service flags the missed pings within its grace; restore, both clear.
5. **Heartbeat.** The service shows a ping at least every 5 min for an hour.
6. **Delivery test** (component on, real endpoints). A throwaway VMRule with `probe="delivery"` firing now reaches the chat in under 2 min as one "[PROBE]" message with no "resolved"; `alertmanager_notifications_total{integration="slack"}` rises, `..._failed_total` does not. At the first real window (06:00 or 18:00 UTC) the same, unprovoked.
7. **Delivery broken on purpose.** Replace the webhook secret by an invalid URL: `AlertDeliveryFailing` fires within one window, `AlertDeliveryNotAcknowledged` within the window plus 20 min, the Watchdog stops, and the heartbeat service pages by its own channel. Restore: all four clear and pings resume. Record each time.
8. **Gateway RED.** One request produces `istio_requests_total` with `response_code`; a route to a missing backend produces 503s and the burn ratio query returns the expected value; with a throwaway copy of the rule using 5 min and 1 min windows, it fires within 5 min and clears after the fault.
9. **USE.** A pod over its memory limit: `ContainerOOMKilled`. A pod burning its CPU limit: throttle ratio above 0.5 (rule `for` shortened in a throwaway copy). A pod requesting more than any node has: `PodPendingTooLong` at 15 min. An I/O load (fio) on a scratch volume of a non-etcd worker: `node_pressure_io_stalled_seconds_total` and `node_disk_io_time_seconds_total` rise; `NodeIoPressure` or `NodeDiskBusy` pending, with the thresholds noted for tuning.
10. **Longhorn.** A scratch volume with a replica deleted shows `longhorn_volume_robustness{state="degraded"}` 1 then `{state="healthy"}` 1 after the rebuild (the metric trails the Longhorn object by up to 3 minutes); disk usage and node status series match the Longhorn objects.
11. **CNPG.** Replica lag series exists and is near 0; stopping the replication of one replica (pod kill) makes `CnpgReplicaNotStreaming` or `CnpgInstanceDown` pending in a throwaway copy with short `for`.
12. **Backups.** `time() - kube_cronjob_status_last_successful_time` is a few hours for each backup CronJob; no `BackupNotSucceededRecently`; with the M2 verifier deployed, `oa_backup_verify_ok` is 1 and `oa_backup_verify_last_success_timestamp_seconds` is recent for every dataset, `oa_backup_verify_store_ok` is 1 per store, and `BackupVerificationStale` and `BackupVerificationFailed` are inactive (then break a copy in a throwaway: `BackupVerificationFailed` fires). Without the verifier the series are absent and the alerts inactive.
13. **Loki retention (#127).** With a throwaway overlay (retention 3 h, delete delay 10 min): the compactor metric exists and advances, the object count of the logs bucket falls after the delay with the dedicated key pair's documented rights, a query for the expired window returns nothing, `LokiRetentionNotSwept` stays inactive.
14. **Alloy WAL.** `scripts/dev/probe-alloy-wal.sh` on the Alloy version in use: both YES before any enabling.
15. **Soak (7 days).** Only Watchdog and the expected probe alerts, no alert flapping more than twice a day; `AlertDeliveryNotAcknowledged` never fired by itself.
