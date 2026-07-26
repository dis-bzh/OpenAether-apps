# apps/flux — DAG Flux et pioche modulaire

Seul socle figé : **CNI (Cilium) + Flux**, injectés au bootstrap par
`OpenAether-infra` (inlineManifests Talos). Tout le reste est **piochable** :
un cluster n'installe que les briques voulues, leurs dépendances suivent
automatiquement.

## Structure

- `base/` — le DAG complet (source de vérité) : Kustomizations Flux numérotées,
  dépendances encodées dans `spec.dependsOn`.
- `bricks.yaml` — catalogue UX de la pioche : alias, socle, compagnons,
  descriptions. **Jamais de dépendances ici** (elles vivent dans `dependsOn`).
- `management/` — overlay du cluster de management (DAG complet).
- `workload/` — overlay(s) des clusters clients (profils réduits).
- `local/` — overlay local Docker/WSL2 (tout suspendu, apply manuel).
- `<profil>/` — profils générés par la pioche (voir ci-dessous).

## Pioche : `scripts/pick.py`

```bash
python3 scripts/pick.py --list                  # briques, alias, dépendances
python3 scripts/pick.py --validate              # santé du DAG + catalogue
python3 scripts/pick.py --check                 # profils générés à jour ? (CI)
python3 scripts/pick.py vault gateway           # plan (dry-run)
python3 scripts/pick.py zitadel -o apps/flux/edge   # génère le profil `edge`
```

`--validate` + `--check` sont regroupés dans `task apps-validate` (Taskfile de
`OpenAether-infra`) — à lancer après toute modification du DAG.

Résolution automatique : la **fermeture transitive** des `dependsOn` est
incluse (piocher `zitadel` tire cnpg, istio, services-gateway, OpenBao, ESO,
la PKI…). S'ajoutent :

- le **socle sécurité** (`baseline` : namespaces, default-deny, flux-egress) —
  débrayable via `--no-baseline`, déconseillé ;
- les **compagnons** (`companions`) : briques de configuration ajoutées dès que
  toutes leurs dépendances sont déjà sélectionnées (ex. `external-secrets-stores`
  quand ESO **et** OpenBao sont piochés), jamais motrices de nouvelles
  dépendances.

Le profil généré référence `../base` entier et **supprime** (`$patch: delete`)
les Kustomizations non retenues : pas de duplication, la base reste l'unique
source de vérité, et aucune Kustomization retenue ne dépend d'une supprimée.
Compatible `kubectl kustomize` (pas de référence de fichier hors racine).

Pour activer un profil : pointer la Kustomization Flux racine sur son chemin
(`spec.path: ./apps/flux/<profil>`) côté `OpenAether-infra`
(bootstrap-manifests), ou par patch d'overlay.

## Invariant à maintenir

**`dependsOn` = dépendances réelles**, pas seulement un ordre de convergence.
Toute nouvelle brique doit déclarer *tout* ce qu'elle exige : StorageClass
(`platform-local-path-provisioner`), PriorityClass (`platform-priority-classes`
— une PriorityClass absente fait rejeter le pod à l'admission), pool LB
(`platform-cilium-lb-ipam`), etc. C'est ce qui rend la pioche sûre.
`scripts/pick.py --validate` vérifie l'intégrité (cycles, cibles inconnues,
chemins morts, fichiers non câblés).

**Un profil généré fige la liste des Kustomizations EXCLUES** : ajouter une
brique au DAG périme donc tous les profils déjà générés — la nouvelle brique est
héritée de `../base` sans avoir été pioché, et reste bloquée si ses dépendances,
elles, sont exclues. C'est arrivé en réel avec `orc` (dépendante de
`cluster-api-providers`, absente des clusters workload) : elle a bloqué les deux
clusters edge. `scripts/pick.py --check` rejoue la génération en mémoire depuis
l'en-tête `# Pioche :` de chaque profil et sort 1 si l'un a divergé —
**régénérer les profils fait partie de toute modification du DAG**.
