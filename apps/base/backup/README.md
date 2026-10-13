# backup — sauvegarde chiffrée multi-destinations du critique

Deux sous-briques Flux (`backup-openbao`, `backup-databases`), moteur commun
**restic** : chiffrement **client** authentifié (AES-256-CTR + Poly1305-AES,
clé dérivée par scrypt de `RESTIC_PASSWORD`) — les buckets ne voient jamais
de clair —, déduplication, rétention (14 j / 8 sem / 12 mois) et `restic check`
à chaque run. Chaque backup est poussé vers **2 dépôts S3 indépendants**
(providers différents en prod) ; l'échec d'une destination fait échouer le Job.

## Ce qui est sauvegardé (et pourquoi ça suffit)

| Donnée | Mécanisme | Reconstructible autrement ? |
|---|---|---|
| OpenBao (raft : KV, PKI intermediate, policies) | snapshot raft quotidien (`openbao-snapshot`, foundation-vault) | partiellement (DR by-design) — le snapshot raccourcit le RTO |
| Bases CNPG (zitadel, grafana) | `pg_dump -Fc` quotidien (`cnpg-dump`, foundation-databases) | non |
| Ressources K8s | — | oui : GitOps (Flux ré-applique tout) |
| tfstate, kube/talosconfig | OpenAether-infra (PBKDF2/AES-GCM + gpg, 2 buckets) | — |
| etcd | OpenAether-infra `task etcd-snapshot` | oui (GitOps) — bretelle RTO |
| Volumes Longhorn | LUKS at-rest ; `backupTarget` par env + RecurringJob opt-in (voir « What is not backed up ») | selon l'app |

## Seed des secrets (une fois par cluster)

1. `backup/restic` — **seedé automatiquement** par le Job de configuration
   OpenBao (`foundation/vault/scripts/configure.sh`, password aléatoire,
   **dans aucun log**). L'opérateur le lit UNE FOIS avec l'identité
   `openaether-admin` (commande dans `foundation/vault/README.md`, Day-0) et
   le met en **ESCROW OFFLINE** : sans ce password, les backups sont
   indéchiffrables le jour où OpenBao est perdu — c'est précisément le
   scénario DR principal. Rien ne vérifie que l'escrow a été fait.
2. `backup/s3-primary` et `backup/s3-replica` — **seedés par l'opérateur**
   (creds provider inconnues in-cluster). `endpoint` inclut le schéma :

   ```bash
   bao kv put secret/backup/s3-primary \
     endpoint="https://s3.fr-par.scw.cloud" \
     bucket="s3-openaether-scw-backups" \
     access_key="…" secret_key="…"
   bao kv put secret/backup/s3-replica \
     endpoint="https://s3.eu-west-par.io.cloud.ovh.net" \
     bucket="s3-openaether-ovh-backups" \
     access_key="…" secret_key="…"
   # Local (test) : endpoint="http://minio.foundation-storage.svc.cluster.local:9000"
   ```

   Tant qu'ils manquent, les `ExternalSecret backup-restic-env` restent
   NotReady et les CronJobs ne démarrent pas (by design, `wait: false`).
   Nouveau FQDN S3 ⇒ l'ajouter aussi aux `toFQDNs` des networkpolicies ici.
3. **Les buckets doivent préexister** : restic ne les crée pas (et retry un
   NoSuchBucket en silence). Cloud : opérateur / `ensure-buckets.sh` côté
   infra. Local : chart MinIO (`backups-primary`/`backups-replica` dans les
   values).

## Restauration

OpenBao (perte totale) :
```bash
export RESTIC_REPOSITORY="s3:<endpoint>/<bucket>/openbao" RESTIC_PASSWORD=…  # escrow
restic restore latest --target /tmp/r
# cluster OpenBao vierge redéployé par Flux, puis :
bao operator raft snapshot restore /tmp/r/backup/openbao-raft.snap
# ⚠ le snapshot est chiffré par la clé de seal statique : poser d'abord le Secret
# openbao-seal-key depuis l'escrow de l'ANCIEN cluster (runbook foundation/vault/README.md, Disaster recovery).
```

CNPG :
```bash
export RESTIC_REPOSITORY="s3:<endpoint>/<bucket>/cnpg" RESTIC_PASSWORD=…
restic restore latest --target /tmp/r
pg_restore -h zitadel-db-rw -U zitadel -d zitadel --clean --if-exists /tmp/r/backup/zitadel-db.dump
```

Test périodique conseillé : `restic snapshots` + `restic restore latest` vers
un volume jetable (les `restic check` de chaque run ne lisent que les
métadonnées).

Monthly restore tests (the `*-restore-test` CronJobs) go one step past
restic: the CNPG one runs `pg_restore --list` on every restored dump (the
archive must be readable by a client at least as recent as the server), the
OpenBao one unpacks the raft snapshot and checks its `SHA256SUMS`. Neither loads
the data into a live database or OpenBao: a real restore is still a manual
exercise (this section, and the OpenBao runbook in `foundation/vault/README.md`).

CNPG point-in-time recovery (barman archive, RPO of minutes). Restore into a
NEW Cluster next to the original, in `foundation-databases` (the only namespace
the S3 credentials Secret `cnpg-backup-s3` and the egress policy exist in), and
never reuse the original's name or its `backup:` block (CNPG refuses to archive
into a non-empty path):

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

Check with `kubectl cnpg status zitadel-db-restore` and a `SELECT` on the data,
then delete the scratch Cluster (its PVC is `Retain`: delete it too). This
procedure is written from the CNPG 1.23 documentation and has NOT been run on
a cluster: treat the first run as the test. The PITR chain lives on the
cluster's own provider only (one destination in CNPG 1.23); the second
provider holds the daily `pg_dump` (RPO 24 h). The barman objects carry no
client-side encryption (CNPG 1.23 offers server-side AES256/KMS only, unset
here): they rely on the bucket's own protection.

## What is not backed up

By decision, not by omission: VictoriaMetrics data (local-path, 30 d
retention), Loki chunks, and any other telemetry are reconstructible and are not
backed up (no `vmbackup`). Kubernetes resources come back through GitOps.
Longhorn volumes are backed up only when the `storage-backup-target` brick has a
`BACKUP_TARGET_URL` AND the volume belongs to the `oa-backup` recurring-job
group (or `LONGHORN_BACKUP_GROUP=default`, see
`storage/backup-target/recurringjob.yaml`); no platform component uses Longhorn
today.

## Limites connues / upgrades

- **CNPG PITR** : les dumps restic sont quotidiens (RPO 24 h). Le PITR par
  `barmanObjectStore` est un bloc **actif** de `cnpg/cluster.yaml` (valeurs
  `${BACKUP_S3_*}` du ConfigMap `cluster-identity`, `cnpg/scheduledbackups.yaml`) :
  il vit à côté de ce moteur, sans le remplacer.
- **Longhorn** : volumes LUKS (backups Longhorn de volumes chiffrés restent
  chiffrés) ; `backupTarget` est un Setting par environnement, non câblé en
  base — voir `storage/longhorn.yaml`.
- **Alerting** : `BackupJobFailed` et les règles voisines sont dans
  `observability/vm-customresources/vmrule-backup.yaml` (échec de Job, CronJob
  qui ne tire plus).
