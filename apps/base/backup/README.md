# backup — encrypted, multi-destination backup of the critical data

Two Flux sub-bricks (`backup-openbao`, `backup-databases`) on a common **restic** engine: authenticated **client-side**
encryption (AES-256-CTR + Poly1305-AES, key derived by scrypt from `RESTIC_PASSWORD`, so the buckets never see plaintext),
deduplication, retention (14 days / 8 weeks / 12 months) and a `restic check` on every run. Each backup is pushed to **2 independent
S3 repositories** (different providers in production); a failing destination fails the Job.

## Backup policy

One row per dataset. RPO is the worst-case data loss implied by the schedule. RTO is a **target, not measured**: no restore has
been timed on a cluster (the drill below is how it becomes a number).

| Data | Mechanism and schedule | RPO / RTO target | Retention | Destination | Key holder | Restore test | Destruction |
|---|---|---|---|---|---|---|---|
| OpenBao (raft: KV, PKI intermediate, policies) | `openbao-snapshot` CronJob, daily 02:17 UTC, restic | 24 h / 4 h (target) | 14 d / 8 w / 12 m | the 2 restic repositories | restic password (OpenBao itself + offline escrow) AND the OpenBao seal key (offline escrow) | monthly `openbao-snapshot-restore-test` (checksums and unpack); a full restore is manual | retention `--prune`; decommission below |
| CNPG databases (zitadel, grafana): logical dump | `cnpg-dump` CronJob, daily 02:47 UTC, `pg_dump -Fc`, restic | 24 h / 4 h (target) | 14 d / 8 w / 12 m | the 2 restic repositories | restic password | monthly `cnpg-dump-restore-test` (`pg_restore --list` on every dump) | as above |
| CNPG databases: continuous archive (PITR) | CNPG `barmanObjectStore`, WAL archive plus a `ScheduledBackup` at 03:10 / 03:25 UTC | minutes / 4 h (target) | 30 d | the cluster's own provider only (CNPG offers one destination) | S3 credentials (no client-side encryption) | none automated | bucket lifecycle at the provider |
| Longhorn volumes | opt-in RecurringJob group (`storage/backup-target`) | per job / not set | per job | the `backupTarget` of the environment | LUKS passphrase | none | at the provider |
| Kubernetes resources | none: GitOps (Flux re-applies the repository) | n/a | Git history | Git | n/a | the DAG on a fresh cluster | n/a |
| tfstate, kubeconfig/talosconfig, etcd snapshots | OpenAether-infra (`task` targets, encrypted with the state passphrase) | see the infra backup policy | see infra | 2 buckets | state passphrase | infra | infra |

