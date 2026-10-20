# backup — encrypted, multi-destination backup of the critical data

Two Flux sub-bricks (`backup-openbao`, `backup-databases`) on a common **restic** engine: authenticated **client-side**
encryption (AES-256-CTR + Poly1305-AES, key derived by scrypt from `RESTIC_PASSWORD`, so the buckets never see plaintext),
deduplication, retention (14 days / 8 weeks / 12 months) and a `restic check` on every run. Each backup is pushed to **2 independent
S3 repositories** (different providers in production); a failing destination fails the Job, and does not keep the other from receiving its copy.

## Backup policy

One row per dataset. RPO is the worst-case data loss implied by the schedule. RTO is a **target, not measured**: no restore has
been timed on a cluster (the drill below is how it becomes a number).

| Data | Mechanism and schedule | RPO / RTO target | Retention | Destination | Key holder | Restore test | Destruction |
|---|---|---|---|---|---|---|---|
| OpenBao (raft: KV, PKI intermediate, policies) | `openbao-snapshot` CronJob, daily 02:17 UTC, restic | 24 h / 4 h (target) | 14 d / 8 w / 12 m | the 2 restic repositories | restic password (OpenBao itself + offline escrow) AND the OpenBao seal key (offline escrow) | monthly `openbao-snapshot-restore-test` (checksums and unpack), the verifier restores the latest snapshot daily; a full raft restore only in the DR chain (infra `dr-restore-plan`) | retention `--prune`; decommission below |
| CNPG databases (zitadel, grafana): logical dump | `cnpg-dump` CronJob, daily 02:47 UTC, `pg_dump -Fc`, restic | 24 h / 4 h (target) | 14 d / 8 w / 12 m | the 2 restic repositories | restic password | daily `cnpg-dump-load-restore-test` (each dump loaded into a private PostgreSQL, canary checked); monthly `cnpg-dump-restore-test` (`pg_restore --list`) | as above |
| CNPG databases: continuous archive (PITR) | CNPG Barman Cloud plugin (`ObjectStore` objects in `cnpg/objectstores.yaml`), WAL archive plus a `ScheduledBackup` at 03:10 / 03:25 UTC | minutes / 4 h (target) | 30 d | the primary store, copied to the replica store every 15 min (`cnpg-store-replicate`, replica key pair) | S3 credentials; server-side encryption requested, **no client-side encryption** | daily `cnpg-recovery-restore-test`, weekly `cnpg-pitr-restore-test` (a scratch Cluster, canary compared) | bucket lifecycle at the provider |
| Longhorn volumes | opt-in RecurringJob group (`storage/backup-target`) | per job / not set | per job | the `backupTarget` of the environment | LUKS passphrase | none | opt-in component `longhorn-restore-test` (weekly, a canary volume) |
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
   `wait: false`). A new S3 FQDN means adding it to the `toFQDNs` of every policy that carries the provider-S3 rule
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
version included (`OA_ISOLATION_REAL_VERSION=1`). Lab 19 (Scaleway primary pair: 10 of 10 held, the real delete-version denied) and an OVH replica store: **OVH Object Storage has no
bucket policies through the S3 API, so a replica pair on OVH CANNOT be isolated per bucket**: with `s3-isolation-check.sh` 2 of 9
assertions failed (the state and management twin buckets of the same OVH project were readable by the pair) and
`DeleteObjectVersion` was allowed on the replica, so a stolen replica key can delete versions there. Use a separate OVH project (or
another provider) for the replica when that matters (OpenAether-infra #379). Outscale: not verified; the AWS rule (HeadBucket
needs `s3:ListBucket`) is the expectation.

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

Targets, the command that runs this chain (`task dr-restore-plan`) and a yearly drill are in `OpenAether-infra/docs/disaster-recovery.md`; the chain has not been run on a cluster.

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

The monthly restore tests (`openbao-snapshot-restore-test`, `cnpg-dump-restore-test`) stay as the shallow check: the CNPG one runs
`pg_restore --list` on every restored dump, the OpenBao one unpacks the raft snapshot and checks its `SHA256SUMS`. The full restores are
the next section. A raft snapshot is restored into an OpenBao only in the DR chain (`OpenAether-infra`, `task dr-restore-plan`).

## Restore tests (0.4.0)

A backup that cannot be restored is useless, so the platform restores its own backups on a schedule, with no person, compares what
came back with what it should be, and alerts. The comparison needs data it can recognise: the **canary**, a table `oa_canary` in
zitadel-db and grafana-db that `cnpg-canary` writes to every 5 minutes (a counter, a time, and a hash chained to the previous row), 14 days
kept. A restored copy proves itself from it: the counters are contiguous, every hash follows from its predecessor, and the newest row is
the live one's (same hash) and not older than the lag budget of the store (primary 20 min, replica 50 min: the 15-minute copy plus margin).

