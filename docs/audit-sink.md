# Audit sink: a second copy of the Kubernetes audit log outside the cluster (opt-in)

> Infra #318. **Rung: mocked/static plus a real restic and busybox in a container, and one run on a cluster (lab 23: Scaleway control planes, an OVH
> object store): see "Measured in lab 23" at the end.** It claims no compliance with any ANSSI document. The design (choice of placement, key custody, honest limits, lab
> pass criteria) is `docs/audit-sink.md` in OpenAether-infra; this page is what the apps repository ships and how to operate it.

## What it is

The audit and security logs are **class A, zero loss** (maintainer decision of 2026-10-22); ordinary metrics and logs stay class B. The opt-in
component `audit-log-sink` copies the apiserver audit log of each control plane to an object-locked bucket **outside the cluster**
(another provider by default, or the same provider in another region, or both), client-side encrypted, with a delivery check and alerts.

| Part | Where | What |
|---|---|---|
| Shipper | `apps/base/observability-audit-sink/shipper.yaml`, `scripts/ship.sh` | DaemonSet on the control planes: reads `/var/log/audit/kube` read-only, ships byte ranges with `restic backup --stdin --no-lock` |
| Delivery check | `checker.yaml`, `scripts/check.py` | lists the sink every 5 minutes, exports `oa_audit_sink_ok{sink}`, `oa_audit_sink_last_object_age_seconds{sink,node}` |
| Alerts | `vmrule.yaml`, `docs/runbooks.md` | `AuditSinkDown`, `AuditSinkStale`, `AuditSinkEmpty`, `AuditSinkShipperUnavailable`, `AuditSinkCheckAbsent` (all critical) |
| Secret | `externalsecret.yaml` | `secret/audit/sink` (endpoint, bucket, region, prefix, key pair, repository password), seeded by OpenAether-infra |
| Network | `apps/base/observability-audit-sink-policy/` | DNS and the sink's host on 443 only (`AUDIT_SINK_HOST` of `cluster-identity`), no ingress but vmagent on the check |
| Second sink | `observability-audit-sink-secondary`, component `audit-log-sink-secondary` | the same shipper for `audit_sink_placement = both`, its own credentials and position |

It is **off in every profile**: no namespace, DaemonSet, ExternalSecret, rule or policy exists until the component is added. The only default
changes are an exemption by pod name in four Kyverno policies for a namespace that does not exist by default, and two create-only paths
(`audit/sink`, `audit/sink-secondary`) in the `secret-seeder` OpenBao policy.

## How the data moves, and what that guarantees

1. The shipper reads the apiserver's files (the live `kube-apiserver.log` and the rotated ones) by **byte range**. The position is
   `(inode, offset)`. It never writes to the audit directory and the apiserver never waits for it.
2. Every 300 s it cuts what was appended at the last newline (at most 16 MiB), and stores it as one restic snapshot in the repository of the
   node and the UTC day, `<prefix>/<node>/<YYYY-MM-DD>`, tagged `oa-pos=<inode>:<from>:<to>[:eof]`. `restic backup` returns 0 only when the
   snapshot object is stored: that is the acknowledgement, and the only thing that moves the position.
3. **The sink is the cursor.** At start the shipper reads the last `oa-pos` back (up to 8 days of repositories). No local state outlives the pod.
4. A rotated file is finished with an `eof` chunk. A file that disappeared before it was shipped becomes an explicit `oa-gap` snapshot.
5. restic runs with `--no-lock`: the shipper's key pair needs **no delete right**, and the bucket is object-locked.
6. The buffer when the sink is down is **the apiserver's own rotated files** on the node, not a copy: how long an outage the node absorbs is
   decided by the apiserver's rotation settings (read them in the lab, criterion 7 of the infra design). The shipper's own disk use is bounded
   (`emptyDir` 384 MiB, a chunk of at most 16 MiB).

## Turning it on

Infra side first (OpenAether-infra, `docs/audit-sink.md`): `audit_sink_placement` and the endpoint, region, lock mode and retention in the tfvars;
`ensure-buckets.sh` (creates the bucket with object lock and reads it back; a refusal is an error); a dedicated key pair from
`backup-key-recipe.sh <tfvars> audit-sink`; `seed-openbao.sh` (writes `secret/audit/sink` and the `cluster-identity` host names); then
`escrow-openbao.sh export` so that the repository password is escrowed. Then, in the cluster's own overlay:

```yaml
components:
  - ../components/audit-log-sink
  # - ../components/audit-log-sink-secondary   # audit_sink_placement = both
```

`audit-log-collector` (alloy-audit to Loki) is independent; most clusters want both. Rollback: remove the line. What is already in the sink
stays (it is locked).

