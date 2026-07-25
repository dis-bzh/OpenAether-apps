# apps/clusters — clusters clients CAPI (kubeception / gitception)

⚠️ **Surcouche scaffoldée, non instanciée end-to-end.** Les apiVersions des
templates sont validées contre les providers épinglés (CAPS v0.2.1 →
`v1alpha2`, CACPPT v0.5.13 / CABPT v0.6.12 → `v1alpha3`) ; reste à exercer
sur un cluster de management réel.

Un cluster de management (briques `cluster-api-operator` + `cluster-api-providers`
piochées) pilote ici ses clusters clients : **un fichier par cluster**, listé
dans `kustomization.yaml`, réconcilié par la Kustomization Flux `capi-clusters`.

## Principe (100 % Flux natif, zéro outillage neuf)

1. **CAPI** provisionne les machines Talos (template
   `apps/base/cluster-api-clusters/templates/…`, paramétré par
   `postBuild.substitute`) et publie le Secret `<nom>-kubeconfig`.
2. Le management applique **à distance** (`spec.kubeConfig` des
   HelmRelease/Kustomization Flux) dans l'enfant :
   Cilium (CNI socle) → `flux-gotk` (Flux épinglé en git) → `child-gitops`
   (GitRepository `openaether` + Kustomization racine `openaether-platform`,
   mêmes noms que le bootstrap parent).
3. **Gitception** : l'enfant réconcilie ensuite lui-même son profil
   `apps/flux/${CHILD_PROFILE}` — mêmes briques, même pioche
   (`scripts/pick.py … -o apps/flux/workload-<nom>`), autonome même si le
   management tombe.

Pas d'ordre strict requis : chaque étape retry jusqu'à ce que sa condition
(kubeconfig présent, API joignable) soit vraie — convergence à la Flux.

Voir `example-scaleway.yaml.example` pour le cycle complet commenté.

## Prérequis hors git (une fois par management)

```bash
kubectl create secret generic scaleway-capi-credentials -n capi-clusters \
  --from-literal=SCW_ACCESS_KEY=… --from-literal=SCW_SECRET_KEY=…   # docs CAPS : seules ces 2 clés sont lues
kubectl create secret generic <enfant>-substitutes -n flux-system \
  --from-literal=SCW_PROJECT_ID=…                                    # consommé par postBuild.substituteFrom
```

Kubeconfig de l'enfant : Secret `<enfant>-kubeconfig` (ns `capi-clusters`,
clé `value`). Parcours jour-1 complet : `OpenAether-infra/docs/admin-access.md`.

## Décommissionner

Retirer le fichier du kustomization → `prune: true` supprime les CRs CAPI →
CAPI détruit les machines. Les backups de l'enfant (brique backup, si piochée)
restent dans les buckets S3.