| CronJob (`foundation-databases` unless said) | When | What it does | Series (`oa_restore_test_*`) |
|---|---|---|---|
| `cnpg-recovery-restore-test` | daily 04:20 UTC | for zitadel-db and grafana-db, from the primary AND the replica store: recover into a one-instance scratch Cluster in `backup-restore-test`, wait healthy, run `snapshot.sql` (tables, row counts, canary), compare with the live snapshot taken just before, delete the Cluster | `ok`, `last_success_timestamp_seconds`, `last_run_timestamp_seconds`, `duration_seconds`, `recovery_seconds` (the data part of the RTO), `data_lag_seconds`, `tables_missing`, labels `kind="cnpg"`, `dataset`, `store`, `mode="latest"` |
| `cnpg-pitr-restore-test` | weekly, Sunday 06:20 UTC | the same with a recovery target: the time of the newest canary row older than 2 h and after the last base backup; the copy must end at exactly that row, with the live hash | `mode="pitr"` |
| `cnpg-dump-load-restore-test` | daily 05:40 UTC | restic restore of the latest cnpg snapshot of BOTH repositories, `pg_restore` of each dump into a private throwaway server (no network), canary and tables judged, newest row under 26 h old | `kind="dump"` |
| `longhorn-restore-test` (`foundation-storage`, opt-in component `longhorn-restore-test`) | weekly, Sunday 07:20 UTC | restore the newest backup of the canary volume (`oa-canary`, 24 files written hourly) into a scratch Longhorn Volume, mount it read-only in `backup-restore-test`, check every file against what its generation number says | `kind="longhorn"`, `mode="volume"` |

How they stay harmless. The scratch Cluster has one instance, local-path storage (deleted with it), the source's image and `max_connections`,
limits, and **no WAL-archiving plugin and no backup section**: a copy that archived would write into the store it was restored from. Everything
lives in `backup-restore-test` (restricted, a ResourceQuota of one Cluster, 3 CPU and 3 GiB of limits), whose ObjectStores have no retention
policy. The ServiceAccount has a Role in that namespace, read on Clusters and Backups and patch of ONE ConfigMap in `foundation-databases`; no
ClusterRole. The script refuses any object that is not `backup-restore-test/rt-*`; the Kyverno policy `restrict-restore-test-clusters`
refuses an `rt-` Cluster anywhere else and a scratch Cluster that archives. A Job has `activeDeadlineSeconds`, removes its scratch objects on
SIGTERM and on failure (finalizer-safe), and the next run removes a leftover older than an hour; `RestoreScratchLeftover` fires after 3 h.
A failed comparison is a failed Job (so `RestoreTestNotSucceeding` sees it too) and `oa_restore_test_ok 0` (`RestoreTestFailed`, critical);
no pass for 3 days (10 for the weekly ones) is `RestoreTestNoRecentPass`. The two key pairs are the archive's and the replica copy's: a read-only pair
is a follow-up. Run one by hand: `kubectl -n foundation-databases create job --from=cronjob/cnpg-recovery-restore-test manual-$(date +%s)`.

Lab pass criteria (M3; observed in lab 20, results below). On a cluster with both stores seeded: (1) after the first night `oa_restore_test_ok` is 1 for the four
`kind="cnpg"` label sets (2 clusters x 2 stores, `mode="latest"`), the four `kind="dump"` ones and, after the first Sunday, the four `mode="pitr"` ones,
`recovery_seconds` is at most 120 for every one (labs 15c and 19 saw 54 to 72 s; the alert is at 900), the daily CNPG Job takes under 30 minutes, `data_lag_seconds`
is at most 1200 on the primary store and 3000 on the replica store; (2) the point in time run ends at exactly the chosen canary row; (3) corrupt the
replica store (delete a WAL segment or the base backup of one cluster in a versioned bucket): the next run fails for that store and cluster only
(`oa_restore_test_ok{store="replica"} 0`, the Job Failed, `RestoreTestFailed` firing within about 20 minutes), the primary runs still pass, the scratch Cluster is gone, and
repairing it turns the alert off at the next run; delete the largest restic pack of `cnpg` on the replica: `cnpg-dump-load-restore-test` fails for that store; (4) kill a running
restore Job (delete its pod): the scratch Cluster is gone within 6 minutes; (5) a Cluster named `rt-x` in `foundation-databases` is refused by Kyverno, and
`kubectl auth can-i create clusters.postgresql.cnpg.io -n foundation-databases --as system:serviceaccount:foundation-databases:cnpg-restore-test` says no; (6) suspend `cnpg-canary` (and suspend the Kustomizations `openaether-platform` and `backup-databases` first: the root Kustomization re-applies its children from Git every 10 minutes and a suspended brick is resumed, lab 20):
`CnpgCanaryNotWriting` fires in about 45 minutes; (7) with `BACKUP_TARGET_URL` set and the component on, the canary volume has a daily backup and
`longhorn-restore-test` passes in under 5 minutes (82 s for three volumes in lab 15c); (8) nothing else suffers: no new `CpuRequestsHigh`, no Pending pod outside the scratch namespace, quota usage under its limits.

