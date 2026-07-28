# CLAUDE.md — OpenAether-apps

Manifests **communs / plateforme** (OpenBao, ESO, cert-manager, istio gateway,
CNPG, storage, observability…) + **wiring Flux** qui référence les repos applicatifs.
Réconcilié par Flux depuis `apps/flux/*` (voir `apps/flux/base/gitrepository.yaml`).

## Objectif — socle figé + pioche modulaire

**Seul socle figé : CNI (Cilium) + Flux.** Tout le reste (mesh Istio, Zitadel,
OpenBao, Harbor, CNPG, observability…) est **optionnel et composable** : un cluster
pioche dans ces bases selon ses dépendances (mesh ou non, Zitadel sans OpenBao,
Harbor + OpenBao, etc.). Le DAG Flux (`apps/flux/base/*.yaml`, numéroté + `dependsOn`)
doit rester **décomposable** — activer un sous-ensemble sans casser les dépendances.

- `apps/flux/base` = socle commun ; `management/` = surcouche CAPI (cluster de
  management, **optionnelle**) ; `workload/` = ce qu'un cluster client/workload embarque.
- **Pioche** : `scripts/pick.py` (cf. `apps/flux/README.md`) génère un profil
  (fermeture transitive des `dependsOn` + socle + compagnons). Invariant :
  `dependsOn` = dépendances **réelles** (StorageClass, PriorityClass, LB pool…).
- **Backups** : brique `apps/base/backup` (restic, chiffré client, 2 dépôts S3,
  compagnons auto) — cf. son README (seed s3-primary/replica + escrow password).
- **Clusters clients CAPI** : `apps/clusters/` (kubeception/gitception, scaffold
  non testé) — Cilium+Flux injectés à distance via `spec.kubeConfig`, puis
  l'enfant réconcilie son profil `apps/flux/<profil>` en autonome.
- **CAPI n'est pas dans le socle** : un cluster ne devient « management » qu'une fois
  `cluster-api-operator` + `cluster-api-providers` activés. Sans eux, c'est un cluster
  autonome standard. Cf. `OpenAether-infra/CLAUDE.md`.

## Langue

**L'anglais est la langue par défaut du dépôt** : commentaires de manifests,
messages de commit et documentation. Le français est une **traduction**, jamais
la source. README : `README.md` = anglais (canonique), `README.fr.md` = français.
Échange avec l'utilisateur : en français.

⚠️ Fond de commentaires français antérieur à cette convention : les convertir
**au fil des modifications**, jamais en masse — ils encodent des pièges durement
acquis (`ipam.mode`, `cni.exclusive`, substitution Flux…) qu'une traduction
automatique abîmerait.

## Backlog

Les améliorations identifiées (SSO Zitadel↔Grafana, tokens OpenBao nominatifs,
alerting backups, fix namespaces CAPI…) vivent dans
**`OpenAether-infra/docs/backlog.md`** — le consulter avant d'ouvrir un chantier.

## Règle de découpage

- Les manifests **métier** vivent dans **chaque repo applicatif** (`deploy/k8s/`),
  PAS ici. Ici on ne met que le commun + le pointeur Flux vers ces repos.
- Convention ESO : chaque app porte son `ExternalSecret` dans son propre repo
  (cf. minio/zitadel/grafana ici pour le modèle) ; ce repo fournit le
  `ClusterSecretStore openbao` partagé.

