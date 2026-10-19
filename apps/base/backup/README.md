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
| CNPG databases: continuous archive (PITR) | CNPG Barman Cloud plugin (`ObjectStore` objects in `cnpg/objectstores.yaml`), WAL archive plus a `ScheduledBackup` at 03:10 / 03:25 UTC | minutes / 4 h (target) | 30 d | the primary store (see the replica copy in `cnpg-store-replicate`) | S3 credentials; server-side encryption requested, **no client-side encryption** | none automated | bucket lifecycle at the provider |
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
   the day OpenBao is lost, which is precisely the main DR scenario. The cluster only checks that an operator attested it (the `openbao-escrow-check` CronJob, vault README Day 0, step 5), not that
   the restic password was escrowed: that one is on your checklist.
2. `backup/s3-primary` and `backup/s3-replica`: **seeded by the operator** (provider credentials are unknown in-cluster).
   `endpoint` includes the scheme:

   ```bash
   bao_admin kv put secret/backup/s3-primary \
     endpoint="https://s3.fr-par.scw.cloud" \
     bucket="s3-<project>-scaleway-backups-<env>" \
     access_key="…" secret_key="…"
   bao_admin kv put secret/backup/s3-replica \
     endpoint="https://s3.eu-west-par.io.cloud.ovh.net" \
     bucket="s3-<project>-scaleway-backups-<env>-backup" \
     access_key="…" secret_key="…"
   # Local (test): endpoint="http://minio.foundation-storage.svc.cluster.local:9000"
   ```

   Until they exist, the `backup-restic-env` ExternalSecrets stay NotReady and the CronJobs do not start (by design,
   `wait: false`). A new S3 FQDN means adding it to the `toFQDNs` of the five policies that carry the provider-S3 rule
   (`observability`, `backup/openbao`, `backup/databases`, `cnpg` restricted, `storage`); `scripts/dev/test-s3-egress-fqdns.py`
   checks that they agree.
3. **The buckets must exist beforehand**: restic does not create them (and retries a NoSuchBucket silently). Cloud:
   `ensure-buckets.sh` (infra) creates the state and artifact buckets, their `-backup` twins and Loki's logs bucket; the
   `backups-<env>` bucket of these backups (and its `-backup` twin, per the `backup.tf` convention) is created **by the operator**;
   the image buckets come from `talos-image.sh`. Local: the MinIO chart (`backups-primary` / `backups-replica` in its values).

## S3 rights of a dedicated key pair