Not covered, on purpose: customer data (only the canary is compared exactly; tables and row counts are exported but the live side moves),
the OpenBao raft snapshot (the verifier restores it daily and checks its sums; the DR chain restores it into an OpenBao), the etcd snapshot (same,
bolt meta pages), Longhorn volumes other than the canary. `scripts/dev/test-restore-test-*.{py,sh}` prove the logic offline (a kubectl double, a
real PostgreSQL, a real restic); lab 20 ran every one of them on a cluster (below), and found four defects that the doubles could not show.

**Observed in lab 20** (Scaleway, 3 + 3 POP2-4C-16G, OVH replica store, 2026-10-20; Jobs started by hand with `--from=cronjob`): the daily CNPG Job takes
5 min 36 s to 6 min 25 s for the four combinations; `recovery_seconds` 77 to 101 for the latest copy and 61 to 72 for the point in time; the lag of a copy
from the primary store is 0 s and from the replica store 0 to 900 s (the 15-minute copy); the dump-load test takes 16 s; the Longhorn restore test 47 s
(37 s to restore and verify). The point in time runs ended on the chosen canary row. A damaged replica store (a WAL segment deleted) failed exactly
`zitadel-db` on the replica (`trails the live database by 4800 s`), the three other combinations passed, the scratch Cluster was gone, `RestoreTestFailed`
fired about 17 to 20 minutes after the start of the run, and repairing the store turned it off at the next run; a deleted restic pack failed the
replica side of the dump-load test and not the primary. Deleting the Job's pod removed the scratch Cluster in 4 seconds. `kubectl auth can-i` says no
for creating and deleting Clusters in `foundation-databases`. What the doubles had hidden: the etcd discovery used a kubectl flag that makes kubectl
ignore the in-cluster config; the Longhorn restore test looked the BackupVolume up by the volume's name (Longhorn adds a suffix) and verified `/data`
instead of `/data/canary`; the canary volume was never backed up because Longhorn skips detached volumes in recurring jobs; the Kyverno guard on
`rt-` Clusters did not enforce because it was created before the CNPG CRDs (it has its own brick now); a good primary copy failed when the canary wrote
a row during the 80-second recovery. A killed run publishes nothing for its combinations until the next run (the `ok` series is absent, not 0).

The two stores hold INDEPENDENT restic repositories (each has its own keys and master key): never copy the files of one over the other to "restore" a wiped store, that corrupts it (lab 20: `ciphertext verification failed` until the copied files were removed). Restore from a store into a new cluster; the wiped store gets a new repository at the next push. The archive of the CNPG plugin, in contrast, is the same bytes in both stores (the copy job). A pod killed during `restic forget --prune` leaves an exclusive lock that blocks every later push until `restic unlock` (the push scripts do it now).

