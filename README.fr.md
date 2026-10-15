# OpenAether Apps

Manifests Kubernetes **communs / plateforme** de OpenAether, réconciliés par
**Flux** (GitOps). Ce dépôt est lu par les contrôleurs Flux amorcés depuis
[dis-bzh/OpenAether-infra](https://github.com/dis-bzh/OpenAether-infra).

🇬🇧 [English version](README.md)

**Versions.** apps 0.2.0 vise les nouveaux clusters : un cluster initialisé par 0.1.0 ne peut pas y être migré sur place, et un cluster
0.1.1 doit faire recréer ses volumes VictoriaMetrics (CHANGELOG, Upgrade notes). Elle s'associe à OpenAether-infra 0.2.1 (pas encore
publiée), pas à 0.2.0. La table d'association, ce qui a été validé où et ce qui n'est pas corrigé sont dans [CHANGELOG.md](CHANGELOG.md).

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
