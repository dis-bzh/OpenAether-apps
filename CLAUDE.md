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
- **CAPI n'est pas dans le socle** : un cluster ne devient « management » qu'une fois
  `cluster-api-operator` + `cluster-api-providers` activés. Sans eux, c'est un cluster
  autonome standard. Cf. `OpenAether-infra/CLAUDE.md`.

## Règle de découpage

- Les manifests **métier** vivent dans **chaque repo applicatif** (`deploy/k8s/`),
  PAS ici. Ici on ne met que le commun + le pointeur Flux vers ces repos.
- Convention ESO : chaque app porte son `ExternalSecret` dans son propre repo
  (cf. minio/zitadel/grafana ici pour le modèle) ; ce repo fournit le
  `ClusterSecretStore openbao` partagé.

## Wiring seestar-fits (à ajouter)

seestar est déployé depuis son repo `seestar-fits-back` (manifests kustomize sous
`deploy/k8s/base` + `overlays/{test,prod}`, images épinglées par digest, patchées
par sa CI). À câbler côté plateforme :

- `GitRepository` seestar → `dis-bzh/seestar-fits-back`, branche `main`.
- `Kustomization` Flux `path: ./deploy/k8s/overlays/<env>`, `prune: true`,
  `dependsOn` : storage (StorageClass `local-path` pour le PVC Valkey), external-secrets,
  gateway.
- Écrire le secret S3 dans **OpenBao** à `secret/seestar/s3` (clés
  `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_URL`, `S3_BUCKET_NAME`) — consommé
  par l'`ExternalSecret` du repo seestar via le `ClusterSecretStore openbao`.
- `HTTPRoute` / gateway istio → Service `front` (namespace `seestar-fits`).

Cluster cible : Talos 1 CP + 1 worker sur Proxmox (SYS-1) — cf. `OpenAether-infra`.