**The CNPG archive on the replica store.** The plugin archives to one store per Cluster. The `cnpg-store-replicate` CronJob
(every 15 min, `backup/databases`) copies `cnpg/<cluster>/` from the primary bucket to the replica bucket with the replica key pair
(`backup/s3-replica`, never the primary pair): objects the replica lacks or holds with another size are copied, base-backup data
before its `backup.info`, uploaded with the SSE-S3 request. So the replica trails the primary by up to 15 min plus the copy time: that is
the RPO of a recovery **from the replica store**, not the minutes of a WAL switch. An object that left the primary (Barman
retention) is removed from the replica only after 45 days, and never against an empty primary or when it would remove more than half
of a cluster's replica objects (the job fails instead). A primary that lists NO object while the replica holds some (wiped bucket,
lifecycle rule, deleted prefix) also fails the job (`BackupCopyJobFailed`, "the primary lists no object while the replica holds N"),
it is no longer a quiet exit 0; both stores empty is a cluster that has archived nothing yet and passes. The `<cluster>-replica` ObjectStores name the replica bucket
(`BACKUP_REPLICA_S3_*` in `cluster-identity`) for a recovery. A failed job is the signal (`BackupJobFailed`, plus the freshness
series of the verifier). Run in lab 19: 46 s for 22 objects, SSE-S3 present on the replica objects, nothing copied on the second run; recovery from the replica store alone (primary emptied) took 66 s.

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
(a store operator, a stolen key) reads the database content; the copy on the replica store (`cnpg-store-replicate`) is the same bytes,
uploaded with the same SSE-S3 request (read back on the OVH objects in lab 19), also without client-side encryption. Client-side
encrypted, with the restic password: the `openbao`, `cnpg-dump` and `etcd` datasets only (apps #149 stays open). The provider-independent, client-side encrypted copy is the daily restic
`pg_dump` (RPO 24 h).

## Scheduled etcd snapshot

`backup-etcd` (its own Kustomization: the `talos.dev` ServiceAccount kind exists only where the control planes enable the Talos
API access from Kubernetes, infra variable `etcd_snapshot_api_access`, on by default). The CronJob `etcd-snapshot` (04:10 UTC)
takes a snapshot with `talosctl etcd snapshot` through a talosconfig that Talos generates for the grant `etcd-snapshot-talos`
(role `os:etcd:backup` only, namespace `foundation-databases` only). That config names the endpoint `talos.default`, which resolved
to nothing in lab 19, and `talosctl` refuses to run without a node: the init container `discover-controlplane` (ServiceAccount
`etcd-snapshot`: get/list on Nodes, nothing else; the only container that holds a Kubernetes token) lists the control-plane nodes,
tries each InternalIP on port 50000 in random order until one accepts a connection, and writes a copy of the config with that address as
endpoint and node. No node, no answer or a refused API call fails the Job. Nothing is seeded. The snapshot is kept in a memory-backed volume (the snapshot is every
Secret of the cluster in clear), refuses a file that is not a bolt database, and pushes it to both restic repositories
`<cluster>/etcd` (client-side encryption, 14 d / 8 w / 12 m, `restic check`). Why not a bastion timer: it would need the tofu state,
its passphrase, the S3 keys and a Talos admin credential on an internet-facing host with closed egress. The risk taken instead: a
pod of the namespace that mounts the grant Secret can take a snapshot (RBAC keeps that to the Job's ServiceAccount; the namespace
holds the CNPG instances and the backup Jobs only). Failure signal: `BackupCopyJobFailed`, `EtcdSnapshotStale` (36 h), and the
verifier below. Restore: `docs` of the infra repository (disaster recovery, step 10). By hand, lab 19 took a 97.7 MB snapshot with `-e <ip> -n <ip>`, shipped it to both stores, and restored it from the replica store alone into a
local etcd; the discovery above and `talosctl bootstrap --recover-from` on a throwaway Talos cluster are NOT exercised on a cluster:
UNVERIFIED.

## Verification (what proves a backup readable)

`backup-verify` (CronJob, 05:10 UTC; the three stages are listed at the top of `databases/verify-cronjob.yaml`) publishes the
series below through a ConfigMap served by `backup-verify-exporter` and scraped by vmagent (`VMServiceScrape backup-verify`):

| Series | Meaning |
|---|---|
| `oa_backup_verify_ok{dataset}` | 1 when the last check passed on every store, 0 when one failed (the alert contract) |
| `oa_backup_verify_last_success_timestamp_seconds{dataset}` | the last run where every store passed; kept through failures |
| `oa_backup_verify_store_ok{dataset,store}`, `oa_backup_verify_newest_backup_age_seconds{dataset,store}`, `oa_backup_verify_last_run_timestamp_seconds` | per store, and the run itself |

Datasets: `openbao`, `cnpg-dump`, `etcd` (restic: `restic check --read-data-subset=n/10`, so every pack is read within ten days,
and a restore of the latest snapshot: the raft snapshot's archive and `SHA256SUMS`, the etcd snapshot's bolt meta pages, length
and sha256 trailer); `cnpg` (the Barman archive: the latest base backup is `DONE` and read to its end, so the gzip CRC and the tar
structure are verified, every WAL segment from its `begin_wal` to the newest exists, a sample reads to its end, and on the replica
store no object older than two hours is missing compared with the primary); `longhorn` (the newest backup of each volume is
`Completed`, with a size and no error: the blocks are NOT read). A repository that does not exist, a cluster with no base backup
yet and an archive younger than 30 hours are skipped (no series), not failed, **unless another store of the same dataset (the same
cluster for `cnpg`) passed its check**: then the store without repository or archive is a store that lost its copy, and it FAILS
(`oa_backup_verify_ok{dataset} 0`, `oa_backup_verify_store_ok{dataset,store} 0`, message `MISSING ...`; apps #151). Not detected: both
stores wiped at once (nothing is left to compare; the freshness alerts of the jobs and `BackupNotSucceededRecently` are the only signal). What it does NOT prove: that a restore into a running
database or etcd works (the restore tests, M3), and that a byte flipped inside an etcd snapshot's data pages (no hash trailer) is
seen (`etcdutl snapshot status` would; restic's own authentication covers the stored copy). It uses the key pairs of the backup
Jobs (restic needs to write its lock files); a read-only pair is a follow-up.

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
