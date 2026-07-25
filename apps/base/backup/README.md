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
| Volumes Longhorn | LUKS at-rest ; `backupTarget` par env (voir bas de page) | selon l'app |

## Seed des secrets (une fois par cluster)

1. `backup/restic` — **seedé automatiquement** par le Job
   `openbao-vault-bootstrap` (password aléatoire, affiché UNE FOIS dans ses
   logs). **ESCROW OFFLINE OBLIGATOIRE** (Bitwarden EU) : sans ce password,
   les backups sont indéchiffrables le jour où OpenBao est perdu — c'est
   précisément le scénario DR principal.
2. `backup/s3-primary` et `backup/s3-replica` — **seedés par l'opérateur**
   (creds provider inconnues in-cluster). `endpoint` inclut le schéma :

   ```bash
   bao kv put secret/backup/s3-primary \
     endpoint="https://s3.fr-par.scw.cloud" \
     bucket="s3-openaether-scw-backups" \
     access_key="…" secret_key="…"
   bao kv put secret/backup/s3-replica \
     endpoint="https://s3.gra.cloud.ovh.com" \
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
# ⚠ le snapshot rétablit AUSSI le seal d'origine → unseal avec les parts
# Shamir escrowées de l'ANCIEN cluster (runbook foundation/vault/README.md).
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

## Limites connues / upgrades

- **CNPG PITR** : les dumps sont quotidiens (RPO 24 h). Pour du PITR, activer
  `barmanObjectStore` (bloc commenté dans `cnpg/cluster.yaml`) dans un overlay
  cloud — valeurs provider en dur dans le CR, d'où le choix restic en base.
- **Longhorn** : volumes LUKS (backups Longhorn de volumes chiffrés restent
  chiffrés) ; `backupTarget` est un Setting par environnement, non câblé en
  base — voir `storage/longhorn.yaml`.
- **Alerting** : pas encore de VMRule sur l'échec des CronJobs (suivre
  `kube_job_status_failed` namespace foundation-*).