The contract for a pair limited to the backups bucket (`backup/s3-primary`, `backup/s3-replica`). Each consumer needs these
and nothing else; the bucket policy (or the provider's equivalent) must carry them on the right resource, bucket or objects:

| Consumer | S3 action (API call) | On |
|---|---|---|
| restic, Barman, Longhorn | `ListBucket` (ListObjects, ListObjectsV2) | the bucket |
| **Barman (CNPG WAL archive and base backup)** | **`ListBucket` as HeadBucket**: `barman-cloud-check-wal-archive` and every WAL archive start with HeadBucket; a refusal is `ContinuousArchiving=False`, "exit status 4", `HeadBucket ... 403` | the bucket |
| all | `GetObject` (GetObject, HeadObject), `PutObject` (PutObject, CopyObject, CreateMultipartUpload, UploadPart, CompleteMultipartUpload), `DeleteObject` (DeleteObject, DeleteObjects: retention and `restic forget --prune`) | the objects (`<bucket>/*`) |
| restic, Barman, Longhorn (large objects) | `AbortMultipartUpload`, `ListBucketMultipartUploads` (ListMultipartUploads), `ListMultipartUploadParts` (ListParts) | objects / the bucket |
| Longhorn | `GetBucketLocation` (granted in the lab; not shown to be required) | the bucket |

Withhold, so that a stolen pair cannot rewrite history: `DeleteObjectVersion`, bucket versioning, lifecycle, object-lock and
retention changes, bucket deletion. Check a pair with `scripts/ops/check-backup-key-rights.sh` (every right above; reads and
writes only under `.oa-rights-probe/`, removed at the end) BEFORE seeding it, and the Cluster condition after:
`kubectl get cluster -n foundation-databases <name> -o jsonpath='{.status.conditions[?(@.type=="ContinuousArchiving")].status}'` is `True`.

**Scaleway: the bucket policy is not enough.** Both layers must allow the call (bucket policy version `2023-04-17`: an action
is permitted only if the bucket policy allows it AND an IAM policy allows the principal). HeadBucket maps to `s3:ListBucket`
in the bucket policy, but in IAM it belongs only to the permission sets `ObjectStorageBucketsRead`, `ObjectStorageReadOnly`
and `ObjectStorageFullAccess`, not to `ObjectStorageObjectsRead`/`Write`/`Delete`. A pair whose IAM policy holds only the
`ObjectStorageObjects*` sets lists and writes (restic and Longhorn work) and gets a 403 on HeadBucket (lab 11, measured with a
bucket policy granting ListBucket, GetBucketLocation and the object rights). Give the application's IAM policy
`ObjectStorageBucketsRead` next to the object sets: it also lets the pair list the bucket NAMES of the project, which the bucket
policy cannot narrow. Run in lab 12 (Scaleway): with `ObjectStorageBucketsRead` next to the three object sets the pair's HeadBucket succeeds
(`check-backup-key-rights.sh`: every right held) and both CNPG clusters report `ContinuousArchiving=True`. **The IAM sets are
project-wide, so the bucket policies carry the isolation, and a bucket WITHOUT a policy is open to the pair**: with policies
only on the backups and logs buckets the pairs listed the tfstate and management buckets (`s3-isolation-check.sh`: 4 of 8
assertions failed); with an operator-only policy on each of the other buckets, 10 of 10 held, the delete of a real object
version included (`OA_ISOLATION_REAL_VERSION=1`). OVH and Outscale: not verified; the AWS rule (HeadBucket needs `s3:ListBucket`) is the
expectation.

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
export RESTIC_REPOSITORY="s3:<endpoint>/<bucket>/<CLUSTER_NAME>/cnpg" RESTIC_PASSWORD=…
restic restore latest --target /tmp/r
pg_restore -h zitadel-db-rw -U zitadel -d zitadel --clean --if-exists /tmp/r/backup/zitadel-db.dump
```

Recommended periodic test: `restic snapshots` + `restic restore latest` to a throwaway volume (the `restic check` of each run
reads only metadata).

The monthly restore tests (the `*-restore-test` CronJobs) go one step past restic: the CNPG one runs `pg_restore --list` on every
restored dump (the archive must be readable by a client at least as recent as the server), the OpenBao one unpacks the raft
snapshot and checks its `SHA256SUMS`. Neither loads the data into a live database or OpenBao: a real restore is still a manual
exercise (this section, and the OpenBao runbook in `foundation/vault/README.md`).

CNPG point-in-time recovery (Barman Cloud plugin, RPO of minutes). Restore into a NEW Cluster next to the original, in
`foundation-databases` (the namespace of the ObjectStore objects, the S3 credentials Secret `cnpg-backup-s3` and the egress
policy), and never reuse the original's name or its `plugins:` block (the barman check refuses to archive into a non-empty path):

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: zitadel-db-restore
  namespace: foundation-databases
spec:
  instances: 1
  imageName: ghcr.io/cloudnative-pg/postgresql:18.6   # same major as the origin
  storage: { size: 5Gi, storageClass: local-path-retain }
  bootstrap:
    recovery:
      source: zitadel-db-origin
      # Omit recoveryTarget to replay to the end of the archive.
      recoveryTarget:
        targetTime: "2026-01-01 12:00:00+00"   # UTC, inside the archive range
  externalClusters:
    - name: zitadel-db-origin
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters:
          barmanObjectName: zitadel-db-primary   # the ObjectStore of cnpg/objectstores.yaml
          serverName: zitadel-db                  # the archive lives under <destinationPath>/<serverName>
```

Check with `kubectl cnpg status zitadel-db-restore` and a `SELECT` on the data, then delete the scratch Cluster (its PVC is
`Retain`: delete it too). Measured with the in-tree object store in lab 12 (CNPG 1.30.1, Scaleway): a Backup of zitadel-db, then this
restore into a new Cluster, healthy in about 1 minute with the same 150 tables as the origin. **The plugin form of this restore has not
been run on a cluster** (UNVERIFIED until the M2 lab). To restore from the replica store instead, name the `<cluster>-replica` ObjectStore.

**Encryption of the PITR data, exactly.** The plugin has no client-side encryption (its CRD offers `encryption: AES256 | aws:kms`, which
are server-side). The ObjectStores request `AES256` for WAL and data, so the store encrypts at rest if it honours the header
(measured with the same header on artifacts: Scaleway and OVH; not measured through barman-cloud, and not measured on Outscale);
transport is TLS. NOT encrypted by us: the WAL and base backup bytes before the store sees them, so whoever can read the bucket
(a store operator, a stolen key) reads the database content. The provider-independent, client-side encrypted copy is the daily restic
`pg_dump` (RPO 24 h).

## Known limits / upgrades

- **CNPG PITR**: the restic dumps are daily (RPO 24 h). PITR goes through the Barman Cloud plugin: `plugins:` in each Cluster,
  the ObjectStores in `cnpg/objectstores.yaml` (`${BACKUP_S3_*}` values of the `cluster-identity` ConfigMap) and
  `cnpg/scheduledbackups.yaml` (`method: plugin`). It lives next to this engine and does not replace it.
- **Longhorn**: LUKS volumes (Longhorn backups of encrypted volumes stay encrypted); the backup target is not a Setting but the
  `BackupTarget` CR laid down by the `storage-backup-target` brick from `BACKUP_TARGET_URL`. With no URL published, Longhorn keeps
  no volume backups (see `storage/longhorn.yaml`).
- **Alerting**: `BackupJobFailed` and the neighbouring rules are in `observability/vm-customresources/vmrule-backup.yaml` (Job
  failure, CronJob that no longer fires).
- **Object lock** (immutability against a compromised credential) is not configured by this repository: a stolen S3 key can
  delete the repositories. Enabling it, with the retention it implies, is a bucket decision on the infra side.
