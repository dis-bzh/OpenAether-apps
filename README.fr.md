# OpenAether Apps

Manifests Kubernetes **communs / plateforme** de OpenAether, réconciliés par
**Flux** (GitOps). Ce dépôt est lu par les contrôleurs Flux amorcés depuis
[dis-bzh/OpenAether-infra](https://github.com/dis-bzh/OpenAether-infra).

🇬🇧 [English version](README.md)

**Versions.** La version courante est apps 0.4.5, un correctif de 0.4.4 (version du changelog coupée le 2026-11-11 ; notes sous `[0.4.5]` du CHANGELOG.md, avec ses lignes WORSE (PIRES) et ce qui n'est pas prouvé), associée à OpenAether-infra 0.4.5. Elle n'apporte aucune fonctionnalité et ne traite qu'un thème, la chaîne d'approvisionnement : elle met dans le scan hebdomadaire le sidecar du plugin Barman Cloud et la copie de Flux que reçoivent les enfants CAPI (la matrice passe de 51 à 59 images), les fait passer, avec Longhorn, à des builds qu'un scan local a lus sans CRITICAL les 2026-11-09 et 2026-11-10 (plugin v0.16.0, Flux 2.10.0 pour les enfants, Longhorn 1.12.2 ; CVE-2026-123158, apparue le 2026-11-10 et sans image corrigée pour l'instant, laisse trois images de la matrice en rouge : `forgejo`, `restic` et le `kustomize-controller` des enfants, voir `[0.4.5]`, « Known open »), supprime une dérogation et renouvelle l'autre jusqu'au 2027-01-09, déclare `Alert` et `Provider` de Flux en `v1`, et fait redémarrer le Cilium des enfants un agent à la fois. La première synchronisation redémarre chaque pod PostgreSQL une fois, et les deux Clusters de la plateforme redémarrent en même temps. Un cluster 0.4.4 y monte comme le dit le chemin de mise à jour sous `[0.4.5]`. La preuve est sur Scaleway seulement, sur des références de branche mobiles et non sur le tag, une exécution par chiffre ; un déplacement épinglé sur le tag n'est pas prouvé. Ce qui suit est l'historique : apps 0.4.4, une version de dette de 0.4.3 (publiée le 2026-11-09 ; notes sous `[0.4.4]`), associée à OpenAether-infra 0.4.4, qui a fait passer l'image PostgreSQL des tags Debian 11 figés aux builds `system` Debian 12 datés, fait envoyer vmalert à chaque pod Alertmanager et resserré les contrôles du scan et des dérogations (la matrice est passée de 40 à 51 images). apps 0.4.3, une version d'hygiène de sécurité de 0.4.2 (publiée le 2026-11-05 ; notes sous `[0.4.3]`), associée à OpenAether-infra 0.4.3, qui a fermé un trou du Gateway commun (un forge derrière le load balancer applicatif répondait à `//metrics` avec ses métriques en 0.4.2, mesuré ; 0.4.1 a le même arbre `apps/` et n'a pas été exécutée), passé Zitadel en v4.20.1 et fait attendre ses Clusters à la Kustomization `cnpg` ; Flux 2.10.0 est dans OpenAether-infra. apps 0.4.2, un correctif de 0.4.1 (publiée le 2026-11-04 ; notes sous `[0.4.2]`), associée à OpenAether-infra 0.4.2, dont le seul changement était la procédure de publication (le workflow `SBOM` joint l'inventaire à une release en brouillon ; 0.3.2, 0.4.0 et 0.4.1 sont sorties sans), sans changement d'exécution depuis 0.4.1. apps 0.4.1, un correctif de 0.4.0 (publiée le 2026-11-03 ; notes sous `[0.4.1]`), associée à OpenAether-infra 0.4.1. apps 0.4.0, la version résilience (publiée le 2026-11-01 ; notes sous `[0.4.0]`), associée à OpenAether-infra 0.4.0 ; un cluster 0.3.x y monte comme le dit `docs/patching.md`. apps 0.3.2 et 0.3.1 étaient des correctifs de 0.3.0 (notes sous `[0.3.2]` et `[0.3.1]`), associés à OpenAether-infra 0.3.2 et 0.3.1. apps 0.3.0 (publiée le 2026-10-19 ; notes de version sous `[0.3.0]` du CHANGELOG.md) s'associe à OpenAether-infra 0.3.0 : une installation neuve arrive sur les composants mis à jour, et un cluster 0.2.0 monte d'une version mineure à la fois comme le dit `docs/patching.md`. apps 0.2.0 vise les nouveaux clusters : un cluster initialisé par 0.1.0 ne peut pas y être migré sur place, et un cluster
0.1.1 doit faire recréer ses volumes VictoriaMetrics, ce qui en perd l'historique de métriques (CHANGELOG, Upgrade notes ; #71). Elle s'associe à OpenAether-infra 0.2.1, pas à 0.2.0. La table d'association, ce qui a été validé où et ce qui n'est pas corrigé sont dans [CHANGELOG.md](CHANGELOG.md).

## Principe : socle figé + pioche modulaire

**Seul socle figé : CNI (Cilium) + Flux.** Tout le reste — mesh Istio, Zitadel,
OpenBao, CNPG, Longhorn, observabilité… — est **optionnel et composable**. Un
cluster pioche les briques dont il a besoin ; la fermeture transitive des
dépendances est calculée par `scripts/pick.py`.

Les manifests **métier** ne vivent PAS ici : ils restent dans chaque dépôt
applicatif (`deploy/k8s/`). Ce dépôt ne porte que le commun et le pointeur Flux.

## Structure

```
apps/
├── base/                      # Manifests, une brique par répertoire
│   ├── namespaces/            # Découpage foundation-*/services-* + labels PSA
│   ├── platform/              # StorageClasses, Gateway API, PriorityClasses,
│   │                          #   LB-IPAM, network policies, metrics-server
│   ├── foundation/vault/      # OpenBao HA raft (auto-unseal statique, TLS interne)
│   ├── foundation/vault-ca/   # CA du listener OpenBao (ns cert-manager)
│   ├── external-secrets/      # ESO + ClusterSecretStore openbao
│   ├── cert-manager/          # cert-manager + ClusterIssuers adossés à OpenBao
│   ├── istio/                 # Istio ambient (istiod, CNI, ztunnel) + policies
│   ├── services-gateway/      # Gateway nord-sud + NodePorts FIGÉS 30080/30443
│   ├── cnpg/                  # CloudNativePG + clusters zitadel-db / grafana-db
│   ├── storage/               # Longhorn (volumes répliqués, LUKS via OpenBao)
│   ├── backup/                # Snapshots OpenBao + pg_dump → restic, 2 dépôts S3
│   ├── observability/         # VictoriaMetrics, Loki, Grafana, Alloy
│   ├── identity/              # Zitadel (IAM/SSO, backend CNPG)
│   ├── kyverno/               # Policy engine + policies OpenAether
│   ├── cluster-api-operator/  # Opérateur CAPI          ┐
│   ├── cluster-api-providers/ # Providers + inventaire   │ surcouche
│   ├── cluster-api-clusters/  # Templates d'enfants      │ management
│   └── orc/                   # Dépendance CAPO          ┘  (optionnelle)
├── clusters/                  # Un fichier par cluster enfant CAPI (edge-1, edge-2…)
└── flux/
    ├── base/                  # DAG COMPLET — source de vérité des dependsOn
    ├── bricks.yaml            # Catalogue : alias, socle, compagnons, descriptions
    ├── management/            # Profil : DAG complet
    ├── workload/              # Profil généré (son en-tête `# Pick:` liste les briques)
    └── local/                 # Profil : Docker local
```

## La pioche (`scripts/pick.py`)

Un profil = `../base` + des patches `$patch: delete` retirant les briques non
retenues. La **fermeture transitive** garantit qu'aucune brique conservée ne
dépend d'une brique supprimée.

```bash
python3 scripts/pick.py --list                      # catalogue
python3 scripts/pick.py vault eso certs gateway cnpg storage observability identity kyverno metrics \
        -o apps/flux/workload                        # générer un profil
python3 scripts/pick.py --validate                   # DAG + catalogue cohérents
python3 scripts/pick.py --check                      # profils à jour (CI)
```

**Invariant** : `spec.dependsOn` doit encoder les dépendances **réelles**
(StorageClass, PriorityClass, pool LB…). Le catalogue ne fait que décorer.

Avant toute modification du DAG : `task apps-validate` (depuis le dépôt infra).

## Amorçage

```
OpenAether-infra: tofu apply -var talos_bootstrap=true
  └─► inlineManifests Talos :
        ├── cilium.yaml          # CNI
        ├── flux-install.yaml    # contrôleurs Flux + CRDs
        └── flux-bootstrap.yaml  # GitRepository (→ ce dépôt) + Kustomization racine
              └─► Flux synchronise apps/flux/<profil>/
                    └─► le DAG déploie apps/base/ dans l'ordre des dependsOn
```

## Clusters enfants (kubeception / gitception)

`apps/clusters/<nom>.yaml` décrit un cluster enfant complet :

1. une **Kustomization** qui instancie un template CAPI (`cluster-talos-*`) ;
2. une **HelmRelease Cilium** appliquée **à distance** via `spec.kubeConfig` ;
3. **Flux** posé à distance, puis `child-gitops` qui pointe l'enfant sur SON
   propre profil — l'enfant réconcilie ensuite en autonomie.

⚠️ **Deux clusters de management ne doivent jamais lire le même
`apps/clusters`** : ils se disputeraient les mêmes CR CAPI. Les isoler par le
`git_ref` du management (côté infra — chacun sur son `refs/heads/<branche>`) ou
par chemin. Pas avec `CHILD_REF`, qui choisit ce que suit un *enfant*, pas ce
que lit le management.

Aucun enfant n'est activé par défaut, pour la même raison : `apps/clusters` est
lu par tout management qui suit ce dépôt, donc un nom listé là est un nom qu'un
cluster inconnu tente de réconcilier.

## Ajouter une brique

1. créer `apps/base/<brique>/` avec son `kustomization.yaml` ;
2. ajouter la `Kustomization` dans `apps/flux/base/` avec ses `dependsOn`
   **réels** ;
3. la référencer dans `apps/flux/base/kustomization.yaml` ;
4. décrire la brique dans `apps/flux/bricks.yaml` (alias, compagnon, description) ;
5. régénérer les profils (`pick.py`) puis `task apps-validate` ;
6. commiter et pousser → Flux réconcilie.

⚠️ **Opérateur et ses CR dans deux Kustomizations séparées** : un bundle
contenant à la fois un opérateur et des CR de ses propres CRDs est rejeté au
dry-run tant que les CRDs n'existent pas. La Kustomization des CR doit `dependsOn` celle de l'opérateur, en
`wait: true` pour que les CRDs soient servies avant les CR (`external-secrets` puis `external-secrets-stores`).

## Développement local

Nécessite [OpenAether-infra](https://github.com/dis-bzh/OpenAether-infra) cloné
à côté :

```bash
cd ../OpenAether-infra
task local-up      # cluster Talos 3 CP + 3 workers sous Docker (Cilium, sans Flux ni apps)
task local-test    # + contrôleurs Flux et apps/flux/local, toutes les Kustomizations SUSPENDUES
```

En local, Flux est volontairement un no-op : pour les apps, `task local-test` prouve seulement que les contrôleurs
démarrent et que les objets Kustomization sont admis (il vérifie aussi le cluster lui-même : quorum etcd, nœuds, Cilium). Pour déployer une brique, on l'applique soi-même
depuis l'arbre de travail (`kubectl apply -k apps/base/<brique>`), ce qui permet de tester une
modification **avant** de la pousser. Une lane CI qui réconcilie réellement un sous-ensemble de briques
sous Docker existe (`.github/workflows/local-docker.yml`, voir [docs/local-docker.md](docs/local-docker.md)) :
Docker local, briques partielles, pas un substitut du rang cloud.

## Licence

**OpenAether** est distribué sous [licence Apache 2.0](LICENSE). Le projet était
sous AGPLv3 jusqu'aux versions 0.x ; le changement est un assouplissement, donc ce que
vous déteniez déjà sous AGPLv3 le reste.

Source : **https://github.com/dis-bzh/OpenAether-apps**