## Operating it

- Is it delivering: `kubectl -n services-observability-audit-sink logs ds/audit-sink-shipper` (`shipped oa-pos=...` every interval) and the
  `oa_audit_sink_*` series; `AuditSink*` alerts have runbooks in `docs/runbooks.md`.
- Prove the sequence and the lock (operator machine, with the escrowed password): `scripts/ops/audit-sink-check.sh <tfvars>` in OpenAether-infra.
- A change of `ship.sh` reaches running pods at their next restart (the ConfigMap has no content hash because the second shipper mounts it by
  name): after an upgrade that changes it, `kubectl -n services-observability-audit-sink rollout restart ds/audit-sink-shipper`. Safe at any time.
- Rotate the S3 key pair: replace `secret/audit/sink` `access_key`/`secret_key` by hand (create-only seed), refresh the ExternalSecret, restart the pods.
  The repository password is not rotated by this; `restic key add`/`remove` on each daily repository would be needed and is not scripted.

## Honest limits

- The password and the S3 pair are in the cluster (the shipper needs them). A cluster-admin can read the sink and add objects, not delete or
  shorten a locked version. A public-key design would close the read side and needs an image with that tool and an S3 client: not done.
- A line is only as good as the apiserver wrote it: the window between the write and the shipping (300 s) is not protected from a compromised control plane.
- `.gz` rotated files are not shipped; node and flow logs are not covered yet (infra design, section 8).
- The restic repository is per node and day: a long-lived cluster accumulates many small repositories; listing for the check reads only the last two days.
- UNVERIFIED until a lab: the Talos rotation settings and file mode, restic against Scaleway, OVH and Outscale object lock (the existing backups use
  restic on these stores, but not with `--no-lock` and a locked bucket), the FQDN policy with the real hosts, the series and alerts, the cost.

## Tests (all offline)

`scripts/dev/test-audit-sink-shipper.sh` (real restic 0.19.1 and busybox from the pinned image: whole lines, contiguous ranges, restart from the
sink, rotation with `eof`, outage then recovery with no loss and no repeat, an explicit gap, no lock object), `scripts/dev/test-audit-sink.py
--selftest` (opt-in, narrow, exempt by name), `scripts/dev/test-audit-sink-exporter.py` (the delivery check against a fake `aws`: ages, a failing store, an empty sink), `scripts/dev/test-vmrules.sh` (unit cases of the five alerts), `scripts/dev/test-alert-metrics.py`
(the four `oa_audit_sink_*` series are catalogued UNVERIFIED), `scripts/dev/test-kyverno-policies.sh` (the shipper is spared, an intruder next to it is not).

## Measured in lab 23 (Scaleway cluster, OVH sink, GOVERNANCE 1 day, 2026-10-22)

The full table of the lab criteria is in OpenAether-infra `docs/audit-sink.md`, section 10. What changed here:

- **The shipper chased the file.** The API server appends to its audit file all the time, and a cycle looped until it had caught up: each put takes a
  moment, the file has grown by then, so the cycle never ended, the 300 s interval was never honoured and a snapshot was made every one to two seconds
  (about 1000 per node in 45 minutes); after 45 minutes without a completed cycle its liveness probe killed it. A cycle now ships what the file held when
  it began (`ship.sh`; `test-audit-sink-shipper.sh` appends to the live file during a cycle: 21 snapshots before, 1 after). The script reaches running pods
  at their next restart (the ConfigMap has no hash in its name), so restart the DaemonSet after upgrading.
- **`AuditSinkDown`** now fires after two failed listings in a row (`for: 5m`): the check lists every 5 minutes and the 10 minute rule fired 16 min 41 s
  after the sink was cut, against a 15 minute target. `AuditSinkStale` fired 22 minutes after the cut.
- **Observed**: delivery within 2 minutes of the component; a window of 2 h 29 min with 7 rotations of the 100 MB file, 3 shipper restarts and a 40 minute outage
  read back by `audit-sink-check.sh` with no gap and 0 missing range; the backlog of the outage (about 190 MB per node) shipped in 4 to 8 minutes after the
  sink was back; the shipper's work directory stayed at 19 to 32 MB of its 384 Mi; `kubectl` answered in under 0.4 s throughout; the apiserver keeps 10 rotated files
  of 100 MB (`--audit-log-maxbackup=10`), 20 to 33 minutes per file on that busy lab, so an outage of 3.5 to 5 hours is absorbed there before the oldest file is lost.
- **Limit on OVH**: the sink pair cannot be restricted by a bucket policy; it listed every bucket of the project and could write delete markers and change the
  bucket's default retention (not delete or shorten a locked version).
