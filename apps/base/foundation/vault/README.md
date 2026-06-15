# OpenBao (foundation-vault) — Architecture & Runbook

**OpenAether 0.5.0** — `apps/base/foundation/vault/`

Un **seul** OpenBao par cluster (fondation agnostique). Plus de root, plus de transit, plus
de KMS. Seal **Shamir 5/3**, descellé par un **unsealer HA RAM-only**. Reste **hors mesh**
Istio ambient (sécurité par CiliumNetworkPolicy L3/L4 + authN native OpenBao).

## Architecture

```
┌──────────────────────── ns: foundation-vault (HORS mesh) ────────────────────────┐
│                                                                                   │
│  StatefulSet openbao (3 répl, raft, local-path-retain)                            │
│    seal = Shamir 5/3   ·   tls_disable=1 (TLS = Wave 2)                            │
│    clients: ESO, cert-manager, CSI, istiod, Cilium GW, Prometheus                 │
│                          ▲ unseal (8200)                                          │
│  Deployment openbao-unsealer (2 répl, image officielle openbao, RAM-only) ────────┤
│    boucle `bao operator unseal` sur openbao-{0,1,2} ; parts via Secret monté      │
│                                                                                   │
│  Job openbao-init (one-shot)      → operator init 5/3, écrit les 2 Secrets        │
│  Job openbao-vault-bootstrap      → PKI + KV + auth k8s + rôles + seed (idempotent)│
└───────────────────────────────────────────────────────────────────────────────────┘

Secrets (créés par l'init-Job, HORS Flux — anti-drift) :
  openbao-unseal-keys   → 5 parts Shamir (clé `unseal_keys`)   ← lu par l'unsealer
  openbao-recovery      → root_token                            ← lu par le bootstrap
  bitwarden-unseal-creds → placeholder (optionnel, source=bitwarden)
```

## Séquence de bootstrap (idempotente)

1. Les 3 pods `openbao` démarrent **non-initialisés + scellés** (Shamir).
2. **`openbao-init`** attend l'API d'`openbao-0`, fait `operator init` (5 parts, seuil 3),
   écrit `openbao-unseal-keys` + `openbao-recovery`, et **affiche les parts + root_token UNE
   fois dans ses logs** (escrow manuel → Bitwarden EU).
3. **`openbao-unsealer`** (monte `openbao-unseal-keys` en `optional: false` → démarre quand le
   Secret existe) descelle les 3 pods ; les standby rejoignent le raft via `retry_join`.
4. **`openbao-vault-bootstrap`** (lit `openbao-recovery`) configure : mount PKI `pki`, rôle
   `openaether`, KV v2 `secret/`, auth `kubernetes`, rôles `external-secrets-operator` +
   `cert-manager-issuer`, policies, seed des secrets applicatifs. Il **génère le CSR
   intermediate** (affiché, non bloquant) ; l'import du signé est **manuel** (voir PKI).

> Re-unseal automatique à **chaque** restart de pod (rolling update, crash, drain) via
> l'unsealer. Seul cas non couvert = **toutes** les répliques mortes (cold-start total) →
> unseal manuel (ci-dessous), borné au DR.

## PKI (intermediate dans OpenBao, root offline)

- Mount unique `pki` (PAS de mount imbriqué — voir piège ci-dessous). Rôle `openaether`
  (`allowed_domains: openaether.local` + sous-domaines).
- L'issuer cert-manager `openbao` signe via `pki/sign/openaether`
  (`apps/base/cert-manager-issuers/clusterissuer-openbao.yaml`).
- **La CA intermediate doit être signée hors-ligne par la root CA** (clé jamais en cluster) :
  procédure dans [`pki-root-offline-runbook.md`](./pki-root-offline-runbook.md). Tant que
  `set-signed` n'est pas fait, l'issuer `openbao` reste **NotReady** (par design souverain).

```bash
# Récupérer le CSR généré par le bootstrap
kubectl logs -n foundation-vault job/openbao-vault-bootstrap | sed -n '/BEGIN.*REQUEST/,/END.*REQUEST/p'
# → signer offline avec la root CA (runbook) → intermediate-signed.pem → importer :
BAO_ADDR=http://127.0.0.1:8200 bao write pki/intermediate/set-signed certificate=@intermediate-signed.pem
```