**Not backed up, by decision:** VictoriaMetrics data (local-path, 30 d retention), Loki chunks (log retention is one year on
the provider's S3, not a backup) and other telemetry; local-path volumes; Longhorn volumes outside the recurring-job group.
Kubernetes resources come back through GitOps. Longhorn volumes are backed up only when `storage-backup-target` has a
`BACKUP_TARGET_URL` AND the volume belongs to the `oa-backup` group (or `LONGHORN_BACKUP_GROUP=default`, see
`storage/backup-target/recurringjob.yaml`); no platform component uses Longhorn today.

**Decommissioning a bucket** (a provider is left, or a repository is replaced after a restic password leak): confirm the other
repository restores, revoke the S3 key at the provider, delete **every object version and delete marker** (a versioned bucket
keeps them after a plain delete), then delete the bucket and check at the provider that it is gone. A snapshot you cannot read
anymore is not erased: only deleting the objects is.

## Seeding the secrets (once per cluster)

1. `backup/restic`: **seeded automatically** by the OpenBao configuration Job (`foundation/vault/scripts/configure.sh`, a random
   password, **in no log**). The operator reads it ONCE with the `openaether-admin` identity (command in
   `foundation/vault/README.md`, Day 0) and puts it in **offline escrow**: without this password the backups are unreadable
   the day OpenBao is lost, which is precisely the main DR scenario. Nothing in the cluster checks that the escrow was done.
2. `backup/s3-primary` and `backup/s3-replica`: **seeded by the operator** (provider credentials are unknown in-cluster).
   `endpoint` includes the scheme:

   ```bash
   bao kv put secret/backup/s3-primary \
     endpoint="https://s3.fr-par.scw.cloud" \
     bucket="s3-openaether-scw-backups" \
     access_key="…" secret_key="…"
   bao kv put secret/backup/s3-replica \
     endpoint="https://s3.eu-west-par.io.cloud.ovh.net" \
     bucket="s3-openaether-ovh-backups" \
     access_key="…" secret_key="…"
   # Local (test): endpoint="http://minio.foundation-storage.svc.cluster.local:9000"
   ```

   Until they exist, the `backup-restic-env` ExternalSecrets stay NotReady and the CronJobs do not start (by design,
   `wait: false`). A new S3 FQDN must also be added to the `toFQDNs` of the network policies here.
3. **The buckets must exist beforehand**: restic does not create them (and retries a NoSuchBucket silently). Cloud: the
   operator or `ensure-buckets.sh` on the infra side. Local: the MinIO chart (`backups-primary` / `backups-replica` in its values).

## Order of a full recovery

The apps-side links of the chain, in the order that works (the infra documents carry the cluster-up step before them):

1. A cluster with Flux and the `foundation-vault` namespace; put the escrowed `openbao-seal-key` Secret in it BEFORE the vault pods
   start (`foundation/vault/README.md`, Disaster recovery).
2. OpenBao: restore the raft snapshot (below), put the source cluster's recovery shares back.
3. External Secrets resumes: the Secrets rebuilt from OpenBao include the restic password, `zitadel-masterkey` and the database
   passwords. **Zitadel's masterkey lives in OpenBao**, so this step comes BEFORE the Zitadel database restore: a database restored
   under a fresh masterkey is unusable.
4. CNPG: restore the databases (dump below, or point-in-time recovery).
5. Zitadel and the rest of the DAG reconcile; check logins.

Targets and a yearly drill are in `OpenAether-infra/docs/disaster-recovery.md`; the drill has not been run on a cluster.

## Restore

OpenBao (total loss):
```bash
export RESTIC_REPOSITORY="s3:<endpoint>/<bucket>/openbao" RESTIC_PASSWORD=…  # from escrow
restic restore latest --target /tmp/r
# a blank OpenBao cluster redeployed by Flux, then:
bao operator raft snapshot restore /tmp/r/backup/openbao-raft.snap
# ⚠ the snapshot is encrypted by the static seal key: first put the Secret openbao-seal-key back from the OLD
# cluster's escrow (runbook foundation/vault/README.md, Disaster recovery).
```

CNPG:
```bash
export RESTIC_REPOSITORY="s3:<endpoint>/<bucket>/cnpg" RESTIC_PASSWORD=…
restic restore latest --target /tmp/r
pg_restore -h zitadel-db-rw -U zitadel -d zitadel --clean --if-exists /tmp/r/backup/zitadel-db.dump
```

Recommended periodic test: `restic snapshots` + `restic restore latest` to a throwaway volume (the `restic check` of each run
reads only metadata).

The monthly restore tests (the `*-restore-test` CronJobs) go one step past restic: the CNPG one runs `pg_restore --list` on every
restored dump (the archive must be readable by a client at least as recent as the server), the OpenBao one unpacks the raft
snapshot and checks its `SHA256SUMS`. Neither loads the data into a live database or OpenBao: a real restore is still a manual
exercise (this section, and the OpenBao runbook in `foundation/vault/README.md`).

CNPG point-in-time recovery (barman archive, RPO of minutes). Restore into a NEW Cluster next to the original, in
`foundation-databases` (the only namespace the S3 credentials Secret `cnpg-backup-s3` and the egress policy exist in), and never
reuse the original's name or its `backup:` block (CNPG refuses to archive into a non-empty path):

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: zitadel-db-restore
  namespace: foundation-databases
spec:
  instances: 1
  imageName: ghcr.io/cloudnative-pg/postgresql:18.4   # same major as the origin
  storage: { size: 5Gi, storageClass: local-path-retain }
  bootstrap:
    recovery:
      source: zitadel-db-origin
      # Omit recoveryTarget to replay to the end of the archive.
      recoveryTarget:
        targetTime: "2026-01-01 12:00:00+00"   # UTC, inside the archive range
  externalClusters:
    - name: zitadel-db-origin
      # The archive lives under <destinationPath>/<serverName>.
      serverName: zitadel-db
      barmanObjectStore:
        destinationPath: s3://<bucket>/cnpg/zitadel-db   # as in cnpg/cluster.yaml
        endpointURL: <endpoint>
        s3Credentials:
          accessKeyId: { name: cnpg-backup-s3, key: ACCESS_KEY_ID }
          secretAccessKey: { name: cnpg-backup-s3, key: ACCESS_SECRET_KEY }
```

Check with `kubectl cnpg status zitadel-db-restore` and a `SELECT` on the data, then delete the scratch Cluster (its PVC is
`Retain`: delete it too). This procedure is written from the CNPG 1.23 documentation and has NOT been run on a cluster: treat the
first run as the test. The PITR chain lives on the cluster's own provider only (one destination in CNPG 1.23); the second provider
holds the daily `pg_dump` (RPO 24 h). The barman objects carry no client-side encryption (CNPG 1.23 offers server-side AES256/KMS
only, unset here): they rely on the bucket's own protection.

## Known limits / upgrades

- **CNPG PITR**: the restic dumps are daily (RPO 24 h). PITR through `barmanObjectStore` is an **active** block of
  `cnpg/cluster.yaml` (`${BACKUP_S3_*}` values of the `cluster-identity` ConfigMap, `cnpg/scheduledbackups.yaml`): it lives next
  to this engine and does not replace it.
- **Longhorn**: LUKS volumes (Longhorn backups of encrypted volumes stay encrypted); `backupTarget` is a per-environment Setting,
  not wired in the base, see `storage/longhorn.yaml`.
- **Alerting**: `BackupJobFailed` and the neighbouring rules are in `observability/vm-customresources/vmrule-backup.yaml` (Job
  failure, CronJob that no longer fires).
- **Object lock** (immutability against a compromised credential) is not configured by this repository: a stolen S3 key can
  delete the repositories. Enabling it, with the retention it implies, is a bucket decision on the infra side.
