# workload — profil par défaut des clusters clients/spoke

`kustomization.yaml` est **généré** par la pioche (ne pas éditer à la main) :

```bash
python3 scripts/pick.py vault eso certs gateway cnpg storage observability identity kyverno metrics -o apps/flux/workload
```

Socle sécurité + OpenBao + ESO + PKI + gateway (+ backup-openbao en
compagnon). Ni CAPI, ni identity, ni observability — un spoke n'en a pas
besoin par défaut.

- Cluster workload provisionné par **l'infra** (tofu) : Flux pointe ici via
  `apps/flux/${cluster_role}` (bootstrap-manifests).
- Cluster enfant **CAPI** : profil sélectionné par `${CHILD_PROFILE}`
  (cf. `apps/clusters/README.md`) — soit `workload`, soit un profil dédié
  généré par la pioche (ex : `apps/flux/workload-edge-1`).
