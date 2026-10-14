# backup — encrypted multi-destination backup of the critical data

Two Flux sub-bricks (`backup-openbao`, `backup-databases`), one common engine,
**restic**: authenticated **client-side** encryption (AES-256-CTR +
Poly1305-AES, key derived by scrypt from `RESTIC_PASSWORD`) — the buckets never
see clear text —, deduplication, retention (14 days / 8 weeks / 12 months) and
`restic check` on every run. Each backup is pushed to **2 independent S3
repositories** (different providers in production); a failing destination fails
the Job.

## What is backed up (and why that is enough)

| Data | Mechanism | Rebuildable otherwise? |
|---|---|---|
| OpenBao (raft: KV, PKI intermediate, policies) | daily raft snapshot (`openbao-snapshot`, foundation-vault) | partially (DR by design) — the snapshot shortens the RTO |
| CNPG databases (zitadel, grafana) | daily `pg_dump -Fc` (`cnpg-dump`, foundation-databases); both clusters also archive WAL with `barmanObjectStore` (see below) | no |
| Kubernetes resources | — | yes: GitOps (Flux re-applies everything) |
| tfstate, kube/talosconfig | OpenAether-infra (PBKDF2/AES-GCM + gpg; the tfstate and artefact buckets, each with its `-backup` twin) | — |
| etcd | OpenAether-infra `task etcd-snapshot` | yes (GitOps) — RTO shortcut |
| Longhorn volumes | LUKS at rest; backups through the `storage-backup-target` brick (see below) | depends on the app |

A monthly restore test per brick (`openbao-snapshot-restore-test`,
`cnpg-dump-restore-test`) re-reads part of the data and restores `latest` into
a throwaway volume; the `BackupJobFailed` alert covers the CronJobs.

## Seeding the secrets (once per cluster)

1. `backup/restic` — **seeded automatically** by the Job
   `openbao-vault-bootstrap` (random password, create-only, **never printed**:
   pod logs are shipped to Loki and kept). **MANDATORY OFFLINE ESCROW** (Bitwarden
   EU): without this password the backups are unreadable the day OpenBao is
   lost, which is precisely the main DR scenario. Read it with the `bao_admin`
   helper of `foundation/vault/README.md` (Day 0, step 3):
   `bao_admin kv get -mount=secret -field=password backup/restic`.
   A re-initialisation of OpenBao regenerates it.
2. `backup/s3-primary` and `backup/s3-replica` — **seeded by the operator**
   (the provider credentials are unknown in the cluster). `endpoint` includes
   the scheme:

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

   While they are missing, the `backup-restic-env` ExternalSecrets stay
   NotReady and the CronJobs do not start (by design, `wait: false`).
   A new S3 FQDN ⇒ add it to the `toFQDNs` of the five policies that carry the provider-S3 rule (`observability`, `backup/openbao`, `backup/databases`, `cnpg` restricted, `storage`); `scripts/dev/test-s3-egress-fqdns.py` lists them and fails when they differ.
3. **The buckets must already exist**: restic does not create them (and retries
   a NoSuchBucket silently). Cloud: `ensure-buckets.sh` (infra) creates four
   buckets (tfstate and artefacts, and their `-backup` twins); the `backups-<env>` bucket of these backups (and its `-backup`
   twin, per the `backup.tf` convention) is created **by the operator**; the image buckets come from `talos-image.sh`. Local: the MinIO chart (`backups-primary`/`backups-replica` in its
   values).

## Restore

The repositories are prefixed with the cluster name:
`s3:<endpoint>/<bucket>/<CLUSTER_NAME>/openbao` and `…/cnpg`.

OpenBao (total loss): follow **Disaster recovery** in
`foundation/vault/README.md`: the snapshot is encrypted by the static seal key,
so the escrowed `openbao-seal-key` Secret of the OLD cluster must exist before
the vault pods start, and the restore itself needs a break-glass root token.
The snapshot comes out of restic like this:
```bash
export RESTIC_REPOSITORY="s3:<endpoint>/<bucket>/<CLUSTER_NAME>/openbao" RESTIC_PASSWORD=…  # escrow
restic restore latest --target /tmp/r
# then, as that runbook says: bao operator raft snapshot restore -force /tmp/r/backup/openbao-raft.snap
```

CNPG:
```bash
export RESTIC_REPOSITORY="s3:<endpoint>/<bucket>/<CLUSTER_NAME>/cnpg" RESTIC_PASSWORD=…
restic restore latest --target /tmp/r
pg_restore -h zitadel-db-rw -U zitadel -d zitadel --clean --if-exists /tmp/r/backup/zitadel-db.dump
```

`restic check` on every run only reads the metadata; the monthly restore test
above is what proves the data is readable.

## Known limits / upgrades

- **CNPG PITR**: both clusters archive WAL continuously (`barmanObjectStore` in
  `cnpg/cluster.yaml` and `cnpg/grafana-db-cluster.yaml`), which needs `BACKUP_S3_BUCKET` and
  `BACKUP_S3_ENDPOINT` from the `cluster-identity` ConfigMap (see the header of
  `cluster-api-clusters/child-gitops/gitrepository.yaml` for children). The
  daily dumps (RPO 24 h) remain the restic-encrypted, two-destination copy;
  the WAL archive is a single S3 target with provider values in the CR.
- **Longhorn**: LUKS volumes (Longhorn backups of encrypted volumes stay
  encrypted); the backup target is not a Setting but the `BackupTarget` CR laid
  down by the `storage-backup-target` brick from `BACKUP_TARGET_URL`. With no
  URL published, Longhorn keeps no volume backups (see `storage/longhorn.yaml`).