## Runbook — unseal manuel (cold-start / unsealer indisponible)

```bash
export KUBECONFIG=.../cluster/kubeconfig

# Source des parts : Secret en cluster, SINON Bitwarden EU (escrow) si le Secret est perdu.
KEYS=$(kubectl get secret openbao-unseal-keys -n foundation-vault \
  -o jsonpath='{.data.unseal_keys}' | base64 -d)

for p in 0 1 2; do
  echo "$KEYS" | head -3 | while read -r k; do
    kubectl exec -n foundation-vault openbao-$p -- env BAO_ADDR=http://127.0.0.1:8200 \
      bao operator unseal "$k"
  done
done
kubectl exec -n foundation-vault openbao-0 -- bao status | grep Sealed   # → false
```

Si le Secret `openbao-unseal-keys` est perdu (DR total) : recréer les 2 Secrets depuis
l'escrow Bitwarden EU, OU wiper les PVCs `data-openbao-{0,1,2}` pour un re-init propre.

## Rotation des parts (rekey Shamir)

```bash
RT=$(kubectl get secret openbao-recovery -n foundation-vault -o jsonpath='{.data.root_token}' | base64 -d)
kubectl exec -ti -n foundation-vault openbao-0 -- env BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN="$RT" \
  bao operator rekey -init -key-shares=5 -key-threshold=3
# … fournir les parts actuelles, récupérer les nouvelles, MAJ le Secret + Bitwarden EU.
```

## CLI dans le pod

Le serveur écoute en clair (`tls_disable=1` jusqu'à Wave 2). L'env `BAO_ADDR=http://127.0.0.1:8200`
est posé dans le StatefulSet → `kubectl exec openbao-0 -n foundation-vault -- bao status` marche
**sans préfixe** (sinon le CLI tente `https://` → *HTTP response to HTTPS client*).

## Réseau (CiliumNetworkPolicy)

`networkpolicy.yaml` — la CNP `openbao` autorise en ingress 8200 : Cilium GW, ESO, CSI,
cert-manager, istiod, Prometheus, **peers raft (8200/8201)**, **unsealer**, **init**, bootstrap.
Egress : DNS, kube-apiserver, peers raft. CNP egress dédiées pour unsealer / init / bootstrap.

## Alertes (`prometheusrule.yaml`)

- `OpenBaoWorkloadSealed > 2min` → critical (unsealer HA inopérant : parts non chargées, CNP, ou unsealer down).
- `OpenBaoRaftNoLeader` → critical.
- `OpenBaoRaftLagHigh` → warning.

## Pièges & limitations

- **Mount PKI imbriqué = interdit** : ne JAMAIS monter `pki/` ET `pki/<x>/` — le 2e mount échoue
  (HTTP 400) et les sous-chemins renvoient 404. Ici on monte UNIQUEMENT `pki` (avec `max_lease_ttl` long).
- **Secrets de seal hors Flux** : `openbao-unseal-keys` / `openbao-recovery` sont créés par l'init-Job,
  PAS déclarés en kustomize (sinon Flux écraserait/prunerait les vraies parts).
- **Escrow = manuel (dev)** : les parts + root_token transitent dans les logs de l'init-Job.
  Après escrow : `kubectl delete job openbao-init -n foundation-vault` (purge les logs).
- **TLS désactivé** (`tls_disable=1`) — activation cert-manager prévue **Wave 2**.
- **Backup raft snapshot off-site** : bretelle RTO (pas une dépendance — DR reconstructible-by-design).

## Validation locale

`task local-test` (cluster Talos Docker, voir OpenAether-infra) puis apply manuel
(`kubectl apply -k apps/base/foundation/vault`, Flux étant suspendu en local). Vérifier :
`kubectl get pods -n foundation-vault` (3× openbao 1/1, unsealer 2/2, init+bootstrap Completed)
et `kubectl exec openbao-0 -n foundation-vault -- bao status` (`shamir / Initialized / Sealed=false`).
